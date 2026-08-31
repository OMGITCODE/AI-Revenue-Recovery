"""
test_messaging.py — Tests for Outbound Messaging & Twilio Inbound Webhook
========================================================================
Validates:
1. MessagingClient in mock mode (zero network calls, safe defaults).
2. MessagingClient fallback on connection/API error (never crashes pipeline).
3. Demo phone override mechanism.
4. Integration with WhatsAppNudgeIntervention.
5. Inbound Twilio webhook (POST /api/webhook/whatsapp/twilio with Form-encoded data).
6. Phone normalization and intent dispatch matching the JSON webhook behaviour.
"""

import pytest
from fastapi.testclient import TestClient
from api.main import app
from src.integrations.messaging import MessagingClient, MessageResult, messenger
from src.agent.upi_interventions import WhatsAppNudgeIntervention
from src.models.risk_models import RevenueRisk, RiskType, RiskSeverity
from src.models.upi_models import UPIFailureCode
from src.agent.promise_tracker import promise_tracker
from src.agent.whatsapp_inbound import suppression_registry, InboundIntent


client = TestClient(app)


class TestMessagingClient:
    def test_mock_mode_default(self):
        m = MessagingClient(force_mock=True)
        assert not m.is_live
        res = m.send_whatsapp(to="+919876543210", body="Test message")
        assert isinstance(res, MessageResult)
        assert res.channel == "whatsapp"
        assert res.to == "+919876543210"
        assert res.body == "Test message"
        assert res.sent is False
        assert res.mode == "mock"
        assert res.error is None

    def test_sms_mock_mode(self):
        m = MessagingClient(force_mock=True)
        res = m.send_sms(to="+919876543210", body="SMS Alert")
        assert res.channel == "sms"
        assert res.sent is False
        assert res.mode == "mock"

    def test_demo_override_in_mock(self):
        m = MessagingClient(force_mock=True)
        m.demo_whatsapp_override = "whatsapp:+919999999999"
        # in mock mode, logging shows target
        res = m.send_whatsapp(to="+919800000001", body="Payment link")
        assert res.mode == "mock"

    def test_error_graceful_fallback_in_live_mode(self, monkeypatch):
        # Even if someone constructs client with invalid credentials, sending fails gracefully
        m = MessagingClient(force_mock=False)
        m._client = object()  # dummy client without .messages
        res = m.send_whatsapp(to="+919876543210", body="Test fail")
        assert res.sent is False
        assert res.mode == "mock"
        assert res.error is not None


class TestWhatsAppInterventionWithMessenger:
    @pytest.mark.asyncio
    async def test_nudge_uses_messenger(self, monkeypatch):
        # Guarantee mock mode by construction for the intervention module's messenger
        mock_messenger = MessagingClient(force_mock=True)
        monkeypatch.setattr("src.agent.upi_interventions.messenger", mock_messenger)

        from datetime import datetime, timezone
        nudge = WhatsAppNudgeIntervention()
        risk = RevenueRisk(
            id="risk_test_001",
            risk_type=RiskType.PAYMENT_FAILURE,
            severity=RiskSeverity.HIGH,
            amount=999.0,
            currency="INR",
            customer_id="cust_123",
            detected_at=datetime.now(timezone.utc),
            metadata={"failure_code": UPIFailureCode.U30, "customer_vpa": "rahul@oksbi"},
        )
        assert nudge.can_handle(risk) is True
        result = await nudge.execute(risk)
        assert result.success is True
        assert result.metadata.get("delivery_mode") == "mock"
        assert result.metadata.get("sent_live") is False
        assert "rahul@oksbi" in result.message


class TestTwilioInboundWebhook:
    def setup_method(self):
        suppression_registry.reset()
        promise_tracker._promises.clear()

    def test_twilio_webhook_promise_intent(self):
        # Twilio sends application/x-www-form-urlencoded data
        form_data = {
            "From": "whatsapp:+919876543210",
            "Body": "Bhai kal pakka pay kar dunga",
        }
        response = client.post("/api/webhook/whatsapp/twilio", data=form_data)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["intent"] == "promise"
        assert "Shukriya" in data["reply"]

        # Verify Promise-to-Pay was created in tracker
        active = promise_tracker.active_promises_for_vpa("user_3210@upi")
        assert len(active) == 1
        assert active[0].amount == 999.0

    def test_twilio_webhook_already_paid(self):
        form_data = {
            "From": "whatsapp:+919876543210",
            "Body": "Mera account se ₹999 kat gaya hai check bank statement",
        }
        response = client.post("/api/webhook/whatsapp/twilio", data=form_data)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["intent"] == "already_paid"
        assert "Dhanyawaad" in data["reply"]

        # Verify hold was placed
        is_suppressed, reason = suppression_registry.is_suppressed("+919876543210")
        assert is_suppressed is True
        assert "already_paid" in reason

    def test_twilio_webhook_dispute(self):
        form_data = {
            "From": "whatsapp:+919876543210",
            "Body": "Maine cancel kar diya tha, refund karo fraud mat karo",
        }
        response = client.post("/api/webhook/whatsapp/twilio", data=form_data)
        assert response.status_code == 200
        data = response.json()
        assert data["intent"] == "dispute"
        assert "dispute note" in data["reply"].lower()

    def test_twilio_webhook_phone_normalization(self):
        # Test various phone formats from Twilio
        form_data = {
            "From": "  whatsapp:+919811223344  ",
            "Body": "Galat number hai bhai stop messaging me",
        }
        response = client.post("/api/webhook/whatsapp/twilio", data=form_data)
        assert response.status_code == 200
        data = response.json()
        assert data["intent"] == "wrong_number"

        # Verify blacklist
        is_suppressed, reason = suppression_registry.is_suppressed("+919811223344")
        assert is_suppressed is True
        assert "wrong_number" in reason

    def test_razorpay_webhook_signature_verification(self, monkeypatch):
        import hmac
        import hashlib
        import json

        secret = "rzp_test_secret_xyz"
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", secret)

        payload = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_sig_test_001",
                        "amount": 99900,
                        "currency": "INR",
                        "status": "failed",
                        "method": "upi",
                        "vpa": "signature_test@oksbi",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Payment failed",
                        "error_reason": "payment_failed",
                        "notes": {"failure_code": "U30", "bank": "SBI"},
                    }
                }
            }
        }
        body_bytes = json.dumps(payload).encode("utf-8")

        # 1. Valid Signature -> 200 OK
        valid_sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        res_valid = client.post(
            "/api/webhook",
            content=body_bytes,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": valid_sig},
        )
        assert res_valid.status_code == 200

        # 2. Invalid Signature -> 401 Unauthorized
        res_invalid = client.post(
            "/api/webhook",
            content=body_bytes,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": "invalid_tampered_sig"},
        )
        assert res_invalid.status_code == 401
        assert "Invalid Razorpay webhook signature" in res_invalid.json()["detail"]

    def test_twilio_webhook_signature_verification(self, monkeypatch):
        import hmac
        import hashlib
        import base64

        auth_token = "twilio_auth_token_secret_123"
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", auth_token)

        form_data = {
            "From": "whatsapp:+919876543210",
            "Body": "Kal payment kar dunga pakka",
        }
        url = "http://testserver/api/webhook/whatsapp/twilio"

        # Compute valid Twilio HMAC-SHA1 signature
        s = url
        for k in sorted(form_data.keys()):
            s += f"{k}{form_data[k]}"
        valid_sig = base64.b64encode(
            hmac.new(auth_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")

        # 1. Valid Signature -> 200 OK
        res_valid = client.post(
            "/api/webhook/whatsapp/twilio",
            data=form_data,
            headers={"X-Twilio-Signature": valid_sig},
        )
        assert res_valid.status_code == 200
        assert res_valid.json()["status"] == "ok"

        # 2. Invalid Signature -> 401 Unauthorized
        res_invalid = client.post(
            "/api/webhook/whatsapp/twilio",
            data=form_data,
            headers={"X-Twilio-Signature": "invalid_forged_twilio_signature"},
        )
        assert res_invalid.status_code == 401
        assert "Invalid Twilio webhook signature" in res_invalid.json()["detail"]


class TestAPISecurityAndAuth:
    def test_security_headers_present(self):
        res = client.get("/api/health")
        assert res.status_code == 200
        assert res.headers.get("X-Content-Type-Options") == "nosniff"
        assert res.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert res.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_api_key_auth_environment_control(self, monkeypatch):
        api_key = "prod_api_key_secret_888"
        monkeypatch.setenv("RECOVERIQ_API_KEY", api_key)

        # 1. Public route accessible without API key
        res_public = client.get("/api/health")
        assert res_public.status_code == 200

        # 2. Protected control route blocked without API key -> 401
        res_blocked = client.post("/api/reset")
        assert res_blocked.status_code == 401
        assert "Unauthorized" in res_blocked.json()["detail"]

        # 3. Protected control route blocked with invalid API key -> 401
        res_invalid = client.post("/api/reset", headers={"X-API-Key": "wrong_key"})
        assert res_invalid.status_code == 401

        # 4. Protected control route allowed with valid X-API-Key -> 200
        res_valid_header = client.post("/api/reset", headers={"X-API-Key": api_key})
        assert res_valid_header.status_code == 200
        assert res_valid_header.json()["status"] == "reset"

        # 5. Protected control route allowed with valid Bearer token -> 200
        res_valid_bearer = client.post("/api/reset", headers={"Authorization": f"Bearer {api_key}"})
        assert res_valid_bearer.status_code == 200
        assert res_valid_bearer.json()["status"] == "reset"

    def test_state_mutating_and_pii_routes_require_api_key(self, monkeypatch):
        api_key = "prod_api_key_secret_888"
        monkeypatch.setenv("RECOVERIQ_API_KEY", api_key)

        # Test exact routes reported in issue:
        # A. State Mutating: POST /api/b2b/receivables -> 401 without key, 200 with key
        b2b_payload = {
            "debtor_name": "Secure Acme Corp",
            "debtor_vpa": "acme@okhdfcbank",
            "debtor_phone": "+919876543210",
            "invoice_number": "INV-SEC-001",
            "amount": 75000.0,
            "due_date": "2026-08-01",
        }
        res_b2b_blocked = client.post("/api/b2b/receivables", json=b2b_payload)
        assert res_b2b_blocked.status_code == 401
        res_b2b_auth = client.post("/api/b2b/receivables", json=b2b_payload, headers={"X-API-Key": api_key})
        assert res_b2b_auth.status_code == 200
        rec_id = res_b2b_auth.json()["receivable_id"]

        # B. State Mutating: POST /api/b2b/receivables/{id}/settle -> 401 without key, 200 with key
        res_settle_blocked = client.post(f"/api/b2b/receivables/{rec_id}/settle")
        assert res_settle_blocked.status_code == 401
        res_settle_auth = client.post(f"/api/b2b/receivables/{rec_id}/settle", headers={"X-API-Key": api_key})
        assert res_settle_auth.status_code == 200

        # C. PII-exposing: GET /api/customers -> 401 without key, 200 with key
        res_cust_blocked = client.get("/api/customers")
        assert res_cust_blocked.status_code == 401
        res_cust_auth = client.get("/api/customers", headers={"X-API-Key": api_key})
        assert res_cust_auth.status_code == 200

        # D. PII-exposing: GET /api/customer/{identifier}/history -> 401 without key, 200 with key
        res_hist_blocked = client.get("/api/customer/rahul@oksbi/history")
        assert res_hist_blocked.status_code == 401
        res_hist_auth = client.get("/api/customer/rahul@oksbi/history", headers={"X-API-Key": api_key})
        assert res_hist_auth.status_code == 200

        # E. AI Execution: POST /api/decide -> 401 without key, 200 with key
        decide_payload = {
            "failure_code": "U30",
            "mandate_state": "active",
            "amount": 1000.0,
            "customer_vpa": "test@upi",
        }
        res_decide_blocked = client.post("/api/decide", json=decide_payload)
        assert res_decide_blocked.status_code == 401
        res_decide_auth = client.post("/api/decide", json=decide_payload, headers={"X-API-Key": api_key})
        assert res_decide_auth.status_code == 200

        # F. Read-only stats and telemetry remain public
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/stats").status_code == 200
        assert client.get("/api/scenarios").status_code == 200
        assert client.get("/api/events").status_code == 200

