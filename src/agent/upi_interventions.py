"""
UPI Autopay Intervention Strategies.

Five recovery actions for UPI Autopay failures, ordered by priority:

  1. SmartRetryIntervention       — schedule future debit (salary-cycle aware)
  2. UPICollectIntervention       — send UPI collect request to customer VPA
  3. MandateRenewalIntervention   — generate magic link for mandate re-registration
  4. WhatsAppNudgeIntervention    — WhatsApp message with payment link
  5. EscalationIntervention       — flag to support after all else fails

All interventions are async, stateless, and idempotent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..models.risk_models import RevenueRisk
from .retry_scheduler import UPIRetryScheduler, RetryDecision
from ..models.upi_models import UPIAutopayEvent, UPIFailureCode
from ..utils.logger import get_logger
from ..integrations.messaging import messenger

logger = get_logger(__name__)


# ── Result & Type ─────────────────────────────────────────────────────────────

class UPIInterventionType(Enum):
    SMART_RETRY      = "smart_retry"
    UPI_COLLECT      = "upi_collect"
    MANDATE_RENEWAL  = "mandate_renewal"
    WHATSAPP_NUDGE   = "whatsapp_nudge"
    ESCALATION       = "escalation"


@dataclass
class UPIInterventionResult:
    """Outcome of a single UPI recovery intervention."""
    intervention_type: UPIInterventionType
    success:           bool
    amount_at_stake:   float
    amount_recovered:  float           # 0 until payment confirmed; full amount if scheduled
    message:           str
    scheduled_at:      datetime | None = None   # for retry-based interventions
    action_url:        str | None = None        # magic link or collect request URL
    metadata:          dict = field(default_factory=dict)

    @property
    def recovery_likelihood(self) -> str:
        """Human-readable estimated recovery likelihood."""
        LIKELIHOOD = {
            UPIInterventionType.SMART_RETRY:     "High (40–60% — salary window)",
            UPIInterventionType.UPI_COLLECT:     "Medium (25–40% — customer action needed)",
            UPIInterventionType.MANDATE_RENEWAL: "Medium (30–50% — re-registration flow)",
            UPIInterventionType.WHATSAPP_NUDGE:  "Medium (20–35% — engagement dependent)",
            UPIInterventionType.ESCALATION:      "Low (manual intervention)",
        }
        return LIKELIHOOD.get(self.intervention_type, "Unknown")


# ── Base Class ────────────────────────────────────────────────────────────────

class BaseUPIIntervention:
    """Abstract base for UPI recovery interventions."""

    def can_handle(self, risk: RevenueRisk) -> bool:
        """Return True if this intervention is appropriate for the given risk."""
        raise NotImplementedError

    async def execute(self, risk: RevenueRisk) -> UPIInterventionResult:
        """Execute the intervention. Must not raise — return success=False on error."""
        raise NotImplementedError

    def _extract_upi_event(self, risk: RevenueRisk) -> UPIAutopayEvent | None:
        meta = risk.metadata or {}
        return meta.get("upi_event")

    def _extract_failure_code(self, risk: RevenueRisk) -> UPIFailureCode:
        meta = risk.metadata or {}
        return meta.get("failure_code", UPIFailureCode.UNKNOWN)


# ── 1. Smart Retry Intervention ───────────────────────────────────────────────

class SmartRetryIntervention(BaseUPIIntervention):
    """
    Schedules a future automatic debit attempt using the salary-cycle-aware scheduler.
    Best for: U30 (insufficient funds), TM/TE (technical errors), U13 (paused).
    """

    def __init__(self):
        self._scheduler = UPIRetryScheduler()

    def can_handle(self, risk: RevenueRisk) -> bool:
        meta = risk.metadata or {}
        failure_code: UPIFailureCode = meta.get("failure_code", UPIFailureCode.UNKNOWN)
        requires_renewal = meta.get("requires_renewal", False)
        max_retries = meta.get("retry_attempt", 0) >= 3
        return failure_code.is_recoverable and not requires_renewal and not max_retries

    async def execute(self, risk: RevenueRisk) -> UPIInterventionResult:
        meta   = risk.metadata or {}
        code   = meta.get("failure_code", UPIFailureCode.UNKNOWN)
        bank   = meta.get("bank_name", "DEFAULT")
        attempt = meta.get("retry_attempt", 0)

        decision: RetryDecision = self._scheduler.schedule(
            failure_code=code,
            bank_name=bank,
            attempt_number=attempt,
            failure_time=risk.detected_at,
        )

        if not decision.should_retry:
            return UPIInterventionResult(
                intervention_type=UPIInterventionType.SMART_RETRY,
                success=False,
                amount_at_stake=risk.amount,
                amount_recovered=0.0,
                message=decision.strategy,
                metadata={"decision": decision._asdict()},
            )

        logger.info(
            "Smart retry scheduled: risk=%s | at=%s | strategy=%s",
            risk.id, decision.scheduled_at, decision.strategy,
        )
        return UPIInterventionResult(
            intervention_type=UPIInterventionType.SMART_RETRY,
            success=True,
            amount_at_stake=risk.amount,
            amount_recovered=risk.amount,  # optimistically — confirmed on actual debit
            message=decision.strategy,
            scheduled_at=decision.scheduled_at,
            metadata={"decision": decision._asdict()},
        )


# ── 2. UPI Collect Intervention ───────────────────────────────────────────────

class UPICollectIntervention(BaseUPIIntervention):
    """
    Sends a UPI collect request directly to the customer's VPA.
    The customer gets a push notification in their UPI app.
    Best for: U30 (alternate route), technical errors, paused mandates.
    """

    def can_handle(self, risk: RevenueRisk) -> bool:
        meta = risk.metadata or {}
        vpa = meta.get("customer_vpa", "")
        requires_renewal = meta.get("requires_renewal", False)
        return bool(vpa) and not requires_renewal

    async def execute(self, risk: RevenueRisk) -> UPIInterventionResult:
        from ..integrations.razorpay_upi import trigger_collect_request

        meta = risk.metadata or {}
        vpa  = meta.get("customer_vpa", "customer@upi")

        note = f"Payment due: ₹{risk.amount:,.2f}. Please approve in your UPI app."

        try:
            response = await trigger_collect_request(
                customer_vpa=vpa,
                amount_inr=risk.amount,
                note=note,
            )
            logger.info("UPI collect sent to %s for ₹%.2f", vpa, risk.amount)
            return UPIInterventionResult(
                intervention_type=UPIInterventionType.UPI_COLLECT,
                success=True,
                amount_at_stake=risk.amount,
                amount_recovered=risk.amount,
                message=f"UPI collect request sent to {vpa}. Awaiting customer approval.",
                action_url=response.get("payment_id"),
                metadata=response,
            )
        except Exception as exc:
            logger.error("UPI collect failed: %s", exc)
            return UPIInterventionResult(
                intervention_type=UPIInterventionType.UPI_COLLECT,
                success=False,
                amount_at_stake=risk.amount,
                amount_recovered=0.0,
                message=f"UPI collect request failed: {exc}",
            )


# ── 3. Mandate Renewal Intervention ──────────────────────────────────────────

class MandateRenewalIntervention(BaseUPIIntervention):
    """
    Generates a Razorpay magic link for the customer to re-register their mandate.
    Best for: BT01 (revoked), BT02 (expired), BA (account issues).
    """

    def can_handle(self, risk: RevenueRisk) -> bool:
        meta = risk.metadata or {}
        return meta.get("requires_renewal", False)

    async def execute(self, risk: RevenueRisk) -> UPIInterventionResult:
        from ..integrations.razorpay_upi import generate_mandate_renewal_link

        meta        = risk.metadata or {}
        customer_id = risk.customer_id
        code        = meta.get("failure_code", UPIFailureCode.UNKNOWN)

        try:
            link = await generate_mandate_renewal_link(
                customer_id=customer_id,
                plan_id="plan_demo_monthly",
                amount_inr=risk.amount,
            )
            logger.info("Mandate renewal link generated for %s: %s", customer_id, link)
            return UPIInterventionResult(
                intervention_type=UPIInterventionType.MANDATE_RENEWAL,
                success=True,
                amount_at_stake=risk.amount,
                amount_recovered=0.0,  # no recovery until customer re-registers
                message=(
                    f"Mandate {code.human_reason}. "
                    f"Renewal link sent to customer {customer_id}."
                ),
                action_url=link,
                metadata={"renewal_link": link, "failure_code": code.value},
            )
        except Exception as exc:
            logger.error("Mandate renewal link generation failed: %s", exc)
            return UPIInterventionResult(
                intervention_type=UPIInterventionType.MANDATE_RENEWAL,
                success=False,
                amount_at_stake=risk.amount,
                amount_recovered=0.0,
                message=f"Could not generate renewal link: {exc}",
            )


# ── 4. WhatsApp Nudge Intervention ────────────────────────────────────────────

class WhatsAppNudgeIntervention(BaseUPIIntervention):
    """
    Sends a WhatsApp message to the customer with context + a payment/renewal link.

    In production: integrates with Razorpay's WhatsApp payment links or Interakt.
    In demo mode: logs the message that would be sent.
    """

    def can_handle(self, risk: RevenueRisk) -> bool:
        # Always fire as a parallel nudge (combined with retry or renewal)
        # but only if the failure code is not a pure technical glitch
        meta = risk.metadata or {}
        code = meta.get("failure_code", UPIFailureCode.UNKNOWN)
        return code not in {UPIFailureCode.TM, UPIFailureCode.TE}

    async def execute(self, risk: RevenueRisk) -> UPIInterventionResult:
        meta        = risk.metadata or {}
        code        = meta.get("failure_code", UPIFailureCode.UNKNOWN)
        vpa         = meta.get("customer_vpa", "customer@upi")
        customer_id = risk.customer_id

        if code.requires_mandate_renewal:
            msg_body = (
                f"Hi! Your UPI Autopay mandate has been {code.human_reason.lower()}. "
                f"Please re-register to continue your subscription: "
                f"https://rzp.io/l/renew-{customer_id}"
            )
            action = "mandate_renewal_whatsapp"
        else:
            msg_body = (
                f"Hi! Your UPI Autopay payment of ₹{risk.amount:,.2f} could not be processed "
                f"({code.human_reason}). We'll retry automatically. "
                f"Or pay now: https://rzp.io/l/pay-{customer_id}"
            )
            action = "payment_reminder_whatsapp"

        # Outbound WhatsApp delivery (Twilio-backed in live mode, logged in mock mode)
        send_result = messenger.send_whatsapp(to=vpa, body=msg_body)
        logger.info("[WhatsApp → %s] (%s) %s", vpa, send_result.mode, msg_body)

        return UPIInterventionResult(
            intervention_type=UPIInterventionType.WHATSAPP_NUDGE,
            success=True,
            amount_at_stake=risk.amount,
            amount_recovered=0.0,
            message=f"WhatsApp message sent to {vpa} ({send_result.mode}): {msg_body[:80]}…",
            metadata={
                "whatsapp_body": msg_body,
                "action": action,
                "delivery_mode": send_result.mode,
                "sent_live": send_result.sent,
                "provider_sid": send_result.provider_sid,
            },
        )


# ── 5. Escalation Intervention ────────────────────────────────────────────────

class EscalationIntervention(BaseUPIIntervention):
    """
    Final fallback: flags the risk to a support/CRM queue after all auto-retries fail.
    """

    def can_handle(self, risk: RevenueRisk) -> bool:
        meta = risk.metadata or {}
        return meta.get("retry_attempt", 0) >= 3

    async def execute(self, risk: RevenueRisk) -> UPIInterventionResult:
        meta = risk.metadata or {}
        code = meta.get("failure_code", UPIFailureCode.UNKNOWN)
        logger.warning(
            "ESCALATION: risk=%s | customer=%s | amount=₹%.2f | code=%s | attempts=%d",
            risk.id, risk.customer_id, risk.amount, code.value, meta.get("retry_attempt", 0),
        )
        return UPIInterventionResult(
            intervention_type=UPIInterventionType.ESCALATION,
            success=True,
            amount_at_stake=risk.amount,
            amount_recovered=0.0,
            message=(
                f"Escalated to support queue after {meta.get('retry_attempt', 0)} failed attempts. "
                f"Customer: {risk.customer_id} | Code: {code.value} | Amount: ₹{risk.amount:,.2f}"
            ),
            metadata={"escalated_at": datetime.now().isoformat()},
        )
