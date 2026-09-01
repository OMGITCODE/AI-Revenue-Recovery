"""
Unit and integration tests for Unified Customer Identity Resolution & Behavioral History.

Verifies:
  1. Multi-identifier alias resolution (customer_id, VPA, phone, email) to a single canonical profile.
  2. Spend pattern transaction recording under one identifier updates the profile across all aliases.
  3. Promise-to-Pay trust score aggregation across aliases.
  4. WhatsApp inbound suppression on phone number suppresses Autopay debit attempts on VPA/customer_id.
  5. DecisionEngine guardrails evaluate cumulative customer touches and retries across aliases.
  6. EventStore customer timeline filtering.
  7. REST API endpoints /api/customer/{id}/history and /api/customers.
"""

import pytest
from starlette.testclient import TestClient

from src.agent.customer_identity import (
    CustomerIdentityRegistry,
    CustomerProfile,
    customer_identity_registry,
    normalize_identifier,
)
from src.agent.spend_pattern import spend_pattern_tracker, SpendPatternTracker
from src.agent.promise_tracker import promise_tracker, PromiseStatus
from src.agent.whatsapp_inbound import whatsapp_inbound_handler, suppression_registry, InboundIntent
from src.agent.decision_engine import DecisionEngine, infer_tier, CustomerTier
from src.models.upi_models import (
    MandateFrequency, MandateState,
    UPIAutopayEvent, UPIFailureCode, UPIMandate,
)
from src.agent.upi_detector import UPIAutopayDetector
from api.store import RecoveryEvent, store
from api.main import app


class TestCustomerIdentityRegistry:

    def setup_method(self):
        customer_identity_registry.reset()

    def test_preseeded_archetype_resolution(self):
        # Rahul Sharma archetype aliases
        cid1 = customer_identity_registry.resolve_canonical_id("rahul@oksbi")
        cid2 = customer_identity_registry.resolve_canonical_id("CUST-SBI-001")
        cid3 = customer_identity_registry.resolve_canonical_id("+919800000001")
        cid4 = customer_identity_registry.resolve_canonical_id("CUST-SPIKE-007")

        assert cid1 == cid2 == cid3 == cid4 == "cust:rahul@oksbi"
        assert customer_identity_registry.is_same_person("rahul@oksbi", "CUST-SBI-001")
        assert customer_identity_registry.is_same_person("+919800000001", "CUST-SPIKE-007")

    def test_dynamic_alias_linking(self):
        # Link a new customer_id and phone to a new VPA
        cid = customer_identity_registry.resolve_canonical_id(
            "karan@okaxis", "CUST-KARAN-99", "+919811122233"
        )
        assert cid == "cust:karan@okaxis"

        # Now querying by phone should return the exact same canonical profile
        prof = customer_identity_registry.get_profile("+919811122233")
        assert prof is not None
        assert prof.canonical_id == cid
        assert "cust-karan-99" in prof.customer_ids
        assert "+919811122233" in prof.phones

    def test_touch_and_retry_tracking_across_aliases(self):
        vpa = "touch_test@oksbi"
        cust_id = "CUST-TOUCH-01"

        # Record touch using VPA
        customer_identity_registry.record_touch(vpa)
        # Record touch using Customer ID
        customer_identity_registry.record_touch(cust_id)
        # Link them together
        customer_identity_registry.link_identifiers(vpa, cust_id)

        # Both should report 2 touches today
        assert customer_identity_registry.get_daily_touches(vpa) == 2
        assert customer_identity_registry.get_daily_touches(cust_id) == 2


class TestCrossAliasSpendPatternHistory:

    def setup_method(self):
        customer_identity_registry.reset()
        spend_pattern_tracker.reset_history()

    def test_transaction_recorded_by_cust_id_visible_by_vpa(self):
        vpa = "user_cross@oksbi"
        cust_id = "CUST-CROSS-88"

        # Record transactions under customer ID
        spend_pattern_tracker.record_transaction(
            vpa="",
            amount=150.0,
            customer_id=cust_id,
        )
        spend_pattern_tracker.record_transaction(
            vpa="",
            amount=180.0,
            customer_id=cust_id,
        )

        # Link identifiers
        customer_identity_registry.link_identifiers(vpa, cust_id)

        # Query history by VPA
        hist = spend_pattern_tracker.get_history(vpa)
        assert hist == [150.0, 180.0]

        # Profile computed on VPA
        profile = spend_pattern_tracker.get_profile(vpa)
        assert profile.transaction_count == 2
        assert profile.mean_amount == 165.0

    def test_archetype_spend_history_accessible_by_customer_id(self):
        # "CUST-SBI-001" maps to Rahul Sharma archetype (seeded history [99, 149, 110, 100, ...])
        hist = spend_pattern_tracker.get_history(vpa="", customer_id="CUST-SBI-001")
        assert len(hist) > 0
        assert hist[0] == 99.0

        prof = spend_pattern_tracker.get_profile(vpa="", customer_id="CUST-SBI-001")
        assert prof.transaction_count > 0
        assert prof.mean_amount > 0


class TestCrossAliasPromiseTracker:

    def setup_method(self):
        customer_identity_registry.reset()
        promise_tracker._promises.clear()

    def test_promise_created_by_vpa_recognized_by_customer_id(self):
        vpa = "rahul@oksbi"
        cust_id = "CUST-SBI-001"

        p = promise_tracker.create(
            vpa=vpa,
            amount=999.0,
            bank="SBI",
            failure_code="U30",
            deadline_hours=24,
            notes="Salary commitment",
        )

        # has_active queried with customer_id should find the promise
        assert promise_tracker.has_active(cust_id, 999.0) is True
        assert promise_tracker.has_active(vpa, 999.0) is True

        # Payer trust score computed with customer_id should reflect the pending promise
        ts_vpa = promise_tracker.payer_trust_score(vpa)
        ts_cust = promise_tracker.payer_trust_score(cust_id)
        assert ts_vpa == ts_cust

        # Fulfill active using customer_id
        fulfilled = promise_tracker.fulfill_active(cust_id, 999.0)
        assert fulfilled is not None
        assert fulfilled.status == PromiseStatus.FULFILLED

        # Now trust score should increase for both aliases
        assert promise_tracker.payer_trust_score(vpa) > 0.50
        assert promise_tracker.payer_trust_score(cust_id) > 0.50


class TestCrossAliasSuppression:

    def setup_method(self):
        customer_identity_registry.reset()
        suppression_registry.reset()

    @pytest.mark.asyncio
    async def test_whatsapp_inbound_wrong_number_suppresses_vpa_and_cust_id(self):
        phone = "+919800000001"
        vpa = "rahul@oksbi"
        cust_id = "CUST-SBI-001"

        # Customer sends "galat number hai opt out" via WhatsApp phone
        res = await whatsapp_inbound_handler.handle_inbound(
            from_phone=phone,
            customer_vpa=vpa,
            message="Galat number hai, stop messaging me not my account",
        )
        assert res.intent == InboundIntent.WRONG_NUMBER

        # Suppression should be active for phone, VPA, and customer ID
        is_supp_phone, reason_phone = suppression_registry.is_suppressed(phone)
        is_supp_vpa, reason_vpa = suppression_registry.is_suppressed(vpa)
        is_supp_cust, reason_cust = suppression_registry.is_suppressed(cust_id)

        assert is_supp_phone is True
        assert is_supp_vpa is True
        assert is_supp_cust is True
        assert "wrong_number" in reason_vpa

        # Decision engine should block all retries and nudges
        engine = DecisionEngine()
        dec = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=999.0,
            customer_vpa=vpa,
            customer_id=cust_id,
        )
        assert dec.approved is False
        assert "compliance_blacklist_wrong_number" in dec.guardrails_fired


class TestCustomerHistoryAPI:

    @pytest.fixture(autouse=True)
    def setup_client(self):
        customer_identity_registry.reset()
        spend_pattern_tracker.reset_history()
        promise_tracker._promises.clear()
        suppression_registry.reset()
        store.reset()
        self.client = TestClient(app)

    def test_get_customer_history_endpoint(self):
        # Create an event in store for Rahul
        ev = RecoveryEvent(
            id="EVT-TEST-001",
            timestamp="10:00:00",
            event_type="mandate.execution.failed",
            failure_code="U30",
            failure_reason="Insufficient funds",
            customer_id="CUST-SBI-001",
            customer_vpa="rahul@oksbi",
            bank="SBI",
            amount=999.0,
            severity="medium",
            interventions=["smart_retry"],
            intervention_msgs=["Scheduled retry"],
            scheduled_at=None,
            action_url=None,
            success=True,
            status="recovered",
            amount_recovered=999.0,
        )
        import asyncio
        asyncio.run(store.add_event(ev))

        # Query customer history by customer_id
        resp = self.client.get("/api/customer/CUST-SBI-001/history")
        assert resp.status_code == 200
        data = resp.json()

        assert data["canonical_id"] == "cust:rahul@oksbi"
        assert len(data["spend_history"]) > 0
        assert data["spend_profile"]["mean_amount"] > 0
        assert data["total_events_count"] == 1
        assert data["events"][0]["id"] == "EVT-TEST-001"

    def test_list_customers_endpoint(self):
        resp = self.client.get("/api/customers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_customers"] > 0
        assert any(c["canonical_id"] == "cust:rahul@oksbi" for c in data["customers"])
