"""
tests/test_setu_aa_api.py — Verification for Setu Account Aggregator Endpoint
=============================================================================
Tests:
1. Successful balance fetch with valid VPA and default U30.
2. Balance fetch with custom amount and bank.
3. VPA format validation (rejects invalid VPAs with 422).
4. Security & API Key gating:
   - 401 when RECOVERIQ_API_KEY is configured and no key is provided.
   - 200 when RECOVERIQ_API_KEY is configured and valid X-API-Key header is sent.
   - Open access in default development mode (no key configured).
"""

import pytest
from starlette.testclient import TestClient
from api.main import app
from src.config import settings


@pytest.fixture
def client():
    return TestClient(app)


def test_setu_check_balance_success(client):
    """Verify valid Setu AA check returns complete response structure."""
    payload = {
        "vpa": "rahul.sharma@oksbi",
        "amount_due": 1499.0,
        "bank": "State Bank of India",
        "failure_code": "U30",
    }
    res = client.post("/api/setu/check-balance", json=payload)
    assert res.status_code == 200
    data = res.json()

    assert data["consent_id"].startswith("CON-")
    assert "bridge.setu.co/aa-sandbox/consent" in data["consent_url"]
    assert data["vpa"] == "rahul.sharma@oksbi"
    assert data["bank"] == "State Bank of India"
    assert isinstance(data["balance"], (int, float))
    assert isinstance(data["funds_available"], bool)
    assert data["amount_due"] == 1499.0
    assert data["source"] == "setu_aa_sandbox"
    assert "AA sandbox" in data["note"]
    assert "timestamp" in data


def test_setu_check_balance_different_failure_code(client):
    """Verify Setu AA check handles non-U30 codes (e.g. TM technical error) properly."""
    payload = {
        "vpa": "arjun@okicici",
        "amount_due": 4500.0,
        "bank": "ICICI",
        "failure_code": "TM",
    }
    res = client.post("/api/setu/check-balance", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["funds_available"] is True
    assert data["balance"] >= 4500.0
    assert "Sufficient balance verified" in data["note"]


def test_setu_check_balance_invalid_vpa(client):
    """Verify validation error when VPA is malformed."""
    payload = {
        "vpa": "invalid_vpa_without_at",
        "amount_due": 500.0,
    }
    res = client.post("/api/setu/check-balance", json=payload)
    assert res.status_code == 422


def test_setu_check_balance_api_key_gating(client, monkeypatch):
    """
    Verify endpoint is protected under SecurityAndAuthMiddleware.
    When RECOVERIQ_API_KEY is configured, requests without key must get 401.
    """
    monkeypatch.setattr(settings, "recoveriq_api_key", "secret-test-key-999")

    payload = {
        "vpa": "arun@okaxis",
        "amount_due": 2000.0,
    }

    # 1. Without header -> 401
    res_no_key = client.post("/api/setu/check-balance", json=payload)
    assert res_no_key.status_code == 401

    # 2. With invalid header -> 401
    res_bad_key = client.post(
        "/api/setu/check-balance",
        json=payload,
        headers={"X-API-Key": "wrong-key"},
    )
    assert res_bad_key.status_code == 401

    # 3. With valid header -> 200
    res_valid_key = client.post(
        "/api/setu/check-balance",
        json=payload,
        headers={"X-API-Key": "secret-test-key-999"},
    )
    assert res_valid_key.status_code == 200
    assert res_valid_key.json()["vpa"] == "arun@okaxis"
