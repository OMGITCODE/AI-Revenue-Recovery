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
    Asynchronous, fail-safe LLM intent classifier.
    """

    def __init__(self, api_url: str = "https://api.openai.com/v1/chat/completions", timeout_seconds: float = 6.0):
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds

    async def classify(self, message: str) -> Optional[Dict[str, Any]]:
        """
        Classifies an inbound customer message using OpenAI.
        Returns parsed dictionary if successful, or None on ANY failure/omission.
        """
        api_key = (settings.openai_api_key or "").strip()
        if not api_key:
            return None

        clean_message = message.strip()
        if not clean_message:
            return None

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": settings.openai_model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": clean_message},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 150,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.api_url, headers=headers, json=payload)
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

                raw_intent = str(parsed.get("intent", "")).lower().strip()
                if raw_intent not in VALID_INTENTS:
                    logger.warning("[LLM_CLASSIFIER] Unrecognized intent '%s' from LLM; falling back to regex", raw_intent)
                    return None

                return {
                    "intent": raw_intent,
                    "confidence": max(0.50, min(0.99, float(parsed.get("confidence", 0.85)))),
                    "reasoning": str(parsed.get("reasoning", "Classified via LLM")),
                    "extracted_deadline_hours": parsed.get("extracted_deadline_hours"),
                }

        except Exception as e:
            logger.warning(
                "[LLM_CLASSIFIER] Exception during LLM intent classification: %s (safely falling back to regex)",
                str(e),
            )
            return None


llm_classifier = LLMIntentClassifier()
