"""
Unit tests for Contextual Thompson Sampling Bandit & Monte Carlo Benchmark.
"""

import pytest
from src.agent.bandit import ThompsonSamplingEngine, RecoveryArm, get_context_key
from src.agent.decision_engine import DecisionEngine, CustomerTier
from benchmark import run_benchmark, simulate_baseline_on_event, simulate_ai_agent_on_event


class TestThompsonSamplingBandit:
    def test_bandit_initialization(self):
        engine = ThompsonSamplingEngine()
        summary = engine.get_summary()
        assert len(summary) > 0
        key = get_context_key("insufficient_funds", "silver", "med")
        assert key in summary
        assert RecoveryArm.SMART_RETRY_SALARY.value in summary[key]

    def test_bandit_select_best_arm(self):
        engine = ThompsonSamplingEngine()
        decision = engine.select_best_arm(
            failure_category="insufficient_funds",
            amount=999.0,
            customer_tier="silver",
            trust_score=0.8,
            allowed_actions=["smart_retry", "upi_collect", "whatsapp_nudge"]
        )
        assert decision.selected_arm in (
            RecoveryArm.SMART_RETRY_SALARY,
            RecoveryArm.UPI_COLLECT_DIRECT,
            RecoveryArm.WHATSAPP_PAY_LINK
        )
        assert 0.0 <= decision.sampled_score <= 1.0
        assert 0.0 <= decision.expected_win_rate <= 1.0
        assert len(decision.confidence_interval) == 2

    def test_bandit_bayesian_update(self):
        engine = ThompsonSamplingEngine()
        key = get_context_key("insufficient_funds", "gold", "high")
        
        # Initial arm mean
        arm_state = engine._get_or_create_arm(key, RecoveryArm.SMART_RETRY_SALARY)
        initial_mean = arm_state.mean
        initial_alpha = arm_state.alpha

        # Update with success
        engine.update(key, RecoveryArm.SMART_RETRY_SALARY, success=True, amount_recovered=5000.0)
        assert arm_state.alpha == initial_alpha + 1.0
        assert arm_state.mean > initial_mean

    def test_decision_engine_includes_bandit(self):
        engine = DecisionEngine()
        decision = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=1500.0,
            retry_count=0,
            has_promise=False,
            trust_score=0.75,
        )
        assert decision.approved is True
        assert decision.bandit_decision is not None
        assert "selected_arm" in decision.bandit_decision
        assert decision.bandit_decision["selected_arm"] == decision.allowed_actions[0]


class TestBenchmarkSuite:
    def test_run_benchmark_completes(self):
        base, ai = run_benchmark(n_runs=10)
        assert base.total_events == 40
        assert ai.total_events == 40
        assert ai.total_recovered > base.total_recovered
        assert ai.compliance_violations == 0
        assert base.compliance_violations > 0
        assert ai.net_roi > base.net_roi

    def test_benchmark_monte_carlo_stats(self):
        base, ai = run_benchmark(n_runs=20)
        assert hasattr(ai, "_ai_rate_mean")
        assert hasattr(ai, "_ai_rate_std")
        assert hasattr(ai, "_ai_rec_mean")
        assert hasattr(ai, "_ai_rec_std")
        assert ai._n_runs == 20
        assert ai.total_recovered == ai._ai_rec_mean
        assert 50.0 <= ai._ai_rate_mean <= 95.0
        assert ai._ai_rate_std >= 0.0

    def test_api_benchmark_endpoint(self):
        from fastapi.testclient import TestClient
        from api.main import app

        client = TestClient(app)
        res = client.get("/api/benchmark")
        assert res.status_code == 200
        data = res.json()
        assert "baseline" in data
        assert "ai_agent" in data
        assert "delta" in data
        assert "recovery_rate_std" in data["ai_agent"]
        assert "total_recovered_std" in data["ai_agent"]
        assert data["ai_agent"]["recovery_rate_pct"] > data["baseline"]["recovery_rate_pct"]
        assert data["ai_agent"]["total_recovered"] > data["baseline"]["total_recovered"]
        assert data["delta"]["violations_eliminated"] > 0
        assert "sensitivity_analysis_20pct_haircut" in data

    def test_benchmark_sensitivity_analysis(self):
        from benchmark import run_sensitivity_analysis
        sens = run_sensitivity_analysis(n_runs=10, haircut_pct=0.20)
        assert sens["haircut_pct"] == 20
        assert sens["ai_rate_mean"] > sens["base_rate"]
        assert sens["ai_recovered_mean"] > sens["base_recovered"]
        assert sens["net_uplift_revenue"] > 50000

    def test_benchmark_exact_reproducibility(self):
        from benchmark import run_benchmark
        b1, a1 = run_benchmark(n_runs=20)
        b2, a2 = run_benchmark(n_runs=20)
        assert a1.total_recovered == a2.total_recovered
        assert a1.recovered_events == a2.recovered_events
        assert a1.retries_fired == a2.retries_fired
        assert a1._ai_rec_mean == a2._ai_rec_mean
        assert a1._ai_rate_mean == a2._ai_rate_mean


class TestLiveBanditLearningInAPI:
    def test_live_simulation_updates_bandit(self):
        from fastapi.testclient import TestClient
        from api.main import app
        from src.agent.bandit import bandit_engine

        client = TestClient(app)
        client.post("/api/reset")

        # Simulate U30 scenario
        res = client.post("/api/simulate/u30")
        assert res.status_code == 200

        # Verify bandit was updated in real time
        total_pulls = sum(
            st.total_pulls
            for arms in bandit_engine._contexts.values()
            for st in arms.values()
        )
        assert total_pulls > 0

    def test_live_p2p_fulfillment_updates_bandit(self):
        from fastapi.testclient import TestClient
        from api.main import app
        from src.agent.bandit import bandit_engine, get_context_key, RecoveryArm
        from src.agent.promise_tracker import promise_tracker

        client = TestClient(app)
        client.post("/api/reset")

        p = promise_tracker.create("payer@oksbi", 1200.0, "SBI", "U30", deadline_hours=24, channel="whatsapp")
        ckey = get_context_key("insufficient_funds", "bronze", "high")
        arm_state = bandit_engine._get_or_create_arm(ckey, RecoveryArm.WHATSAPP_PAY_LINK)
        initial_pulls = arm_state.total_pulls
        initial_alpha = arm_state.alpha

        res = client.post(f"/api/promises/{p.promise_id}/fulfill")
        assert res.status_code == 200
        assert arm_state.total_pulls == initial_pulls + 1
        assert arm_state.alpha == initial_alpha + 1.0

    def test_live_checkout_recovery_updates_bandit(self):
        from fastapi.testclient import TestClient
        from api.main import app
        from src.agent.bandit import bandit_engine, get_context_key, RecoveryArm
        from src.agent.checkout_recovery import checkout_agent

        client = TestClient(app)
        client.post("/api/reset")

        s = checkout_agent.record_drop_off("user@oksbi", "+91-9999999999", 2500.0, "Merchant A", "payment_page_exit")
        ckey = get_context_key("insufficient_funds", "silver", "med")
        arm_state = bandit_engine._get_or_create_arm(ckey, RecoveryArm.WHATSAPP_PAY_LINK)
        initial_pulls = arm_state.total_pulls

        res = client.post(f"/api/checkout/{s.session_id}/recover")
        assert res.status_code == 200
        assert arm_state.total_pulls == initial_pulls + 1
        assert arm_state.total_revenue_recovered >= 2500.0

    def test_live_b2b_settle_updates_bandit(self):
        from fastapi.testclient import TestClient
        from api.main import app
        from src.agent.bandit import bandit_engine, get_context_key, RecoveryArm
        from src.agent.b2b_chaser import b2b_chaser

        client = TestClient(app)
        client.post("/api/reset")

        r = b2b_chaser.add_receivable("Corp Ltd", "corp@okhdfc", "+91-9800000001", "INV-99", 50000, "2026-07-01")
        ckey = get_context_key("b2b_overdue", "silver", "med")
        arm_state = bandit_engine._get_or_create_arm(ckey, RecoveryArm.B2B_IVR_CHASER)
        initial_pulls = arm_state.total_pulls

        res = client.post(f"/api/b2b/receivables/{r.receivable_id}/settle?amount_received=50000")
        assert res.status_code == 200
        assert arm_state.total_pulls == initial_pulls + 1
        assert arm_state.total_revenue_recovered >= 50000.0

