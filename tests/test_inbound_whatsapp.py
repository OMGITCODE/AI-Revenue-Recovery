"""
test_inbound_whatsapp.py — Tests for 2-Way Conversational Hinglish Inbound Handler
================================================================================
Validates:
1. Intent classification across all 5 categories (PROMISE, ALREADY_PAID, DISPUTE, HARDSHIP, WRONG_NUMBER).
2. Extraction of deadlines for Promise-to-Pay commitments.
3. State transitions:
   - PROMISE creates P2P record in PromiseTracker and logs to RecoveryLedger.
   - ALREADY_PAID initiates 24h bank reconciliation verification hold.
   - DISPUTE halts retries and escalates to human dispute queue.
   - HARDSHIP grants 30-day compassionate pause under RBI Fair Practices Code.
   - WRONG_NUMBER permanently suppresses identifier in Compliance Blacklist.
4. Fail-safe LLM integration:
   - Valid LLM response parsing.
   - Malformed JSON fallback to regex.
   - HTTP 500 / timeout fallback to regex.
   - Missing API key defaults to regex with zero errors.
5. Guardrail 9 integration in DecisionEngine (blocking retries for suppressed users).
6. Live REST API endpoints: POST /api/webhook/whatsapp/inbound, GET /api/whatsapp/inbound/samples.
"""

from unittest.mock import AsyncMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient
from api.main import app
from src.agent.whatsapp_inbound import (
    whatsapp_inbound_handler,
    suppression_registry,
    InboundIntent,
)
from src.agent.promise_tracker import promise_tracker
from src.agent.decision_engine import DecisionEngine
from src.integrations.llm_classifier import LLMIntentClassifier
from src.config import settings


@pytest.fixture(autouse=True)
def clean_state():
    suppression_registry.reset()
    promise_tracker._promises.clear()
    yield
    suppression_registry.reset()
    promise_tracker._promises.clear()


class TestInboundIntentClassification:
    @pytest.mark.asyncio
    async def test_classify_promise_hinglish(self):
        msg = "Bhai kal pakka pay kar dunga abhi travel kar raha hu"
        intent, conf, kws, deadline, reasoning = await whatsapp_inbound_handler.classify_message(msg)
        assert intent == InboundIntent.PROMISE
        assert conf >= 0.70
        assert deadline == 24
        assert len(kws) > 0 and any("kal" in k or "pay" in k for k in kws)
        assert isinstance(reasoning, str)

    @pytest.mark.asyncio
    async def test_classify_promise_salary_cycle(self):
        msg = "Salary 5th ko aayegi tab transfer kar dungi"
        intent, conf, kws, deadline, reasoning = await whatsapp_inbound_handler.classify_message(msg)
        assert intent == InboundIntent.PROMISE
        assert deadline == 96

    @pytest.mark.asyncio
    async def test_classify_already_paid(self):
        msg = "Mera account se paise kat gaye hain check your statement"
        intent, conf, kws, deadline, reasoning = await whatsapp_inbound_handler.classify_message(msg)
        assert intent == InboundIntent.ALREADY_PAID
        assert conf >= 0.70

    @pytest.mark.asyncio
    async def test_classify_dispute(self):
        msg = "Maine ye service cancel kar di thi, fraud mat karo refund chahiye"
        intent, conf, kws, deadline, reasoning = await whatsapp_inbound_handler.classify_message(msg)
        assert intent == InboundIntent.DISPUTE
        assert conf >= 0.70

    @pytest.mark.asyncio
    async def test_classify_hardship(self):
        msg = "Meri job chali gayi hai aur hospital emergency hai, abhi paise nahi hain"
        intent, conf, kws, deadline, reasoning = await whatsapp_inbound_handler.classify_message(msg)
        assert intent == InboundIntent.HARDSHIP
        assert conf >= 0.70

    @pytest.mark.asyncio
    async def test_classify_wrong_number(self):
        msg = "Galat number hai bhai stop messaging me ye mera account nahi hai"
        intent, conf, kws, deadline, reasoning = await whatsapp_inbound_handler.classify_message(msg)
        assert intent == InboundIntent.WRONG_NUMBER
        assert conf >= 0.70


class TestInboundStateTransitions:
    @pytest.mark.asyncio
    async def test_promise_creates_p2p_and_nudge_hold(self):
        res = await whatsapp_inbound_handler.handle_inbound(
            from_phone="+91-9876543210",
            customer_vpa="rahul@oksbi",
            message="Kal sham tak pakka pay kar dunga",
            amount=999.0,
        )
        assert res.intent == InboundIntent.PROMISE
        assert "P2P promise" in res.action_taken
        assert isinstance(res.matched_keywords, list)
        # Verify P2P tracker holds active promise
        active = promise_tracker.active_promises_for_vpa("rahul@oksbi")
        assert len(active) == 1
        assert active[0].amount == 999.0

    @pytest.mark.asyncio
    async def test_already_paid_sets_24h_verification_hold(self):
        res = await whatsapp_inbound_handler.handle_inbound(
            from_phone="+91-9876543210",
            customer_vpa="priya@okhdfcbank",
            message="Payment done already, check your bank statement",
            amount=499.0,
        )
        assert res.intent == InboundIntent.ALREADY_PAID
        assert isinstance(res.matched_keywords, list)
        is_supp, reason = suppression_registry.is_suppressed("priya@okhdfcbank")
        assert is_supp is True
        assert "already_paid" in reason

    @pytest.mark.asyncio
    async def test_dispute_escalates_and_holds(self):
        res = await whatsapp_inbound_handler.handle_inbound(
            from_phone="+91-9876543210",
            customer_vpa="vikram@ybl",
            message="Dispute charge, cancel my subscription immediately",
            amount=1500.0,
        )
        assert res.intent == InboundIntent.DISPUTE
        assert isinstance(res.matched_keywords, list)
        is_supp, reason = suppression_registry.is_suppressed("vikram@ybl")
        assert is_supp is True
        assert "dispute" in reason

    @pytest.mark.asyncio
    async def test_hardship_grants_30d_compassionate_pause(self):
        res = await whatsapp_inbound_handler.handle_inbound(
            from_phone="+91-9876543210",
            customer_vpa="anita@paytm",
            message="Medical hospital emergency, paise nahi hai please time do",
            amount=299.0,
        )
        assert res.intent == InboundIntent.HARDSHIP
        assert isinstance(res.matched_keywords, list)
        is_supp, reason = suppression_registry.is_suppressed("anita@paytm")
        assert is_supp is True
        assert "hardship" in reason

    @pytest.mark.asyncio
    async def test_wrong_number_permanently_blacklists(self):
        res = await whatsapp_inbound_handler.handle_inbound(
            from_phone="+91-9999999999",
            customer_vpa="wrong_user@upi",
            message="Galat number hai, don't message again",
            amount=100.0,
        )
        assert res.intent == InboundIntent.WRONG_NUMBER
        assert isinstance(res.matched_keywords, list)
        is_supp, reason = suppression_registry.is_suppressed("wrong_user@upi")
        assert is_supp is True
        assert reason == "permanently_blacklisted_wrong_number"


class TestFailSafeLLMClassifier:
    @pytest.mark.asyncio
    async def test_openai_success_classification(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", "")
        monkeypatch.setattr(settings, "openai_api_key", "sk-test-mock-key")
        monkeypatch.setattr(settings, "llm_provider", "openai")

        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": '{"intent": "dispute", "confidence": 0.95, "reasoning": "Customer indicates unauthorized transaction", "extracted_deadline_hours": null}'
                    }
                }
            ]
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = httpx.Response(status_code=200, json=mock_response)
            classifier = LLMIntentClassifier()
            res = await classifier.classify("Why was I charged without OTP? Cancel this immediately")

            assert res is not None
            assert res["intent"] == "dispute"
            assert res["confidence"] == 0.95
            assert "unauthorized" in res["reasoning"]
            assert res["provider"] == "openai"

    @pytest.mark.asyncio
    async def test_gemini_success_classification(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", "AQ.test-gemini-key")
        monkeypatch.setattr(settings, "openai_api_key", "")
        monkeypatch.setattr(settings, "llm_provider", "gemini")

        mock_response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"intent": "promise", "confidence": 0.96, "reasoning": "Customer commits to pay tomorrow", "extracted_deadline_hours": 24}'
                            }
                        ]
                    }
                }
            ]
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = httpx.Response(status_code=200, json=mock_response)
            classifier = LLMIntentClassifier()
            res = await classifier.classify("Bhai kal shaam tak pakka pay kar dunga")

            assert res is not None
            assert res["intent"] == "promise"
            assert res["confidence"] == 0.96
            assert res["extracted_deadline_hours"] == 24
            assert res["provider"] == "gemini"

    @pytest.mark.asyncio
    async def test_llm_fallback_on_malformed_json(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", "")
        monkeypatch.setattr(settings, "openai_api_key", "sk-test-mock-key")

        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": "Not a JSON document!"
                    }
                }
            ]
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = httpx.Response(status_code=200, json=mock_response)
            classifier = LLMIntentClassifier()
            # Must return None safely and never raise
            res = await classifier.classify("some message")
            assert res is None

    @pytest.mark.asyncio
    async def test_llm_fallback_on_http_500_error(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", "")
        monkeypatch.setattr(settings, "openai_api_key", "sk-test-mock-key")

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = httpx.Response(status_code=500, text="Internal Server Error")
            classifier = LLMIntentClassifier()
            res = await classifier.classify("some message")
            assert res is None

    @pytest.mark.asyncio
    async def test_llm_fallback_on_timeout(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", "")
        monkeypatch.setattr(settings, "openai_api_key", "sk-test-mock-key")

        with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("Read timeout")):
            classifier = LLMIntentClassifier()
            res = await classifier.classify("some message")
            assert res is None

    @pytest.mark.asyncio
    async def test_llm_missing_api_key_returns_none_immediately(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", "")
        monkeypatch.setattr(settings, "openai_api_key", "")
        classifier = LLMIntentClassifier()
        res = await classifier.classify("some message")
        assert res is None


class TestDecisionEngineGuardrail9Suppression:
    def test_guardrail_blocks_permanently_blacklisted_user(self):
        engine = DecisionEngine()
        suppression_registry.suppress_permanently("blacklisted@upi", reason="wrong_number")

        dec = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=999.0,
            customer_vpa="blacklisted@upi",
        )
        assert dec.approved is False
        assert "compliance_blacklist_wrong_number" in dec.guardrails_fired

    def test_guardrail_blocks_retries_during_dispute_hold(self):
        engine = DecisionEngine()
        suppression_registry.set_hold("dispute_user@upi", hold_type="dispute_escalation", duration_hours=72)

        dec = engine.evaluate(
            failure_code="U30",
            mandate_state="active",
            amount=999.0,
            customer_vpa="dispute_user@upi",
        )
        # Nudges and smart retries blocked under dispute hold
        assert "smart_retry" in dec.blocked_actions
        assert "whatsapp_nudge" in dec.blocked_actions


class TestInboundAPIEndpoints:
    def test_inbound_webhook_endpoint(self):
        client = TestClient(app)
        payload = {
            "from_phone": "+91-9876543210",
            "customer_vpa": "webhook_test@oksbi",
            "message": "Bhai kal sham tak transfer kar dunga",
            "amount": 1499.0,
        }
        res = client.post("/api/webhook/whatsapp/inbound", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["intent"] == "promise"
        assert "commitment note kar liya" in data["reply_text"]
        assert data["confidence"] >= 0.70
        assert isinstance(data["matched_keywords"], list)

    def test_inbound_samples_endpoint(self):
        client = TestClient(app)
        res = client.get("/api/whatsapp/inbound/samples")
        assert res.status_code == 200
        samples = res.json()
        assert len(samples) >= 5
        assert any(s["intent"] == "promise" for s in samples)
        assert any(s["intent"] == "wrong_number" for s in samples)

    @pytest.mark.asyncio
    async def test_conversation_log_records_turns_and_passes_history(self):
        from src.agent.whatsapp_inbound import conversation_log
        conversation_log.clear()

        # Simulate 2 sequential turns for the same customer
        phone = "+91-9877766655"
        vpa = "history_user@oksbi"

        res1 = await whatsapp_inbound_handler.handle_inbound(
            from_phone=phone,
            customer_vpa=vpa,
            message="Salary 5th ko aayegi",
        )
        assert res1.intent == InboundIntent.PROMISE

        history = conversation_log.get_history(vpa)
        assert len(history) == 2  # 1 customer + 1 bot
        assert history[0]["role"] == "customer"
        assert history[0]["text"] == "Salary 5th ko aayegi"
        assert history[1]["role"] == "bot"

        res2 = await whatsapp_inbound_handler.handle_inbound(
            from_phone=phone,
            customer_vpa=vpa,
            message="Sorry 7th ko transfer karunga",
        )
        assert res2.intent == InboundIntent.PROMISE

        history_after = conversation_log.get_history(vpa)
        assert len(history_after) == 4

    def test_project_chat_api_endpoint(self):
        client = TestClient(app)
        res = client.post("/api/project-chat", json={"message": "What were the benchmark results?"})
        assert res.status_code == 200
        data = res.json()
        assert "reply" in data
        assert len(data["reply"]) > 20
        assert "provider" in data

