"""
Unit and Integration Tests for:
1. Prompt-to-Scenario Natural Language Generator (POST /api/prompt-to-scenario)
2. In-Memory Rate Limiter & Global Daily Quota Circuit Breaker
3. Held-Out Labeled Intent Classifier Evaluation Benchmark (GET /api/classifier/eval)
4. Public Path Exemptions under API key configuration
"""

import json
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
import httpx

from api.main import app, rate_limiter, InMemoryRateLimiter, CustomScenarioRequest
from src.config import settings
from src.integrations.llm_classifier import LLMIntentClassifier, _DAILY_CALLS
from src.agent.classifier_eval import classifier_benchmark, LABELED_EVAL_DATASET


class TestPromptToScenarioGenerator:
    """Tests for Natural Language Prompt-to-Scenario parser and execution."""

    @pytest.mark.asyncio
    async def test_prompt_to_scenario_mocked_gemini_call(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", "test-key-mock")
        monkeypatch.setattr(settings, "llm_provider", "gemini")

        mock_payload = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps({
                            "failure_code": "U30",
                            "vpa": "rahul@oksbi",
                            "bank": "SBI",
                            "amount": 4500.0,
                            "mandate_state": "active",
                            "retry_attempt": 0,
                            "scenario_name": "Rahul Sharma - U30 Insufficient Funds",
                            "echo_summary": "Rahul Sharma (₹4,500, U30 Insufficient Funds, SBI)",
                        })
                    }]
                }
            }]
        }

        mock_resp = httpx.Response(200, json=mock_payload, request=httpx.Request("POST", "http://test"))
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            classifier = LLMIntentClassifier()
            res = await classifier.parse_natural_language_scenario("Simulate Rahul Sharma ₹4,500 on SBI")

            assert res["failure_code"] == "U30"
            assert res["amount"] == 4500.0
            assert res["bank"] == "SBI"
            assert res["vpa"] == "rahul@oksbi"
            assert res["provider"] == "gemini"
            assert "Rahul Sharma" in res["echo_summary"]

    @pytest.mark.asyncio
    async def test_prompt_to_scenario_offline_heuristic_fallback(self, monkeypatch):
        # Force no API keys to verify offline heuristic parsing
        monkeypatch.setattr(settings, "gemini_api_key", "")
        monkeypatch.setattr(settings, "openai_api_key", "")

        classifier = LLMIntentClassifier()
        res = await classifier.parse_natural_language_scenario("Simulate ₹1.85L B2B invoice on HDFC with BT01 mandate revoked")

        assert res["failure_code"] == "BT01"
        assert res["amount"] == 185000.0
        assert res["bank"] == "HDFC"
        assert res["mandate_state"] == "revoked"
        assert res["provider"] == "offline_heuristic"

    def test_api_prompt_to_scenario_endpoint(self):
        client = TestClient(app)
        payload = {"prompt": "Simulate Priya Patel ₹1,299 U30 insufficient funds on HDFC Bank"}
        res = client.post("/api/prompt-to-scenario", json=payload)

        assert res.status_code == 200
        data = res.json()
        assert "echo" in data
        assert "scenario" in data
        assert "event" in data
        assert data["scenario"]["failure_code"] == "U30"
        assert data["scenario"]["bank"] == "HDFC"
        assert data["scenario"]["amount"] == 1299.0

    def test_api_prompt_to_scenario_empty_prompt_rejected(self):
        client = TestClient(app)
        res = client.post("/api/prompt-to-scenario", json={"prompt": "   "})
        assert res.status_code == 400


class TestRateLimiterAndQuota:
    """Tests for in-memory rate limiter and daily call quota."""

    def test_rate_limiter_allows_localhost_exemptions(self):
        limiter = InMemoryRateLimiter(requests_per_minute=2)
        # Mock request from localhost
        class MockClient:
            host = "127.0.0.1"

        class MockRequest:
            client = MockClient()

        # Should never raise for localhost even with 50 calls
        for _ in range(50):
            limiter.check(MockRequest())

    def test_rate_limiter_blocks_external_ip_exceeding_limit(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_rate_limit_per_minute", 3)
        monkeypatch.setattr(settings, "llm_aggregate_rate_limit_per_minute", 100)
        from fastapi import HTTPException
        limiter = InMemoryRateLimiter(requests_per_minute=3, aggregate_requests_per_minute=100)

        class MockClient:
            host = "203.0.113.195"

        class MockRequest:
            client = MockClient()

        # First 3 allowed
        limiter.check(MockRequest())
        limiter.check(MockRequest())
        limiter.check(MockRequest())

        # 4th must raise 429
        with pytest.raises(HTTPException) as exc:
            limiter.check(MockRequest())
        assert exc.value.status_code == 429

    def test_aggregate_rate_limiter_blocks_combined_ip_surge(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_rate_limit_per_minute", 10)
        monkeypatch.setattr(settings, "llm_aggregate_rate_limit_per_minute", 4)
        from fastapi import HTTPException
        limiter = InMemoryRateLimiter(requests_per_minute=10, aggregate_requests_per_minute=4)

        # 4 different IPs make 1 call each (under per-IP limit of 10)
        for i in range(4):
            class MockClient:
                host = f"203.0.113.{i+1}"

            class MockRequest:
                client = MockClient()

            limiter.check(MockRequest())

        # 5th call from another distinct IP must raise 429 because aggregate ceiling is 4
        class MockClient5:
            host = "203.0.113.99"

        class MockRequest5:
            client = MockClient5()

        with pytest.raises(HTTPException) as exc:
            limiter.check(MockRequest5())
        assert exc.value.status_code == 429
        assert "Global rate limit exceeded" in exc.value.detail

    @pytest.mark.asyncio
    async def test_global_daily_cap_trips_to_fallback(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_global_daily_cap", 2)
        _DAILY_CALLS.clear()

        classifier = LLMIntentClassifier()
        # First 2 calls use daily quota
        from src.integrations.llm_classifier import _check_and_increment_daily_quota
        assert _check_and_increment_daily_quota() is True
        assert _check_and_increment_daily_quota() is True
        # 3rd call exceeds cap -> returns False
        assert _check_and_increment_daily_quota() is False


class TestClassifierEvalBenchmark:
    """Tests for the held-out intent classifier evaluation dataset & endpoint."""

    def test_classifier_eval_cached_endpoint(self):
        client = TestClient(app)
        res = client.get("/api/classifier/eval")
        assert res.status_code == 200
        data = res.json()

        assert data["total_samples"] == 30
        assert data["overall_accuracy"] >= 0.85
        assert "compliance_intent_recall" in data
        assert data["compliance_intent_recall"]["hardship_recall"] == 1.0
        assert data["compliance_intent_recall"]["wrong_number_recall"] == 1.0

    def test_eval_dataset_integrity(self):
        assert len(LABELED_EVAL_DATASET) == 30
        intents = set(item["expected"] for item in LABELED_EVAL_DATASET)
        assert intents == {"promise", "already_paid", "dispute", "hardship", "wrong_number"}


class TestPublicPathSecurityAllowlist:
    """Tests that public evaluation routes remain accessible without API key."""

    def test_public_routes_accessible_with_auth_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "recoveriq_api_key", "secret-test-key-999")
        client = TestClient(app)

        # Public endpoints should return 200 without Authorization header
        res_eval = client.get("/api/classifier/eval")
        assert res_eval.status_code == 200

        res_prompt = client.post("/api/prompt-to-scenario", json={"prompt": "Test U30 ₹999"})
        assert res_prompt.status_code == 200

        res_chat = client.post("/api/project-chat", json={"message": "What is RecoverIQ?"})
        assert res_chat.status_code == 200


class TestXSSDefensesAndSanitization:
    """Tests that adversarial prompt injections and raw HTML in chatbot and scenario payloads are sanitized."""

    @pytest.mark.asyncio
    async def test_scenario_generator_sanitizes_html_tags(self):
        classifier = LLMIntentClassifier()
        # Prompt containing adversarial HTML/XSS payloads
        adversarial_prompt = 'Simulate <script>alert("XSS")</script> <img src=x onerror="alert(1)"> ₹5,000 U30 on SBI'
        res = await classifier.parse_natural_language_scenario(adversarial_prompt)

        assert res["failure_code"] == "U30"
        assert res["amount"] == 5000.0
        # Check that script tags are not executed or parsed into sensitive keys
        assert "<script>" not in res["bank"]

