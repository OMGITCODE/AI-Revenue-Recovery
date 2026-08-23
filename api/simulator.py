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

import uuid
from datetime import datetime, timedelta, timezone

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
from api.store import RecoveryEvent, store

IST = timezone(timedelta(hours=5, minutes=30))

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
    return UPIAutopayEvent(
        event_id=uuid.uuid4().hex[:12].upper(),
        event_type=cfg["event_type"],
        payment_id=f"pay_{uuid.uuid4().hex[:10]}",
        mandate=mandate,
        failure_code=cfg["failure_code"],
        failure_message=cfg["failure_code"].human_reason,
        debit_amount=cfg["amount"],
        occurred_at=now,
        retry_attempt=cfg["retry_attempt"],
    )


async def run_scenario(scenario_key: str) -> RecoveryEvent | None:
    """
    Run a named scenario through the full agent pipeline
    and publish the result to the event store.
    """
    cfg = SCENARIOS.get(scenario_key)
    if not cfg:
        return None

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
        success=bool(iv_types),
        scenario_name=cfg["name"],
    )

    await store.add_event(ev)
    return ev


async def run_custom_form(payload: dict) -> RecoveryEvent | None:
    """
    Run a custom event from the dashboard form.
    """
    code_str = payload.get("failure_code", "U30")
    try:
        failure_code = UPIFailureCode(code_str)
    except ValueError:
        failure_code = UPIFailureCode.TM

    # Determine event type based on failure code
    event_type = "payment.failed"
    if failure_code in [UPIFailureCode.BT01, UPIFailureCode.BT02, UPIFailureCode.RB]:
        event_type = "mandate.revoked" if failure_code in [UPIFailureCode.BT01, UPIFailureCode.RB] else "mandate.expired"
    elif failure_code == UPIFailureCode.U13:
        event_type = "mandate.paused"
        
    cfg = {
        "name":          "Custom Event",
        "failure_code":  failure_code,
        "event_type":    event_type,
        "vpa":           payload.get("vpa", "unknown@upi"),
        "bank":          payload.get("bank", "Unknown Bank"),
        "amount":        float(payload.get("amount", 0.0)),
        "mandate_state": MandateState.ACTIVE if event_type == "payment.failed" else (
                         MandateState.REVOKED if event_type == "mandate.revoked" else (
                         MandateState.EXPIRED if event_type == "mandate.expired" else MandateState.PAUSED)),
        "retry_attempt": payload.get("retry_attempt", 0),
        "customer_id":   f"CUST-CUST-{uuid.uuid4().hex[:4].upper()}",
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
        success=bool(iv_types),
        scenario_name="Custom Form Event",
    )

    await store.add_event(ev)
    return ev


async def run_custom_webhook(payload: dict) -> RecoveryEvent | None:
    """
    Run a raw custom webhook payload through the agent pipeline.
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
        success=bool(iv_types),
        scenario_name="Custom Webhook",
    )
    await store.add_event(ev)
    return ev
