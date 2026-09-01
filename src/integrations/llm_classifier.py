import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
import httpx
from datetime import datetime, timezone, timedelta

from ..config import settings
from ..utils.logger import get_logger

IST = timezone(timedelta(hours=5, minutes=30))

logger = get_logger(__name__)

# Daily global call tracking to enforce circuit breaker
_DAILY_CALLS: Dict[str, int] = {}

def _check_and_increment_daily_quota() -> bool:
    """Returns True if within global daily LLM cap, False if circuit breaker should trip."""
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    current_count = _DAILY_CALLS.get(today_str, 0)
    cap = getattr(settings, "llm_global_daily_cap", 500)
    if current_count >= cap:
        logger.warning("[LLM_CIRCUIT_BREAKER] Daily global cap of %d calls reached for %s. Tripping to offline fallback.", cap, today_str)
        return False
    _DAILY_CALLS[today_str] = current_count + 1
    return True

SYSTEM_PROMPT = """You are an expert conversational intent classifier for an Indian digital payments revenue recovery platform (RecoverIQ).
Customers reply in English, Hindi, or code-mixed Hinglish regarding failed UPI autopay or recurring subscription debits.
You may be provided with recent conversation history between the recovery bot and the customer. Use the conversation context to understand intent shifts, deadline updates, or clarifications.

Classify the latest customer message into exactly ONE of the following 5 intents:
1. PROMISE: Customer commits to pay on a future date/time or after salary (e.g., 'kal dunga', 'salary on 5th', 'shaam tak transfer karunga', 'will pay tomorrow', or updating a previous commitment).
2. ALREADY_PAID: Customer claims the amount has already been debited/paid (e.g., 'kat gaya', 'already debited', 'check statement', 'paise chale gaye').
3. DISPUTE: Customer disputes the charge, claims unauthorized transaction, scam/fraud, or demands cancellation/refund (e.g., 'scam', 'band karo', 'fraud', 'didn\'t buy', 'galat kata').
4. HARDSHIP: Customer reports severe financial crisis, job loss, medical emergency, or inability to pay (e.g., 'lost my job', 'hospitalized', 'paise nahi hai', 'crisis').
5. WRONG_NUMBER: Customer indicates wrong person, requests opt-out, or demands stopping messages (e.g., 'wrong person', 'unsubscribe', 'who are you', 'not my account', 'dnd').

Return ONLY a JSON object with this exact schema:
{
  "intent": "promise" | "already_paid" | "dispute" | "hardship" | "wrong_number",
  "confidence": <float between 0.50 and 0.99>,
  "reasoning": "<concise 1-sentence explanation of linguistic signals and conversational context>",
  "extracted_deadline_hours": <12 | 24 | 48 | 96 | null>
}
"""

VALID_INTENTS = {"promise", "already_paid", "dispute", "hardship", "wrong_number"}

# Cache README documentation for the Project Q&A Chatbot
_README_CACHE: Optional[str] = None

def get_cached_readme() -> str:
    global _README_CACHE
    if _README_CACHE is None:
        try:
            readme_path = Path(__file__).resolve().parent.parent.parent / "README.md"
            if readme_path.exists():
                _README_CACHE = readme_path.read_text(encoding="utf-8")
            else:
                _README_CACHE = "RecoverIQ: Autonomous revenue recovery agent for India's UPI Autopay ecosystem."
        except Exception as e:
            logger.warning("[LLM_CLASSIFIER] Could not read README.md: %s", str(e))
            _README_CACHE = "RecoverIQ: Autonomous revenue recovery agent for India's UPI Autopay ecosystem."
    return _README_CACHE


class LLMIntentClassifier:
    """
    Asynchronous, fail-safe LLM intent classifier supporting Google Gemini and OpenAI.
    Also powers the project-grounded Q&A chatbot for dashboard reviewers & judges.
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

    async def _classify_gemini(
        self,
        message: str,
        api_key: str,
        model: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Invokes Google Gemini native REST API with conversational history context."""
        url = self.custom_api_url or f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        
        contents = []
        if history:
            for turn in history[-6:]:
                role = "user" if turn.get("role") in ("user", "customer") else "model"
                txt = (turn.get("text") or turn.get("content") or "").strip()
                if txt:
                    contents.append({"role": role, "parts": [{"text": txt}]})

        contents.append({"role": "user", "parts": [{"text": message}]})

        payload = {
            "system_instruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": contents,
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

    async def _classify_openai(
        self,
        message: str,
        api_key: str,
        model: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Invokes OpenAI Chat Completions API with conversational history context."""
        url = self.custom_api_url or "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            for turn in history[-6:]:
                role = "user" if turn.get("role") in ("user", "customer") else "assistant"
                txt = (turn.get("text") or turn.get("content") or "").strip()
                if txt:
                    messages.append({"role": role, "content": txt})

        messages.append({"role": "user", "content": message})

        payload = {
            "model": model,
            "messages": messages,
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

    async def classify(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Classifies an inbound customer message using Gemini or OpenAI with multi-turn history.
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
                parsed = await self._classify_gemini(clean_message, api_key, model, history=history)
            else:
                parsed = await self._classify_openai(clean_message, api_key, model, history=history)

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

    # ── Task 2: Project-Grounded Q&A Chatbot (Ask RecoverIQ) ─────────────────

    async def ask_project_assistant(
        self,
        query: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Answers questions about the RecoverIQ project, architecture, benchmarks, and features,
        grounded strictly in the project's README.md documentation.
        """
        clean_query = query.strip()
        if not clean_query:
            return {"reply": "Please provide a question about RecoverIQ.", "provider": "fallback"}

        readme_text = get_cached_readme()
        
        system_instruction = f"""You are RecoverIQ AI Assistant, an expert on the RecoverIQ project for judges and developers.
Answer user questions accurately, factually, and concisely based strictly on the project documentation below.

Rules:
1. Ground all numbers and architectural facts in the README below (e.g. ₹1,55,751 recovered, 74.5% recovery rate, +59.5 pts uplift, 147 test cases, Bayesian Thompson Sampling MAB, RBI Category Guardrails: ₹1L vs ₹15k).
2. If asked about something not covered in the project documentation, explicitly state: "This is not covered in the project documentation."
3. Keep answers clear, structured, and easy to read with markdown bullet points or bold highlights. Limit answers to 2-3 focused paragraphs.

=== RECOVERIQ PROJECT DOCUMENTATION (README.md) ===
{readme_text}
===================================================
"""

        provider, api_key, model = self._resolve_provider_and_config()
        
        if not api_key or not provider or not model:
            # High quality grounded offline answer
            return {
                "reply": self._generate_offline_qa_response(clean_query, readme_text),
                "provider": "offline_grounded",
            }

        try:
            if provider == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                contents = []
                if history:
                    for turn in history[-6:]:
                        role = "user" if turn.get("role") in ("user", "human") else "model"
                        txt = (turn.get("text") or turn.get("content") or "").strip()
                        if txt:
                            contents.append({"role": role, "parts": [{"text": txt}]})
                contents.append({"role": "user", "parts": [{"text": clean_query}]})

                payload = {
                    "system_instruction": {"parts": [{"text": system_instruction}]},
                    "contents": contents,
                    "generationConfig": {
                        "temperature": 0.2,
                        "maxOutputTokens": 600,
                    }
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.post(url, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            reply = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                            if reply:
                                return {"reply": reply, "provider": "gemini"}

            elif provider == "openai":
                url = "https://api.openai.com/v1/chat/completions"
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                messages = [{"role": "system", "content": system_instruction}]
                if history:
                    for turn in history[-6:]:
                        role = "user" if turn.get("role") in ("user", "human") else "assistant"
                        txt = (turn.get("text") or turn.get("content") or "").strip()
                        if txt:
                            messages.append({"role": role, "content": txt})
                messages.append({"role": "user", "content": clean_query})

                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": 500,
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.post(url, headers=headers, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        reply = data["choices"][0]["message"]["content"].strip()
                        if reply:
                            return {"reply": reply, "provider": "openai"}

        except Exception as e:
            logger.warning("[PROJECT_CHAT] Exception during LLM query: %s (falling back to grounded search)", str(e))

        return {
            "reply": self._generate_offline_qa_response(clean_query, readme_text),
            "provider": "offline_grounded",
        }

    def _generate_offline_qa_response(self, query: str, readme: str) -> str:
        """Deterministic offline fallback for project documentation questions."""
        q = query.lower()
        if "benchmark" in q or "results" in q or "uplift" in q or "roi" in q:
            return (
                "**RecoverIQ Benchmark Results (vs. Razorpay Fixed-Schedule Baseline):**\n\n"
                "Across 50 Monte Carlo simulation runs on our 40-scenario real-world failure dataset:\n"
                "- **Total Recovered Revenue**: **₹1,55,751 ± ₹19,774** (vs. ₹19,547 baseline) → **+₹1,36,204 mean net uplift**\n"
                "- **Recovery Rate**: **74.5% ± 5.6%** (vs. 15.0% baseline) → **+59.5 percentage points**\n"
                "- **Wasted Retries Eliminated**: Reduced from 120 blind retries to **8 salary-targeted retries** (-112 wasted attempts)\n"
                "- **Compliance Breaches**: 0 violations (100% compliant with RBI/TRAI guidelines)."
            )
        elif "u30" in q or "insufficient" in q or "salary" in q:
            return (
                "**How RecoverIQ Handles U30 (Insufficient Funds):**\n\n"
                "Instead of blind month-end retries ($D+1, D+2, D+3$) which recover only ~14% and exhaust retry limits:\n"
                "1. **Salary Window Scheduling**: Reschedules the retry to the customer's active salary cycle (1st–7th IST).\n"
                "2. **Setu Account Aggregator (AA)**: Performs a pre-flight balance check before firing the debit.\n"
                "3. **Conversational Nudge**: Sends a 1-click UPI collect or payment link via WhatsApp if needed."
            )
        elif "rbi" in q or "guardrail" in q or "limit" in q:
            return (
                "**RBI Category Limits & Guardrails Enforced (GR7 / GR8):**\n\n"
                "- **Enhanced Limit (₹1,00,000)**: Applicable to Insurance premiums, Mutual Fund subscriptions, and Credit Card bill payments.\n"
                "- **Standard Limit (₹15,000)**: Applicable to Education fees and general merchant commerce.\n"
                "- **TRAI DND Quiet Hours**: Zero outbound communications dispatched during 21:00–08:00 IST.\n"
                "- **Touch Caps**: Maximum 3 automated retries per mandate lifetime."
            )
        elif "bandit" in q or "thompson" in q or "bayesian" in q:
            return (
                "**Bayesian Contextual Thompson Sampling MAB:**\n\n"
                "RecoverIQ models each recovery channel (Smart Retry, UPI Collect, Mandate Renewal, WhatsApp Nudge, Escalation) using **Beta(α, β) distributions**:\n"
                "- Samples expected conversion probability $\\theta \\sim \\text{Beta}(\\alpha, \\beta)$.\n"
                "- Optimizes Net Utility: $\\text{Utility} = \\theta \\times \\text{Amount} - \\text{Channel Cost}$.\n"
                "- Performs continuous online Bayesian updates upon verified debit or customer payment."
            )
        else:
            return (
                "**RecoverIQ Overview:**\n\n"
                "RecoverIQ is an autonomous revenue recovery agent for India's UPI Autopay and recurring commerce ecosystem. "
                "It combines 14 NPCI error code root-cause diagnostics, RBI/TRAI deterministic guardrails, Bayesian Thompson Sampling channel optimization, and a fail-safe Google Gemini 2-way conversational WhatsApp recovery engine.\n\n"
                "Ask me about: **Benchmark stats**, **U30 salary retries**, **RBI ₹1L limits**, **Thompson Sampling**, or **WhatsApp NLP intents**!"
            )

    async def parse_natural_language_scenario(self, prompt: str) -> Dict[str, Any]:
        """
        Parses a freeform natural language simulation request into a structured scenario dictionary.
        Enforces strict schema constraints, checks daily call quotas, and falls back to deterministic heuristic parsing.
        """
        clean_prompt = prompt.strip()
        if not clean_prompt:
            return self._parse_scenario_heuristically("Test Default Scenario ₹999 U30")

        # Check global daily quota circuit breaker
        if not _check_and_increment_daily_quota():
            return self._parse_scenario_heuristically(clean_prompt)

        provider, api_key, model = self._resolve_provider_and_config()
        if not provider or not api_key:
            return self._parse_scenario_heuristically(clean_prompt)

        system_instruction = (
            "You are an expert NLP parser for an Indian payments and UPI Autopay recovery simulator (RecoverIQ).\n"
            "Given a freeform user prompt describing a payment failure scenario, extract and generate a valid simulation scenario.\n"
            "Valid failure codes: 'U30' (Insufficient Funds), 'BT01' (Mandate Revoked), 'BT02' (Mandate Expired), 'TM' (Bank Timeout), "
            "'U69' (Limit Exceeded), 'U13' (Invalid Mandate), 'ZA' (Customer Inactive), 'BA' (Bank Account Closed), 'ZM' (Invalid VPA).\n"
            "Return ONLY a JSON object with this exact schema:\n"
            "{\n"
            '  "failure_code": "U30" | "BT01" | "BT02" | "TM" | "U69" | "U13" | "ZA" | "BA" | "ZM",\n'
            '  "vpa": "<e.g. user@oksbi or derived from bank/name>",\n'
            '  "bank": "<e.g. SBI, HDFC, ICICI, Axis, Kotak, PNB>",\n'
            '  "amount": <float amount in INR, default 999.0 if not specified>,\n'
            '  "mandate_state": "active" | "revoked" | "expired",\n'
            '  "retry_attempt": <integer 0 to 5, default 0>,\n'
            '  "scenario_name": "<concise title e.g. Rahul Sharma - U30 Insufficient Funds>",\n'
            '  "echo_summary": "<natural language summary e.g. Rahul Sharma (₹4,500, U30 Insufficient Funds, SBI)>"\n'
            "}"
        )

        try:
            if provider == "gemini":
                url = self.custom_api_url or f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
                payload = {
                    "system_instruction": {"parts": [{"text": system_instruction}]},
                    "contents": [{"role": "user", "parts": [{"text": clean_prompt}]}],
                    "generationConfig": {
                        "response_mime_type": "application/json",
                        "temperature": 0.1,
                    },
                }
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    res = await client.post(url, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                            parsed = json.loads(raw_text)
                            parsed["provider"] = "gemini"
                            return self._validate_and_sanitize_scenario(parsed, clean_prompt)

            elif provider == "openai":
                url = "https://api.openai.com/v1/chat/completions"
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": clean_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                }
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    res = await client.post(url, headers=headers, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        raw_text = data["choices"][0]["message"]["content"].strip()
                        parsed = json.loads(raw_text)
                        parsed["provider"] = "openai"
                        return self._validate_and_sanitize_scenario(parsed, clean_prompt)

        except Exception as e:
            logger.warning("[PROMPT_TO_SCENARIO] LLM parsing failed (%s), using heuristic fallback.", str(e))

        return self._parse_scenario_heuristically(clean_prompt)

    def _validate_and_sanitize_scenario(self, data: Dict[str, Any], raw_prompt: str) -> Dict[str, Any]:
        """Ensures all required keys exist and have safe types."""
        code = str(data.get("failure_code") or "U30").upper().strip()
        if code not in {"U30", "BT01", "BT02", "TM", "U69", "U13", "ZA", "BA", "ZM", "XH", "U28", "U07", "XY", "00"}:
            code = "U30"

        try:
            amount = float(data.get("amount") or 999.0)
            if amount <= 0:
                amount = 999.0
        except (ValueError, TypeError):
            amount = 999.0

        bank = str(data.get("bank") or "SBI").strip()
        vpa = str(data.get("vpa") or f"user@ok{bank.lower()}").strip()
        mandate_state = str(data.get("mandate_state") or ("revoked" if code == "BT01" else "expired" if code == "BT02" else "active")).lower()
        retry_attempt = int(data.get("retry_attempt") or 0)
        scenario_name = str(data.get("scenario_name") or f"Simulated Scenario - {code} ({bank})").strip()
        echo = str(data.get("echo_summary") or f"{scenario_name} (₹{amount:,.0f}, {code}, {bank})").strip()

        return {
            "failure_code": code,
            "vpa": vpa,
            "bank": bank,
            "amount": amount,
            "mandate_state": mandate_state,
            "retry_attempt": retry_attempt,
            "scenario_name": scenario_name,
            "echo_summary": echo,
            "provider": data.get("provider", "gemini"),
        }

    def _parse_scenario_heuristically(self, prompt: str) -> Dict[str, Any]:
        """Deterministic offline rule-based extractor for scenario parameters."""
        p = prompt.lower()

        # 1. Failure code detection
        code = "U30"
        if re.search(r"\b(bt01|revok|cancel)\b", p):
            code = "BT01"
        elif re.search(r"\b(bt02|expir)\b", p):
            code = "BT02"
        elif re.search(r"\b(tm|timeout|down|gateway)\b", p):
            code = "TM"
        elif re.search(r"\b(u69|limit)\b", p):
            code = "U69"
        elif re.search(r"\b(u13)\b", p):
            code = "U13"
        elif re.search(r"\b(za|inactive)\b", p):
            code = "ZA"
        elif re.search(r"\b(ba|closed)\b", p):
            code = "BA"
        elif re.search(r"\b(u30|insufficient|balance|salary)\b", p):
            code = "U30"

        # 2. Amount extraction (e.g. ₹1,85,000, 4500, 1.85L, 299)
        amount = 999.0
        # Lakh notation
        lakh_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakh|l\b)", p)
        if lakh_match:
            amount = float(lakh_match.group(1)) * 100000.0
        else:
            amt_match = re.search(r"(?:rs\.?|inr|₹)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]+)?|[0-9]+)", p)
            if amt_match:
                clean_num = amt_match.group(1).replace(",", "")
                try:
                    parsed_amt = float(clean_num)
                    if parsed_amt > 10:  # avoid picking up attempt numbers
                        amount = parsed_amt
                except ValueError:
                    pass

        # 3. Bank extraction
        bank = "SBI"
        if "hdfc" in p:
            bank = "HDFC"
        elif "icici" in p:
            bank = "ICICI"
        elif "axis" in p:
            bank = "Axis"
        elif "kotak" in p:
            bank = "Kotak"
        elif "pnb" in p:
            bank = "PNB"
        elif "paytm" in p:
            bank = "Paytm"

        # 4. Mandate state
        mandate_state = "active"
        if code == "BT01":
            mandate_state = "revoked"
        elif code == "BT02":
            mandate_state = "expired"

        vpa = f"custom_user@ok{bank.lower()}"
        if "infosys" in p:
            vpa = "infosys@okhdfc"
            bank = "HDFC"
        elif "rahul" in p:
            vpa = "rahul@oksbi"

        scenario_name = f"Natural Language Scenario: {code} on {bank} (₹{amount:,.0f})"
        echo = f"Parsed Scenario: {code} ({bank} Bank, ₹{amount:,.0f}) — Executing live recovery pipeline..."

        return {
            "failure_code": code,
            "vpa": vpa,
            "bank": bank,
            "amount": amount,
            "mandate_state": mandate_state,
            "retry_attempt": 0,
            "scenario_name": scenario_name,
            "echo_summary": echo,
            "provider": "offline_heuristic",
        }


llm_classifier = LLMIntentClassifier()


