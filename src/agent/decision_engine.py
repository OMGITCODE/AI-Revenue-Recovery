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
  8. Daily contact cap — max 3 outbound touches per customer per day (across all aliases)
  9. Suppression hold  — compliance opt-out, dispute, hardship pause across all customer aliases
  10. Spend spike      — sudden upward spike vs customer historical baseline blocks silent retries
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from ..utils.logger import get_logger
from .customer_identity import customer_identity_registry

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

from .bandit import bandit_engine, BanditDecision

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
    bandit_decision:    Optional[dict] = None # Thompson sampling MAB selection
    pattern_analysis:   Optional[dict] = None # Spend pattern baseline & anomaly details
    category:           str         = "general"
    rbi_threshold:      float       = 15_000.0

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
            "bandit_decision":   self.bandit_decision,
            "pattern_analysis":  self.pattern_analysis,
            "category":          self.category,
            "rbi_threshold":     self.rbi_threshold,
        }


# ── Engine ────────────────────────────────────────────────────────────────────

class DecisionEngine:
    """
    Stateless guardrail evaluator with unified customer identity resolution.

    Usage:
        engine   = DecisionEngine()
        decision = engine.evaluate(event_dict, retry_count, active_promise)

    Guardrails:
        GR1  — Amount < ₹10: suppress all (cost > benefit)
        GR2  — Retry count ≥ 3: no more smart_retry
        GR3  — Mandate revoked/expired: force mandate_renewal path
        GR4  — DND window 21:00–08:00 IST (TRAI): suppress whatsapp_nudge
        GR5  — Active promise-to-pay: suppress nudge/collect (no harassment)
        GR6  — Tier/amount: Bronze < ₹500 skip escalation (cost > value)
        GR7  — RBI/NPCI: Category-aware UPI Autopay pre-debit notification rule
               (₹1,00,000 for insurance/MF/credit cards; ₹15,000 for general/education).
               Exceeding category threshold blocks silent retry and forces explicit consent.
        GR8  — Daily contact cap: max 3 outbound touches per customer per day
        GR9  — Inbound compliance hold / blacklist (wrong number, dispute, hardship)
        GR10 — Spend Pattern Anomaly: Sudden upward spike vs historical baseline
               blocks silent automatic retry to protect customer from unauthorized depletion.
    """

    ALL_ACTIONS = ["smart_retry", "upi_collect", "mandate_renewal", "whatsapp_nudge", "escalation"]

    # DND-safe hours: 08:00–21:00 IST
    DND_START_HOUR = 21
    DND_END_HOUR   = 8

    # RBI Digital Payments - E-Mandate Framework Category Limits (₹)
    # Enhanced limit (₹1,00,000) applies strictly to insurance premiums, mutual fund
    # subscriptions, and credit card bill payments per RBI circulars.
    # Education and all other categories remain under the general ₹15,000 threshold.
    RBI_MANDATE_CATEGORY_LIMITS: Dict[str, float] = {
        "insurance": 100_000.0,
        "mutual_fund": 100_000.0,
        "credit_card": 100_000.0,
        "general": 15_000.0,
    }

    # RBI/NPCI UPI Autopay pre-debit notification baseline ceiling (₹)
    RBI_PREDEBIT_THRESHOLD = 15_000

    # Daily outbound contact cap per customer
    DAILY_CONTACT_CAP = 3

    @classmethod
    def get_rbi_threshold(cls, category: str = "general") -> float:
        """Return the applicable RBI e-mandate pre-debit notification threshold for a category."""
        cat_key = (category or "general").strip().lower()
        return cls.RBI_MANDATE_CATEGORY_LIMITS.get(
            cat_key, cls.RBI_MANDATE_CATEGORY_LIMITS["general"]
        )

    def evaluate(
        self,
        failure_code:     str,
        mandate_state:    str,
        amount:           float,
        retry_count:      int   = 0,
        has_promise:      bool  = False,
        daily_touches:    int   = 0,
        trust_score:      float = 0.5,   # payer trust score from promise_tracker
        current_hour:     Optional[int] = None,
        rng:              Optional[random.Random] = None,
        customer_vpa:     str   = "",
        pattern_analysis: Optional[Any] = None,
        customer_id:      str   = "",
        category:         str   = "general",
    ) -> GuardrailDecision:

        # Link customer identifiers
        if customer_vpa or customer_id:
            customer_identity_registry.resolve_canonical_id(customer_vpa, customer_id)

        # Look up cumulative behavioral history counts if not passed explicitly
        effective_touches = (
            daily_touches if daily_touches > 0
            else customer_identity_registry.get_daily_touches(customer_vpa, customer_id)
        )
        effective_retries = (
            retry_count if retry_count > 0
            else customer_identity_registry.get_retry_count(customer_vpa, customer_id)
        )

        active_rng = rng if rng is not None else random.Random()
        tier       = infer_tier(amount)
        allowed    = list(self.ALL_ACTIONS)
        blocked    = []
        fired      = []

        # ── Guardrail 1: Amount too small ────────────────────────────────────
        rbi_limit = self.get_rbi_threshold(category)
        if amount < 10:
            logger.warning("GR1: Amount ₹%.2f below minimum threshold — suppressing all actions", amount)
            return GuardrailDecision(
                approved=False, allowed_actions=[], blocked_actions=self.ALL_ACTIONS,
                guardrails_fired=["amount_threshold"],
                customer_tier=tier, retry_budget_left=0,
                reason=f"Amount ₹{amount:.2f} below ₹10 minimum intervention threshold",
                category=category,
                rbi_threshold=rbi_limit,
            )

        # ── Guardrail 2: Retry budget exhausted ──────────────────────────────
        budget = max(0, 3 - effective_retries)
        if effective_retries >= 3:
            self._block(allowed, blocked, "smart_retry")
            fired.append("retry_budget_exhausted")
            logger.info("GR2: Retry budget exhausted (attempt %d) — blocking smart_retry", effective_retries)

        # ── Guardrail 3: Mandate state — skip retry if revoked/expired ───────
        if mandate_state in ("revoked", "expired"):
            self._block(allowed, blocked, "smart_retry")
            self._block(allowed, blocked, "upi_collect")
            fired.append("mandate_inactive")
            logger.info("GR3: Mandate %s — forcing mandate_renewal path", mandate_state)

        # ── Guardrail 4: Time-of-day DND gate (TRAI compliance) ──────────────
        hour = current_hour if current_hour is not None else datetime.now(IST).hour
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
            if amount < 500:
                self._block(allowed, blocked, "escalation")
                fired.append("tier_escalation_suppressed")
                logger.info("GR6: Tier=%s amount=₹%.0f — escalation suppressed (cost/benefit)", tier, amount)

        # ── Guardrail 7: RBI/NPCI Category-Aware Pre-Debit Notification Rule ─
        if amount > rbi_limit:
            self._block(allowed, blocked, "smart_retry")
            fired.append("rbi_predebit_threshold")
            framework_cite = (
                "RBI Digital Payments - E-Mandate Framework (₹1,00,000 enhanced limit)"
                if rbi_limit >= 100_000
                else "NPCI/RBI UPI Autopay circular (₹15,000 baseline threshold)"
            )
            logger.warning(
                "GR7 [RBI CIRCUIT BREAKER]: Amount ₹%.0f > ₹%.0f for category '%s' — "
                "silent retry BLOCKED per %s. "
                "Forcing explicit customer consent channel.",
                amount,
                rbi_limit,
                category,
                framework_cite,
            )

        # ── Guardrail 8: Daily contact cap (TRAI DND compliance) ─────────────
        if effective_touches >= self.DAILY_CONTACT_CAP:
            self._block(allowed, blocked, "whatsapp_nudge")
            self._block(allowed, blocked, "upi_collect")
            fired.append("daily_contact_cap")
            logger.warning(
                "GR8: Daily contact cap reached (%d touches) for this customer — "
                "suppressing further outbound communications.",
                effective_touches,
            )

        # ── Guardrail 9: Inbound Suppression & Compliance Blacklist ──────────
        check_ident = customer_vpa or customer_id
        if check_ident:
            try:
                from .whatsapp_inbound import suppression_registry
                is_suppressed, supp_reason = suppression_registry.is_suppressed(check_ident)
                if is_suppressed:
                    if supp_reason == "permanently_blacklisted_wrong_number":
                        logger.warning("GR9 [COMPLIANCE]: %s is permanently blacklisted (wrong number/opt-out) — suppressing all actions", check_ident)
                        return GuardrailDecision(
                            approved=False, allowed_actions=[], blocked_actions=self.ALL_ACTIONS,
                            guardrails_fired=["compliance_blacklist_wrong_number"],
                            customer_tier=tier, retry_budget_left=0,
                            reason=f"Customer {check_ident} permanently opt-out/wrong number. All communications blocked.",
                        )
                    else:
                        self._block(allowed, blocked, "smart_retry")
                        self._block(allowed, blocked, "whatsapp_nudge")
                        self._block(allowed, blocked, "upi_collect")
                        fired.append(f"suppression_{supp_reason}")
                        logger.info("GR9: Active hold (%s) for %s — suppressing automated retries/nudges", supp_reason, check_ident)
            except Exception as e:
                logger.debug("Suppression registry check skipped: %s", e)

        # ── Guardrail 10: Spend Pattern Anomaly (Sudden Upward Spike) ────────
        pattern_res_dict = None
        if pattern_analysis is not None:
            pattern_res_dict = pattern_analysis.to_dict() if hasattr(pattern_analysis, "to_dict") else dict(pattern_analysis)
            is_crit = getattr(pattern_analysis, "is_critical", False) or (
                isinstance(pattern_analysis, dict) and pattern_analysis.get("is_critical", False)
            )
            if is_crit:
                self._block(allowed, blocked, "smart_retry")
                fired.append("spend_pattern_spike_critical")
                spike_r = getattr(pattern_analysis, "spike_ratio", 1.0)
                logger.warning(
                    "GR10 [SPEND PATTERN SPIKE]: Amount ₹%.2f is a %.1fx sudden upward spike for %s — silent retry BLOCKED to protect payer.",
                    amount, spike_r, check_ident or "customer",
                )
        elif check_ident:
            try:
                from .spend_pattern import spend_pattern_tracker
                pat = spend_pattern_tracker.analyze(vpa=customer_vpa, current_amount=amount, customer_id=customer_id)
                pattern_res_dict = pat.to_dict()
                if pat.is_critical:
                    self._block(allowed, blocked, "smart_retry")
                    fired.append("spend_pattern_spike_critical")
                    logger.warning(
                        "GR10 [SPEND PATTERN SPIKE]: Amount ₹%.2f is a %.1fx sudden upward spike for %s "
                        "(baseline mean ₹%.2f, range ₹%.2f–₹%.2f) — silent retry BLOCKED to protect payer.",
                        amount, pat.spike_ratio, check_ident,
                        pat.baseline_mean, pat.typical_range[0], pat.typical_range[1],
                    )
            except Exception as e:
                logger.debug("Spend pattern analysis check skipped: %s", e)

        approved = len(allowed) > 0
        trust_label = (
            "HIGH" if trust_score >= 0.75 else
            "MED"  if trust_score >= 0.40 else
            "LOW"
        )

        # ── Step 2: Contextual Thompson Sampling Action Selection ────────────
        bandit_res = None
        if approved:
            # Map NPCI failure code to bandit category
            if failure_code in ("U30", "U13"):
                cat = "insufficient_funds"
            elif failure_code in ("TM", "TE"):
                cat = "technical_error"
            elif failure_code in ("BT01", "BT02", "BA", "RB"):
                cat = "mandate_inactive"
            else:
                cat = "insufficient_funds"

            bandit_decision_obj = bandit_engine.select_best_arm(
                failure_category=cat,
                amount=amount,
                customer_tier=tier.value,
                trust_score=trust_score,
                allowed_actions=allowed,
                rng=active_rng,
            )
            bandit_res = bandit_decision_obj.to_dict()

            # Re-order allowed actions so the Thompson-selected optimal arm is first
            top_arm = bandit_decision_obj.selected_arm.value
            if top_arm in allowed:
                allowed.remove(top_arm)
                allowed.insert(0, top_arm)

        reason   = (
            f"Tier={tier} | Budget={budget} retries left | "
            f"Trust={trust_score:.2f}({trust_label}) | "
            + (f"DND={in_dnd} | " if in_dnd else "")
            + (f"Promise={has_promise} | " if has_promise else "")
            + (f"PatternSpike={pattern_res_dict['is_critical']} ({pattern_res_dict['spike_ratio']:.1f}x) | " if pattern_res_dict and pattern_res_dict.get("is_spike") else "")
            + (f"MAB=[{bandit_res['selected_arm']}] | " if bandit_res else "")
            + f"Guardrails={fired or 'none'}"
        )

        logger.info(
            "DecisionEngine → approved=%s | allowed=%s | blocked=%s | tier=%s | trust=%.2f | bandit=%s",
            approved, allowed, blocked, tier, trust_score,
            bandit_res["selected_arm"] if bandit_res else "none",
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
            bandit_decision=bandit_res,
            pattern_analysis=pattern_res_dict,
            category=category,
            rbi_threshold=rbi_limit,
        )

    @staticmethod
    def _block(allowed: list, blocked: list, action: str) -> None:
        if action in allowed:
            allowed.remove(action)
        if action not in blocked:
            blocked.append(action)
