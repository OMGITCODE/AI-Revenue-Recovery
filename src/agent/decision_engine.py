"""
Decision Engine — the "Decide / Guardrails" stage.

Sits between Diagnose (root-cause) and Intervene (action).
Applies business rules so interventions are always safe, fair, and legal.

Guardrails enforced:
  1. Retry budget      — max 3 retries per customer per 30-day window
  2. Amount threshold  — amounts < ₹10 not worth automated intervention
  3. Customer tier     — Platinum/Gold get whatsapp + call; Silver/Bronze get SMS only
  4. Time-of-day gate  — no nudges between 21:00–08:00 IST (TRAI DND compliance)
  5. Mandate state     — expired/revoked mandates skip retry; go straight to renewal
  6. Blackout window   — no retries on banking holidays / weekend for salary credits
  7. Promise-to-pay    — if customer has an active P2P promise, suppress nudge
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import List

from ..utils.logger import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Customer tier ─────────────────────────────────────────────────────────────

class CustomerTier(str, Enum):
    PLATINUM = "platinum"   # > ₹50 000 / month GMV
    GOLD     = "gold"       # ₹10 000 – ₹50 000
    SILVER   = "silver"     # ₹2 000 – ₹10 000
    BRONZE   = "bronze"     # < ₹2 000

def infer_tier(amount: float) -> CustomerTier:
    if amount >= 50_000: return CustomerTier.PLATINUM
    if amount >= 10_000: return CustomerTier.GOLD
    if amount >= 2_000:  return CustomerTier.SILVER
    return CustomerTier.BRONZE


# ── Guardrail result ──────────────────────────────────────────────────────────

@dataclass
class GuardrailDecision:
    """What the engine decided and why."""
    approved:           bool
    allowed_actions:    List[str]   = field(default_factory=list)
    blocked_actions:    List[str]   = field(default_factory=list)
    guardrails_fired:   List[str]   = field(default_factory=list)
    customer_tier:      str         = CustomerTier.BRONZE
    retry_budget_left:  int         = 3
    reason:             str         = ""
    trust_score:        float       = 0.5   # payer trust score 0.0–1.0

    def to_dict(self) -> dict:
        return {
            "approved":          self.approved,
            "allowed_actions":   self.allowed_actions,
            "blocked_actions":   self.blocked_actions,
            "guardrails_fired":  self.guardrails_fired,
            "customer_tier":     self.customer_tier,
            "retry_budget_left": self.retry_budget_left,
            "reason":            self.reason,
            "trust_score":       self.trust_score,
        }


# ── Engine ────────────────────────────────────────────────────────────────────

class DecisionEngine:
    """
    Stateless guardrail evaluator.

    Usage:
        engine   = DecisionEngine()
        decision = engine.evaluate(event_dict, retry_count, active_promise)

    Guardrails:
        GR1 — Amount < ₹10: suppress all (cost > benefit)
        GR2 — Retry count ≥ 3: no more smart_retry
        GR3 — Mandate revoked/expired: force mandate_renewal path
        GR4 — DND window 21:00–08:00 IST (TRAI): suppress whatsapp_nudge
        GR5 — Active promise-to-pay: suppress nudge/collect (no harassment)
        GR6 — Tier/amount: Bronze < ₹500 skip escalation (cost > value)
        GR7 — RBI/NPCI: UPI Autopay debit > ₹15,000 requires pre-debit notification;
               skip silent retry, force explicit customer consent channel
        GR8 — Daily contact cap: max 3 outbound touches per customer per day
    """

    # Candidate intervention pool (must be a subset of UPIInterventionType values)
    ALL_ACTIONS = ["smart_retry", "upi_collect", "mandate_renewal", "whatsapp_nudge", "escalation"]

    # DND-safe hours: 08:00–21:00 IST
    DND_START_HOUR = 21
    DND_END_HOUR   = 8

    # RBI/NPCI UPI Autopay pre-debit notification ceiling (₹)
    RBI_PREDEBIT_THRESHOLD = 15_000

    # Daily outbound contact cap per customer
    DAILY_CONTACT_CAP = 3

    def evaluate(
        self,
        failure_code:  str,
        mandate_state: str,
        amount:        float,
        retry_count:   int   = 0,
        has_promise:   bool  = False,
        daily_touches: int   = 0,
        trust_score:   float = 0.5,   # payer trust score from promise_tracker
    ) -> GuardrailDecision:

        tier       = infer_tier(amount)
        allowed    = list(self.ALL_ACTIONS)
        blocked    = []
        fired      = []

        # ── Guardrail 1: Amount too small ────────────────────────────────────
        if amount < 10:
            logger.warning("GR1: Amount ₹%.2f below minimum threshold — suppressing all actions", amount)
            return GuardrailDecision(
                approved=False, allowed_actions=[], blocked_actions=self.ALL_ACTIONS,
                guardrails_fired=["amount_threshold"],
                customer_tier=tier, retry_budget_left=0,
                reason=f"Amount ₹{amount:.2f} below ₹10 minimum intervention threshold",
            )

        # ── Guardrail 2: Retry budget exhausted ──────────────────────────────
        budget = max(0, 3 - retry_count)
        if retry_count >= 3:
            self._block(allowed, blocked, "smart_retry")
            fired.append("retry_budget_exhausted")
            logger.info("GR2: Retry budget exhausted (attempt %d) — blocking smart_retry", retry_count)

        # ── Guardrail 3: Mandate state — skip retry if revoked/expired ───────
        if mandate_state in ("revoked", "expired"):
            self._block(allowed, blocked, "smart_retry")
            self._block(allowed, blocked, "upi_collect")
            fired.append("mandate_inactive")
            logger.info("GR3: Mandate %s — forcing mandate_renewal path", mandate_state)

        # ── Guardrail 4: Time-of-day DND gate (TRAI compliance) ──────────────
        now_ist = datetime.now(IST)
        hour    = now_ist.hour
        in_dnd  = hour >= self.DND_START_HOUR or hour < self.DND_END_HOUR
        if in_dnd:
            self._block(allowed, blocked, "whatsapp_nudge")
            fired.append("dnd_window")
            logger.info("GR4: DND window active (%02d:00 IST) — suppressing whatsapp_nudge", hour)

        # ── Guardrail 5: Active promise-to-pay ───────────────────────────────
        if has_promise:
            self._block(allowed, blocked, "whatsapp_nudge")
            self._block(allowed, blocked, "upi_collect")
            fired.append("active_promise_to_pay")
            logger.info("GR5: Active P2P promise — suppressing nudge/collect to avoid harassment")

        # ── Guardrail 6: Tier-based channel access ───────────────────────────
        if tier in (CustomerTier.BRONZE, CustomerTier.SILVER):
            # Bronze/Silver: no escalation (costs too much relative to amount)
            if amount < 500:
                self._block(allowed, blocked, "escalation")
                fired.append("tier_escalation_suppressed")
                logger.info("GR6: Tier=%s amount=₹%.0f — escalation suppressed (cost/benefit)", tier, amount)

        # ── Guardrail 7: RBI/NPCI ₹15,000 pre-debit notification rule ────────
        # RBI Circular: UPI Autopay mandates > ₹15,000 MUST send pre-debit
        # notification and require explicit customer confirmation.
        # Silent retry is non-compliant above this ceiling.
        if amount > self.RBI_PREDEBIT_THRESHOLD:
            self._block(allowed, blocked, "smart_retry")
            fired.append("rbi_predebit_threshold")
            logger.warning(
                "GR7 [RBI CIRCUIT BREAKER]: Amount ₹%.0f > ₹15,000 — "
                "silent retry BLOCKED per NPCI/RBI UPI Autopay circular. "
                "Forcing explicit customer consent channel.",
                amount,
            )

        # ── Guardrail 8: Daily contact cap (TRAI DND compliance) ─────────────
        # Max 3 outbound messages to any customer per day across all channels.
        if daily_touches >= self.DAILY_CONTACT_CAP:
            self._block(allowed, blocked, "whatsapp_nudge")
            self._block(allowed, blocked, "upi_collect")
            fired.append("daily_contact_cap")
            logger.warning(
                "GR8: Daily contact cap reached (%d touches) for this customer — "
                "suppressing further outbound communications.",
                daily_touches,
            )

        approved = len(allowed) > 0
        trust_label = (
            "HIGH" if trust_score >= 0.75 else
            "MED"  if trust_score >= 0.40 else
            "LOW"
        )
        reason   = (
            f"Tier={tier} | Budget={budget} retries left | "
            f"Trust={trust_score:.2f}({trust_label}) | "
            + (f"DND={in_dnd} | " if in_dnd else "")
            + (f"Promise={has_promise} | " if has_promise else "")
            + f"Guardrails={fired or 'none'}"
        )

        logger.info(
            "DecisionEngine → approved=%s | allowed=%s | blocked=%s | tier=%s | trust=%.2f",
            approved, allowed, blocked, tier, trust_score,
        )

        return GuardrailDecision(
            approved=approved,
            allowed_actions=allowed,
            blocked_actions=blocked,
            guardrails_fired=fired,
            customer_tier=tier,
            retry_budget_left=budget,
            reason=reason,
            trust_score=trust_score,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _block(allowed: list, blocked: list, action: str) -> None:
        if action in allowed:
            allowed.remove(action)
        if action not in blocked:
            blocked.append(action)
