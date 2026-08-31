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
}
_CHECKOUT_AUTO_CODES = {
    "BT01": ("upi_intent_abandoned", "hinglish"),   # revoked mandate → treat like UPI abandoned
    "U69":  ("bank_error_exit",      "hinglish"),   # daily limit hit → redirect to alternate payment
    "TE":   ("otp_timeout",          "hinglish"),   # expired → OTP timeout analogue
    "RB":   ("bank_error_exit",      "english"),    # bank declined → bank error exit
}

INTERVENTIONS = [
    SmartRetryIntervention(),
    UPICollectIntervention(),
    MandateRenewalIntervention(),
    WhatsAppNudgeIntervention(),
    EscalationIntervention(),
]

# ── Predefined scenarios ──────────────────────────────────────────────────────

SCENARIOS: dict[str, dict] = {
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
    "bt01": {
        "name":          "BT01 — Mandate Revoked",
        "failure_code":  UPIFailureCode.BT01,
        "event_type":    "mandate.revoked",
        "vpa":           "priya@okhdfcbank",
        "bank":          "HDFC",
        "amount":        499.0,
        "mandate_state": MandateState.REVOKED,
        "retry_attempt": 0,
        "customer_id":   "CUST-HDFC-002",
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
    "bt02": {
        "name":          "BT02 — Mandate Expired",
        "failure_code":  UPIFailureCode.BT02,
        "event_type":    "mandate.expired",
        "vpa":           "vikram@ybl",
        "bank":          "Yes Bank",
        "amount":        799.0,
        "mandate_state": MandateState.EXPIRED,
        "retry_attempt": 0,
        "customer_id":   "CUST-YBL-005",
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
        event_id = uuid.uuid4().hex[:12].upper()
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


import random

# ── Empirical channel conversion rates (aligned with benchmark.py) ────────────
CHANNEL_CONVERSION_RATES = {
    "mandate_renewal": 0.68,   # NPCI WhatsApp magic links
    "smart_retry":     0.88,   # U30 during salary window (1st-7th)
    "upi_collect":     0.65,   # Instant collect approval
    "whatsapp_nudge":  0.72,   # Conversational payment link
    "escalation":      0.0,    # Support queue — not instant auto-recovery
}

def evaluate_recovery_outcome(interventions: list[str], amount: float) -> tuple[bool, str, float]:
    """
    Evaluates realistic recovery outcome based on empirical conversion rates.
    Returns (success: bool, status: str, amount_recovered: float).
    """
    if not interventions:
        return False, "failed", 0.0

    # If escalation is among interventions (e.g. TM max retries exhausted)
    if "escalation" in interventions and len(interventions) <= 2:
        return False, "escalated", 0.0

    # Calculate combined success probability across active interventions
    fail_prob = 1.0
    for iv in interventions:
        rate = CHANNEL_CONVERSION_RATES.get(iv, 0.50)
        fail_prob *= (1.0 - rate)

    success_prob = 1.0 - fail_prob
    if random.random() < success_prob:
        return True, "recovered", float(amount)
    else:
        return False, "failed", 0.0


async def process_and_log_event(ev: RecoveryEvent | None, cfg: dict) -> RecoveryEvent | None:
    """Log every decision step of an executed event to the Recovery Ledger,
    update Thompson Sampling Bayesian priors, and auto-create cross-panel records."""
    if not ev:
        return None

    # 1) DETECT entry
    conf_detect = 0.75 if ev.severity in ("high", "critical") else 0.55
    recovery_ledger.log(
        event_type = "detect",
        vpa        = ev.customer_vpa,
        amount     = ev.amount,
        reasoning  = (
            f"{ev.failure_code} [{ev.failure_reason}] detected on {ev.bank}. "
            f"Severity={ev.severity}."
        ),
        confidence = conf_detect,
        channel    = "",
    )

    # 1b) TRUST SCORE — compute from P2P history before deciding
    trust_score = promise_tracker.payer_trust_score(ev.customer_vpa)

    # 1c) AA BALANCE CHECK — for U30 (insufficient funds) only
    #     Replace salary-cycle guess with a verified balance signal.
    aa_check = ""
    if ev.failure_code == "U30":
        aa_result = setu_aa.check_balance(
            vpa          = ev.customer_vpa,
            amount_due   = ev.amount,
            bank         = ev.bank,
            failure_code = ev.failure_code,
        )
        aa_check = aa_result.note
        # Boost or dampen trust score based on verified funds
        if aa_result.funds_available:
            trust_score = min(1.0, trust_score + 0.20)  # confirmed salary credit
        else:
            trust_score = max(0.05, trust_score - 0.10)  # still short
        recovery_ledger.log(
            event_type = "aa_check",
            vpa        = ev.customer_vpa,
            amount     = ev.amount,
            reasoning  = (
                f"[AA] Setu sandbox consent approved. "
                + aa_result.note
                + f" (Trust adjusted → {trust_score:.2f})"
            ),
            confidence = 0.92,   # AA signal is high-confidence vs. heuristic
            channel    = "setu_aa",
        )

    # Patch computed fields back onto the event
    ev.trust_score = round(trust_score, 2)
    ev.aa_check    = aa_check

    # 2) DECIDE entry — log guardrail / strategy
    mandate_state_val = cfg.get("mandate_state", "active")
    if hasattr(mandate_state_val, "value"):
        mandate_state_val = mandate_state_val.value

    decision = _decision_engine.evaluate(
        failure_code  = ev.failure_code,
        mandate_state = str(mandate_state_val),
        amount        = ev.amount,
        retry_count   = cfg.get("retry_attempt", 0),
        has_promise   = False,
        trust_score   = trust_score,
    )
    confidence_decide = 0.90 if decision.guardrails_fired else 0.72
    evt_type_decide   = "guardrail" if decision.guardrails_fired else "decide"
    first_channel     = decision.allowed_actions[0] if decision.allowed_actions else ""
    e_decide = recovery_ledger.log(
        event_type = evt_type_decide,
        vpa        = ev.customer_vpa,
        amount     = ev.amount,
        reasoning  = decision.reason,
        confidence = confidence_decide,
        channel    = first_channel,
    )

    # 3) INTERVENE entry — log what was actually dispatched
    if ev.interventions:
        channel = ev.interventions[0]
        e_iv = recovery_ledger.log(
            event_type = "intervene",
            vpa        = ev.customer_vpa,
            amount     = ev.amount,
            reasoning  = ev.intervention_msgs[0] if ev.intervention_msgs else channel,
            confidence = 0.68,
            channel    = channel,
        )
        outcome = "success" if ev.success else ("escalated" if getattr(ev, "status", "") == "escalated" else "failure")
        rec_amt = getattr(ev, "amount_recovered", ev.amount if ev.success else 0.0)
        recovery_ledger.mark_outcome(e_iv.ledger_id, outcome, rec_amt)
        # Online Bayesian Posterior Update
        if decision.bandit_decision:
            ckey = decision.bandit_decision.get("context_key")
            selected_arm = decision.bandit_decision.get("selected_arm") or channel
            if ckey:
                bandit_engine.update(
                    context_key=ckey,
                    arm=selected_arm,
                    success=(outcome == "success"),
                    amount_recovered=rec_amt,
                )
    elif not decision.approved:
        recovery_ledger.mark_outcome(e_decide.ledger_id, "skipped", 0)

    # ── 4) Cross-wiring: auto-create linked panel records ─────────────────────
    modules_changed = False

    # Auto-create a Promise-to-Pay for applicable failure codes
    if ev.failure_code in _P2P_AUTO_CODES:
        if not promise_tracker.has_active(ev.customer_vpa, ev.amount):
            deadline_h, notes = _P2P_AUTO_CODES[ev.failure_code]
            promise_tracker.create(
                vpa           = ev.customer_vpa,
                amount        = ev.amount,
                bank          = ev.bank,
                failure_code  = ev.failure_code,
                deadline_hours= deadline_h,
                channel       = "whatsapp",
                notes         = notes,
            )
            recovery_ledger.log(
                event_type = "p2p",
                vpa        = ev.customer_vpa,
                amount     = ev.amount,
                reasoning  = f"Auto P2P created from {ev.failure_code} scenario. {notes}",
                confidence = 0.70,
                channel    = "whatsapp",
            )
            modules_changed = True

    # Auto-create a Checkout Drop-off for applicable failure codes
    if ev.failure_code in _CHECKOUT_AUTO_CODES:
        if not checkout_agent.has_active(ev.customer_vpa, ev.amount):
            reason, lang = _CHECKOUT_AUTO_CODES[ev.failure_code]
            checkout_agent.record_drop_off(
                customer_vpa    = ev.customer_vpa,
                customer_phone  = "",
                cart_amount     = ev.amount,
                merchant        = ev.bank + " Merchant",
                drop_off_reason = reason,
                language        = lang,
            )
            recovery_ledger.log(
                event_type = "checkout",
                vpa        = ev.customer_vpa,
                amount     = ev.amount,
                reasoning  = f"Auto checkout session from {ev.failure_code}: customer redirected to alternate payment. Hinglish nudge dispatched.",
                confidence = 0.62,
                channel    = "whatsapp",
            )
            modules_changed = True

    # Broadcast SSE so browser panels refresh automatically
    if modules_changed:
        await _notify_module_listeners()

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
    cfg_copy["event_id"] = f"EVT-SIM-{scenario_key.upper()}"

    upi_event = _make_upi_event(cfg_copy)
    detector  = UPIAutopayDetector()
    risk      = await detector.detect_from_upi_event(upi_event)
    if not risk:
        return None

    iv_types, iv_msgs, scheduled_at, action_url = [], [], None, None

    for iv in INTERVENTIONS:
        if iv.can_handle(risk):
            result = await iv.execute(risk)
            iv_types.append(result.intervention_type.value)
            iv_msgs.append(result.message)
            if result.scheduled_at and not scheduled_at:
                scheduled_at = result.scheduled_at.strftime("%d %b %Y, %I:%M %p IST")
            if result.action_url and not action_url:
                action_url = result.action_url

    success, status, amount_rec = evaluate_recovery_outcome(iv_types, risk.amount)

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
        scenario_name=cfg["name"],
    )

    await store.add_event(ev)
    await process_and_log_event(ev, cfg_copy)
    return ev


async def run_custom_webhook(payload: dict) -> RecoveryEvent | None:
    """
    Run a raw custom webhook payload through the agent pipeline,
    log to Recovery Ledger, and publish to the event store.
    """
    from src.integrations.razorpay_upi import parse_upi_webhook
    upi_event = parse_upi_webhook(payload)
    if not upi_event:
        return None

    detector = UPIAutopayDetector()
    risk     = await detector.detect_from_upi_event(upi_event)
    if not risk:
        return None

    iv_types, iv_msgs, scheduled_at, action_url = [], [], None, None
    for iv in INTERVENTIONS:
        if iv.can_handle(risk):
            result = await iv.execute(risk)
            iv_types.append(result.intervention_type.value)
            iv_msgs.append(result.message)
            if result.scheduled_at and not scheduled_at:
                scheduled_at = result.scheduled_at.strftime("%d %b %Y, %I:%M %p IST")
            if result.action_url and not action_url:
                action_url = result.action_url

    success, status, amount_rec = evaluate_recovery_outcome(iv_types, risk.amount)

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
        scenario_name="Custom Webhook",
    )
    await store.add_event(ev)
    await process_and_log_event(ev, {"mandate_state": "active", "retry_attempt": 0})
    return ev


async def run_custom_scenario(form: dict) -> RecoveryEvent | None:
    """
    Build a UPIAutopayEvent from a user-supplied form dict, run the
    full agent pipeline, log to Recovery Ledger, and publish to the event store.
    """
    # Resolve failure code — default to UNKNOWN if invalid
    try:
        fc = UPIFailureCode(form["failure_code"].upper())
    except (ValueError, KeyError):
        fc = UPIFailureCode.UNKNOWN

    # Resolve mandate state — default ACTIVE
    state_map = {s.value: s for s in MandateState}
    mandate_state = state_map.get(form.get("mandate_state", "active").lower(), MandateState.ACTIVE)

    # Map failure code to a sensible event_type
    if fc in (UPIFailureCode.BT01,):
        event_type = "mandate.revoked"
    elif fc in (UPIFailureCode.BT02,):
        event_type = "mandate.expired"
    elif fc in (UPIFailureCode.U13,):
        event_type = "mandate.paused"
    else:
        event_type = "mandate.execution.failed"

    cfg = {
        "failure_code":  fc,
        "event_type":    event_type,
        "vpa":           form.get("vpa", "user@oksbi"),
        "bank":          form.get("bank", "Unknown Bank"),
        "amount":        float(form.get("amount", 100)),
        "mandate_state": mandate_state,
        "retry_attempt": int(form.get("retry_attempt", 0)),
        "customer_id":   f"CUST-CUSTOM-{uuid.uuid4().hex[:6].upper()}",
        "name":          form.get("scenario_name", "Custom Scenario"),
    }

    upi_event = _make_upi_event(cfg)
    detector  = UPIAutopayDetector()
    risk      = await detector.detect_from_upi_event(upi_event)
    if not risk:
        return None

    iv_types, iv_msgs, scheduled_at, action_url = [], [], None, None
    for iv in INTERVENTIONS:
        if iv.can_handle(risk):
            result = await iv.execute(risk)
            iv_types.append(result.intervention_type.value)
            iv_msgs.append(result.message)
            if result.scheduled_at and not scheduled_at:
                scheduled_at = result.scheduled_at.strftime("%d %b %Y, %I:%M %p IST")
            if result.action_url and not action_url:
                action_url = result.action_url

    success, status, amount_rec = evaluate_recovery_outcome(iv_types, risk.amount)

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
        scenario_name=cfg["name"],
    )
    await store.add_event(ev)
    await process_and_log_event(ev, cfg)
    return ev

