"""
tests/test_rbi_category_guardrail.py
====================================
Comprehensive tests for RBI Digital Payments - E-Mandate Framework compliance (GR7).
Verifies:
  1. Category threshold limits (₹1,00,000 for insurance, mutual funds, credit cards;
     ₹15,000 for general, education, and all others).
  2. Backward compatibility of DecisionEngine.RBI_PREDEBIT_THRESHOLD.
  3. Static helper DecisionEngine.get_rbi_threshold().
  4. DecisionEngine.evaluate() behavior across amounts and categories.
  5. GuardrailDecision serialization (to_dict).
  6. /api/decide integration via FastAPI TestClient.
  7. Benchmark baseline compliance checking with category awareness.
  8. Simulator execution for predefined scenarios (rbi_threshold, rbi_enhanced_insurance, rbi_enhanced_breach).
"""

import pytest
from fastapi.testclient import TestClient

from src.agent.decision_engine import DecisionEngine, GuardrailDecision
from benchmark import simulate_baseline_on_event, simulate_ai_agent_on_event
from api.main import app
from api.simulator import run_scenario, SCENARIOS, process_and_log_event


class TestRBICategoryLimits:
    """Validate RBI category limits and static accessors."""

    def test_backward_compatibility_constant(self):
        """Ensure RBI_PREDEBIT_THRESHOLD is preserved as 15_000 for backward compatibility."""
        assert DecisionEngine.RBI_PREDEBIT_THRESHOLD == 15_000

    def test_rbi_mandate_category_limits_dictionary(self):
        """Ensure enhanced categories are strictly insurance, mutual_fund, credit_card."""
        limits = DecisionEngine.RBI_MANDATE_CATEGORY_LIMITS
        assert limits["insurance"] == 100_000.0
        assert limits["mutual_fund"] == 100_000.0
        assert limits["credit_card"] == 100_000.0
        assert limits["general"] == 15_000.0
        # Education must NOT be in the enhanced limits dict
        assert "education" not in limits

    @pytest.mark.parametrize(
        "category, expected_threshold",
        [
            ("insurance", 100_000.0),
            ("mutual_fund", 100_000.0),
            ("credit_card", 100_000.0),
            ("INSURANCE", 100_000.0),
            (" Credit_Card ", 100_000.0),
            ("general", 15_000.0),
            ("education", 15_000.0),      # Education strictly falls back to 15,000
            ("streaming", 15_000.0),
            ("ott", 15_000.0),
            ("", 15_000.0),
            (None, 15_000.0),
        ],
    )
    def test_get_rbi_threshold_resolution(self, category, expected_threshold):
        """Verify get_rbi_threshold correctly resolves known and fallback categories."""
        assert DecisionEngine.get_rbi_threshold(category) == expected_threshold


class TestDecisionEngineGR7:
    """Validate GR7 guardrail execution across categories and amounts."""

    @pytest.fixture
    def engine(self):
        return DecisionEngine()

    def test_general_under_threshold_allowed(self, engine):
        """General transaction <= ₹15,000 does not trigger GR7."""
        dec = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=14_000.0,
            category="general",
        )
        assert "smart_retry" in dec.allowed_actions
        assert "rbi_predebit_threshold" not in dec.guardrails_fired
        assert dec.category == "general"
        assert dec.rbi_threshold == 15_000.0

    def test_general_over_threshold_blocked(self, engine):
        """General transaction > ₹15,000 triggers GR7 circuit breaker."""
        dec = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=18_500.0,
            category="general",
        )
        assert "smart_retry" in dec.blocked_actions
        assert "rbi_predebit_threshold" in dec.guardrails_fired
        assert dec.category == "general"
        assert dec.rbi_threshold == 15_000.0

    def test_education_over_15k_blocked(self, engine):
        """Education fees > ₹15,000 must be blocked by GR7 (not treated as ₹1L enhanced)."""
        dec = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=35_000.0,
            category="education",
        )
        assert "smart_retry" in dec.blocked_actions
        assert "rbi_predebit_threshold" in dec.guardrails_fired
        assert dec.category == "education"
        assert dec.rbi_threshold == 15_000.0

    @pytest.mark.parametrize("enhanced_cat", ["insurance", "mutual_fund", "credit_card"])
    def test_enhanced_categories_under_100k_allowed(self, engine, enhanced_cat):
        """Enhanced categories <= ₹1,00,000 pass GR7 without blocking smart_retry."""
        dec = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=75_000.0,
            category=enhanced_cat,
        )
        assert "smart_retry" in dec.allowed_actions
        assert "rbi_predebit_threshold" not in dec.guardrails_fired
        assert dec.category == enhanced_cat
        assert dec.rbi_threshold == 100_000.0

    @pytest.mark.parametrize("enhanced_cat", ["insurance", "mutual_fund", "credit_card"])
    def test_enhanced_categories_over_100k_blocked(self, engine, enhanced_cat):
        """Enhanced categories > ₹1,00,000 trigger GR7 circuit breaker."""
        dec = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=120_000.0,
            category=enhanced_cat,
        )
        assert "smart_retry" in dec.blocked_actions
        assert "rbi_predebit_threshold" in dec.guardrails_fired
        assert dec.category == enhanced_cat
        assert dec.rbi_threshold == 100_000.0

    def test_guardrail_decision_serialization(self, engine):
        """Ensure to_dict() includes category and rbi_threshold fields."""
        dec = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=45_000.0,
            category="insurance",
        )
        d = dec.to_dict()
        assert d["category"] == "insurance"
        assert d["rbi_threshold"] == 100_000.0
        assert "smart_retry" in d["allowed_actions"]


class TestBenchmarkCompliance:
    """Validate benchmark compliance checks with category awareness."""

    def test_baseline_compliance_general_and_education(self):
        import random
        rng = random.Random(42)

        # General > 15k is a violation
        ev_gen = {"amount": 20_000, "category": "general", "failure_code": "U30", "mandate_state": "active"}
        res_gen = simulate_baseline_on_event(ev_gen, rng)
        assert res_gen["violations"] >= 1

        # Education > 15k is also a violation
        ev_edu = {"amount": 20_000, "category": "education", "failure_code": "U30", "mandate_state": "active"}
        res_edu = simulate_baseline_on_event(ev_edu, rng)
        assert res_edu["violations"] >= 1

    def test_baseline_compliance_enhanced_limits(self):
        import random
        rng = random.Random(42)

        # Insurance at 45k is within 100k -> 0 violations
        ev_ins = {"amount": 45_000, "category": "insurance", "failure_code": "U30", "mandate_state": "active"}
        res_ins = simulate_baseline_on_event(ev_ins, rng)
        assert res_ins["violations"] == 0

        # Insurance at 110k exceeds 100k -> violation
        ev_ins_breach = {"amount": 110_000, "category": "insurance", "failure_code": "U30", "mandate_state": "active"}
        res_ins_breach = simulate_baseline_on_event(ev_ins_breach, rng)
        assert res_ins_breach["violations"] >= 1


class TestAPIDecideAndSimulator:
    """Validate /api/decide endpoint and simulator scenarios."""

    def test_api_decide_category_aware(self):
        client = TestClient(app)

        # Insurance ₹45k -> allowed
        resp_ins = client.post("/api/decide", json={
            "failure_code": "U30",
            "mandate_state": "active",
            "amount": 45000.0,
            "category": "insurance",
        })
        assert resp_ins.status_code == 200
        data_ins = resp_ins.json()
        assert data_ins["category"] == "insurance"
        assert data_ins["rbi_threshold"] == 100000.0
        assert "smart_retry" in data_ins["allowed_actions"]

        # Education ₹45k -> blocked
        resp_edu = client.post("/api/decide", json={
            "failure_code": "U30",
            "mandate_state": "active",
            "amount": 45000.0,
            "category": "education",
        })
        assert resp_edu.status_code == 200
        data_edu = resp_edu.json()
        assert data_edu["category"] == "education"
        assert data_edu["rbi_threshold"] == 15000.0
        assert "smart_retry" in data_edu["blocked_actions"]

    @pytest.mark.asyncio
    async def test_simulator_scenarios(self):
        # rbi_threshold scenario (general, 18.5k)
        assert "rbi_threshold" in SCENARIOS
        assert SCENARIOS["rbi_threshold"]["category"] == "general"
        ev1 = await run_scenario("rbi_threshold")
        assert ev1 is not None

        # rbi_enhanced_insurance scenario (insurance, 45k)
        assert "rbi_enhanced_insurance" in SCENARIOS
        assert SCENARIOS["rbi_enhanced_insurance"]["category"] == "insurance"
        ev2 = await run_scenario("rbi_enhanced_insurance")
        assert ev2 is not None
        assert "smart_retry" in ev2.interventions

        # rbi_enhanced_breach scenario (credit_card, 115k)
        assert "rbi_enhanced_breach" in SCENARIOS
        assert SCENARIOS["rbi_enhanced_breach"]["category"] == "credit_card"
        ev3 = await run_scenario("rbi_enhanced_breach")
        assert ev3 is not None
        assert "smart_retry" not in ev3.interventions
