"""
Checkout Drop-off Recovery Agent.

Detects abandoned checkout sessions and recovers them via:
  1. Smart re-engagement link (short URL with pre-filled cart)
  2. WhatsApp / SMS nudge with Hinglish message
  3. Timed follow-up sequence (T+10min, T+1h, T+24h)

Drop-off reasons captured:
  - payment_page_exit     — left at payment method selection
  - otp_timeout           — OTP expired, didn't retry
  - bank_error_exit       — saw error page, didn't retry
  - upi_intent_abandoned  — UPI app opened but not completed
  - address_form_exit     — left before reaching payment
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, List, Optional

from ..utils.logger import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


# ── Drop-off reason ───────────────────────────────────────────────────────────

class DropOffReason(str, Enum):
    PAYMENT_PAGE_EXIT     = "payment_page_exit"
    OTP_TIMEOUT           = "otp_timeout"
    BANK_ERROR_EXIT       = "bank_error_exit"
    UPI_INTENT_ABANDONED  = "upi_intent_abandoned"
    ADDRESS_FORM_EXIT     = "address_form_exit"
    SESSION_EXPIRED       = "session_expired"
    UNKNOWN               = "unknown"


class RecoveryStatus(str, Enum):
    OPEN      = "open"
    CONTACTED = "contacted"
    RECOVERED = "recovered"
    EXPIRED   = "expired"


# ── Checkout session ──────────────────────────────────────────────────────────

@dataclass
class CheckoutSession:
    session_id:       str
    customer_vpa:     str
    customer_phone:   str
    cart_amount:      float
    merchant:         str
    drop_off_reason:  DropOffReason
    dropped_at:       datetime
    status:           RecoveryStatus = RecoveryStatus.OPEN
    recovery_url:     str = ""
    nudges_sent:      int = 0
    last_nudge_at:    Optional[datetime] = None
    recovered_at:     Optional[datetime] = None
    recovery_message: str = ""     # the Hinglish message sent

    def to_dict(self) -> dict:
        return {
            "session_id":      self.session_id,
            "customer_vpa":    self.customer_vpa,
            "customer_phone":  self.customer_phone,
            "cart_amount":     self.cart_amount,
            "merchant":        self.merchant,
            "drop_off_reason": self.drop_off_reason.value,
            "dropped_at":      self.dropped_at.isoformat(),
            "status":          self.status.value,
            "recovery_url":    self.recovery_url,
            "nudges_sent":     self.nudges_sent,
            "last_nudge_at":   self.last_nudge_at.isoformat() if self.last_nudge_at else None,
            "recovered_at":    self.recovered_at.isoformat() if self.recovered_at else None,
            "recovery_message": self.recovery_message,
        }


# ── Hinglish message templates ────────────────────────────────────────────────

HINGLISH_TEMPLATES: Dict[str, Dict[DropOffReason, str]] = {
    "t10min": {
        DropOffReason.PAYMENT_PAGE_EXIT:    "Arey yaar! Aapka cart abhi bhi wait kar raha hai 🛒 Sirf ek click baaki tha. Complete karein: {url}",
        DropOffReason.OTP_TIMEOUT:          "OTP expire ho gaya kya? Koi baat nahi — yahan se fresh try karein: {url}  Hum hain na 😊",
        DropOffReason.BANK_ERROR_EXIT:      "Bank ne thoda problem kiya? Try karein dobara — usually 2nd try mein ho jaata hai ✅  {url}",
        DropOffReason.UPI_INTENT_ABANDONED: "UPI payment adhoori reh gayi 😕 Ek baar aur try karein, 30 second ka kaam hai: {url}",
        DropOffReason.ADDRESS_FORM_EXIT:    "Address save karna bhool gaye? Jaldi complete karein — stock limited hai! {url}",
        DropOffReason.SESSION_EXPIRED:      "Aapka session expire ho gaya but cart saved hai 💾 Wapas aayein: {url}",
        DropOffReason.UNKNOWN:              "Hi! Lagta hai payment complete nahi hua. Cart abhi bhi available hai: {url}",
    },
    "t1h": {
        DropOffReason.PAYMENT_PAGE_EXIT:    "Ek ghanta ho gaya par aapka order abhi bhi pending hai 📦 Kya koi help chahiye? {url}",
        DropOffReason.OTP_TIMEOUT:          "Abhi bhi time hai! Apna order complete karein: {url}  Koi issue ho to reply karein.",
        DropOffReason.BANK_ERROR_EXIT:      "Bank issue resolve hua? Ab try karein: {url}  Ya UPI/card se bhi pay kar sakte hain.",
        DropOffReason.UPI_INTENT_ABANDONED: "Kya UPI se koi dikkat hai? Net banking ya card bhi try kar sakte hain: {url}",
        DropOffReason.ADDRESS_FORM_EXIT:    "Sirf address baki tha! 2 minute mein complete hoga: {url}",
        DropOffReason.SESSION_EXPIRED:      "Fresh link: {url}  Aapka cart safe hai, items available hain.",
        DropOffReason.UNKNOWN:              "Reminder: Aapka payment pending hai. Agar koi problem ho: {url}",
    },
    "t24h": {
        DropOffReason.PAYMENT_PAGE_EXIT:    "Last reminder 🔔 Kal jo cart chhoda tha, aaj complete karein. Stock kam ho raha hai: {url}",
        DropOffReason.OTP_TIMEOUT:          "Final nudge: Aapka order aaj bhi place ho sakta hai: {url}  Kal link expire ho jayega.",
        DropOffReason.BANK_ERROR_EXIT:      "Last chance! Bank issue tha to ab try karein — bahut baar resolve ho jaata hai: {url}",
        DropOffReason.UPI_INTENT_ABANDONED: "Aakhri yaad-dihani 😊 Agar chahein to complete karein: {url}  Nahi chahiye to ignore karein.",
        DropOffReason.ADDRESS_FORM_EXIT:    "Kal se cart nahi milega — aaj hi complete karein: {url}",
        DropOffReason.SESSION_EXPIRED:      "Aapka cart kal delete ho jayega. Save karna ho to abhi complete karein: {url}",
        DropOffReason.UNKNOWN:              "Last reminder: Order complete nahi hua. Link kal expire hoga: {url}",
    },
}

ENGLISH_TEMPLATES: Dict[str, Dict[DropOffReason, str]] = {
    "t10min": {
        DropOffReason.PAYMENT_PAGE_EXIT:    "Hi! You were just one click away. Complete your order here: {url}",
        DropOffReason.OTP_TIMEOUT:          "OTP expired? No worries — try again with a fresh link: {url}",
        DropOffReason.BANK_ERROR_EXIT:      "Looks like your bank had a hiccup. Try again — it usually works on the 2nd attempt: {url}",
        DropOffReason.UPI_INTENT_ABANDONED: "Your UPI payment wasn't completed. Quick retry (30 secs): {url}",
        DropOffReason.ADDRESS_FORM_EXIT:    "You were so close! Complete your address and place the order: {url}",
        DropOffReason.SESSION_EXPIRED:      "Your session expired but your cart is saved. Come back: {url}",
        DropOffReason.UNKNOWN:              "Hi! It looks like your payment didn't go through. Your cart is still waiting: {url}",
    },
}


# ── Recovery Agent ────────────────────────────────────────────────────────────

class CheckoutRecoveryAgent:
    """
    Manages checkout drop-off sessions and recovery nudges.

    Nudge sequence: T+10min → T+1h → T+24h
    After 3 nudges, session marked as expired (no more harassment).
    """

    NUDGE_WINDOWS = [10/60, 1, 24]  # hours

    def __init__(self):
        self._sessions: Dict[str, CheckoutSession] = {}

    # ── Session management ────────────────────────────────────────────────────

    def has_active(self, customer_vpa: str, cart_amount: float) -> bool:
        return any(
            s.customer_vpa == customer_vpa and abs(s.cart_amount - cart_amount) < 1 and s.status in (RecoveryStatus.OPEN, RecoveryStatus.CONTACTED)
            for s in self._sessions.values()
        )

    def record_drop_off(
        self,
        customer_vpa:    str,
        customer_phone:  str,
        cart_amount:     float,
        merchant:        str,
        drop_off_reason: str = "unknown",
        language:        str = "hinglish",
    ) -> CheckoutSession:
        """Record a new checkout abandonment."""
        existing = next((s for s in self._sessions.values() if s.customer_vpa == customer_vpa and abs(s.cart_amount - cart_amount) < 1 and s.status in (RecoveryStatus.OPEN, RecoveryStatus.CONTACTED)), None)
        if existing:
            return existing

        reason = DropOffReason(drop_off_reason) if drop_off_reason in DropOffReason._value2member_map_ else DropOffReason.UNKNOWN
        session_id   = "CHK-" + str(uuid.uuid4())[:6].upper()
        recovery_url = f"https://rzp.io/l/recover-{session_id.lower()}"

        session = CheckoutSession(
            session_id      = session_id,
            customer_vpa    = customer_vpa,
            customer_phone  = customer_phone,
            cart_amount     = cart_amount,
            merchant        = merchant,
            drop_off_reason = reason,
            dropped_at      = datetime.now(IST),
            recovery_url    = recovery_url,
        )
        self._sessions[session_id] = session

        # Fire first nudge immediately
        msg = self._send_nudge(session, "t10min", language)
        logger.info(
            "Checkout drop-off recorded: %s | vpa=%s | ₹%.0f | reason=%s",
            session_id, customer_vpa, cart_amount, reason.value,
        )
        return session

    def mark_recovered(self, session_id: str) -> Optional[CheckoutSession]:
        s = self._sessions.get(session_id)
        if s:
            s.status       = RecoveryStatus.RECOVERED
            s.recovered_at = datetime.now(IST)
            logger.info("Checkout RECOVERED: %s | ₹%.0f", session_id, s.cart_amount)
        return s

    # ── Nudge ────────────────────────────────────────────────────────────────

    def _send_nudge(self, session: CheckoutSession, window: str, language: str = "hinglish") -> str:
        templates = HINGLISH_TEMPLATES if language == "hinglish" else ENGLISH_TEMPLATES
        tmpl_map  = templates.get(window, HINGLISH_TEMPLATES["t10min"])
        tmpl      = tmpl_map.get(session.drop_off_reason, tmpl_map[DropOffReason.UNKNOWN])
        msg       = tmpl.format(url=session.recovery_url)

        session.nudges_sent   += 1
        session.last_nudge_at  = datetime.now(IST)
        session.recovery_message = msg
        if session.nudges_sent >= 3:
            session.status = RecoveryStatus.CONTACTED

        logger.info(
            "[Checkout Nudge %s → %s] %s",
            window, session.customer_vpa, msg,
        )
        return msg

    # ── Queries ───────────────────────────────────────────────────────────────

    def all_sessions(self) -> List[CheckoutSession]:
        return sorted(self._sessions.values(), key=lambda s: s.dropped_at, reverse=True)

    def stats(self) -> dict:
        all_ = list(self._sessions.values())
        recovered = [s for s in all_ if s.status == RecoveryStatus.RECOVERED]
        total_cart = sum(s.cart_amount for s in all_)
        recovered_amt = sum(s.cart_amount for s in recovered)
        return {
            "total_sessions":   len(all_),
            "recovered":        len(recovered),
            "recovery_rate":    round(len(recovered) / len(all_) * 100, 1) if all_ else 0,
            "total_cart_value": total_cart,
            "recovered_amount": recovered_amt,
            "nudges_sent":      sum(s.nudges_sent for s in all_),
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
checkout_agent = CheckoutRecoveryAgent()
