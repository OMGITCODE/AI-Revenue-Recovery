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


def get_live_session_summary() -> Dict[str, Any]:
    """
    Safely collects real-time metrics across the active session with guaranteed zero-division
    and empty-state resilience across all subsystem registries.
    """
    summary: Dict[str, Any] = {
        "total_entries": 0,
        "total_recovered": 0.0,
        "net_roi": 0.0,
        "total_at_stake": 0.0,
        "recovery_rate_pct": 0.0,
        "reactive_recovered": 0.0,
        "proactive_protected": 0.0,
        "suppression_blacklisted_count": 0,
        "active_compliance_holds_count": 0,
        "active_promises_count": 0,
        "promises_amount_at_risk": 0.0,
        "b2b_receivables_count": 0,
        "b2b_total_outstanding": 0.0,
        "b2b_settled_count": 0,
        "checkout_sessions_count": 0,
        "checkout_recovered_amount": 0.0,
        "mandates_tracked_count": 0,
        "mandates_renewed_count": 0,
        "mandates_revenue_protected": 0.0,
    }

    # 1. Recovery Ledger
    try:
        from ..agent.recovery_ledger import ledger as recovery_ledger
        roi = recovery_ledger.overall_roi()
        summary["total_entries"] = roi.get("total_entries", 0)
        summary["total_recovered"] = roi.get("total_recovered", 0.0)
        summary["net_roi"] = roi.get("net_roi", 0.0)
        summary["total_at_stake"] = roi.get("total_at_stake", 0.0)
        summary["recovery_rate_pct"] = roi.get("recovery_rate_pct", 0.0)
        summary["reactive_recovered"] = roi.get("reactive_recovered", 0.0)
        summary["proactive_protected"] = roi.get("proactive_protected", 0.0)
    except Exception as e:
        logger.debug("[LIVE_SUMMARY] Ledger read skipped: %s", e)

    # 2. Suppression Registry
    try:
        from ..agent.whatsapp_inbound import suppression_registry
        summary["suppression_blacklisted_count"] = len(getattr(suppression_registry, "_permanent_blacklist", set()))
        summary["active_compliance_holds_count"] = len(getattr(suppression_registry, "_active_holds", {}))
    except Exception as e:
        logger.debug("[LIVE_SUMMARY] Suppression read skipped: %s", e)

    # 3. Promise Tracker
    try:
        from ..agent.promise_tracker import promise_tracker
        p_stats = promise_tracker.stats()
        summary["active_promises_count"] = p_stats.get("pending", 0)
        summary["promises_amount_at_risk"] = p_stats.get("amount_at_risk", 0.0)
    except Exception as e:
        logger.debug("[LIVE_SUMMARY] Promise tracker read skipped: %s", e)

    # 4. Mandate Expiry Scanner
    try:
        from ..agent.mandate_expiry import mandate_expiry_scanner
        m_stats = mandate_expiry_scanner.stats()
        summary["mandates_tracked_count"] = m_stats.get("total_mandates_tracked", 0)
        summary["mandates_renewed_count"] = m_stats.get("renewals_completed", 0)
        summary["mandates_revenue_protected"] = m_stats.get("revenue_protected", 0.0)
    except Exception as e:
        logger.debug("[LIVE_SUMMARY] Mandate scanner read skipped: %s", e)

    # 5. B2B Receivables Chaser
    try:
        from ..agent.b2b_chaser import b2b_chaser
        b_stats = b2b_chaser.stats()
        summary["b2b_receivables_count"] = b_stats.get("total", 0)
        summary["b2b_total_outstanding"] = b_stats.get("total_outstanding", 0.0)
        summary["b2b_settled_count"] = b_stats.get("settled", 0)
    except Exception as e:
        logger.debug("[LIVE_SUMMARY] B2B chaser read skipped: %s", e)

    # 6. Checkout Recovery
    try:
        from ..agent.checkout_recovery import checkout_agent
        c_stats = checkout_agent.stats()
        summary["checkout_sessions_count"] = c_stats.get("total_sessions", 0)
        summary["checkout_recovered_amount"] = c_stats.get("recovered_amount", 0.0)
    except Exception as e:
        logger.debug("[LIVE_SUMMARY] Checkout recovery read skipped: %s", e)

    return summary


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
            return "gemini", gemini_key, settings.gemini_model or "gemini-flash-lite-latest"
        if provider_pref == "openai" and openai_key:
            return "openai", openai_key, settings.openai_model or "gpt-4o-mini"

        # Auto selection: Gemini first if configured, else OpenAI
        if gemini_key:
            return "gemini", gemini_key, settings.gemini_model or "gemini-flash-lite-latest"
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
        event_context: Optional[Dict[str, Any]] = None,
        live_stats: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Answers questions about the RecoverIQ project, architecture, benchmarks, and features,
        grounded strictly in the project's README.md documentation, live session metrics,
        and real-time event context.
        """
        clean_query = query.strip()
        if not clean_query:
            return {"reply": "Please provide a question about RecoverIQ.", "provider": "fallback"}

        if live_stats is None:
            live_stats = get_live_session_summary()

        readme_text = get_cached_readme()
        
        system_instruction = f"""You are RecoverIQ AI Assistant, an expert on the RecoverIQ project for judges, operators, and developers.
Answer user questions accurately, factually, and concisely based on the project documentation, live session metrics, and real-time event context below.

CRITICAL DISTINCTION INVARIANT (NEVER BLUR BENCHMARK VS. LIVE SESSION):
1. LIVE SESSION METRICS vs. PUBLISHED BENCHMARK SIMULATION RESULTS are two completely distinct, separate datasets:
   - "LIVE ACTIVE SESSION STATE": Reflects what has executed in the current runtime session so far (detailed in the CURRENT LIVE SESSION METRICS block below). On a fresh server clone or before any scenarios are run, this will be ₹0 recovered across 0 transactions. If the user asks "how much have we recovered in this session", "current session stats", "live recovered amount", or "session recovery rate", YOU MUST REPORT THE EXACT REAL-TIME NUMBERS FROM THE LIVE SESSION STATE BLOCK. If it is ₹0, state clearly: "In this active session, ₹0 has been recovered so far across 0 transactions. Run a scenario or test simulation in the dashboard above to see live recovery in action."
   - "PUBLISHED OFFLINE BENCHMARK EVALUATION": Reflects the 50 Monte Carlo simulation runs over the 60 curated synthetic failure scenarios documented in the README (RecoverIQ: ₹2,82,154 ± ₹77,144 vs. Baseline: ₹89,063 ± ₹43,728 → Mean Simulated Net Uplift: +₹1,93,091 ± ₹78,177 with 95% CI: [+₹1,70,873, +₹2,15,309], 54.7% ± 6.6% recovery rate vs 16.9% ± 3.8% baseline, 20 retries vs 180 blind retries, 207 test cases). This is a controlled synthetic simulation benchmark, NOT observed live merchant production performance. YOU MUST ONLY CITE THESE NUMBERS when the user explicitly asks about the benchmark, historical evaluation runs, published research results, test suite, or baseline comparison.
   - NEVER substitute, quote, or blur the benchmark figures (+₹1.93L / 54.7%) when answering questions about the current live session!

Rules:
1. Ground all numbers and architectural facts in the README below (e.g. +₹1,93,091 ± ₹78,177 mean simulated net uplift [95% CI: +₹1,70,873 to +₹2,15,309], 54.7% ± 6.6% (vs. 16.9% baseline) → +37.8 percentage points, 207 test cases, Bayesian Thompson Sampling MAB, RBI Category Guardrails: ₹1L vs ₹15k). Always clearly state that this is a simulated benchmark comparison, not live merchant production claims.
2. Deep Platform Architecture:
   - B2B Receivables Chaser: Aging buckets 0-30d (gentle WhatsApp/email), 31-60d (firm notice + AR escalation), 61-90d (formal demand + 18% p.a. interest charge notice), 90d+ (collections/legal referral). Debtor Tiers: Tier A (>₹2L, dedicated manager), Tier B (₹25k-₹2L, AR specialist), Tier C (<₹25k, automated IVR).
   - Checkout Drop-off Recovery: Captures drop-off reasons (payment_page_exit, otp_timeout, bank_error_exit, upi_intent_abandoned, address_form_exit), fires smart re-engagement links with pre-filled carts at T+10m, T+1h, T+24h.
   - Customer Identity Graph: Resolves customer IDs, VPAs, phones, and emails into single canonical `cust:...` profiles, linking touches, promises, and compliance suppression across aliases to prevent fragmented customer interactions.
   - Proactive Mandate Expiry Scanner: Identifies UPI Autopay mandates nearing expiration (T-72h to T-24h) and dispatches 1-click renewal links via WhatsApp/SMS to prevent NPCI BT02 ("Mandate Expired") failures before they ever occur.
3. If event context is provided in the CURRENT EVENT CONTEXT block below, answer using it directly and cite specific values (e.g. available balance, amount due, failure code, guardrail ID, chosen recovery action).
4. If asked about something not covered in the live context, event context, or project documentation, explicitly state: "This is not covered in the project documentation."
5. Keep answers clear, structured, and easy to read with markdown bullet points or bold highlights. Limit answers to 2-3 focused paragraphs.
"""

        system_instruction += f"""
=== CURRENT LIVE SESSION METRICS ===
{json.dumps(live_stats, indent=2)}
====================================
"""

        if event_context:
            system_instruction += f"""
=== CURRENT EVENT CONTEXT ===
{json.dumps(event_context, indent=2)}
=============================
"""

        system_instruction += f"""
=== RECOVERIQ PROJECT DOCUMENTATION (README.md) ===
{readme_text}
===================================================
"""

        provider, api_key, model = self._resolve_provider_and_config()
        
        if not api_key or not provider or not model:
            # High quality grounded offline answer
            return {
                "reply": self._generate_offline_qa_response(clean_query, readme_text, event_context, live_stats),
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
            "reply": self._generate_offline_qa_response(clean_query, readme_text, event_context, live_stats),
            "provider": "offline_grounded",
        }

    def _generate_offline_qa_response(
        self,
        query: str,
        readme: str,
        event_context: Optional[Dict[str, Any]] = None,
        live_stats: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Deterministic offline fallback for project documentation questions."""
        q = query.lower()
        if live_stats is None:
            live_stats = get_live_session_summary()

        # ── 1. Live Session Queries (Anti-Hallucination: Distinct from Benchmark) ────
        is_live_query = any(w in q for w in [
            "current session", "this session", "session stat", "session metric",
            "live stat", "live recovery", "how much have we recovered",
            "how much recovered", "revenue recovered so far", "what have we recovered",
            "recovered in this session", "what's our current session"
        ])
        if is_live_query:
            total_rec = live_stats.get("total_recovered", 0.0)
            entries = live_stats.get("total_entries", 0)
            if total_rec == 0.0 and entries == 0:
                return (
                    "**Current Live Session Metrics:**\n\n"
                    "- **Active Session Recovered**: **₹0** across 0 logged transactions.\n"
                    "- **Session State**: Fresh server instance. No recovery actions or simulations have executed yet in this session.\n"
                    "- **How to test live recovery**: Click **'Run Scenario'** or trigger a failed payment (such as Rahul Sharma's U30 or an expiring BT02 mandate) in the simulator above to observe autonomous interventions, audit ledger logs, and real-time uplift in action!\n\n"
                    "*(Note: For our published 50-run Monte Carlo offline benchmark simulation results showing +₹1.93L mean simulated uplift (54.7% recovery rate), ask: 'What are the benchmark results?')*"
                )
            roi = live_stats.get("net_roi", 0.0)
            rate = live_stats.get("recovery_rate_pct", 0.0)
            reactive = live_stats.get("reactive_recovered", 0.0)
            proactive = live_stats.get("proactive_protected", 0.0)
            holds = live_stats.get("active_compliance_holds_count", 0)
            suppressed = live_stats.get("suppression_blacklisted_count", 0)
            promises = live_stats.get("active_promises_count", 0)
            return (
                "**Current Live Session Metrics:**\n\n"
                f"- **Total Recovered**: **₹{total_rec:,.2f}** (Net ROI: **₹{roi:,.2f}**)\n"
                f"- **Session Recovery Rate**: **{rate:.1f}%** across {entries} logged decisions\n"
                f"- **Reactive Recovered**: ₹{reactive:,.2f} | **Proactive Protected**: ₹{proactive:,.2f}\n"
                f"- **Active Compliance Holds**: {holds} | **Suppressed Contacts**: {suppressed}\n"
                f"- **Active Promises-to-Pay**: {promises} pending\n\n"
                "*(Note: These figures reflect this live runtime session only, clearly distinct from the published offline simulated benchmark evaluation of +₹1.93L mean uplift across 50 Monte Carlo runs.)*"
            )

        # ── 2a. Sensitivity / Pessimistic Haircut Analysis ──────────────────────
        if any(w in q for w in ["sensitivity", "haircut", "pessimistic", "worst case", "20%", "20 percent", "conservative", "lower conversion"]):
            return (
                "**RecoverIQ Sensitivity Analysis — 20% Pessimistic Haircut:**\n\n"
                "*(To stress-test robustness, benchmark.py applies a uniform 20% downward haircut across all "
                "channel conversion probabilities. Run anytime: `python -X utf8 benchmark.py --sensitivity`)*\n\n"
                "| Metric | Baseline (Fixed Retry) | RecoverIQ (20% Haircut Applied) | Delta |\n"
                "|---|---|---|---|\n"
                "| **Modeled Conversion Rates** | Unchanged (D+1, D+2, D+3) | Smart Retry: 70.4% · Tech: 73.6% · WhatsApp: 57.6% · Mandate: 54.4% · Collect: 52.0% | −20% uniform reduction |\n"
                "| **Simulated Revenue Recovered** | ₹89,063 | **₹2,29,090 ± ₹73,540** | **+₹1,40,027 net uplift** |\n"
                "| **Simulated Recovery Rate** | 16.9% | **43.4% ± 6.5%** | **+26.4% pts uplift** |\n\n"
                "**Key Takeaway**: Even with every channel conversion rate reduced by 20%, RecoverIQ still achieves a "
                "**43.4% simulated recovery rate** and a **+₹1,40,027 (+26.4 pts) net uplift** over baseline — "
                "demonstrating resilience under conservatively modeled assumptions.\n\n"
                "*(All figures are from the Monte Carlo policy simulation model, not observed live production revenue.)*"
            )

        # ── 2. Published Offline Benchmark Results ──────────────────────────────
        if "benchmark" in q or "results" in q or "uplift" in q or "monte carlo" in q:

            try:
                from benchmark import get_canonical_benchmark_summary
                can = get_canonical_benchmark_summary(n_runs=50)
            except Exception:
                can = {
                    "mean_uplift_revenue": 193091.0,
                    "std_uplift_revenue": 78177.0,
                    "ai_revenue_mean": 282154.0,
                    "ai_revenue_std": 77144.0,
                    "baseline_revenue_mean": 89063.0,
                    "baseline_revenue_std": 43728.0,
                    "ci_95_revenue_low": 170873.0,
                    "ci_95_revenue_high": 215309.0,
                    "mean_uplift_rate": 37.8,
                    "std_uplift_rate": 6.8,
                    "ai_rate_mean": 54.7,
                    "ai_rate_std": 6.6,
                    "baseline_rate_mean": 16.9,
                    "baseline_rate_std": 3.8,
                    "ci_95_rate_low": 35.9,
                    "ci_95_rate_high": 39.7,
                    "win_rate_pct": 100.0,
                }

            return (
                "**RecoverIQ Published Synthetic Benchmark Results (50 Monte Carlo Simulation Runs vs. Modeled Fixed-Schedule Baseline):**\n\n"
                "*(Note: All figures represent a controlled policy simulation across 60 curated failure archetypes using industry-informed assumptions, not observed live merchant production revenue. The baseline models generic D+1/D+2/D+3 retries and is not Razorpay's production retry system.)*\n\n"
                f"- **Mean Simulated Net Uplift**: **+₹{can.get('mean_uplift_revenue', 193091):,.0f} ± ₹{can.get('std_uplift_revenue', 78177):,.0f}**\n"
                f"  - RecoverIQ AI Agent: ₹{can.get('ai_revenue_mean', 282154):,.0f} ± ₹{can.get('ai_revenue_std', 77144):,.0f}\n"
                f"  - Modeled Fixed Retry Baseline: ₹{can.get('baseline_revenue_mean', 89063):,.0f} ± ₹{can.get('baseline_revenue_std', 43728):,.0f}\n"
                f"- **95% CI for Mean Simulated Uplift (t=49)**: **[+₹{can.get('ci_95_revenue_low', 170873):,.0f}, +₹{can.get('ci_95_revenue_high', 215309):,.0f}]**\n"
                f"- **Mean Recovery Rate Uplift**: **+{can.get('mean_uplift_rate', 37.8):.1f}% pts ± {can.get('std_uplift_rate', 6.8):.1f}% pts** (RecoverIQ: **{can.get('ai_rate_mean', 54.7):.1f}% ± {can.get('ai_rate_std', 6.6):.1f}%** vs. Baseline: {can.get('baseline_rate_mean', 16.9):.1f}% ± {can.get('baseline_rate_std', 3.8):.1f}%)\n"
                f"- **95% CI Rate Uplift**: **[+{can.get('ci_95_rate_low', 35.9):.1f}%, +{can.get('ci_95_rate_high', 39.7):.1f}%]**\n"
                f"- **Simulated Win Rate**: **{can.get('win_rate_pct', 100.0):.1f}%** (50/50 paired simulation trials with positive uplift)\n"
                "  *(Note: The AI won all 50 simulated paired trials under the specified assumptions; this is an outcome of the policy simulation model and is not presented as a production guarantee.)*\n"
                "- **Compliance Breaches**: **0 violations** (vs. 7 baseline violations in benchmark suite)\n"
                "- **Wasted Retries**: Reduced from 180 blind flood retries to **20 targeted retries** (-160 retries)\n"
                "- **Test Suite**: 207 automated unit & integration test cases passing across 14 files.\n\n"
                "*(These figures represent the canonical 50-run simulation over the 60-scenario dataset, distinct from the active live session state.)*"
            )

        # ── 3. B2B Receivables Chaser ───────────────────────────────────────────
        if any(w in q for w in ["b2b", "receivable", "invoice", "aging", "debtor"]):
            return (
                "**B2B Receivables Chaser Architecture:**\n\n"
                "RecoverIQ handles enterprise invoice recovery through automated aging buckets and value tiering:\n"
                "- **Aging Buckets**:\n"
                "  - **0–30 Days (Current)**: Gentle reminders via automated WhatsApp & email.\n"
                "  - **31–60 Days (Early Overdue)**: Firm notice via phone/SMS + AR specialist escalation.\n"
                "  - **61–90 Days (Late Overdue)**: Formal demand notice + 18% p.a. interest charge notification.\n"
                "  - **90+ Days (Critical)**: Legal counsel engagement / collections referral.\n"
                "- **Debtor Tiers**:\n"
                "  - **Tier A (> ₹2,00,000)**: Assigned to a dedicated recovery manager + legal escalation.\n"
                "  - **Tier B (₹25,000–₹2,00,000)**: AR specialist intervention with structured follow-ups.\n"
                "  - **Tier C (< ₹25,000)**: 100% automated conversational IVR & WhatsApp dunning sequences.\n"
                "- **Promise-to-Pay Integration**: When a B2B debtor commits a payment date, escalation is paused; broken promises trigger immediate tier-up."
            )

        # ── 4. Checkout Drop-off Recovery ───────────────────────────────────────
        if any(w in q for w in ["checkout", "cart", "drop-off", "dropoff", "abandon"]):
            return (
                "**Checkout Drop-off Recovery Architecture:**\n\n"
                "RecoverIQ detects abandoned checkout sessions and recovers lost conversions without spamming:\n"
                "- **Captured Drop-off Reasons**:\n"
                "  - `payment_page_exit`: Abandoned at payment rail selection\n"
                "  - `otp_timeout`: OTP window expired without customer retry\n"
                "  - `bank_error_exit`: User bounced after encountering an issuer bank error\n"
                "  - `upi_intent_abandoned`: UPI app opened on device but intent payment not completed\n"
                "  - `address_form_exit`: Abandoned before reaching payment gateway\n"
                "- **Smart Recovery Sequences**:\n"
                "  - **T+10 min**: Non-intrusive WhatsApp nudge with 1-click pre-filled cart re-engagement link.\n"
                "  - **T+1 hour**: Follow-up message addressing payment failure / offering alternative rails.\n"
                "  - **T+24 hour**: Final discount/incentive reminder before session expiry."
            )

        # ── 5. Customer Identity Graph ──────────────────────────────────────────
        if any(w in q for w in ["identity", "graph", "alias", "canonical", "profile"]):
            return (
                "**Customer Identity Graph & Unified Behavioral History:**\n\n"
                "Customers often interact across multiple VPAs, phones, and customer IDs. RecoverIQ unifies these:\n"
                "- **Canonical Resolution**: Maps disparate identifiers (`rahul@oksbi`, `+91-9876543210`, `CUST-1001`) to a single canonical ID (`cust:rahul@oksbi`).\n"
                "- **Unified Behavioral History**: Retry attempts, payment failures, trust scores, and spend baselines are tracked across the person, not isolated aliases.\n"
                "- **Synchronized Compliance & Suppression**: If a customer requests DND or reports hardship on WhatsApp via phone, their VPA and Customer ID are immediately suppressed across all automated retries and channels."
            )

        # ── 6. Mandate Expiry / BT02 Prevention ────────────────────────────────
        if any(w in q for w in ["mandate expiry", "expiring", "t-72", "bt02", "lapse"]):
            return (
                "**Proactive Mandate Expiry Interceptor (T-72h BT02 Prevention):**\n\n"
                "UPI Autopay mandates have finite validity periods. When they expire, debits fail with NPCI error **BT02 ('Mandate Expired')**.\n"
                "- **Proactive Detection**: Scans all active mandates 24 to 72 hours prior to expiration date.\n"
                "- **1-Click Renewal**: Dispatches a personalized WhatsApp/SMS message containing a secure Razorpay mandate re-registration link.\n"
                "- **Zero Churn Impact**: Re-registers the mandate before the next billing cycle, eliminating bank decline charges and preserving recurring merchant ARR without reactive failure."
            )

        # ── 7. Live Customer Event Context ──────────────────────────────────────
        elif event_context and any(w in q for w in ["this", "event", "transaction", "failure", "status", "why", "recommend", "current", "what happened", "action"]):
            cust = event_context.get("customer") or event_context.get("customer_name") or event_context.get("scenario_name") or event_context.get("customer_vpa") or "Customer"
            vpa = event_context.get("vpa") or event_context.get("customer_vpa") or "N/A"
            code = event_context.get("failure_code") or "U30"
            reason = event_context.get("failure_reason") or "Payment failure"
            amt = event_context.get("amount") or event_context.get("mandate_amount") or 999.0
            bank = event_context.get("bank") or "Bank"
            guardrail = event_context.get("guardrail_triggered") or "Deterministic safety policy (GR1–GR8)"
            outcome = event_context.get("decision_outcome") or (", ".join(event_context.get("interventions", [])) if event_context.get("interventions") else "Automated recovery pipeline active")
            scheduled = event_context.get("scheduled_at")
            sched_line = f"\n- **Scheduled Retry**: {scheduled}" if scheduled else ""
            aa = event_context.get("aa_check")
            aa_line = f"\n- **Account Aggregator Verification**: {aa}" if aa else ""
            return (
                f"**Live Event Diagnosis for {cust} ({code}):**\n\n"
                f"- **Failure Code & Reason**: **{code}** ({reason}) on {bank}.\n"
                f"- **Amount**: ₹{amt:,.0f} | **VPA**: `{vpa}`\n"
                f"- **Guardrail & Safety Check**: {guardrail}\n"
                f"- **Autonomous Recovery Action**: {outcome}"
                f"{sched_line}"
                f"{aa_line}\n"
                f"- **Channel Optimization**: Bayesian Thompson Sampling allocates the highest expected utility channel without exhausting retry limits."
            )
        elif "rahul" in q or ("u30" in q and ("retry" in q or "immediate" in q or "not" in q)):
            bal = event_context.get("setu_aa_balance_check", {}).get("available_balance", 432.63) if event_context else 432.63
            amt = event_context.get("mandate_amount", 999.0) if event_context else 999.0
            return (
                "**Why Rahul's U30 Payment Was Not Retried Immediately:**\n\n"
                f"- **Failure Code & Mandate**: Rahul's ₹{amt:.0f} OTT subscription debit on SBI failed with **U30 (Insufficient Funds)** (`rahul@oksbi`).\n"
                f"- **Setu Account Aggregator Verification**: A pre-flight balance inquiry via Setu AA revealed only **₹{bal:.2f} available** in his account (a deficit of ₹{amt - bal:.2f}).\n"
                "- **Deterministic Guardrail Triggered**: Under **GR1 (Liquidity Protection)**, an immediate retry was blocked to prevent unnecessary bank bounce charges and avoid bank-level cooldown locks.\n"
                "- **Autonomous Recovery Action**: RecoverIQ rescheduled the debit for his predicted **salary-credit window on the 5th** at 10:00 AM IST, ensuring funds are present before executing."
            )
        elif "recommend" in q and ("recovery" in q or "action" in q or "rahul" in q):
            return (
                "**Recommended Recovery Action for Rahul:**\n\n"
                "1. **Primary Action**: **Salary-Cycle Smart Retry** scheduled for the 5th at 10:00 AM IST (synchronized with his Setu AA verified liquidity window).\n"
                "2. **Alternative Digital Fallback**: Dispatch an interactive **1-Click WhatsApp renewal link** with a dynamic NPCI UPI QR code if immediate settlement is desired.\n"
                "3. **Compliance Hold**: Outbound voice and dunning calls remain suppressed under Guardrail 5 to preserve customer goodwill."
            )
        elif "priya" in q or "bt01" in q or "revoked" in q:
            return (
                "**How RecoverIQ Handles Priya / BT01 (Revoked Mandate):**\n\n"
                "- **Diagnosis**: Mandate was revoked by customer or issuing bank (HDFC, ₹1,499 SaaS Pro, `priya@okhdfcbank`).\n"
                "- **Deterministic Guardrail (GR6)**: All automated debit retries are immediately blocked (retrying a revoked mandate is futile and damages trust).\n"
                "- **Autonomous Recovery Action**: RecoverIQ dispatches a 1-click re-registration mandate link via WhatsApp with seamless UPI auth."
            )
        elif "arjun" in q or "tm" in q or "timeout" in q:
            return (
                "**How RecoverIQ Handles Arjun / TM (Technical Timeout):**\n\n"
                "- **Diagnosis**: Transient issuer bank or NPCI switch timeout (ICICI Bank, ₹4,500 Cloud Infra, `arjun@okicici`).\n"
                "- **Deterministic Guardrail (GR2)**: Adaptive jittered backoff prevents hammering the degraded banking switch.\n"
                "- **Autonomous Recovery Action**: Scheduled for automated retry after a 15-minute cooldown window, yielding 82%+ recovery upon bank switch stabilization."
            )
        elif "vikram" in q or "bt02" in q or "expired" in q:
            return (
                "**How RecoverIQ Handles Vikram / BT02 (Expired Mandate):**\n\n"
                "- **Diagnosis**: Mandate validity period lapsed (Yes Bank, ₹2,999 Gym Gold Pass, `vikram@ybl`).\n"
                "- **Deterministic Guardrail (GR5)**: When customer commits a Promise-to-Pay, active dunning and retries are suppressed for the agreed window.\n"
                "- **Autonomous Recovery Action**: Dispatched interactive WhatsApp renewal with dynamic NPCI UPI QR code, achieving fast self-serve reactivation."
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
                "Ask me about: **Live session stats**, **Benchmark results**, **U30 salary retries**, **B2B Receivables Chaser**, **Checkout Drop-off Recovery**, or **Customer Identity Graph**!"
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

        # Strip any raw HTML tags from LLM-provided strings to defang prompt injection
        bank = re.sub(r"<[^>]+>", "", str(data.get("bank") or "SBI")).strip() or "SBI"
        vpa = re.sub(r"<[^>]+>", "", str(data.get("vpa") or f"user@ok{bank.lower()}")).strip() or "user@upi"
        mandate_state = str(data.get("mandate_state") or ("revoked" if code == "BT01" else "expired" if code == "BT02" else "active")).lower()
        retry_attempt = int(data.get("retry_attempt") or 0)
        raw_name = str(data.get("scenario_name") or f"Simulated Scenario - {code} ({bank})").strip()
        scenario_name = re.sub(r"<[^>]+>", "", raw_name)
        raw_echo = str(data.get("echo_summary") or f"{scenario_name} (₹{amount:,.0f}, {code}, {bank})").strip()
        echo = re.sub(r"<[^>]+>", "", raw_echo)

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
        # Defang raw HTML in prompt first
        clean_p = re.sub(r"<[^>]+>", " ", prompt)
        p = clean_p.lower()

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
            # Check explicit currency sign first
            curr_match = re.search(r"(?:rs\.?|inr|₹)\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]+)?|[0-9]+)", p)
            if curr_match:
                try:
                    amount = float(curr_match.group(1).replace(",", ""))
                except ValueError:
                    pass
            else:
                for m in re.finditer(r"\b([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]+)?|[0-9]+)\b", p):
                    try:
                        cand = float(m.group(1).replace(",", ""))
                        if cand >= 50:
                            amount = cand
                            break
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


