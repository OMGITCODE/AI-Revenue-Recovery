"""
whatsapp_inbound.py — 2-Way Conversational Recovery & Hinglish Inbound Handler
=============================================================================
Processes inbound customer replies from WhatsApp notifications into 5 actionable
intent buckets:

  1. PROMISE      — Customer commits to pay on a date/time
                    -> Creates/updates Promise-to-Pay (P2P), suppresses nudges until deadline.
  2. ALREADY_PAID — Customer claims transaction already debited
                    -> Enters 24h bank reconciliation verification hold, suppresses outbound messages.
  3. DISPUTE      — Customer disputes charges or requests cancellation/refund
                    -> Instantly blocks automated retries, escalates to human support.
  4. HARDSHIP     — Customer reports financial distress, job loss, or medical emergency
                    -> Grants 30-day debt relief/subscription pause, halts dunning sequences.
  5. WRONG_NUMBER — Customer indicates wrong contact info or requests permanent opt-out
                    -> Permanently suppresses phone/VPA in compliance registry (RBI/TRAI anti-harassment).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime, timezone, timedelta

from .recovery_ledger import ledger as recovery_ledger
from .promise_tracker import promise_tracker
from .customer_identity import customer_identity_registry, normalize_identifier
from ..integrations.llm_classifier import llm_classifier
from ..utils.logger import get_logger

logger = get_logger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


class InboundIntent(str, Enum):
    PROMISE      = "promise"
    ALREADY_PAID = "already_paid"
    DISPUTE      = "dispute"
    HARDSHIP     = "hardship"
    WRONG_NUMBER = "wrong_number"
    UNKNOWN      = "unknown"


# ── Suppression & Blacklist Registry ──────────────────────────────────────────

class ContactSuppressionRegistry:
    """
    Maintains active and permanent suppression lists to guarantee compliance
    with RBI Fair Practices Code and TRAI DND regulations across all customer aliases.
    """

    def __init__(self):
        self._permanent_blacklist: Set[str] = set()       # Wrong numbers / opt-outs
        self._active_holds: Dict[str, Dict] = {}          # Disputes, hardship, already-paid

    def suppress_permanently(self, identifier: str, reason: str = "wrong_number"):
        """Permanent suppression — no automated attempts or nudges allowed ever across all aliases."""
        clean = normalize_identifier(identifier)
        if clean:
            self._permanent_blacklist.add(clean)
            cid = customer_identity_registry.resolve_canonical_id(clean)
            self._permanent_blacklist.add(cid)
            for alias in customer_identity_registry.get_all_aliases(clean):
                self._permanent_blacklist.add(alias)
            logger.warning("[COMPLIANCE] Permanent contact suppression activated for %s (%s) | reason=%s", clean, cid, reason)

    def set_hold(self, identifier: str, hold_type: str, duration_hours: int = 72, reason: str = ""):
        """Temporary hold — suppresses automated retries and nudges for a duration across all aliases."""
        clean = normalize_identifier(identifier)
        if clean:
            expires_at = datetime.now(IST) + timedelta(hours=duration_hours)
            hold_data = {
                "hold_type": hold_type,
                "expires_at": expires_at,
                "reason": reason,
            }
            self._active_holds[clean] = hold_data
            cid = customer_identity_registry.resolve_canonical_id(clean)
            self._active_holds[cid] = hold_data
            for alias in customer_identity_registry.get_all_aliases(clean):
                self._active_holds[alias] = hold_data
            logger.info("[HOLD] Active hold for %s (%s) | type=%s | expires=%s", clean, cid, hold_type, expires_at.strftime("%Y-%m-%d %H:%M IST"))

    def is_suppressed(self, identifier: str) -> Tuple[bool, Optional[str]]:
        """Returns (is_suppressed, reason) if any active or permanent suppression applies to this person."""
        clean = normalize_identifier(identifier)
        if not clean:
            return False, None

        # Check all aliases belonging to this person
        aliases_to_check = {clean}
        cid = customer_identity_registry.resolve_canonical_id(clean)
        aliases_to_check.add(cid)
        aliases_to_check.update(customer_identity_registry.get_all_aliases(clean))

        for alias in aliases_to_check:
            if alias in self._permanent_blacklist:
                return True, "permanently_blacklisted_wrong_number"

        now = datetime.now(IST)
        for alias in aliases_to_check:
            if alias in self._active_holds:
                hold = self._active_holds[alias]
                if now < hold["expires_at"]:
                    return True, f"active_hold_{hold['hold_type']}"
                else:
                    del self._active_holds[alias]

        return False, None

    def reset(self):
        self._permanent_blacklist.clear()
        self._active_holds.clear()


suppression_registry = ContactSuppressionRegistry()


# ── Inbound Intent Classifier ──────────────────────────────────────────────────

@dataclass
class InboundClassificationResult:
    intent: InboundIntent
    confidence: float
    reasoning: str
    extracted_deadline_hours: Optional[int]
    action_taken: str
    reply_text: str
    matched_keywords: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 2),
            "reasoning": self.reasoning,
            "extracted_deadline_hours": self.extracted_deadline_hours,
            "action_taken": self.action_taken,
            "reply_text": self.reply_text,
            "matched_keywords": self.matched_keywords,
        }


class WhatsAppInboundHandler:
    """
    Classifies conversational Hinglish/English inbound replies and routes
    them to deterministic recovery, relief, dispute, or compliance workflows.
    """

    # Keyword dictionaries for Hinglish / Hindi transliteration & English
    PATTERNS: Dict[InboundIntent, List[str]] = {
        InboundIntent.PROMISE: [
            r"\bkal\b", r"\btomorrow\b", r"\bsalary\b", r"\btarikh\b", r"\btaareekh\b",
            r"\bpay\s+kar\s+dunga\b", r"\bkar\s+dungi\b", r"\bkarunga\b", r"\bkarungi\b",
            r"\bpakka\b", r"\bpromise\b", r"\bsham\s+tak\b", r"\bevening\b",
            r"\b5th\b", r"\b1st\b", r"\b7th\b", r"\b10th\b", r"\bweekend\b",
            r"\bmonday\b", r"\bnext\s+week\b", r"\bdedunga\b", r"\bde\s+dunga\b",
            r"\bthoda\s+time\b", r"\bgive\s+me\s+time\b", r"\bcommit\b", r"\bwill\s+pay\b",
            r"\bshaam\b", r"\baaj\s+raat\b", r"\btonight\b", r"\bparso\b"
        ],
        InboundIntent.ALREADY_PAID: [
            r"\balready\s+paid\b", r"\bkat\s+gaye\b", r"\bkat\s+gaya\b", r"\bkat\s+chuke\b", r"\bcut\s+gaya\b",
            r"\bdebit\s+ho\s+gaya\b", r"\bdebit\s+ho\s+chuka\b", r"\bpayment\s+done\b",
            r"\balready\s+transferred\b", r"\bupi\s+se\s+bhej\s+diya\b", r"\bde\s+diya\b",
            r"\bcheck\s+your\s+statement\b", r"\bcheck\s+bank\s+statement\b", r"\bpaise\s+kat\s+gaye\b",
            r"\bpaise\s+kat\s+gaye\b", r"\bpaid\s+already\b",
            r"\bpaise\s+chale\s+gaye\b", r"\bpaise\s+bhej\s+diye\b"
        ],
        InboundIntent.DISPUTE: [
            r"\bcancel\b", r"\bfraud\b", r"\bscam\b", r"\bwrong\s+charge\b",
            r"\bservice\s+nahi\s+mili\b", r"\brefund\b", r"\bmaine\s+nahi\s+kiya\b",
            r"\bdidn'?t\s+buy\b", r"\bdispute\b", r"\bconsumer\s+court\b",
            r"\bband\s+karo\b", r"\bdhokha\b", r"\bfake\b", r"\bunauthorized\b",
            r"\bgalat\s+kata\b", r"\bcheating\b", r"\bcomplaint\b"
        ],
        InboundIntent.HARDSHIP: [
            r"\bjob\s+chali\s+gayi\b", r"\blost\s+my\s+job\b", r"\bmedical\b",
            r"\bhospital\b", r"\billness\b", r"\bpaise\s+nahi\s+hai\b", r"\bno\s+money\b",
            r"\bcrisis\b", r"\bhardship\b", r"\bfinancial\s+problem\b",
            r"\bcan'?t\s+afford\b", r"\bmajboori\b", r"\btanki\b", r"\bkharab\s+halat\b"
        ],
        InboundIntent.WRONG_NUMBER: [
            r"\bwrong\s+number\b", r"\bgalat\s+number\b", r"\bye\s+kaun\s+hai\b",
            r"\bwrong\s+person\b", r"\bstop\s+messaging\b", r"\bnot\s+my\s+account\b",
            r"\bwho\s+are\s+you\b", r"\bdon'?t\s+call\b", r"\bopt\s+out\b",
            r"\bunsubscribe\b", r"\bdnd\b", r"\bnot\s+me\b", r"\bkaun\s+ho\b",
            r"\bspam\b", r"\bdo\s+not\s+contact\b"
        ]
    }

    def _classify_message_regex(self, message: str) -> Tuple[InboundIntent, float, List[str], Optional[int]]:
        """Deterministic regex-based classification fallback."""
        text = message.lower().strip()
        matched: Dict[InboundIntent, List[str]] = {}

        for intent, patterns in self.PATTERNS.items():
            matches = [p for p in patterns if re.search(p, text)]
            if matches:
                matched[intent] = matches

        if not matched:
            return InboundIntent.UNKNOWN, 0.30, [], None

        # Priority order when multiple patterns trigger
        priority_order = [
            InboundIntent.WRONG_NUMBER,
            InboundIntent.DISPUTE,
            InboundIntent.HARDSHIP,
            InboundIntent.ALREADY_PAID,
            InboundIntent.PROMISE,
        ]

        top_intent = next((i for i in priority_order if i in matched), InboundIntent.UNKNOWN)
        keywords = matched.get(top_intent, [])
        confidence = min(0.98, 0.70 + (len(keywords) * 0.10))

        # Extract deadline if PROMISE
        deadline_hours = None
        if top_intent == InboundIntent.PROMISE:
            if any(k in text for k in ["kal", "tomorrow", "24"]):
                deadline_hours = 24
            elif any(k in text for k in ["sham", "evening", "shaam", "aaj raat", "tonight"]):
                deadline_hours = 12
            elif any(k in text for k in ["salary", "5th", "7th", "1st", "10th", "next week", "weekend"]):
                deadline_hours = 96
            else:
                deadline_hours = 48

        return top_intent, confidence, keywords, deadline_hours

    async def classify_message(self, message: str) -> Tuple[InboundIntent, float, List[str], Optional[int], str]:
        """
        Classifies inbound text using LLM first; gracefully falls back to deterministic regex.
        Returns: (intent, confidence, matched_keywords: List[str], deadline_hours: Optional[int], reasoning: str)
        """
        # 1. Attempt LLM classification
        llm_result = await llm_classifier.classify(message)
        if llm_result:
            raw_intent = llm_result.get("intent", "").lower().strip()
            try:
                intent = InboundIntent(raw_intent)
            except ValueError:
                intent = InboundIntent.UNKNOWN

            conf = float(llm_result.get("confidence", 0.85))
            keywords: List[str] = []
            deadline_hours = llm_result.get("extracted_deadline_hours")
            reasoning = f"[LLM] {llm_result.get('reasoning', 'Classified via LLM')} (conf: {conf:.0%})"
            return intent, conf, keywords, deadline_hours, reasoning

        # 2. Fallback to deterministic regex
        intent, conf, keywords, deadline_hours = self._classify_message_regex(message)
        reasoning = (
            f"[Regex] Classified as '{intent.value}' based on keywords {keywords} (conf: {conf:.0%})."
            if keywords
            else f"[Regex] No keywords matched; default '{intent.value}'."
        )
        return intent, conf, keywords, deadline_hours, reasoning

    def classify_message_sync(self, message: str) -> Tuple[InboundIntent, float, List[str], Optional[int], str]:
        """Synchronous deterministic regex-only classification helper."""
        intent, conf, keywords, deadline_hours = self._classify_message_regex(message)
        reasoning = (
            f"[Regex] Classified as '{intent.value}' based on keywords {keywords} (conf: {conf:.0%})."
            if keywords
            else f"[Regex] No keywords matched; default '{intent.value}'."
        )
        return intent, conf, keywords, deadline_hours, reasoning

    async def handle_inbound(
        self,
        from_phone: str,
        customer_vpa: str,
        message: str,
        amount: float = 999.0,
        customer_id: str = "",
    ) -> InboundClassificationResult:
        """
        Processes the inbound WhatsApp message, executes system state transitions,
        and returns an empathetic Hinglish/English auto-response.
        """
        if from_phone or customer_vpa or customer_id:
            customer_identity_registry.resolve_canonical_id(customer_vpa, from_phone, customer_id)

        intent, conf, keywords, deadline_hours, reasoning = await self.classify_message(message)
        identifier = customer_vpa or from_phone or customer_id

        if intent == InboundIntent.PROMISE:
            # 1) Register / update Promise-to-Pay
            hours = deadline_hours or 48
            p = promise_tracker.create(
                vpa=customer_vpa or f"user_{from_phone[-4:] if from_phone else 'anon'}@upi",
                customer_id=customer_id,
                phone=from_phone,
                amount=amount,
                bank="UPI",
                failure_code="U30",
                deadline_hours=hours,
                channel="whatsapp",
                notes=f"Inbound commitment from customer: '{message}'",
            )
            # Log to audit ledger
            recovery_ledger.log(
                event_type="p2p",
                vpa=customer_vpa or from_phone or customer_id,
                amount=amount,
                reasoning=f"2-Way Inbound: Customer committed payment within {hours}h ('{message}'). Auto P2P #{p.promise_id} created; automated nudges suppressed until deadline.",
                confidence=conf,
                channel="whatsapp",
            )
            action_taken = f"Created P2P promise #{p.promise_id} with {hours}h deadline. Nudges suppressed."
            reply_text = (
                f"Shukriya! Humne aapka commitment note kar liya hai ({hours} ghante tak). "
                f"Smart retries tab tak pause hain. Pay karein: https://rzp.io/l/pay-{p.promise_id[:6].lower()}"
            )

        elif intent == InboundIntent.ALREADY_PAID:
            # 2) Already paid claim -> pause retries for 24h verification
            suppression_registry.set_hold(identifier, hold_type="already_paid_reconciliation", duration_hours=24, reason=message)
            recovery_ledger.log(
                event_type="aa_check",
                vpa=customer_vpa or from_phone or customer_id,
                amount=amount,
                reasoning=f"2-Way Inbound: Customer stated already paid ('{message}'). Outbound retries and nudges paused for 24h reconciliation check.",
                confidence=conf,
                channel="whatsapp",
            )
            action_taken = "Initiated 24h bank reconciliation verification hold. Paused smart retries and nudges."
            reply_text = (
                "Dhanyawaad! Hum aapka payment status bank ke saath verify kar rahe hain. "
                "Verification hone tak aapko koi further messages ya debit attempts nahi aayenge."
            )

        elif intent == InboundIntent.DISPUTE:
            # 3) Dispute / cancellation -> stop retries, escalate to human
            suppression_registry.set_hold(identifier, hold_type="dispute_escalation", duration_hours=720, reason=message)
            recovery_ledger.log(
                event_type="escalate",
                vpa=customer_vpa or from_phone or customer_id,
                amount=amount,
                reasoning=f"2-Way Inbound: Customer raised dispute/cancellation ('{message}'). All automated retries halted; ticket routed to priority human support.",
                confidence=conf,
                channel="escalation",
            )
            action_taken = "Halted automated retries. Escalated ticket to priority dispute resolution queue."
            reply_text = (
                "Aapka dispute note kar liya gaya hai (Ticket #DISP-"
                f"{uuid.uuid4().hex[:6].upper()}). Hamari support team aapse 24 ghante mein contact karegi. "
                "Automated debits rok diye gaye hain."
            )

        elif intent == InboundIntent.HARDSHIP:
            # 4) Financial hardship -> grant 30-day grace/pause
            suppression_registry.set_hold(identifier, hold_type="financial_hardship_pause", duration_hours=720, reason=message)
            recovery_ledger.log(
                event_type="guardrail",
                vpa=customer_vpa or from_phone or customer_id,
                amount=amount,
                reasoning=f"2-Way Inbound: Financial hardship reported ('{message}'). Granted 30-day compassionate payment pause per RBI Fair Practices Code.",
                confidence=conf,
                channel="whatsapp",
            )
            action_taken = "Granted 30-day compassionate relief hold. Paused all dunning and collections."
            reply_text = (
                "Hum aapki situation samajhte hain. Aapka subscription 30 dino ke liye pause kar diya gaya hai "
                "bina kisi extra charge ke. Aap aasaani se ready hone par resume kar sakte hain."
            )

        elif intent == InboundIntent.WRONG_NUMBER:
            # 5) Wrong number / unsubscribe -> permanent blacklisting
            suppression_registry.suppress_permanently(identifier, reason=f"Customer reported wrong number/opt-out: '{message}'")
            recovery_ledger.log(
                event_type="guardrail",
                vpa=customer_vpa or from_phone or customer_id,
                amount=amount,
                reasoning=f"2-Way Inbound [COMPLIANCE]: Customer flagged wrong number/opt-out ('{message}'). Permanently blacklisted identifier to prevent harassment.",
                confidence=conf,
                channel="whatsapp",
            )
            action_taken = "Permanently blacklisted number and VPA across all recovery channels."
            reply_text = (
                "Asuvidha ke liye maafi chahte hain. Yeh number humari system se permanently remove kar diya gaya hai. "
                "Aapko aage se koi message nahi aayega."
            )

        else:
            # General / Unknown
            recovery_ledger.log(
                event_type="decide",
                vpa=customer_vpa or from_phone or customer_id,
                amount=amount,
                reasoning=f"2-Way Inbound: General response received ('{message}'). Provided payment self-service links.",
                confidence=conf,
                channel="whatsapp",
            )
            action_taken = "Dispatched general self-serve help response."
            reply_text = (
                "Namaste! Aapke payment query ke liye: Bill check karein: https://rzp.io/l/help "
                "ya help ke liye support@recoveriq.ai par likhein."
            )

        return InboundClassificationResult(
            intent=intent,
            confidence=conf,
            reasoning=reasoning,
            extracted_deadline_hours=deadline_hours,
            action_taken=action_taken,
            reply_text=reply_text,
            matched_keywords=keywords,
        )


whatsapp_inbound_handler = WhatsAppInboundHandler()
