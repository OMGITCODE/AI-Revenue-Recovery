"""Root Cause Diagnoser.

Analyzes detected revenue risks to determine the underlying cause
and recommend the most effective intervention.

Handles two paths:
  1. UPI Autopay failures — maps NPCI error codes directly to root causes
     with high confidence (deterministic, no ML needed).
  2. Generic payment failures — rule-based fallback using RiskType + metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .detector import RevenueRisk, RiskType
from ..utils.logger import get_logger

logger = get_logger(__name__)


# ── Root Cause Taxonomy ───────────────────────────────────────────────────────

class RootCause(Enum):
    """Unified root causes across all payment failure modes."""
    # Funds / limits
    INSUFFICIENT_FUNDS    = "insufficient_funds"
    DAILY_LIMIT_EXCEEDED  = "daily_limit_exceeded"
    WEEKLY_LIMIT_EXCEEDED = "weekly_limit_exceeded"

    # Card / instrument issues
    CARD_EXPIRED          = "card_expired"
    CARD_DECLINED         = "card_declined"

    # Mandate / autopay issues
    MANDATE_REVOKED       = "mandate_revoked"
    MANDATE_EXPIRED       = "mandate_expired"
    MANDATE_PAUSED        = "mandate_paused"
    MANDATE_LIMIT_BREACH  = "mandate_limit_breach"

    # Account issues
    ACCOUNT_CLOSED        = "account_closed"
    ACCOUNT_BLOCKED       = "account_blocked"
    ACCOUNT_MISMATCH      = "account_mismatch"

    # Technical
    NETWORK_ERROR         = "network_error"
    TRANSACTION_EXPIRED   = "transaction_expired"
    FRAUD_BLOCK           = "fraud_block"

    # Behavioural
    USER_ABANDONED        = "user_abandoned"
    PRICING_FRICTION      = "pricing_friction"

    # Business
    INVOICE_DISPUTE       = "invoice_dispute"

    # Fallback
    UNKNOWN               = "unknown"


# ── Diagnosis Result ──────────────────────────────────────────────────────────

@dataclass
class Diagnosis:
    """Result of diagnosing a revenue risk."""
    risk:                RevenueRisk
    root_cause:          RootCause
    confidence:          float           # 0.0–1.0
    recommended_actions: list[str]
    is_upi:              bool = False    # True when diagnosed from NPCI error code
    context:             dict = field(default_factory=dict)

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.80

    @property
    def summary(self) -> str:
        pct = f"{self.confidence:.0%}"
        return (
            f"{self.root_cause.value} ({pct} confidence) — "
            f"{', '.join(self.recommended_actions[:2])}"
        )


# ── UPI Failure Code → Root Cause mapping ────────────────────────────────────
# Keyed by UPIFailureCode.value (string) so we avoid a hard import cycle.
# Confidence reflects how deterministic the NPCI code is.

_UPI_CODE_MAP: dict[str, tuple[RootCause, float, list[str]]] = {
    # ── Funds / Limits ──────────────────────────────────────────────────────
    "U30": (
        RootCause.INSUFFICIENT_FUNDS, 0.95,
        ["Schedule retry during salary window (1st–7th of month)",
         "Send UPI collect request as immediate fallback",
         "Send WhatsApp reminder with payment link"],
    ),
    "U69": (
        RootCause.DAILY_LIMIT_EXCEEDED, 0.98,
        ["Retry after midnight IST when daily limit resets",
         "Send WhatsApp nudge to inform customer"],
    ),
    "U66": (
        RootCause.WEEKLY_LIMIT_EXCEEDED, 0.98,
        ["Retry after weekly limit resets (Monday midnight IST)",
         "Notify customer via WhatsApp"],
    ),
    "U64": (
        RootCause.WEEKLY_LIMIT_EXCEEDED, 0.95,   # monthly, reusing enum
        ["Retry at start of next month", "Notify customer"],
    ),

    # ── Mandate issues ──────────────────────────────────────────────────────
    "BT01": (
        RootCause.MANDATE_REVOKED, 0.99,
        ["Generate mandate renewal magic link (Razorpay)",
         "Send WhatsApp message with re-registration link",
         "Do NOT auto-retry — mandate is dead"],
    ),
    "BT02": (
        RootCause.MANDATE_EXPIRED, 0.99,
        ["Generate new mandate registration link",
         "Send SMS + WhatsApp with renewal URL"],
    ),
    "U13": (
        RootCause.MANDATE_PAUSED, 0.92,
        ["Ask customer to un-pause mandate in UPI app",
         "Retry automatically in 48h",
         "Send WhatsApp instruction guide"],
    ),
    "U29": (
        RootCause.MANDATE_LIMIT_BREACH, 0.96,
        ["Check mandate max amount vs debit amount",
         "Initiate revised mandate with correct limit",
         "Contact customer for approval"],
    ),

    # ── Account issues ──────────────────────────────────────────────────────
    "BA": (
        RootCause.ACCOUNT_CLOSED, 0.97,
        ["Cancel mandate immediately",
         "Contact customer for updated bank account",
         "Escalate to support team"],
    ),
    "XB": (
        RootCause.ACCOUNT_BLOCKED, 0.95,
        ["Contact customer to unblock account with their bank",
         "Escalate if unresolved in 48h"],
    ),
    "AM": (
        RootCause.ACCOUNT_MISMATCH, 0.93,
        ["Verify mandate registration details",
         "Request customer to re-register mandate with correct account"],
    ),

    # ── Technical ───────────────────────────────────────────────────────────
    "TM": (
        RootCause.NETWORK_ERROR, 0.85,
        ["Retry with exponential backoff (2h → 6h → 24h)",
         "Monitor bank gateway status page"],
    ),
    "TE": (
        RootCause.TRANSACTION_EXPIRED, 0.88,
        ["Retry immediately (expiry is transient)",
         "Escalate if it persists more than 3 times"],
    ),
    "RB": (
        RootCause.CARD_DECLINED, 0.75,
        ["Retry once after 4h",
         "Send UPI collect as alternate channel",
         "Escalate if declined again"],
    ),

    # ── Fallback ────────────────────────────────────────────────────────────
    "UNKNOWN": (
        RootCause.UNKNOWN, 0.50,
        ["Retry with exponential backoff",
         "Review raw webhook payload for clues",
         "Escalate to engineering if pattern persists"],
    ),
}

# ── Generic RiskType → Root Cause fallback ────────────────────────────────────
_RISK_TYPE_MAP: dict[RiskType, tuple[RootCause, float, list[str]]] = {
    RiskType.PAYMENT_FAILURE: (
        RootCause.CARD_DECLINED, 0.70,
        ["Retry payment", "Send payment link to customer"],
    ),
    RiskType.CHECKOUT_ABANDONMENT: (
        RootCause.USER_ABANDONED, 0.85,
        ["Send checkout reminder email", "Offer discount code"],
    ),
    RiskType.SUBSCRIPTION_FAILURE: (
        RootCause.CARD_EXPIRED, 0.75,
        ["Request card update", "Pause subscription grace period"],
    ),
    RiskType.INVOICE_OVERDUE: (
        RootCause.INVOICE_DISPUTE, 0.65,
        ["Send dunning email", "Escalate to sales team"],
    ),
    RiskType.MANDATE_FAILURE: (
        RootCause.MANDATE_EXPIRED, 0.72,
        ["Re-initiate mandate", "Send SMS to customer"],
    ),
    RiskType.UPI_AUTOPAY_MANDATE_FAILURE: (
        RootCause.UNKNOWN, 0.50,
        ["Check NPCI error code for specific action",
         "Retry or renew mandate depending on code"],
    ),
}


# ── Diagnoser ─────────────────────────────────────────────────────────────────

class RootCauseDiagnoser:
    """
    Diagnoses the root cause of revenue risk events.

    For UPI Autopay failures: uses NPCI error code from risk.metadata for
    high-confidence, deterministic diagnosis (no ML required).

    For all other risks: falls back to rule-based mapping by RiskType.
    """

    async def diagnose(self, risk: RevenueRisk) -> Diagnosis:
        """
        Analyse a revenue risk and determine root cause + recommended actions.

        Args:
            risk: Detected RevenueRisk (from detector or UPI detector).

        Returns:
            Diagnosis with root cause, confidence, and action list.
        """
        # ── Path 1: UPI Autopay — use NPCI error code ─────────────────────
        meta = risk.metadata or {}
        upi_failure_code = meta.get("failure_code")

        if upi_failure_code is not None:
            code_str = getattr(upi_failure_code, "value", str(upi_failure_code))
            entry = _UPI_CODE_MAP.get(code_str, _UPI_CODE_MAP["UNKNOWN"])
            root_cause, confidence, actions = entry

            diagnosis = Diagnosis(
                risk=risk,
                root_cause=root_cause,
                confidence=confidence,
                recommended_actions=actions,
                is_upi=True,
                context={
                    "npci_code":   code_str,
                    "bank":        meta.get("bank_name", "Unknown"),
                    "vpa":         meta.get("customer_vpa", ""),
                    "mandate_id":  meta.get("mandate_id", ""),
                    "attempt":     meta.get("retry_attempt", 0),
                },
            )
            logger.info(
                "upi_diagnosis_complete",
                risk_id=risk.id,
                npci_code=code_str,
                root_cause=root_cause.value,
                confidence=f"{confidence:.0%}",
            )
            return diagnosis

        # ── Path 2: Generic risk — rule-based fallback ────────────────────
        entry = _RISK_TYPE_MAP.get(risk.risk_type)
        if entry:
            root_cause, confidence, actions = entry
        else:
            root_cause, confidence, actions = (
                RootCause.UNKNOWN, 0.40,
                ["Manual review required", "Check payment gateway logs"],
            )

        diagnosis = Diagnosis(
            risk=risk,
            root_cause=root_cause,
            confidence=confidence,
            recommended_actions=actions,
            is_upi=False,
            context={"risk_type": risk.risk_type.value},
        )
        logger.info(
            "generic_diagnosis_complete",
            risk_id=risk.id,
            risk_type=risk.risk_type.value,
            root_cause=root_cause.value,
            confidence=f"{confidence:.0%}",
        )
        return diagnosis

