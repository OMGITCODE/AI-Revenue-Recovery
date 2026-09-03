"""
Scenario simulator — runs named UPI failure scenarios through the
full agent pipeline and publishes results to the event store.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

# Ensure project root is on path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncio
import uuid
import random
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable

from src.models.upi_models import (
    MandateFrequency, MandateState,
    UPIAutopayEvent, UPIFailureCode, UPIMandate,
)
from src.agent.upi_detector import UPIAutopayDetector
from src.agent.upi_interventions import (
    SmartRetryIntervention, UPICollectIntervention,
    MandateRenewalIntervention, WhatsAppNudgeIntervention,
    EscalationIntervention,
)
from src.agent.decision_engine import DecisionEngine, infer_tier, CustomerTier
from src.agent.bandit import bandit_engine, RecoveryArm, get_context_key, resolve_arm
from src.agent.promise_tracker import promise_tracker
from src.agent.checkout_recovery import checkout_agent, DropOffReason
from src.agent.recovery_ledger import ledger as recovery_ledger
from src.agent.spend_pattern import spend_pattern_tracker
from src.agent.customer_identity import customer_identity_registry
from src.agent.mandate_expiry import mandate_expiry_scanner
from src.integrations.setu_aa import setu_aa
from api.store import RecoveryEvent, store

IST = timezone(timedelta(hours=5, minutes=30))

_decision_engine = DecisionEngine()
_module_listeners: list[Callable[[], Awaitable[None]]] = []

def register_module_listener(fn: Callable[[], Awaitable[None]]):
    """Register an async callback invoked when P2P or Checkout records are modified."""
    if fn not in _module_listeners:
        _module_listeners.append(fn)

async def _notify_module_listeners():
    for fn in list(_module_listeners):
        try:
            res = fn()
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass

# ── Cross-wiring helpers — failure code → auto-created P2P / Checkout records ──

_P2P_AUTO_CODES = {
    "U30":  (48,  "Salary-window retry scheduled. Customer promised payment after credit."),
    "TM":   (24,  "Tech error recovery: customer advised to retry after bank maintenance window."),
    "BT02": (72,  "Mandate expired — renewal link sent. Customer promised to complete re-registration."),
    "U29":  (48,  "Amount exceeded mandate limit. Customer to adjust limit and retry."),
    "U13":  (36,  "Mandate paused by customer — awaiting re-activation confirmation."),
    "U66":  (72,  "Weekly velocity limit exceeded. Scheduled for next cycle."),
    "RB":   (24,  "Bank decline: customer notified to verify 2FA/App approval."),
}
_CHECKOUT_AUTO_CODES = {
    "BT01": ("upi_intent_abandoned", "hinglish"),   # revoked mandate → treat like UPI abandoned
    "U69":  ("bank_error_exit",      "hinglish"),   # daily limit hit → redirect to alternate payment
    "TE":   ("otp_timeout",          "hinglish"),   # expired → OTP timeout analogue
    "RB":   ("bank_error_exit",      "english"),    # bank declined → bank error exit
    "BA":   ("payment_page_exit",    "hinglish"),   # account closed → alternate payment method
    "XB":   ("bank_error_exit",      "hinglish"),   # account blocked → alternate card/VPA
}

INTERVENTION_MAP = [
    ("smart_retry",     SmartRetryIntervention()),
    ("upi_collect",     UPICollectIntervention()),
    ("mandate_renewal", MandateRenewalIntervention()),
    ("whatsapp_nudge",  WhatsAppNudgeIntervention()),
    ("escalation",      EscalationIntervention()),
]
INTERVENTIONS = [iv for _, iv in INTERVENTION_MAP]

# ── Predefined scenarios ──────────────────────────────────────────────────────

SCENARIOS: dict[str, dict] = {
    "spike_critical": {
        "name":          "⚡ Sudden Spike — ₹100 Base vs ₹70,000 (Critical)",
        "failure_code":  UPIFailureCode.U30,
        "event_type":    "mandate.execution.failed",
        "vpa":           "rahul@oksbi",
        "bank":          "SBI",
        "amount":        70000.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-SPIKE-007",
    },
    "normal_variation": {
        "name":          "📊 Normal Range — ₹10k–₹50k Base vs ₹60,000 (Non-Critical)",
        "failure_code":  UPIFailureCode.U30,
        "event_type":    "mandate.execution.failed",
        "vpa":           "arjun@okicici",
        "bank":          "ICICI",
        "amount":        60000.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-NORMAL-008",
    },
    "u30": {
        "name":          "U30 — Insufficient Funds",
        "failure_code":  UPIFailureCode.U30,
        "event_type":    "mandate.execution.failed",
        "vpa":           "rahul@oksbi",
        "bank":          "SBI",
        "amount":        999.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-SBI-001",
    },
    "u29": {
        "name":          "U29 — Amount Exceeds Mandate Cap",
        "failure_code":  UPIFailureCode.U29,
        "event_type":    "mandate.execution.failed",
        "vpa":           "kavita@okkotak",
        "bank":          "Kotak Mahindra Bank",
        "amount":        3499.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-KOTAK-010",
    },
    "bt01": {
        "name":          "BT01 — Mandate Revoked",
        "failure_code":  UPIFailureCode.BT01,
        "event_type":    "mandate.revoked",
        "vpa":           "priya@okhdfcbank",
        "bank":          "HDFC",
        "amount":        1499.0,
        "mandate_state": MandateState.REVOKED,
        "retry_attempt": 0,
        "customer_id":   "CUST-HDFC-002",
    },
    "bt02": {
        "name":          "BT02 — Mandate Expired",
        "failure_code":  UPIFailureCode.BT02,
        "event_type":    "mandate.expired",
        "vpa":           "vikram@ybl",
        "bank":          "Yes Bank",
        "amount":        2999.0,
        "mandate_state": MandateState.EXPIRED,
        "retry_attempt": 0,
        "customer_id":   "CUST-YBL-003",
    },
    "u13": {
        "name":          "U13 — Mandate Paused",
        "failure_code":  UPIFailureCode.U13,
        "event_type":    "mandate.paused",
        "vpa":           "anita@paytm",
        "bank":          "Paytm Payments Bank",
        "amount":        299.0,
        "mandate_state": MandateState.PAUSED,
        "retry_attempt": 0,
        "customer_id":   "CUST-PTM-006",
    },
    "tm": {
        "name":          "TM — Technical Error (Max Retries)",
        "failure_code":  UPIFailureCode.TM,
        "event_type":    "mandate.execution.failed",
        "vpa":           "arjun@okicici",
        "bank":          "ICICI",
        "amount":        1499.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 3,
        "customer_id":   "CUST-ICICI-003",
    },
    "u69": {
        "name":          "U69 — Daily Limit Exceeded",
        "failure_code":  UPIFailureCode.U69,
        "event_type":    "mandate.execution.failed",
        "vpa":           "meera@okaxis",
        "bank":          "Axis",
        "amount":        2999.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-AXIS-004",
    },
    "ba": {
        "name":          "BA — Account Closed / KYC Frozen",
        "failure_code":  UPIFailureCode.BA,
        "event_type":    "mandate.execution.failed",
        "vpa":           "ramesh@okpnb",
        "bank":          "Punjab National Bank",
        "amount":        1299.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-PNB-011",
    },
    "xb": {
        "name":          "XB — Account Blocked / Freeze",
        "failure_code":  UPIFailureCode.XB,
        "event_type":    "mandate.execution.failed",
        "vpa":           "sneha@okunion",
        "bank":          "Union Bank of India",
        "amount":        899.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-UNION-012",
    },
    "te": {
        "name":          "TE — Transaction Expired / Switch Busy",
        "failure_code":  UPIFailureCode.TE,
        "event_type":    "mandate.execution.failed",
        "vpa":           "deepak@paytm",
        "bank":          "IDFC First Bank",
        "amount":        1999.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 1,
        "customer_id":   "CUST-IDFC-013",
    },
    "rb": {
        "name":          "RB — Bank Generic Decline / Anti-Fraud",
        "failure_code":  UPIFailureCode.RB,
        "event_type":    "mandate.execution.failed",
        "vpa":           "sunil@okcanara",
        "bank":          "Canara Bank",
        "amount":        4999.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-CANARA-014",
    },
    "u66": {
        "name":          "U66 — Weekly Velocity Limit Exceeded",
        "failure_code":  UPIFailureCode.U66,
        "event_type":    "mandate.execution.failed",
        "vpa":           "pooja@oksbi",
        "bank":          "SBI",
        "amount":        5000.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-SBI-015",
    },
    "rbi_threshold": {
        "name":          "🛡️ High-Value Mandate (> ₹15,000 RBI Rule)",
        "failure_code":  UPIFailureCode.U30,
        "event_type":    "mandate.execution.failed",
        "vpa":           "rohan@okhdfcbank",
        "bank":          "HDFC",
        "amount":        18500.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-HDFC-016",
        "category":      "general",
    },
    "rbi_enhanced_insurance": {
        "name":          "🛡️ Enhanced Limit — Insurance (₹45,000 <= ₹1 Lakh RBI Rule)",
        "failure_code":  UPIFailureCode.U30,
        "event_type":    "mandate.execution.failed",
        "vpa":           "aditya@okhdfcbank",
        "bank":          "HDFC",
        "amount":        45000.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-HDFC-017",
        "category":      "insurance",
    },
    "rbi_enhanced_breach": {
        "name":          "🛡️ Enhanced Limit Breach — Credit Card (> ₹1 Lakh RBI Rule)",
        "failure_code":  UPIFailureCode.U30,
        "event_type":    "mandate.execution.failed",
        "vpa":           "swati@okicici",
        "bank":          "ICICI",
        "amount":        115000.0,
        "mandate_state": MandateState.ACTIVE,
        "retry_attempt": 0,
        "customer_id":   "CUST-ICICI-018",
        "category":      "credit_card",
    },
    "proactive_mandate_expiry": {
        "name":          "🔔 Proactive Expiry Interceptor (T-48h Pre-BT02)",
        "failure_code":  UPIFailureCode.BT02,
        "event_type":    "mandate.expired",
        "vpa":           "priya@okhdfcbank",
        "bank":          "HDFC",
        "amount":        1499.0,
        "mandate_state": MandateState.EXPIRED,
        "retry_attempt": 0,
        "customer_id":   "CUST-HDFC-002",
        "category":      "general",
    },
}


def _make_upi_event(cfg: dict) -> UPIAutopayEvent:
    now = datetime.now(IST)
    mandate = UPIMandate(
        mandate_id=f"MND-{cfg['bank'].upper()}-{uuid.uuid4().hex[:6].upper()}",
        customer_id=cfg["customer_id"],
        customer_vpa=cfg["vpa"],
        amount=cfg["amount"],
        frequency=MandateFrequency.MONTHLY,
        state=cfg["mandate_state"],
        bank_name=cfg["bank"],
        bank_ifsc="XXXX0000001",
        created_at=now - timedelta(days=60),
        expiry_date=now + timedelta(days=305),
    )
    event_id = cfg.get("event_id")
    if not event_id:
        event_id = f"EVT-{uuid.uuid4().hex[:10].upper()}"
    return UPIAutopayEvent(
        event_id=event_id,
        event_type=cfg["event_type"],
        payment_id=f"pay_{uuid.uuid4().hex[:10]}",
        mandate=mandate,
        failure_code=cfg["failure_code"],
        failure_message=cfg["failure_code"].human_reason,
        debit_amount=cfg["amount"],
        occurred_at=now,
        retry_attempt=cfg["retry_attempt"],
    )


# ── Empirical channel conversion rates ─────────────────────────────────────────
CHANNEL_CONVERSION_RATES = {
    "mandate_renewal":   0.68,   # NPCI WhatsApp magic links
    "smart_retry":       0.88,   # U30 during salary window (1st-7th)
    "smart_retry_tech":  0.92,   # TM/TE 15-min exponential backoff
    "upi_collect":       0.65,   # Instant collect approval
    "whatsapp_nudge":    0.72,   # Conversational payment link
    "escalation":        0.0,    # Support queue — not instant auto-recovery
}

def evaluate_recovery_outcome(interventions: list[str], amount: float, failure_code: str = "") -> tuple[bool, str, float]:
    """
    Evaluates realistic recovery outcome for the executed intervention arm.
    Returns (success: bool, status: str, amount_recovered: float).
    Strictly single-arm evaluation without parallel compounding.
    """
    if not interventions:
        return False, "failed", 0.0

    if "escalation" in interventions:
        return False, "escalated", 0.0

    iv = interventions[0]
    if iv == "smart_retry" and failure_code in ("TM", "TE"):
        rate = CHANNEL_CONVERSION_RATES.get("smart_retry_tech", 0.92)
    else:
        rate = CHANNEL_CONVERSION_RATES.get(iv, 0.50)

    if random.random() < rate:
        return True, "recovered", float(amount)
    else:
        return False, "failed", 0.0



async def _execute_event_pipeline(upi_event: UPIAutopayEvent, cfg: dict) -> RecoveryEvent | None:
    """
    Executes the full RecoverIQ Agent pipeline for a UPI event:
      1. DETECT: Run UPIAutopayDetector to compute severity and spend pattern analysis.
      2. DECIDE: Run Setu AA check (for U30), evaluate payer trust score, and filter
                 through DecisionEngine guardrails (GR1-GR10) & Thompson Sampling bandit.
      3. INTERVENE: Execute ONLY the guardrail-approved allowed interventions.
      4. OUTCOME & RESOLUTION: Evaluate recovery likelihood, auto-fulfill active P2P on success,
                 update Thompson sampling Bayesian posteriors, log to Recovery Ledger, and
                 auto-create cross-panel records (P2P / Checkout).
    """
    detector = UPIAutopayDetector()
    risk = await detector.detect_from_upi_event(upi_event)
    if not risk:
        return None

    # Link customer identity
    customer_identity_registry.resolve_canonical_id(upi_event.customer_vpa, risk.customer_id)

    # Extract pattern analysis
    pat = risk.metadata.get("pattern_analysis")
    spike_ratio = pat.spike_ratio if pat else 1.0
    is_crit = pat.is_critical if pat else False
    summary = pat.explanation if pat else ""
    baseline = (
        f"Mean: ₹{pat.baseline_mean:,.0f} (Range: ₹{pat.typical_range[0]:,.0f}–₹{pat.typical_range[1]:,.0f})"
        if pat and pat.typical_range[1] > 0 else "New Customer"
    )

    # 1) DETECT Ledger log
    conf_detect = 0.75 if risk.severity.value in ("high", "critical") else 0.55
    recovery_ledger.log(
        event_type = "detect",
        vpa        = upi_event.customer_vpa,
        amount     = risk.amount,
        reasoning  = (
            f"{upi_event.failure_code.value} [{upi_event.failure_code.human_reason}] detected on {upi_event.bank_name}. "
            f"Customer: {risk.customer_id or upi_event.customer_vpa}. Severity={risk.severity.value}."
        ),
        confidence = conf_detect,
        channel    = "",
    )

    # 1b) TRUST SCORE calculation across unified identity
    trust_score = promise_tracker.payer_trust_score(upi_event.customer_vpa)

    # 1c) PATTERN CHECK Ledger log
    conf_pat = 0.98 if is_crit else 0.92
    recovery_ledger.log(
        event_type = "pattern_check",
        vpa        = upi_event.customer_vpa,
        amount     = risk.amount,
        reasoning  = (
            f"[Pattern Analyzer] {summary} "
            f"| SpikeRatio={spike_ratio:.1f}x | Critical={is_crit}"
        ),
        confidence = conf_pat,
        channel    = "pattern_engine",
    )

    # 1d) AA BALANCE CHECK for U30
    aa_check = ""
    aa_funds_available = None
    if upi_event.failure_code == UPIFailureCode.U30:
        aa_result = setu_aa.check_balance(
            vpa          = upi_event.customer_vpa,
            amount_due   = risk.amount,
            bank         = upi_event.bank_name,
            failure_code = upi_event.failure_code.value,
        )
        aa_check = aa_result.note
        aa_funds_available = aa_result.funds_available
        if aa_result.funds_available:
            trust_score = min(1.0, trust_score + 0.20)
        else:
            trust_score = max(0.05, trust_score - 0.10)
        recovery_ledger.log(
            event_type = "aa_check",
            vpa        = upi_event.customer_vpa,
            amount     = risk.amount,
            reasoning  = (
                f"[AA] Setu sandbox consent approved. "
                + aa_result.note
                + f" (Trust adjusted → {trust_score:.2f})"
            ),
            confidence = 0.92,
            channel    = "setu_aa",
        )

    # 2) DECIDE Stage: DecisionEngine guardrails & bandit
    mandate_state_val = cfg.get("mandate_state", "active")
    if hasattr(mandate_state_val, "value"):
        mandate_state_val = mandate_state_val.value

    has_promise = promise_tracker.has_active(upi_event.customer_vpa, risk.amount)
    decision = _decision_engine.evaluate(
        failure_code     = upi_event.failure_code.value,
        mandate_state    = str(mandate_state_val),
        amount           = risk.amount,
        retry_count      = cfg.get("retry_attempt", 0),
        has_promise      = has_promise,
        trust_score      = trust_score,
        customer_vpa     = upi_event.customer_vpa,
        customer_id      = risk.customer_id,
        pattern_analysis = pat,
        category         = cfg.get("category", "general"),
    )
    confidence_decide = 0.90 if decision.guardrails_fired else 0.72
    evt_type_decide   = "guardrail" if decision.guardrails_fired else "decide"
    first_channel     = decision.allowed_actions[0] if decision.allowed_actions else ""
    e_decide = recovery_ledger.log(
        event_type = evt_type_decide,
        vpa        = upi_event.customer_vpa,
        amount     = risk.amount,
        reasoning  = decision.reason,
        confidence = confidence_decide,
        channel    = first_channel,
    )

    # 3) INTERVENE Stage: Execute ONLY the top guardrail-approved & bandit-selected action!
    iv_types, iv_msgs, scheduled_at, action_url = [], [], None, None
    intervention_by_key = dict(INTERVENTION_MAP)
    for action_key in decision.allowed_actions:
        iv = intervention_by_key.get(action_key)
        if iv and iv.can_handle(risk):
            result = await iv.execute(risk)
            iv_types.append(result.intervention_type.value)
            iv_msgs.append(result.message)
            if result.scheduled_at and not scheduled_at:
                scheduled_at = result.scheduled_at.strftime("%d %b %Y, %I:%M %p IST")
            if result.action_url and not action_url:
                action_url = result.action_url
            break  # Strictly single-arm execution: only the bandit's chosen intervention runs!

    # 4) Evaluate Recovery Outcome
    if upi_event.failure_code == UPIFailureCode.U30 and aa_funds_available is False:
        # Verified insufficient funds via Setu AA (e.g. Rahul salary crunch):
        # Cannot auto-recover immediately since the bank account is short on funds.
        # Smart retry is queued for the salary window (or recoverable via Instant UPI QR).
        success, status, amount_rec = False, "failed", 0.0
    else:
        success, status, amount_rec = evaluate_recovery_outcome(
            iv_types, risk.amount, failure_code=upi_event.failure_code.value
        )

    # If successfully recovered, auto-fulfill any active P2P promise for this user
    if success:
        promise_tracker.fulfill_active(upi_event.customer_vpa, risk.amount)

    # 5) Log outcome to ledger and update Thompson bandit posteriors
    if iv_types:
        channel = iv_types[0]
        e_iv = recovery_ledger.log(
            event_type = "intervene",
            vpa        = upi_event.customer_vpa,
            amount     = risk.amount,
            reasoning  = iv_msgs[0] if iv_msgs else channel,
            confidence = 0.68,
            channel    = channel,
        )
        outcome = "success" if success else ("escalated" if status == "escalated" else "failure")
        recovery_ledger.mark_outcome(e_iv.ledger_id, outcome, amount_rec)
        if decision.bandit_decision:
            ckey = decision.bandit_decision.get("context_key")
            selected_arm = decision.bandit_decision.get("selected_arm") or channel
            if ckey:
                bandit_engine.update(
                    context_key=ckey,
                    arm=selected_arm,
                    success=(outcome == "success"),
                    amount_recovered=amount_rec,
                )
    elif not decision.approved:
        recovery_ledger.mark_outcome(e_decide.ledger_id, "skipped", 0)

    # 6) Cross-wiring: auto-create linked panel records
    modules_changed = False
    if upi_event.failure_code.value in _P2P_AUTO_CODES:
        if not promise_tracker.has_active(upi_event.customer_vpa, risk.amount):
            deadline_h, notes = _P2P_AUTO_CODES[upi_event.failure_code.value]
            promise_tracker.create(
                vpa           = upi_event.customer_vpa,
                customer_id   = risk.customer_id,
                amount        = risk.amount,
                bank          = upi_event.bank_name,
                failure_code  = upi_event.failure_code.value,
                deadline_hours= deadline_h,
                channel       = "whatsapp",
                notes         = notes,
            )
            recovery_ledger.log(
                event_type = "p2p",
                vpa        = upi_event.customer_vpa,
                amount     = risk.amount,
                reasoning  = f"Auto P2P created from {upi_event.failure_code.value} scenario. {notes}",
                confidence = 0.70,
                channel    = "whatsapp",
            )
            modules_changed = True

    if upi_event.failure_code.value in _CHECKOUT_AUTO_CODES:
        if not checkout_agent.has_active(upi_event.customer_vpa, risk.amount):
            reason, lang = _CHECKOUT_AUTO_CODES[upi_event.failure_code.value]
            checkout_agent.record_drop_off(
                customer_vpa    = upi_event.customer_vpa,
                customer_phone  = "",
                cart_amount     = risk.amount,
                merchant        = upi_event.bank_name + " Merchant",
                drop_off_reason = reason,
                language        = lang,
            )
            recovery_ledger.log(
                event_type = "checkout",
                vpa        = upi_event.customer_vpa,
                amount     = risk.amount,
                reasoning  = f"Auto checkout session from {upi_event.failure_code.value}: customer redirected to alternate payment. Hinglish nudge dispatched.",
                confidence = 0.62,
                channel    = "whatsapp",
            )
            modules_changed = True

    # Auto-dispatch proactive nudge on expiring mandate if BT02
    if upi_event.failure_code == UPIFailureCode.BT02:
        expiring = mandate_expiry_scanner.find_expiring_mandates(within_hours=72)
        matching = [m for m in expiring if m.customer_vpa == upi_event.customer_vpa]
        if matching and matching[0].status == "PENDING":
            await mandate_expiry_scanner.dispatch_proactive_nudge(matching[0].mandate_id)
            modules_changed = True

    if modules_changed:
        await _notify_module_listeners()

    ev = RecoveryEvent(
        id=upi_event.event_id,
        timestamp=datetime.now(IST).strftime("%H:%M:%S"),
        event_type=upi_event.event_type,
        failure_code=upi_event.failure_code.value,
        failure_reason=upi_event.failure_code.human_reason,
        customer_id=risk.customer_id,
        customer_vpa=upi_event.customer_vpa,
        bank=upi_event.bank_name,
        amount=risk.amount,
        severity=risk.severity.value,
        interventions=iv_types,
        intervention_msgs=iv_msgs,
        scheduled_at=scheduled_at,
        action_url=action_url,
        success=success,
        status=status,
        amount_recovered=amount_rec,
        scenario_name=cfg.get("name", "UPI Recovery Event"),
        trust_score=round(trust_score, 2),
        aa_check=aa_check,
        pattern_spike_ratio=spike_ratio,
        is_pattern_critical=is_crit,
        pattern_summary=summary,
        pattern_baseline=baseline,
    )
    return ev


async def process_and_log_event(ev: Any, cfg: dict) -> RecoveryEvent | None:
    """
    Process an event through the agent pipeline and log to the Recovery Ledger.
    Accepts a UPIAutopayEvent or returns an existing RecoveryEvent for backward compatibility.
    """
    if isinstance(ev, UPIAutopayEvent):
        return await _execute_event_pipeline(ev, cfg)
    return ev


async def run_scenario(scenario_key: str) -> RecoveryEvent | None:
    """
    Run a named scenario through the full agent pipeline,
    log to Recovery Ledger, and publish the result to the event store.
    """
    cfg = SCENARIOS.get(scenario_key)
    if not cfg:
        return None
    cfg_copy = cfg.copy()
    # Unique event ID so repeated runs append to event history without clobbering
    cfg_copy["event_id"] = f"EVT-SIM-{scenario_key.upper()}-{uuid.uuid4().hex[:6].upper()}"

    upi_event = _make_upi_event(cfg_copy)
    ev = await _execute_event_pipeline(upi_event, cfg_copy)
    if ev:
        await store.add_event(ev)
    return ev


async def force_lapse_mandate(mandate_id: str) -> tuple[Any, RecoveryEvent | None]:
    """
    Simulates an expiring recurring mandate lapsing past its validity cutoff without renewal.
    Marks the mandate status as LAPSED, generates a genuine BT02 (Mandate Expired) failure event,
    and runs it through the canonical _execute_event_pipeline sequence.
    """
    m = mandate_expiry_scanner.get_mandate(mandate_id)
    if not m:
        return None, None

    m.status = "LAPSED"

    cfg = {
        "name": f"BT02 — Expired Mandate ({m.plan_name})",
        "failure_code": UPIFailureCode.BT02,
        "event_type": "mandate.expired",
        "vpa": m.customer_vpa,
        "bank": m.bank_name,
        "amount": m.amount,
        "mandate_state": MandateState.EXPIRED,
        "retry_attempt": 0,
        "customer_id": m.customer_id,
        "mandate_id": m.mandate_id,
        "plan_name": m.plan_name,
        "event_id": f"EVT-LAPSE-{m.mandate_id.upper()}-{uuid.uuid4().hex[:4].upper()}",
    }

    upi_event = _make_upi_event(cfg)
    ev = await _execute_event_pipeline(upi_event, cfg)
    if ev:
        await store.add_event(ev)

    await _notify_module_listeners()
    return m, ev


async def run_custom_webhook(payload: dict) -> RecoveryEvent | None:
    """
    Run a raw custom webhook payload through the agent pipeline,
    log to Recovery Ledger, and publish to the event store.
    """
    from src.integrations.razorpay_upi import parse_upi_webhook
    upi_event = parse_upi_webhook(payload)
    if not upi_event:
        return None

    cfg = {"mandate_state": "active", "retry_attempt": 0, "name": "Custom Webhook"}
    ev = await _execute_event_pipeline(upi_event, cfg)
    if ev:
        await store.add_event(ev)
    return ev


async def run_custom_scenario(form: dict) -> RecoveryEvent | None:
    """
    Build a UPIAutopayEvent from a user-supplied form dict, run the
    full agent pipeline, log to Recovery Ledger, and publish to the event store.
    """
    try:
        fc = UPIFailureCode(form["failure_code"].upper())
    except (ValueError, KeyError):
        fc = UPIFailureCode.UNKNOWN

    state_map = {s.value: s for s in MandateState}
    mandate_state = state_map.get(form.get("mandate_state", "active").lower(), MandateState.ACTIVE)

    if fc in (UPIFailureCode.BT01,):
        event_type = "mandate.revoked"
    elif fc in (UPIFailureCode.BT02,):
        event_type = "mandate.expired"
    elif fc in (UPIFailureCode.U13,):
        event_type = "mandate.paused"
    else:
        event_type = "mandate.execution.failed"

    vpa_val = form.get("vpa", "user@oksbi")
    cust_id = form.get("customer_id")
    if not cust_id:
        # Link to known alias if available
        prof = customer_identity_registry.get_profile(vpa_val)
        if prof and prof.customer_ids:
            cust_id = next(iter(prof.customer_ids)).upper()
        else:
            cust_id = f"CUST-{uuid.uuid4().hex[:6].upper()}"

    cfg = {
        "failure_code":  fc,
        "event_type":    event_type,
        "vpa":           vpa_val,
        "bank":          form.get("bank", "Unknown Bank"),
        "amount":        float(form.get("amount", 100)),
        "mandate_state": mandate_state,
        "retry_attempt": int(form.get("retry_attempt", 0)),
        "customer_id":   cust_id,
        "name":          form.get("scenario_name", "Custom Scenario"),
        "category":      form.get("category", "general"),
    }

    upi_event = _make_upi_event(cfg)
    ev = await _execute_event_pipeline(upi_event, cfg)
    if ev:
        await store.add_event(ev)
    return ev
