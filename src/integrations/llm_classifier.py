"""
llm_classifier.py — Fail-Safe LLM Intent Classifier for Inbound Customer Messaging
===================================================================================
Provides an isolated, fail-safe LLM classifier using OpenAI's Chat Completions API.
Uses httpx directly (no extra SDK required).

Guaranteed design invariant:
- Returns None on ANY failure (missing API key, timeout, bad JSON, non-200, network drop).
- NEVER raises an exception to the caller.
- The deterministic regex classifier in whatsapp_inbound.py remains the safety net.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
import httpx

from ..config import settings
from ..utils.logger import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are an expert conversational intent classifier for an Indian digital payments revenue recovery platform (RecoverIQ).
Customers reply in English, Hindi, or code-mixed Hinglish regarding failed UPI autopay or recurring subscription debits.

Classify the customer message into exactly ONE of the following 5 intents:
1. PROMISE: Customer commits to pay on a future date/time or after salary (e.g., 'kal dunga', 'salary on 5th', 'shaam tak transfer karunga', 'will pay tomorrow').
2. ALREADY_PAID: Customer claims the amount has already been debited/paid (e.g., 'kat gaya', 'already debited', 'check statement', 'paise chale gaye').
3. DISPUTE: Customer disputes the charge, claims unauthorized transaction, scam/fraud, or demands cancellation/refund (e.g., 'scam', 'band karo', 'fraud', 'didn\'t buy', 'galat kata').
4. HARDSHIP: Customer reports severe financial crisis, job loss, medical emergency, or inability to pay (e.g., 'lost my job', 'hospitalized', 'paise nahi hai', 'crisis').
5. WRONG_NUMBER: Customer indicates wrong person, requests opt-out, or demands stopping messages (e.g., 'wrong person', 'unsubscribe', 'who are you', 'not my account', 'dnd').

Return ONLY a JSON object with this exact schema:
{
  "intent": "promise" | "already_paid" | "dispute" | "hardship" | "wrong_number",
  "confidence": <float between 0.50 and 0.99>,
  "reasoning": "<concise 1-sentence explanation of linguistic signals>",
  "extracted_deadline_hours": <12 | 24 | 48 | 96 | null>
}
"""

VALID_INTENTS = {"promise", "already_paid", "dispute", "hardship", "wrong_number"}


class LLMIntentClassifier:
    """
    Asynchronous, fail-safe LLM intent classifier supporting Google Gemini and OpenAI.
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        timeout_seconds: float = 8.0,
    ):
        self.custom_api_url = api_url
        self.timeout_seconds = timeout_seconds

    def _resolve_provider_and_config(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Determines the active provider, api_key, and model.
        Returns (provider, api_key, model) or (None, None, None) if no key configured.
        """
        provider_pref = (settings.llm_provider or "auto").lower().strip()

        gemini_key = (settings.gemini_api_key or "").strip()
        openai_key = (settings.openai_api_key or "").strip()

        # Explicit preference
        if provider_pref == "gemini" and gemini_key:
            return "gemini", gemini_key, settings.gemini_model or "gemini-3.6-flash"
        if provider_pref == "openai" and openai_key:
            return "openai", openai_key, settings.openai_model or "gpt-4o-mini"

        # Auto selection: Gemini first if configured, else OpenAI
        if gemini_key:
            return "gemini", gemini_key, settings.gemini_model or "gemini-3.6-flash"
        if openai_key:
            return "openai", openai_key, settings.openai_model or "gpt-4o-mini"

        return None, None, None

    async def _classify_gemini(self, message: str, api_key: str, model: str) -> Optional[Dict[str, Any]]:
        """Invokes Google Gemini native REST API."""
        url = self.custom_api_url or f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "system_instruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": [
                {"parts": [{"text": message}]}
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.0,
                "maxOutputTokens": 200,
            }
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                logger.warning(
                    "[LLM_CLASSIFIER] Gemini API returned HTTP %s: %s (falling back to regex)",
                    response.status_code,
                    response.text[:200],
                )
                return None

            data = response.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None

            raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            parsed = json.loads(raw_text)
            return parsed

    async def _classify_openai(self, message: str, api_key: str, model: str) -> Optional[Dict[str, Any]]:
        """Invokes OpenAI Chat Completions API."""
        url = self.custom_api_url or "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 150,
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code != 200:
                logger.warning(
                    "[LLM_CLASSIFIER] OpenAI API returned HTTP %s: %s (falling back to regex)",
                    response.status_code,
                    response.text[:200],
                )
                return None

            data = response.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return parsed

    async def classify(self, message: str) -> Optional[Dict[str, Any]]:
        """
        Classifies an inbound customer message using Gemini or OpenAI.
        Returns parsed dictionary if successful, or None on ANY failure/omission.
        """
        provider, api_key, model = self._resolve_provider_and_config()
        if not api_key or not provider or not model:
            return None

        clean_message = message.strip()
        if not clean_message:
            return None

        try:
            if provider == "gemini":
                parsed = await self._classify_gemini(clean_message, api_key, model)
            else:
                parsed = await self._classify_openai(clean_message, api_key, model)

            if not parsed or not isinstance(parsed, dict):
                return None

            raw_intent = str(parsed.get("intent", "")).lower().strip()
            if raw_intent not in VALID_INTENTS:
                logger.warning("[LLM_CLASSIFIER] Unrecognized intent '%s' from %s; falling back to regex", raw_intent, provider)
                return None

            return {
                "intent": raw_intent,
                "confidence": max(0.50, min(0.99, float(parsed.get("confidence", 0.85)))),
                "reasoning": str(parsed.get("reasoning", f"Classified via {provider}")),
                "extracted_deadline_hours": parsed.get("extracted_deadline_hours"),
                "provider": provider,
            }

        except Exception as e:
            logger.warning(
                "[LLM_CLASSIFIER] Exception during %s intent classification: %s (safely falling back to regex)",
                (provider or "LLM").upper(),
                str(e),
            )
            return None


llm_classifier = LLMIntentClassifier()

