"""
test_mandate_expiry.py — Test Suite for Proactive Mandate Expiry Interceptor (T-72h)
=====================================================================================
Validates:
1. Detection of active UPI Autopay mandates within the T-72h lookahead window.
2. Generating 1-click renewal magic links and dispatching WhatsApp reminders.
3. Immutable audit trail logging in RecoveryLedger under BT02_PREVENTED.
4. Simulating customer renewal completion and pre-empted revenue protection.
5. Live REST API endpoints: GET /api/mandates/expiring, POST /api/mandates/proactive-nudge, etc.
6. Simulator scenario execution for proactive expiry.
"""

from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient

from api.main import app
from src.agent.mandate_expiry import mandate_expiry_scanner, ExpiringMandate
from src.agent.recovery_ledger import ledger as recovery_ledger
from api.simulator import run_scenario

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def clean_state():
    mandate_expiry_scanner.reset()
    recovery_ledger._entries.clear()
    yield
    mandate_expiry_scanner.reset()
    recovery_ledger._entries.clear()


class TestMandateExpiryScannerLogic:
    def test_find_expiring_mandates_returns_seeded_archetypes(self):
        expiring = mandate_expiry_scanner.find_expiring_mandates(within_hours=72)
        assert len(expiring) == 8
        # Should be sorted by expiry date ascending
        assert expiring[0].hours_remaining() <= expiring[1].hours_remaining()
        assert any(m.customer_vpa == "rahul@oksbi" for m in expiring)
        assert any(m.customer_vpa == "priya@okhdfcbank" for m in expiring)

    def test_filter_window_respects_cutoff(self):
        # Only mandates expiring in <= 24 hours (Arjun: 14.5h, Priya: 18h)
        urgent = mandate_expiry_scanner.find_expiring_mandates(within_hours=24)
        assert len(urgent) == 2
        assert any(m.customer_vpa == "priya@okhdfcbank" for m in urgent)
        assert any(m.customer_vpa == "arjun.nair@okicici" for m in urgent)
        assert all(m.hours_remaining() <= 24 for m in urgent)

    def test_register_new_custom_mandate(self):
        now = datetime.now(IST)
        custom = mandate_expiry_scanner.register_mandate(
            mandate_id="mand_custom_999",
            customer_id="cust_test_999",
            customer_vpa="testuser@oksbi",
            customer_name="Test User",
            amount=599.0,
            plan_name="Monthly Gym Pass",
            bank_name="SBI",
            expiry_date=now + timedelta(hours=40),
        )
        assert custom.mandate_id == "mand_custom_999"
        expiring = mandate_expiry_scanner.find_expiring_mandates(within_hours=72)
        assert any(m.mandate_id == "mand_custom_999" for m in expiring)

    @pytest.mark.asyncio
    async def test_dispatch_proactive_nudge_generates_link_and_logs_ledger(self):
        m = await mandate_expiry_scanner.dispatch_proactive_nudge("mand_sbi_exp_001")
        assert m is not None
        assert m.status == "NUDGED"
        assert m.renewal_link is not None
        assert "rzp.io/l/demo-mandate" in m.renewal_link

        # Verify RecoveryLedger entry
        entries = recovery_ledger.all_entries()
        assert len(entries) >= 1
        prevent_entry = [e for e in entries if "BT02" in e.reasoning][0]
        assert prevent_entry.vpa == "rahul@oksbi"
        assert prevent_entry.outcome == "success"
        assert prevent_entry.recovery_type == "proactive"

    @pytest.mark.asyncio
    async def test_simulate_proactive_renewal_protects_revenue(self):
        # 1. Nudge
        await mandate_expiry_scanner.dispatch_proactive_nudge("mand_hdfc_exp_002")
        # 2. Renew
        renewed = await mandate_expiry_scanner.simulate_proactive_renewal("mand_hdfc_exp_002")
        assert renewed is not None
        assert renewed.status == "RENEWED"
        assert renewed.renewed_at is not None

        # Verify stats
        stats = mandate_expiry_scanner.get_stats()
        assert stats["renewals_completed"] == 1
        assert stats["revenue_protected"] == 1499.0

        # Verified in RecoveryLedger
        ledger_entries = recovery_ledger.all_entries()
        renew_entry = [e for e in ledger_entries if e.event_type == "recover"][0]
        assert renew_entry.amount_recovered == 1499.0
        assert renew_entry.recovery_type == "proactive"

        # Verify overall_roi cleanly separates proactive from reactive
        roi = recovery_ledger.overall_roi()
        assert roi["proactive_protected"] == 1499.0
        assert roi["reactive_recovered"] == 0.0
        assert roi["total_recovered"] == 1499.0

    @pytest.mark.asyncio
    async def test_proactive_vs_reactive_no_double_counting(self):
        """
        Validates that proactive renewals (churn prevention) and reactive recoveries (post-failure)
        are cleanly segregated in RecoveryLedger without double counting.
        """
        # 1. Simulate a reactive recovery (e.g. U30 retry)
        e_rx = recovery_ledger.log(
            event_type="intervene",
            vpa="user_rx@oksbi",
            amount=999.0,
            reasoning="U30 retry executed",
            confidence=0.85,
            channel="smart_retry",
            recovery_type="reactive",
        )
        recovery_ledger.mark_outcome(e_rx.ledger_id, outcome="success", amount_recovered=999.0)

        # 2. Simulate a proactive renewal (churn prevention)
        await mandate_expiry_scanner.dispatch_proactive_nudge("mand_ybl_exp_003")
        await mandate_expiry_scanner.simulate_proactive_renewal("mand_ybl_exp_003")

        stats = mandate_expiry_scanner.get_stats()
        roi = recovery_ledger.overall_roi()

        # Mandate expiry scanner reports ONLY churn prevented
        assert stats["revenue_protected"] == 2999.0

        # Ledger overall_roi reports clean separation:
        assert roi["reactive_recovered"] == 999.0
        assert roi["proactive_protected"] == 2999.0
        assert roi["total_recovered"] == 999.0 + 2999.0
        assert roi["reactive_net_roi"] == 999.0  # smart_retry cost is 0.0
        assert roi["proactive_net_roi"] == 2999.0 - (0.50 + 0.50)  # whatsapp nudge + renewal cost


class TestMandateExpiryAPIEndpoints:
    def test_get_expiring_mandates_endpoint(self):
        client = TestClient(app)
        res = client.get("/api/mandates/expiring?within_hours=72")
        assert res.status_code == 200
        data = res.json()
        assert data["count"] == 8
        assert len(data["mandates"]) == 8
        assert "stats" in data

    def test_get_all_mandates_endpoint(self):
        client = TestClient(app)
        res = client.get("/api/mandates/all")
        assert res.status_code == 200
        data = res.json()
        assert data["count"] >= 8

    def test_get_mandate_stats_endpoint(self):
        client = TestClient(app)
        res = client.get("/api/mandates/stats")
        assert res.status_code == 200
        stats = res.json()
        assert stats["total_mandates_tracked"] == 8
        assert stats["expiring_within_72h"] == 8
        assert stats["revenue_protected"] == 0.0

    def test_post_proactive_nudge_endpoint(self):
        client = TestClient(app)
        res = client.post("/api/mandates/proactive-nudge/mand_ybl_exp_003")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["mandate"]["status"] == "NUDGED"
        assert data["mandate"]["renewal_link"] is not None

    def test_post_renew_mandate_endpoint(self):
        client = TestClient(app)
        res = client.post("/api/mandates/renew/mand_ybl_exp_003")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["mandate"]["status"] == "RENEWED"
        assert "2999.00 protected" in data["message"]

    def test_post_register_mandate_endpoint(self):
        client = TestClient(app)
        payload = {
            "mandate_id": "mand_api_test_001",
            "customer_id": "cust_api_001",
            "customer_vpa": "apiuser@oksbi",
            "customer_name": "API User",
            "amount": 799.0,
            "plan_name": "Pro Annual Plan",
            "bank_name": "SBI",
            "expiry_hours": 30.0,
        }
        res = client.post("/api/mandates/register", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["mandate"]["mandate_id"] == "mand_api_test_001"


class TestSimulatorProactiveExpiryScenario:
    @pytest.mark.asyncio
    async def test_run_proactive_expiry_scenario(self):
        res = await run_scenario("proactive_mandate_expiry")
        assert res is not None
        assert res.failure_code == "BT02"
        assert res.amount == 1499.0
        assert res.customer_vpa == "priya@okhdfcbank"
        assert res.status in ("recovered", "escalated", "failed")
