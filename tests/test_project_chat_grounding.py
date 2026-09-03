"""
test_project_chat_grounding.py — Tests for Live Session Awareness & Full-Platform Grounding
========================================================================================
Validates:
1. Zero-division and empty-state safety on fresh instances.
2. Anti-hallucination invariant: Fresh session reports ₹0 (never ₹4,47,296 benchmark).
3. Live session metrics accurately reflect active ledger transactions.
4. Benchmark queries correctly cite published 50-run Monte Carlo proof.
5. Deep grounding for B2B Chaser, Cart Checkout Recovery, Customer Identity Graph, and Mandate Expiry.
"""

import pytest
from fastapi.testclient import TestClient
from api.main import app
from src.agent.recovery_ledger import ledger as recovery_ledger
from src.agent.whatsapp_inbound import suppression_registry
from src.agent.promise_tracker import promise_tracker
from src.integrations.llm_classifier import get_live_session_summary, llm_classifier


@pytest.fixture(autouse=True)
def clean_ledger_and_registries():
    recovery_ledger.reset()
    suppression_registry.reset()
    promise_tracker._promises.clear()
    yield
    recovery_ledger.reset()
    suppression_registry.reset()
    promise_tracker._promises.clear()


class TestLiveSessionSafetyAndGrounding:

    def test_get_live_session_summary_zero_safe(self):
        """Verifies get_live_session_summary returns clean zero defaults on fresh state."""
        summary = get_live_session_summary()
        assert isinstance(summary, dict)
        assert summary["total_entries"] == 0
        assert summary["total_recovered"] == 0.0
        assert summary["net_roi"] == 0.0
        assert summary["recovery_rate_pct"] == 0.0
        assert summary["active_promises_count"] == 0
        assert summary["suppression_blacklisted_count"] == 0

    def test_fresh_session_reports_zero_without_hallucinating_benchmark(self):
        """
        Anti-Hallucination Invariant:
        When asking for current session stats on a fresh instance, the chatbot MUST report ₹0
        and MUST NOT claim the ₹4,47,296 benchmark figure as current session revenue.
        """
        client = TestClient(app)
        res = client.post("/api/project-chat", json={"message": "How much revenue have we recovered in this session?"})
        assert res.status_code == 200
        data = res.json()
        reply = data.get("reply", "")

        # Must report ₹0 for active session
        assert "₹0" in reply or "0 logged transactions" in reply
        # Must clearly indicate it's a fresh session
        assert any(w in reply.lower() for w in ["fresh", "simulator", "simulation", "scenario", "0 logged", "0 transactions", "0 has been recovered"])
        # Must NOT claim ₹4,47,296 as the active session recovery
        assert "Active Session Recovered: ₹4,47,296" not in reply

    def test_live_session_metrics_reflect_active_ledger(self):
        """Verifies that executing recoveries immediately updates live session stats."""
        # Simulate active recovery
        entry = recovery_ledger.log(
            event_type="recover",
            vpa="test_user@oksbi",
            amount=1499.0,
            reasoning="Smart Retry executed successfully during verified salary liquidity window.",
            confidence=0.92,
            channel="smart_retry",
            outcome="success",
        )
        recovery_ledger.mark_outcome(entry.ledger_id, outcome="success", amount_recovered=1499.0)

        client = TestClient(app)
        res = client.post("/api/project-chat", json={"message": "What are our current session stats?"})
        assert res.status_code == 200
        reply = res.json().get("reply", "")

        assert "1,499" in reply
        assert "100.0%" in reply or "Session Recovery Rate" in reply

    def test_benchmark_query_returns_published_monte_carlo_proof(self):
        """Verifies that asking about benchmarks cites the published 50-run Monte Carlo evaluation."""
        client = TestClient(app)
        res = client.post("/api/project-chat", json={"message": "What are the benchmark results vs baseline?"})
        assert res.status_code == 200
        reply = res.json().get("reply", "")

        assert "4,47,296" in reply
        assert "75.8%" in reply
        assert "Monte Carlo" in reply or "195" in reply

    def test_b2b_receivables_grounding(self):
        """Verifies the assistant accurately details B2B aging buckets and debtor tiers."""
        client = TestClient(app)
        res = client.post("/api/project-chat", json={"message": "How does B2B Receivables Chasing work with aging buckets and tiers?"})
        assert res.status_code == 200
        reply = res.json().get("reply", "")

        assert "Aging Buckets" in reply or "0–30" in reply or "0-30" in reply
        assert "Tier A" in reply or "Tier B" in reply or "Tier C" in reply
        assert "dedicated" in reply.lower() or "specialist" in reply.lower() or "ivr" in reply.lower()

    def test_checkout_dropoff_grounding(self):
        """Verifies the assistant details checkout drop-off recovery sequences and captured reasons."""
        client = TestClient(app)
        res = client.post("/api/project-chat", json={"message": "How does Checkout Drop-off Recovery work?"})
        assert res.status_code == 200
        reply = res.json().get("reply", "")

        assert "payment_page_exit" in reply or "otp_timeout" in reply or "bank_error_exit" in reply
        assert "T+10" in reply or "re-engagement" in reply.lower() or "cart" in reply.lower()

    def test_customer_identity_grounding(self):
        """Verifies the assistant details cross-VPA/phone resolution and unified behavioral history."""
        client = TestClient(app)
        res = client.post("/api/project-chat", json={"message": "How does the Customer Identity Graph resolve aliases?"})
        assert res.status_code == 200
        reply = res.json().get("reply", "")

        assert "canonical" in reply.lower() or "cust:" in reply.lower()
        assert "alias" in reply.lower() or "vpa" in reply.lower() or "unified" in reply.lower()

    def test_mandate_expiry_grounding(self):
        """Verifies the assistant explains proactive T-72h BT02 prevention."""
        client = TestClient(app)
        res = client.post("/api/project-chat", json={"message": "How does the Mandate Expiry Interceptor prevent BT02 errors?"})
        assert res.status_code == 200
        reply = res.json().get("reply", "")

        assert "BT02" in reply or "Mandate Expired" in reply
        assert "T-72" in reply or "proactive" in reply.lower() or "renewal" in reply.lower()
