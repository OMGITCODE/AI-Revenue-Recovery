"""
AI Revenue Recovery Agent - Local Demo
Simulates the full detect → diagnose → intervene pipeline
with mock data so it runs without any API keys.
"""

import asyncio
import sys
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ── Models ────────────────────────────────────────────────────────────────────

class RiskType(Enum):
    PAYMENT_FAILURE        = "payment_failure"
    CHECKOUT_ABANDONMENT   = "checkout_abandonment"
    SUBSCRIPTION_FAILURE   = "subscription_failure"
    INVOICE_OVERDUE        = "invoice_overdue"
    MANDATE_FAILURE        = "mandate_failure"

class RiskSeverity(Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

class RootCause(Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED       = "card_expired"
    CARD_DECLINED      = "card_declined"
    NETWORK_ERROR      = "network_error"
    USER_ABANDONED     = "user_abandoned"
    MANDATE_EXPIRED    = "mandate_expired"
    UNKNOWN            = "unknown"

class InterventionType(Enum):
    PAYMENT_RETRY     = "payment_retry"
    CHECKOUT_REMINDER = "checkout_reminder"
    DUNNING_EMAIL     = "dunning_email"
    SMS_NUDGE         = "sms_nudge"
    VOICE_CALL        = "voice_call"
    ESCALATION        = "escalation"

@dataclass
class RevenueRisk:
    id:          str
    risk_type:   RiskType
    severity:    RiskSeverity
    amount:      float
    currency:    str
    customer_id: str
    detected_at: datetime

@dataclass
class Diagnosis:
    root_cause:          RootCause
    confidence:          float
    recommended_actions: list[str]

@dataclass
class InterventionResult:
    intervention_type: InterventionType
    success:           bool
    amount_recovered:  float
    message:           str


# ── Mock Detector ─────────────────────────────────────────────────────────────

class MockDetector:
    """Simulates detection from incoming payment events."""

    EVENT_MAP = {
        "payment.failed":         (RiskType.PAYMENT_FAILURE,      RiskSeverity.HIGH),
        "checkout.abandoned":     (RiskType.CHECKOUT_ABANDONMENT,  RiskSeverity.MEDIUM),
        "subscription.failed":    (RiskType.SUBSCRIPTION_FAILURE,  RiskSeverity.CRITICAL),
        "invoice.overdue":        (RiskType.INVOICE_OVERDUE,       RiskSeverity.HIGH),
        "mandate.failed":         (RiskType.MANDATE_FAILURE,       RiskSeverity.MEDIUM),
    }

    async def detect(self, event: dict) -> RevenueRisk | None:
        event_type = event.get("type")
        if event_type not in self.EVENT_MAP:
            return None
        risk_type, severity = self.EVENT_MAP[event_type]
        return RevenueRisk(
            id=f"RISK-{event.get('id', '001')}",
            risk_type=risk_type,
            severity=severity,
            amount=event.get("amount", 0.0),
            currency=event.get("currency", "INR"),
            customer_id=event.get("customer_id", "CUST-???"),
            detected_at=datetime.now(),
        )


# ── Mock Diagnoser ────────────────────────────────────────────────────────────

class MockDiagnoser:
    """Simulates AI root-cause analysis."""

    DIAGNOSIS_MAP = {
        RiskType.PAYMENT_FAILURE:      (RootCause.INSUFFICIENT_FUNDS, 0.87, ["Retry in 24h", "Send payment link"]),
        RiskType.CHECKOUT_ABANDONMENT: (RootCause.USER_ABANDONED,     0.92, ["Send reminder email", "Offer discount"]),
        RiskType.SUBSCRIPTION_FAILURE: (RootCause.CARD_EXPIRED,       0.81, ["Request card update", "Pause subscription"]),
        RiskType.INVOICE_OVERDUE:      (RootCause.UNKNOWN,            0.70, ["Send dunning email", "Escalate to sales"]),
        RiskType.MANDATE_FAILURE:      (RootCause.MANDATE_EXPIRED,    0.95, ["Re-initiate mandate", "Send SMS"]),
    }

    async def diagnose(self, risk: RevenueRisk) -> Diagnosis:
        cause, confidence, actions = self.DIAGNOSIS_MAP.get(
            risk.risk_type,
            (RootCause.UNKNOWN, 0.5, ["Manual review required"])
        )
        return Diagnosis(root_cause=cause, confidence=confidence, recommended_actions=actions)


# ── Mock Interventions ────────────────────────────────────────────────────────

class MockIntervention:
    """Simulates executing a recovery action."""

    ACTION_MAP = {
        RootCause.INSUFFICIENT_FUNDS: (InterventionType.PAYMENT_RETRY,     True,  "Payment retried successfully"),
        RootCause.USER_ABANDONED:     (InterventionType.CHECKOUT_REMINDER,  True,  "Reminder email sent"),
        RootCause.CARD_EXPIRED:       (InterventionType.SMS_NUDGE,          True,  "Card update SMS sent"),
        RootCause.UNKNOWN:            (InterventionType.DUNNING_EMAIL,      True,  "Dunning email dispatched"),
        RootCause.MANDATE_EXPIRED:    (InterventionType.PAYMENT_RETRY,      True,  "Mandate re-initiated"),
    }

    def can_handle(self, diagnosis: Diagnosis) -> bool:
        return diagnosis.root_cause in self.ACTION_MAP

    async def execute(self, risk: RevenueRisk, diagnosis: Diagnosis) -> InterventionResult:
        itype, success, msg = self.ACTION_MAP[diagnosis.root_cause]
        recovered = risk.amount if success else 0.0
        return InterventionResult(
            intervention_type=itype,
            success=success,
            amount_recovered=recovered,
            message=msg,
        )


# ── Orchestrator ──────────────────────────────────────────────────────────────

class RecoveryOrchestrator:
    def __init__(self, detector, diagnoser, interventions):
        self.detector      = detector
        self.diagnoser     = diagnoser
        self.interventions = interventions
        self._results: list[InterventionResult] = []

    async def process_event(self, event: dict):
        risk = await self.detector.detect(event)
        if not risk:
            return None
        diagnosis = await self.diagnoser.diagnose(risk)
        for iv in self.interventions:
            if iv.can_handle(diagnosis):
                result = await iv.execute(risk, diagnosis)
                self._results.append(result)
                return risk, diagnosis, result
        return risk, diagnosis, None

    @property
    def total_recovered(self) -> float:
        return sum(r.amount_recovered for r in self._results if r.success)


# ── Demo Runner ───────────────────────────────────────────────────────────────

SAMPLE_EVENTS = [
    {"type": "payment.failed",      "id": "101", "amount": 4999.00, "currency": "INR", "customer_id": "CUST-A1"},
    {"type": "checkout.abandoned",  "id": "102", "amount": 1299.00, "currency": "INR", "customer_id": "CUST-B2"},
    {"type": "subscription.failed", "id": "103", "amount": 799.00,  "currency": "INR", "customer_id": "CUST-C3"},
    {"type": "invoice.overdue",     "id": "104", "amount": 25000.0, "currency": "INR", "customer_id": "CUST-D4"},
    {"type": "mandate.failed",      "id": "105", "amount": 599.00,  "currency": "INR", "customer_id": "CUST-E5"},
    {"type": "unknown.event",       "id": "106", "amount": 100.00,  "currency": "INR", "customer_id": "CUST-F6"},
]

SEP = "─" * 60

async def main():
    print(f"\n{'═'*60}")
    print("   🔄  AI Revenue Recovery Agent  —  Local Demo")
    print(f"{'═'*60}\n")

    orchestrator = RecoveryOrchestrator(
        detector=MockDetector(),
        diagnoser=MockDiagnoser(),
        interventions=[MockIntervention()],
    )

    for event in SAMPLE_EVENTS:
        print(f"{SEP}")
        print(f"📥  Event       : {event['type']}  (ID: {event['id']})")
        print(f"    Customer    : {event['customer_id']}")
        print(f"    Amount      : ₹{event['amount']:,.2f}")

        result = await orchestrator.process_event(event)
        if result is None:
            print(f"    ⚪  No risk detected — skipping.")
            continue

        risk, diagnosis, intervention = result
        print(f"    ⚠️  Risk        : {risk.risk_type.value}  [{risk.severity.value.upper()}]")
        print(f"    🔍  Root Cause  : {diagnosis.root_cause.value}  (confidence: {diagnosis.confidence:.0%})")
        print(f"    💡  Actions     : {', '.join(diagnosis.recommended_actions)}")

        if intervention:
            status = "✅" if intervention.success else "❌"
            print(f"    {status}  Intervention: {intervention.intervention_type.value}")
            print(f"    💰  Recovered   : ₹{intervention.amount_recovered:,.2f}  — {intervention.message}")
        else:
            print(f"    ⚠️  No intervention matched.")

    print(f"\n{'═'*60}")
    print(f"   💰  TOTAL REVENUE RECOVERED : ₹{orchestrator.total_recovered:,.2f}")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
