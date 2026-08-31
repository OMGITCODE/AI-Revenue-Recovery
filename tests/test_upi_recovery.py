"""
Tests for UPI Autopay Failure Recovery.

Covers:
  - UPIFailureCode properties
  - UPIRetryScheduler — all 4 strategies
  - UPIAutopayDetector — risk detection + severity overrides
  - Intervention can_handle routing
  - End-to-end pipeline per scenario
"""

import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.upi_models import (
    MandateFrequency,
    MandateState,
    UPIAutopayEvent,
    UPIFailureCode,
    UPIMandate,
)
from src.agent.upi_detector import UPIAutopayDetector
from src.agent.retry_scheduler import UPIRetryScheduler, IST, MAX_AUTO_RETRIES
from src.agent.upi_interventions import (
    SmartRetryIntervention,
    UPICollectIntervention,
    MandateRenewalIntervention,
    WhatsAppNudgeIntervention,
    EscalationIntervention,
    UPIInterventionType,
)
from src.models.risk_models import RiskSeverity


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_mandate(
    state: MandateState = MandateState.ACTIVE,
    bank: str = "SBI",
    vpa: str = "test@oksbi",
) -> UPIMandate:
    now = datetime.now(IST)
    return UPIMandate(
        mandate_id="MND-TEST-001",
        customer_id="CUST-TEST",
        customer_vpa=vpa,
        amount=999.0,
        frequency=MandateFrequency.MONTHLY,
        state=state,
        bank_name=bank,
        bank_ifsc="SBIN0000001",
        created_at=now - timedelta(days=60),
        expiry_date=now + timedelta(days=305),
    )


def make_event(
    failure_code: UPIFailureCode,
    event_type: str = "mandate.execution.failed",
    amount: float = 999.0,
    retry_attempt: int = 0,
    bank: str = "SBI",
    vpa: str = "test@oksbi",
    mandate_state: MandateState = MandateState.ACTIVE,
) -> UPIAutopayEvent:
    mandate = make_mandate(state=mandate_state, bank=bank, vpa=vpa)
    return UPIAutopayEvent(
        event_id="EVT-TEST-001",
        event_type=event_type,
        payment_id="pay_test_001",
        mandate=mandate,
        failure_code=failure_code,
        failure_message=failure_code.human_reason,
        debit_amount=amount,
        occurred_at=datetime.now(IST),
        retry_attempt=retry_attempt,
    )


# ── UPIFailureCode Tests ───────────────────────────────────────────────────────

class TestUPIFailureCode:
    def test_u30_is_recoverable(self):
        assert UPIFailureCode.U30.is_recoverable is True

    def test_bt01_requires_renewal(self):
        assert UPIFailureCode.BT01.requires_mandate_renewal is True
        assert UPIFailureCode.BT01.is_recoverable is False

    def test_bt02_requires_renewal(self):
        assert UPIFailureCode.BT02.requires_mandate_renewal is True

    def test_tm_is_recoverable(self):
        assert UPIFailureCode.TM.is_recoverable is True
        assert UPIFailureCode.TM.requires_mandate_renewal is False

    def test_u69_is_recoverable(self):
        assert UPIFailureCode.U69.is_recoverable is True

    def test_ba_requires_renewal(self):
        assert UPIFailureCode.BA.requires_mandate_renewal is True

    def test_human_reason_not_empty(self):
        for code in UPIFailureCode:
            assert len(code.human_reason) > 0


# ── RetryScheduler Tests ───────────────────────────────────────────────────────

class TestUPIRetryScheduler:
    def setup_method(self):
        self.scheduler = UPIRetryScheduler()
        self.now = datetime.now(IST)

    def test_u30_schedules_salary_window(self):
        decision = self.scheduler.schedule(UPIFailureCode.U30, "SBI", 0, self.now)
        assert decision.should_retry is True
        assert decision.scheduled_at is not None
        # Must be in future
        assert decision.scheduled_at > self.now
        # Must be at 10am
        assert decision.scheduled_at.hour == 10
        assert decision.scheduled_at.minute == 0
        assert decision.requires_renewal is False

    def test_bt01_no_retry_requires_renewal(self):
        decision = self.scheduler.schedule(UPIFailureCode.BT01, "SBI", 0, self.now)
        assert decision.should_retry is False
        assert decision.requires_renewal is True
        assert decision.scheduled_at is None

    def test_bt02_no_retry_requires_renewal(self):
        decision = self.scheduler.schedule(UPIFailureCode.BT02, "HDFC", 0, self.now)
        assert decision.should_retry is False
        assert decision.requires_renewal is True

    def test_tm_exponential_backoff_attempt_0(self):
        decision = self.scheduler.schedule(UPIFailureCode.TM, "ICICI", 0, self.now)
        assert decision.should_retry is True
        diff_hours = (decision.scheduled_at - self.now).total_seconds() / 3600
        # ICICI cooling = 12h, backoff for attempt 0 = max(2, 12) = 12h
        assert diff_hours >= 12

    def test_tm_exponential_backoff_attempt_1(self):
        decision = self.scheduler.schedule(UPIFailureCode.TM, "SBI", 1, self.now)
        assert decision.should_retry is True
        diff_hours = (decision.scheduled_at - self.now).total_seconds() / 3600
        # SBI cooling = 24h, backoff attempt 1 = max(6, 24) = 24h
        assert diff_hours >= 24

    def test_u69_retries_next_day_at_6am(self):
        decision = self.scheduler.schedule(UPIFailureCode.U69, "Axis", 0, self.now)
        assert decision.should_retry is True
        assert decision.scheduled_at.hour == 6
        assert decision.scheduled_at.minute == 0
        assert decision.scheduled_at.date() > self.now.date()

    def test_max_retries_stops_retry(self):
        decision = self.scheduler.schedule(
            UPIFailureCode.U30, "SBI", MAX_AUTO_RETRIES, self.now
        )
        assert decision.should_retry is False
        assert decision.max_retries_hit is True

    def test_u13_retries_after_48h(self):
        decision = self.scheduler.schedule(UPIFailureCode.U13, "SBI", 0, self.now)
        assert decision.should_retry is True
        diff_hours = (decision.scheduled_at - self.now).total_seconds() / 3600
        assert diff_hours >= 48


# ── UPIAutopayDetector Tests ───────────────────────────────────────────────────

class TestUPIAutopayDetector:
    def setup_method(self):
        self.detector = UPIAutopayDetector()

    @pytest.mark.asyncio
    async def test_u30_produces_proportional_severity_risk(self):
        event = make_event(UPIFailureCode.U30, amount=999.0)
        risk = await self.detector.detect_from_upi_event(event)
        assert risk is not None
        assert risk.severity == RiskSeverity.LOW
        assert risk.amount == 999.0
        assert risk.customer_id == "CUST-TEST"

    @pytest.mark.asyncio
    async def test_bt01_produces_critical_severity(self):
        event = make_event(
            UPIFailureCode.BT01,
            event_type="mandate.revoked",
            mandate_state=MandateState.REVOKED,
        )
        risk = await self.detector.detect_from_upi_event(event)
        assert risk is not None
        assert risk.severity == RiskSeverity.CRITICAL

    @pytest.mark.asyncio
    async def test_third_retry_escalates_to_critical(self):
        event = make_event(UPIFailureCode.TM, retry_attempt=2)
        risk = await self.detector.detect_from_upi_event(event)
        assert risk is not None
        assert risk.severity == RiskSeverity.CRITICAL

    @pytest.mark.asyncio
    async def test_metadata_populated(self):
        event = make_event(UPIFailureCode.U30)
        risk = await self.detector.detect_from_upi_event(event)
        meta = risk.metadata
        assert meta["failure_code"] == UPIFailureCode.U30
        assert meta["customer_vpa"] == "test@oksbi"
        assert meta["bank_name"] == "SBI"
        assert meta["is_recoverable"] is True
        assert meta["requires_renewal"] is False

    @pytest.mark.asyncio
    async def test_unknown_event_type_returns_none(self):
        event = make_event(UPIFailureCode.U30, event_type="some.unknown.event")
        risk = await self.detector.detect_from_upi_event(event)
        assert risk is None


# ── Intervention Routing Tests ────────────────────────────────────────────────

class TestInterventionRouting:
    """Verify can_handle() routes correctly for each intervention."""

    def setup_method(self):
        self.detector = UPIAutopayDetector()

    def _make_risk(self, failure_code: UPIFailureCode, retry_attempt: int = 0):
        """Helper: build a risk object with correct metadata."""
        event = make_event(failure_code, retry_attempt=retry_attempt)
        return type("Risk", (), {
            "metadata": {
                "failure_code": failure_code,
                "customer_vpa": event.customer_vpa,
                "bank_name": event.bank_name,
                "retry_attempt": retry_attempt,
                "is_recoverable": failure_code.is_recoverable,
                "requires_renewal": failure_code.requires_mandate_renewal,
            },
            "amount": 999.0,
            "customer_id": "CUST-TEST",
            "detected_at": datetime.now(IST),
        })()

    def test_smart_retry_handles_u30(self):
        risk = self._make_risk(UPIFailureCode.U30)
        assert SmartRetryIntervention().can_handle(risk) is True

    def test_smart_retry_rejects_bt01(self):
        risk = self._make_risk(UPIFailureCode.BT01)
        assert SmartRetryIntervention().can_handle(risk) is False

    def test_smart_retry_rejects_at_max_retries(self):
        risk = self._make_risk(UPIFailureCode.U30, retry_attempt=3)
        assert SmartRetryIntervention().can_handle(risk) is False

    def test_mandate_renewal_handles_bt01(self):
        risk = self._make_risk(UPIFailureCode.BT01)
        assert MandateRenewalIntervention().can_handle(risk) is True

    def test_mandate_renewal_rejects_u30(self):
        risk = self._make_risk(UPIFailureCode.U30)
        assert MandateRenewalIntervention().can_handle(risk) is False

    def test_escalation_handles_at_3_attempts(self):
        risk = self._make_risk(UPIFailureCode.TM, retry_attempt=3)
        assert EscalationIntervention().can_handle(risk) is True

    def test_escalation_rejects_first_attempt(self):
        risk = self._make_risk(UPIFailureCode.TM, retry_attempt=0)
        assert EscalationIntervention().can_handle(risk) is False

    def test_whatsapp_nudge_rejects_pure_technical(self):
        risk = self._make_risk(UPIFailureCode.TM)
        assert WhatsAppNudgeIntervention().can_handle(risk) is False

    def test_whatsapp_nudge_accepts_u30(self):
        risk = self._make_risk(UPIFailureCode.U30)
        assert WhatsAppNudgeIntervention().can_handle(risk) is True


# ── End-to-End Pipeline Tests ─────────────────────────────────────────────────

class TestEndToEndPipeline:
    """Full detect + all-interventions pipeline per scenario."""

    INTERVENTIONS = [
        SmartRetryIntervention(),
        UPICollectIntervention(),
        MandateRenewalIntervention(),
        WhatsAppNudgeIntervention(),
        EscalationIntervention(),
    ]

    async def _run(self, event: UPIAutopayEvent):
        detector = UPIAutopayDetector()
        risk = await detector.detect_from_upi_event(event)
        results = []
        for iv in self.INTERVENTIONS:
            if iv.can_handle(risk):
                results.append(await iv.execute(risk))
        return risk, results

    @pytest.mark.asyncio
    async def test_scenario_u30_gets_smart_retry_and_collect(self):
        event = make_event(UPIFailureCode.U30)
        risk, results = await self._run(event)
        types = [r.intervention_type for r in results]
        assert UPIInterventionType.SMART_RETRY in types
        assert UPIInterventionType.UPI_COLLECT in types

    @pytest.mark.asyncio
    async def test_scenario_bt01_gets_renewal_and_whatsapp(self):
        event = make_event(
            UPIFailureCode.BT01,
            event_type="mandate.revoked",
            mandate_state=MandateState.REVOKED,
        )
        risk, results = await self._run(event)
        types = [r.intervention_type for r in results]
        assert UPIInterventionType.MANDATE_RENEWAL in types
        assert UPIInterventionType.WHATSAPP_NUDGE in types
        # Smart retry must NOT fire
        assert UPIInterventionType.SMART_RETRY not in types

    @pytest.mark.asyncio
    async def test_scenario_tm_max_retries_escalates(self):
        event = make_event(UPIFailureCode.TM, retry_attempt=3)
        risk, results = await self._run(event)
        types = [r.intervention_type for r in results]
        assert UPIInterventionType.ESCALATION in types
        assert UPIInterventionType.SMART_RETRY not in types

    @pytest.mark.asyncio
    async def test_all_results_have_amount_at_stake(self):
        event = make_event(UPIFailureCode.U30, amount=4999.0)
        _, results = await self._run(event)
        for r in results:
            assert r.amount_at_stake == 4999.0

    @pytest.mark.asyncio
    async def test_smart_retry_result_has_scheduled_at(self):
        event = make_event(UPIFailureCode.U30)
        _, results = await self._run(event)
        retry = next(r for r in results if r.intervention_type == UPIInterventionType.SMART_RETRY)
        assert retry.scheduled_at is not None
        assert retry.scheduled_at > datetime.now(IST)


# ── Simulator & Immutable Recovery Ledger Integration Tests ────────────────────

class TestSimulatorRecoveryLedger:
    """Verifies that simulator.py directly writes full audit trails to Recovery Ledger."""

    @pytest.fixture(autouse=True)
    def clean_state(self):
        from src.agent.recovery_ledger import ledger as recovery_ledger
        from src.agent.promise_tracker import promise_tracker
        from src.agent.checkout_recovery import checkout_agent
        from api.store import store

        recovery_ledger._entries.clear()
        promise_tracker._promises.clear()
        checkout_agent._sessions.clear()
        store.reset()
        yield
        recovery_ledger._entries.clear()
        promise_tracker._promises.clear()
        checkout_agent._sessions.clear()
        store.reset()

    @pytest.mark.asyncio
    async def test_run_custom_scenario_populates_ledger(self):
        from api.simulator import run_custom_scenario
        from src.agent.recovery_ledger import ledger as recovery_ledger

        form = {
            "scenario_name": "Test U30 Salary Crunch",
            "failure_code": "U30",
            "vpa": "test.user@oksbi",
            "bank": "SBI",
            "amount": 999.0,
            "mandate_state": "active",
            "retry_attempt": 0,
        }

        ev = await run_custom_scenario(form)
        assert ev is not None
        assert ev.customer_vpa == "test.user@oksbi"

        # Check ledger entries recorded
        entries = recovery_ledger.all_entries()
        assert len(entries) >= 3  # detect, aa_check (for U30), decide/guardrail, intervene, p2p

        event_types = [e.event_type for e in entries]
        assert "detect" in event_types
        assert any(t in ("decide", "guardrail") for t in event_types)
        assert "intervene" in event_types
        assert "p2p" in event_types  # U30 creates auto P2P

        # Ensure reasoning and confidence are populated
        for e in entries:
            assert len(e.reasoning) > 0
            assert 0.0 <= e.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_run_named_scenario_populates_ledger(self):
        from api.simulator import run_scenario
        from src.agent.recovery_ledger import ledger as recovery_ledger

        ev = await run_scenario("bt01")
        assert ev is not None

        entries = recovery_ledger.all_entries()
        assert len(entries) >= 2
        event_types = [e.event_type for e in entries]
        assert "detect" in event_types
        assert "checkout" in event_types  # BT01 auto-creates checkout drop-off record

    @pytest.mark.asyncio
    async def test_batch_run_populates_ledger_and_exports(self):
        """Simulate data/batch_run.py firing dataset scenarios and verifying ledger export."""
        import json
        from pathlib import Path
        from fastapi.testclient import TestClient
        from api.main import app
        from src.agent.recovery_ledger import ledger as recovery_ledger

        client = TestClient(app)
        client.post("/api/reset")

        dataset_path = Path(__file__).parent.parent / "data" / "upi_failures_dataset.json"
        with open(dataset_path, encoding="utf-8") as f:
            scenarios = json.load(f)

        # Run first 5 scenarios via /api/custom (same as batch_run.py)
        for sc in scenarios[:5]:
            resp = client.post("/api/custom", json=sc)
            assert resp.status_code == 200

        # Verify JSON audit export endpoint returns non-zero records
        export_resp = client.get("/api/ledger/export?format=json")
        assert export_resp.status_code == 200
        export_data = export_resp.json()
        assert export_data.get("total_records", 0) >= 10
        assert len(export_data.get("records", [])) >= 10

        # Verify CSV audit export endpoint
        csv_resp = client.get("/api/ledger/export?format=csv")
        assert csv_resp.status_code == 200
        assert "ledger_id,ts_full,event_type" in csv_resp.text

