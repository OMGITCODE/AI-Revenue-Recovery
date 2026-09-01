"""
UPI Autopay Failure Recovery — Demo
====================================
Simulates 4 real-world UPI Autopay failure scenarios end-to-end:

  Scenario 1 — U30 (Insufficient Funds) at month-end → salary-window retry
  Scenario 2 — BT01 (Mandate Revoked)                → mandate renewal link
  Scenario 3 — TM (Technical Error), 3 attempts       → exponential backoff → escalation
  Scenario 4 — U69 (Daily Limit Exceeded)             → next-morning retry + WhatsApp nudge

No API keys required — runs entirely on mock data.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── IST ───────────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# ── Minimal sys.path setup (run from project root) ───────────────────────────
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.models.upi_models import (
    MandateFrequency,
    MandateState,
    UPIAutopayEvent,
    UPIFailureCode,
    UPIMandate,
)
from src.agent.upi_detector import UPIAutopayDetector
from src.agent.upi_interventions import (
    SmartRetryIntervention,
    UPICollectIntervention,
    MandateRenewalIntervention,
    WhatsAppNudgeIntervention,
    EscalationIntervention,
    UPIInterventionResult,
)

# ── Formatting helpers ────────────────────────────────────────────────────────
SEP  = "─" * 68
DSEP = "═" * 68

SEVERITY_BADGE = {
    "critical": "🔴 CRITICAL",
    "high":     "🟠 HIGH",
    "medium":   "🟡 MEDIUM",
    "low":      "🟢 LOW",
}

ITYPE_ICON = {
    "smart_retry":     "🔁",
    "upi_collect":     "📲",
    "mandate_renewal": "🔗",
    "whatsapp_nudge":  "💬",
    "escalation":      "🚨",
}


def _print_header():
    print(f"\n{DSEP}")
    print("   🇮🇳  UPI Autopay Failure Recovery — Demo")
    print(f"   Powered by AI Revenue Recovery Agent")
    print(f"{DSEP}\n")


def _print_scenario(n: int, title: str, desc: str):
    print(f"\n{SEP}")
    print(f"  📌  Scenario {n}: {title}")
    print(f"      {desc}")
    print(SEP)


def _print_event(event: UPIAutopayEvent):
    print(f"  📥  Event          : {event.event_type}")
    print(f"      Customer VPA   : {event.customer_vpa}")
    print(f"      Bank           : {event.bank_name}")
    print(f"      Amount         : ₹{event.debit_amount:,.2f}")
    print(f"      Failure Code   : {event.failure_code.value} — {event.failure_code.human_reason}")
    print(f"      Retry Attempt  : #{event.retry_attempt}")
    print(f"      Recoverable?   : {'✅ Yes' if event.failure_code.is_recoverable else '❌ No'}")
    print(f"      Needs Renewal? : {'⚠️  Yes' if event.failure_code.requires_mandate_renewal else 'No'}")


def _print_risk(risk):
    badge = SEVERITY_BADGE.get(risk.severity.value, risk.severity.value.upper())
    print(f"\n  ⚠️   Risk Detected  : {risk.id}")
    print(f"      Severity       : {badge}")
    print(f"      Risk Type      : {risk.risk_type.value}")


def _print_result(result: UPIInterventionResult):
    icon = ITYPE_ICON.get(result.intervention_type.value, "🔹")
    status = "✅ Success" if result.success else "❌ Failed"
    print(f"\n  {icon}  Intervention   : {result.intervention_type.value}")
    print(f"      Status         : {status}")
    print(f"      Message        : {result.message}")
    if result.scheduled_at:
        print(f"      Scheduled At   : {result.scheduled_at.strftime('%d %b %Y, %I:%M %p IST')}")
    if result.action_url:
        print(f"      Action URL     : {result.action_url}")
    print(f"      Likelihood     : {result.recovery_likelihood}")
    print(f"      Amount at Stake: ₹{result.amount_at_stake:,.2f}")


def _make_mandate(
    mandate_id: str,
    customer_id: str,
    vpa: str,
    bank: str,
    amount: float,
    state: MandateState = MandateState.ACTIVE,
) -> UPIMandate:
    now = datetime.now(IST)
    return UPIMandate(
        mandate_id=mandate_id,
        customer_id=customer_id,
        customer_vpa=vpa,
        amount=amount,
        frequency=MandateFrequency.MONTHLY,
        state=state,
        bank_name=bank,
        bank_ifsc="SBIN0000001",
        created_at=now - timedelta(days=90),
        expiry_date=now + timedelta(days=275),
        failure_count=0,
    )


def _make_event(
    event_id: str,
    event_type: str,
    mandate: UPIMandate,
    failure_code: UPIFailureCode,
    amount: float,
    retry_attempt: int = 0,
) -> UPIAutopayEvent:
    return UPIAutopayEvent(
        event_id=event_id,
        event_type=event_type,
        payment_id=f"pay_{event_id}",
        mandate=mandate,
        failure_code=failure_code,
        failure_message=failure_code.human_reason,
        debit_amount=amount,
        occurred_at=datetime.now(IST),
        retry_attempt=retry_attempt,
    )


# ── Intervention pipeline ─────────────────────────────────────────────────────
INTERVENTIONS = [
    SmartRetryIntervention(),
    UPICollectIntervention(),
    MandateRenewalIntervention(),
    WhatsAppNudgeIntervention(),
    EscalationIntervention(),
]


async def run_pipeline(event: UPIAutopayEvent) -> list[UPIInterventionResult]:
    detector = UPIAutopayDetector()
    risk = await detector.detect_from_upi_event(event)
    if risk is None:
        print("  ⚪  No risk detected.")
        return []

    _print_risk(risk)

    results = []
    print()
    for iv in INTERVENTIONS:
        if iv.can_handle(risk):
            result = await iv.execute(risk)
            _print_result(result)
            results.append(result)

    return results


# ── Scenario Definitions ──────────────────────────────────────────────────────

async def scenario_1_insufficient_funds():
    """U30 — Month-end salary crunch. Smart retry on salary window."""
    _print_scenario(
        1, "Insufficient Funds (U30)",
        "SBI customer's monthly ₹999 subscription fails at month-end — classic salary crunch."
    )
    mandate = _make_mandate("MND-SBI-001", "CUST-A1", "rahul@oksbi", "SBI", 999.0)
    event   = _make_event("EVT-001", "mandate.execution.failed", mandate, UPIFailureCode.U30, 999.0)
    _print_event(event)
    await run_pipeline(event)


async def scenario_2_mandate_revoked():
    """BT01 — Customer revoked mandate via UPI app. Needs re-registration."""
    _print_scenario(
        2, "Mandate Revoked (BT01)",
        "HDFC customer revoked their UPI Autopay mandate via PhonePe. Renewal flow triggered."
    )
    mandate = _make_mandate(
        "MND-HDFC-002", "CUST-B2", "priya@okhdfcbank", "HDFC", 499.0,
        state=MandateState.REVOKED,
    )
    event = _make_event("EVT-002", "mandate.revoked", mandate, UPIFailureCode.BT01, 499.0)
    _print_event(event)
    await run_pipeline(event)


async def scenario_3_technical_error_then_escalation():
    """TM — Bank timeout. 3 retries with backoff, then escalation."""
    _print_scenario(
        3, "Technical Error → Escalation (TM, attempt #3)",
        "ICICI gateway timeout on 3rd attempt. Max retries exhausted → escalate to support."
    )
    mandate = _make_mandate("MND-ICICI-003", "CUST-C3", "arjun@okicici", "ICICI", 1499.0)
    event   = _make_event("EVT-003", "mandate.execution.failed", mandate, UPIFailureCode.TM, 1499.0, retry_attempt=3)
    _print_event(event)
    await run_pipeline(event)


from src.agent.mandate_expiry import mandate_expiry_scanner

async def scenario_4_daily_limit_exceeded():
    """U69 — Customer hit daily UPI limit. Retry next morning + WhatsApp nudge."""
    _print_scenario(
        4, "Daily Limit Exceeded (U69) + WhatsApp Nudge",
        "Axis customer hit their ₹1L daily UPI limit. Auto-retry at 6 AM + WhatsApp reminder."
    )
    mandate = _make_mandate("MND-AXIS-004", "CUST-D4", "meera@okaxis", "Axis", 2999.0)
    event   = _make_event("EVT-004", "mandate.execution.failed", mandate, UPIFailureCode.U69, 2999.0)
    _print_event(event)
    await run_pipeline(event)


async def scenario_5_proactive_mandate_expiry():
    """T-72h Proactive Expiry — Intercepts mandate expiry before BT02 debit failure occurs."""
    _print_scenario(
        5, "Proactive Mandate Expiry Interceptor (T-72h Prevention)",
        "Scans active UPI Autopay mandates nearing validity lapse. Dispatches 1-click renewal link to prevent BT02 failure."
    )
    expiring = mandate_expiry_scanner.find_expiring_mandates(within_hours=72)
    print(f"  🔍  Scanner Found  : {len(expiring)} active recurring mandates expiring within 72h window")
    for m in expiring[:3]:
        print(f"      • {m.mandate_id} | {m.customer_name} ({m.customer_vpa}) | ₹{m.amount:,.2f} | {m.bank_name} | {m.hours_remaining():.1f}h remaining")

    target = expiring[0]
    print(f"\n  ⚡  Proactive Action: Dispatching 1-Click WhatsApp Renewal Magic Link for {target.customer_vpa}...")
    nudged = await mandate_expiry_scanner.dispatch_proactive_nudge(target.mandate_id)
    print(f"      Status         : ✅ Nudge Dispatched via WhatsApp")
    print(f"      Renewal Link   : {nudged.renewal_link}")
    print(f"      Audit Trail    : Logged to RecoveryLedger as BT02_PREVENTED")

    print(f"\n  🎉  Simulating Customer 1-Click Renewal Self-Cure...")
    renewed = await mandate_expiry_scanner.simulate_proactive_renewal(target.mandate_id)
    print(f"      Outcome        : ✅ Mandate Renewed Successfully")
    print(f"      Protected ₹    : ₹{renewed.amount:,.2f} recurring revenue protected from BT02 churn")


# ── Summary ───────────────────────────────────────────────────────────────────

async def main():
    _print_header()

    await scenario_1_insufficient_funds()
    await scenario_2_mandate_revoked()
    await scenario_3_technical_error_then_escalation()
    await scenario_4_daily_limit_exceeded()
    await scenario_5_proactive_mandate_expiry()

    print(f"\n{DSEP}")
    print("   📊  Demo Complete — All 5 UPI Autopay scenarios processed.")
    print()
    print("   Recovery strategies applied:")
    print("   • Scenario 1 → Smart Retry (salary window) + UPI Collect")
    print("   • Scenario 2 → Mandate Renewal Link + WhatsApp Nudge")
    print("   • Scenario 3 → Escalation to Support (max retries hit)")
    print("   • Scenario 4 → Next-day Retry + WhatsApp Nudge")
    print("   • Scenario 5 → Proactive Mandate Renewal (T-72h pre-failure prevention)")
    print()
    print("   Plug in your RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET in .env")
    print("   to switch from demo mode to live API calls.")
    print(f"{DSEP}\n")


if __name__ == "__main__":
    asyncio.run(main())
