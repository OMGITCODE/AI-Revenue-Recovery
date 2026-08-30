"""
Unit tests for Contextual Thompson Sampling Bandit & Empirical Benchmark.
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
