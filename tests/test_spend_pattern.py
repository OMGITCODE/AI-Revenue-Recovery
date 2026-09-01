"""
Tests for Spend Pattern & Critical Spike Anomaly Detection.

Validates:
  1. Historical pattern retrieval and statistical profile calculation.
  2. Normal variation check: ₹10,000–₹50,000 baseline vs ₹60,000 transaction -> NOT critical.
  3. Sudden upward spike check: ~₹100 baseline vs ₹70,000 transaction -> CRITICAL (700x spike).
  4. Dynamic transaction recording and rolling history window.
  5. Integration with UPIAutopayDetector (escalates risk severity to CRITICAL).
  6. Integration with DecisionEngine (blocks silent retry and triggers GR10).
  7. REST API endpoints /api/pattern/history and /api/pattern/analyze.
"""

import sys
import os
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agent.spend_pattern import (
    SpendPatternTracker,
    spend_pattern_tracker,
    SpendProfile,
    PatternAnalysisResult,
)
from src.agent.customer_identity import customer_identity_registry
from src.models.risk_models import RiskSeverity, RiskType
from src.models.upi_models import (
    UPIAutopayEvent,
    UPIFailureCode,
    UPIMandate,
    MandateState,
    MandateFrequency,
)
from src.agent.upi_detector import UPIAutopayDetector
from src.agent.decision_engine import DecisionEngine
from src.agent.promise_tracker import promise_tracker
from api.main import app

IST = timezone(timedelta(hours=5, minutes=30))


# ── Helper Fixtures ───────────────────────────────────────────────────────────

def make_upi_event(
    vpa: str,
    amount: float,
    failure_code: UPIFailureCode = UPIFailureCode.U30,
    mandate_state: MandateState = MandateState.ACTIVE,
    retry_attempt: int = 0,
    customer_id: str = "",
) -> UPIAutopayEvent:
    now = datetime.now(IST)
    cid = customer_id or f"CUST-{vpa.replace('@', '_').replace('.', '_').upper()}"
    mandate = UPIMandate(
        mandate_id=f"MND-{cid}",
        customer_id=cid,
        customer_vpa=vpa,
        amount=amount,
        frequency=MandateFrequency.MONTHLY,
        state=mandate_state,
        bank_name="SBI",
        bank_ifsc="SBIN0000001",
        created_at=now - timedelta(days=90),
        expiry_date=now + timedelta(days=275),
    )
    return UPIAutopayEvent(
        event_id="EVT-TEST-PAT-001",
        event_type="mandate.execution.failed",
        payment_id="pay_test_pat",
        mandate=mandate,
        failure_code=failure_code,
        failure_message=failure_code.human_reason,
        debit_amount=amount,
        occurred_at=now,
        retry_attempt=retry_attempt,
    )


class TestSpendPatternLogic:
    def setup_method(self):
        self.tracker = SpendPatternTracker()

    def test_normal_variation_is_not_critical(self):
        """
        User Example 1:
        If a person only transacts in between 10,000–50,000, then a 60,000 transaction
        is NOT critical.
        """
        custom_history = [12000.0, 25000.0, 35000.0, 48000.0, 30000.0, 50000.0]
        result = self.tracker.analyze(
            vpa="arjun@okicici",
            current_amount=60000.0,
            custom_history=custom_history,
        )

        assert not result.is_critical
        assert not result.is_spike
        assert result.severity in (RiskSeverity.LOW, RiskSeverity.MEDIUM)
        assert result.spike_ratio < 2.5
        assert result.profile.min_amount == 12000.0
        assert result.profile.max_amount == 50000.0
        assert "NORMAL PATTERN" in result.explanation or "Within normal" in result.explanation

    def test_sudden_upward_spike_is_critical(self):
        """
        User Example 2:
        If normally transaction is around 100 rs and a transaction is around 70,000,
        then the transaction IS CRITICAL.
        """
        custom_history = [89.0, 100.0, 110.0, 99.0, 149.0, 120.0]  # mean ~111, max 149
        result = self.tracker.analyze(
            vpa="rahul@oksbi",
            current_amount=70000.0,
            custom_history=custom_history,
        )

        assert result.is_critical
        assert result.is_spike
        assert result.severity == RiskSeverity.CRITICAL
        # Spike ratio: 70,000 / 110 ~ 636x
        assert result.spike_ratio > 400.0
        assert "CRITICAL SPEND SPIKE" in result.explanation
        assert "BLOCK blind automatic retry" in result.recommendation

    def test_pre_seeded_archetypes(self):
        """Verify pre-seeded VPAs exhibit correct archetype behavior."""
        # Aarav: micro-ticket ~₹100 base
        res_aarav_normal = self.tracker.analyze("aarav@oksbi", 129.0)
        assert not res_aarav_normal.is_critical

        res_aarav_spike = self.tracker.analyze("aarav@oksbi", 70000.0)
        assert res_aarav_spike.is_critical
        assert res_aarav_spike.severity == RiskSeverity.CRITICAL

        # Rahul: normal ~₹999 OTT base
        res_rahul_normal = self.tracker.analyze("rahul@oksbi", 999.0)
        assert not res_rahul_normal.is_critical

        # Arjun: normal ₹4,500 SaaS base
        res_arjun_normal = self.tracker.analyze("arjun@okicici", 4500.0)
        assert not res_arjun_normal.is_critical

        # Arjun with massive ₹10 Lakh spike
        res_arjun_spike = self.tracker.analyze("arjun@okicici", 1000000.0)
        assert res_arjun_spike.is_critical

    def test_insufficient_history_handled_gracefully(self):
        """New customer with 0 or 1 prior transaction should not falsely trigger a critical spike."""
        result_empty = self.tracker.analyze("unknown_new_user@oksbi", 5000.0)
        assert not result_empty.is_critical
        assert result_empty.profile.transaction_count == 0

        result_single = self.tracker.analyze(
            "single_txn_user@oksbi",
            5000.0,
            custom_history=[100.0],
        )
        assert not result_single.is_critical

    def test_dynamic_transaction_recording(self):
        """Verify recording transactions dynamically updates statistical baseline."""
        vpa = "dynamic_user@okaxis"
        assert len(self.tracker.get_history(vpa)) == 0

        self.tracker.record_transaction(vpa, 200.0)
        self.tracker.record_transaction(vpa, 250.0)
        self.tracker.record_transaction(vpa, 220.0)

        profile = self.tracker.get_profile(vpa)
        assert profile.transaction_count == 3
        assert profile.min_amount == 200.0
        assert profile.max_amount == 250.0
        assert profile.mean_amount == pytest.approx(223.33, 0.1)

        # Now test spike against dynamic baseline
        res_spike = self.tracker.analyze(vpa, 80000.0)
        assert res_spike.is_critical
        assert res_spike.spike_ratio > 300.0


class TestPipelineIntegration:
    def setup_method(self):
        spend_pattern_tracker.reset_history()
        customer_identity_registry.reset()

    @pytest.mark.asyncio
    async def test_detector_escalates_to_critical_on_spend_spike(self):
        """UPIAutopayDetector should elevate risk severity to CRITICAL on sudden spend spike."""
        detector = UPIAutopayDetector()

        # Normal U30 on new/unseeded VPA (< ₹1,000) -> LOW severity
        normal_event = make_upi_event("unseeded_user@oksbi", 999.0, UPIFailureCode.U30)
        normal_risk = await detector.detect_from_upi_event(normal_event)
        assert normal_risk is not None
        assert normal_risk.severity == RiskSeverity.LOW

        # Sudden spike on aarav@oksbi (baseline ~₹100, transaction ₹70,000) -> CRITICAL
        spike_event = make_upi_event("aarav@oksbi", 70000.0, UPIFailureCode.U30)
        spike_risk = await detector.detect_from_upi_event(spike_event)
        assert spike_risk is not None
        assert spike_risk.severity == RiskSeverity.CRITICAL
        assert "pattern_analysis" in spike_risk.metadata
        assert spike_risk.metadata["pattern_analysis"].is_critical

    @pytest.mark.asyncio
    async def test_sequential_user_transactions_maintain_pattern_consistency(self):
        """
        Verify that sequential transactions on the same user (e.g. ₹25,000, ₹26,000, ₹25,001)
        maintain consistent Medium severity rather than flipping on a 1-rupee threshold,
        and that a sudden 6x spike on that user triggers Critical anomaly.
        """
        detector = UPIAutopayDetector()
        vpa = "user_pattern_seq@oksbi"

        # 1st transaction: ₹25,000
        r1 = await detector.detect_from_upi_event(make_upi_event(vpa, 25000.0))
        assert r1.severity == RiskSeverity.MEDIUM
        assert not r1.metadata["pattern_analysis"].is_critical

        # 2nd transaction: ₹26,000 (evaluated against ₹25,000 baseline)
        r2 = await detector.detect_from_upi_event(make_upi_event(vpa, 26000.0))
        assert r2.severity == RiskSeverity.MEDIUM
        assert not r2.metadata["pattern_analysis"].is_critical
        assert r2.metadata["pattern_analysis"].spike_ratio == pytest.approx(1.04, 0.05)

        # 3rd transaction: ₹25,001 (1-rupee variance above 25k -> still MEDIUM, consistent!)
        r3 = await detector.detect_from_upi_event(make_upi_event(vpa, 25001.0))
        assert r3.severity == RiskSeverity.MEDIUM
        assert not r3.metadata["pattern_analysis"].is_critical
        assert r3.metadata["pattern_analysis"].spike_ratio == pytest.approx(0.98, 0.05)

        # 4th transaction: ₹1,50,000 (Sudden upward spike for this user -> CRITICAL)
        r4 = await detector.detect_from_upi_event(make_upi_event(vpa, 150000.0))
        assert r4.severity == RiskSeverity.CRITICAL
        assert r4.metadata["pattern_analysis"].is_critical
        assert r4.metadata["pattern_analysis"].spike_ratio > 5.0

    def test_decision_engine_blocks_retry_on_spend_spike(self):
        """DecisionEngine GR10 should block blind silent retries on sudden upward spend spike."""
        engine = DecisionEngine()

        # Aarav with normal ₹129 amount -> smart_retry allowed
        dec_normal = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=129.0,
            customer_vpa="aarav@oksbi",
        )
        assert "smart_retry" in dec_normal.allowed_actions

        # Aarav with ₹70,000 sudden spike -> smart_retry blocked by GR10
        dec_spike = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=70000.0,
            customer_vpa="aarav@oksbi",
        )
        assert "smart_retry" in dec_spike.blocked_actions
        assert "spend_pattern_spike_critical" in dec_spike.guardrails_fired
        assert dec_spike.pattern_analysis is not None
        assert dec_spike.pattern_analysis["is_critical"] is True


class TestPatternAPIEndpoints:
    def setup_method(self):
        self.client = TestClient(app)
        spend_pattern_tracker.reset_history()

    def test_get_pattern_history(self):
        resp = self.client.get("/api/pattern/history?vpa=aarav@oksbi")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vpa"] == "aarav@oksbi"
        assert len(data["history"]) > 0
        assert "profile" in data
        assert data["profile"]["mean_amount"] < 200.0

    def test_post_pattern_analyze_spike(self):
        payload = {
            "vpa": "aarav@oksbi",
            "amount": 70000.0,
        }
        resp = self.client.post("/api/pattern/analyze", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_critical"] is True
        assert data["is_spike"] is True
        assert data["severity"] == "critical"
        assert data["spike_ratio"] > 400.0

    def test_post_pattern_analyze_normal_range(self):
        payload = {
            "vpa": "arjun@okicici",
            "amount": 60000.0,
            "history": [10000.0, 25000.0, 35000.0, 48000.0, 50000.0],
        }
        resp = self.client.post("/api/pattern/analyze", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_critical"] is False
        assert data["is_spike"] is False
        assert data["spike_ratio"] < 2.5

    def test_post_pattern_record(self):
        vpa = "new_record_test@paytm"
        payload = {"vpa": vpa, "amount": 1500.0}
        resp = self.client.post("/api/pattern/record", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["profile"]["transaction_count"] >= 1


class TestRepeatUserPipelineIntegration:
    """Validates repeat user behavior through the full simulator & decision pipeline."""

    def setup_method(self):
        spend_pattern_tracker.reset_history()
        promise_tracker._promises.clear()

    @pytest.mark.asyncio
    async def test_repeat_user_high_spike_keeps_retry_available(self):
        """When aarav@oksbi (base ~100) attempts ₹999, mark high severity without blocking salary retry."""
        from api.simulator import run_custom_scenario

        # 1. High-but-not-critical spike attempt (₹999 on ₹100 base)
        ev_spike = await run_custom_scenario({
            "failure_code": "U30",
            "vpa": "aarav@oksbi",
            "bank": "SBI",
            "amount": 999.0,
            "mandate_state": "active",
            "retry_attempt": 0,
        })
        assert ev_spike is not None
        assert ev_spike.is_pattern_critical is False
        assert ev_spike.severity == "high"
        # A high pattern spike should be visible, but not harsh enough to block retry.
        assert ev_spike.pattern_spike_ratio > 4.0
        assert "smart_retry" in ev_spike.interventions
        assert "upi_collect" in ev_spike.interventions

        # 2. Normal Transaction (₹99 on ₹100 base)
        ev_normal = await run_custom_scenario({
            "failure_code": "U30",
            "vpa": "aarav@oksbi",
            "bank": "SBI",
            "amount": 99.0,
            "mandate_state": "active",
            "retry_attempt": 0,
        })
        assert ev_normal is not None
        assert ev_normal.is_pattern_critical is False
        # Normal transaction allows smart_retry!
        assert "smart_retry" in ev_normal.interventions

    @pytest.mark.asyncio
    async def test_repeat_user_trust_score_does_not_collapse_to_five_percent(self):
        """Active pending promises should not collapse trust score to 5%."""
        vpa = "trust_test_user@oksbi"
        # New user starts with neutral 0.50
        assert promise_tracker.payer_trust_score(vpa) == 0.50

        # Create a pending promise
        p = promise_tracker.create(vpa, 500.0, "SBI", "U30", deadline_hours=24)
        # Pending promise should still retain neutral expectation (0.50), not collapse to 0.05
        assert promise_tracker.payer_trust_score(vpa) == 0.50

        # Fulfill promise
        promise_tracker.fulfill(p.promise_id)
        # Fulfilled promise boosts trust score
        assert promise_tracker.payer_trust_score(vpa) > 0.50
