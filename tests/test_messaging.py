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
from src.agent.detector import RevenueRisk, RiskType, RiskSeverity
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
    async def test_nudge_uses_messenger(self):
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
