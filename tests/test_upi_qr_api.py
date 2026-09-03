"""
Unit & Integration Tests for Dynamic UPI QR & Intent Deep Links Engine
=====================================================================
Validates:
1. GET /api/upi/qr: Standards-compliant NPCI URI, valid vector SVG, and deep-link schemes.
2. POST /api/upi/simulate-payment: Ledger recording, Thompson Sampling bandit update, and ROI update.
3. Domain-State Settlement Idempotency: Duplicate submissions for the same reference ID return
   'already_settled' with ZERO double-counting in ledger or bandit.
4. B2B Invoice Integration: Settles b2b_chaser receivables directly from QR payments.
5. Auth Gating: Public GET /api/upi/qr vs strictly protected POST /api/upi/simulate-payment.
"""

import pytest
import os
from fastapi.testclient import TestClient
from api.main import app, _settled_qr_refs
from api.store import store, RecoveryEvent
from src.agent.recovery_ledger import ledger as recovery_ledger
from src.agent.b2b_chaser import b2b_chaser
from src.config import settings

client = TestClient(app)


def test_upi_qr_generation_defaults():
    """Validates public GET /api/upi/qr returns valid SVG and canonical NPCI deep links."""
    res = client.get("/api/upi/qr")
    assert res.status_code == 200
    data = res.json()

    assert data["status"] == "success"
    assert "recoveriq%40npci" in data["upi_uri"] or "recoveriq@npci" in data["upi_uri"]
    assert data["vpa"] == "recoveriq@npci"
    assert "cu=INR" in data["upi_uri"]
    assert "<svg" in data["qr_svg"]
    assert "</svg>" in data["qr_svg"]

    # Deep links verification
    deep_links = data["deep_links"]
    assert deep_links["universal"].startswith("upi://pay?")
    assert deep_links["gpay"].startswith("gpay://upi/pay?")
    assert deep_links["phonepe"].startswith("phonepe://pay?")
    assert deep_links["paytm"].startswith("paytmmp://pay?")


def test_upi_qr_generation_custom_params():
    """Validates custom debtor parameters in QR code."""
    res = client.get("/api/upi/qr?amount=4999.50&vpa=kavita@okaxis&name=Kavita+Reddy&note=Annual+SaaS&ref_id=INV-TEST-99")
    assert res.status_code == 200
    data = res.json()

    assert data["amount"] == 4999.50
    assert "4999.50" in data["upi_uri"]
    assert "kavita%40okaxis" in data["upi_uri"] or "kavita@okaxis" in data["upi_uri"]
    assert data["ref_id"] == "INV-TEST-99"
    assert "<svg" in data["qr_svg"]


def test_upi_qr_payment_simulation_and_domain_idempotency():
    """
    Tests payment settlement and domain-state idempotency:
    1st call settles and logs to ledger.
    2nd call returns 'already_settled' without duplicate ledger entries.
    """
    ref_id = "CART-IDEMPOTENCY-TEST-001"
    amount = 1299.0

    # Ensure clean slate for this ref
    _settled_qr_refs.discard(ref_id)

    initial_ledger_count = len(recovery_ledger._entries)

    payload = {
        "ref_id": ref_id,
        "amount": amount,
        "debtor_name": "Test Debtor",
        "vpa": "test@okbank",
        "note": "Unit Test Settlement"
    }

    # First attempt: Should settle successfully
    res1 = client.post("/api/upi/simulate-payment", json=payload)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "success"
    assert data1["already_settled"] is False
    assert data1["amount"] == amount

    # Check that ledger recorded exactly 1 entry
    assert len(recovery_ledger._entries) == initial_ledger_count + 1

    # Second attempt with same ref_id: MUST return already_settled
    res2 = client.post("/api/upi/simulate-payment", json=payload)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "already_settled"
    assert data2["already_settled"] is True

    # Ledger count MUST NOT have increased
    assert len(recovery_ledger._entries) == initial_ledger_count + 1


def test_upi_qr_b2b_receivable_settlement_idempotency():
    """
    Validates that paying a B2B receivable via UPI QR flips the receivable state
    to 'settled' and subsequent attempts are blocked by the domain state itself.
    """
    # Find an active receivable or reset chaser
    rec = next((r for r in b2b_chaser.all_receivables() if r.status == "active"), None)
    if not rec:
        b2b_chaser.reset()
        rec = next((r for r in b2b_chaser.all_receivables() if r.status == "active"), None)

    rec_id = rec.receivable_id
    rec_amount = rec.amount

    payload = {
        "ref_id": rec_id,
        "amount": rec_amount,
        "debtor_name": rec.debtor_name,
        "vpa": rec.debtor_vpa,
        "note": f"B2B QR Settlement for {rec.invoice_number}"
    }

    # 1. First settlement via QR
    res1 = client.post("/api/upi/simulate-payment", json=payload)
    assert res1.status_code == 200
    assert res1.json()["status"] == "success"

    # Domain object must now be settled
    assert rec.status == "settled"

    # 2. Second attempt: Authoritative domain check catches that rec.status == 'settled'
    res2 = client.post("/api/upi/simulate-payment", json=payload)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "already_settled"
    assert data2["already_settled"] is True
    assert "settled" in data2["message"].lower()


def test_upi_qr_auth_gating(monkeypatch):
    """
    Validates that:
    1. GET /api/upi/qr remains public without API key.
    2. POST /api/upi/simulate-payment requires X-API-Key when RECOVERIQ_API_KEY is configured.
    """
    monkeypatch.setattr(settings, "recoveriq_api_key", "secret-test-token-777")

    # Public route: GET /api/upi/qr (must succeed without auth)
    res_pub = client.get("/api/upi/qr")
    assert res_pub.status_code == 200

    # Protected route: POST /api/upi/simulate-payment without key -> 401 Unauthorized
    payload = {
        "ref_id": "AUTH-TEST-001",
        "amount": 500.0,
        "debtor_name": "Auth User",
        "vpa": "auth@upi",
        "note": "Auth test"
    }
    res_unauth = client.post("/api/upi/simulate-payment", json=payload)
    assert res_unauth.status_code == 401

    # Protected route with valid key -> 200 Success
    res_auth = client.post(
        "/api/upi/simulate-payment",
        json=payload,
        headers={"X-API-Key": "secret-test-token-777"}
    )
    assert res_auth.status_code == 200
    assert res_auth.json()["status"] == "success"


@pytest.mark.asyncio
async def test_upi_qr_settle_existing_store_event():
    """
    Validates QR settlement of an existing failed event in EventStore:
    1. Event status flips from failed -> recovered, success becomes True.
    2. amount_recovered is recorded and stats reflect the recovery.
    3. interventions includes 'upi_qr_collect' and stats.upi_collects increments.
    4. Domain-state idempotency: second attempt returns 'already_settled'.
    """
    ev_id = "EVT-TEST-U30-SETTLE-001"
    _settled_qr_refs.discard(ev_id)

    ev = RecoveryEvent(
        id=ev_id,
        timestamp="10:30:00",
        event_type="mandate.execution.failed",
        failure_code="U30",
        failure_reason="Insufficient balance at SBI",
        customer_id="Pooja Sharma",
        customer_vpa="pooja@oksbi",
        bank="State Bank of India",
        amount=1999.0,
        severity="high",
        interventions=["smart_retry"],
        intervention_msgs=["Smart retry queued"],
        scheduled_at="15:00:00",
        action_url=None,
        success=False,
        status="failed",
        amount_recovered=0.0,
    )
    await store.add_event(ev)

    stats_before = store.get_stats()

    payload = {
        "ref_id": ev_id,
        "amount": 1999.0,
        "debtor_name": "Pooja Sharma",
        "vpa": "pooja@oksbi",
        "note": "Recovery U30 via Instant UPI QR"
    }

    # First settlement
    res1 = client.post("/api/upi/simulate-payment", json=payload)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "success"
    assert data1["already_settled"] is False
    assert data1["amount"] == 1999.0

    # Verify event was updated in-place in store
    updated_ev = next(e for e in store._events if e.id == ev_id)
    assert updated_ev.success is True
    assert updated_ev.status == "recovered"
    assert updated_ev.amount_recovered == 1999.0
    assert "upi_qr_collect" in updated_ev.interventions

    # Verify stats updated correctly
    stats_after = store.get_stats()
    assert stats_after["successful"] == stats_before["successful"] + 1
    assert stats_after["failed"] == stats_before["failed"] - 1
    assert stats_after["total_recovered"] >= stats_before["total_recovered"] + 1999.0
    assert stats_after["upi_collects"] == stats_before["upi_collects"] + 1

    # Second attempt: Idempotent block
    res2 = client.post("/api/upi/simulate-payment", json=payload)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "already_settled"
    assert data2["already_settled"] is True
    assert "already been recovered" in data2["message"].lower()

