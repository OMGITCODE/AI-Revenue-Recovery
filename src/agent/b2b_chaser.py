"""
B2B Receivables Chaser.

For B2B invoice recovery — applies aging buckets and tiered escalation logic.

Aging buckets:
    0–30  days  → gentle reminder (email + WhatsApp)
    31–60 days  → firm notice (phone + escalate to AR team)
    61–90 days  → formal demand letter + interest charge notice
    90+   days  → legal / collections referral

Tiering (by invoice value):
    < ₹25 000   → Tier C: automated-only
    ₹25k–₹2L    → Tier B: automated + AR specialist
    > ₹2L       → Tier A: dedicated recovery manager + legal

Promise-to-pay integration:
    If debtor makes a P2P commitment, escalation is paused.
    Broken promises trigger immediate tier-up.

Hinglish voice script:
    For Tier C debtors under ₹1 L, a pre-recorded IVR message is used.
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


# ── Enums ─────────────────────────────────────────────────────────────────────

class AgingBucket(str, Enum):
    CURRENT   = "0-30d"
    EARLY     = "31-60d"
    LATE      = "61-90d"
    CRITICAL  = "90d+"


class DebtorTier(str, Enum):
    A = "A"   # > ₹2 L   — dedicated manager
    B = "B"   # ₹25k–₹2L — AR specialist
    C = "C"   # < ₹25k   — automated only


class ChaseStatus(str, Enum):
    ACTIVE    = "active"
    PROMISED  = "promised"    # debtor has active P2P
    ESCALATED = "escalated"   # moved to AR team / legal
    SETTLED   = "settled"
    WRITTEN_OFF = "written_off"


# ── Action log ────────────────────────────────────────────────────────────────

@dataclass
class ChaseAction:
    action_id:    str
    action_type:  str     # "email" | "whatsapp" | "call" | "letter" | "legal"
    message:      str
    channel:      str
    executed_at:  datetime
    outcome:      str = "sent"   # sent / delivered / opened / bounced / replied

    def to_dict(self) -> dict:
        return {
            "action_id":   self.action_id,
            "action_type": self.action_type,
            "message":     self.message,
            "channel":     self.channel,
            "executed_at": self.executed_at.isoformat(),
            "outcome":     self.outcome,
        }


# ── Receivable ────────────────────────────────────────────────────────────────

@dataclass
class Receivable:
    receivable_id:  str
    debtor_name:    str
    debtor_vpa:     str
    debtor_phone:   str
    invoice_number: str
    amount:         float
    currency:       str
    due_date:       datetime
    status:         ChaseStatus        = ChaseStatus.ACTIVE
    actions:        List[ChaseAction]  = field(default_factory=list)
    promise_id:     Optional[str]      = None    # linked P2P if any
    interest_rate:  float              = 0.18    # 18% per annum

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def days_overdue(self) -> int:
        delta = datetime.now(IST) - self.due_date
        return max(0, delta.days)

    @property
    def aging_bucket(self) -> AgingBucket:
        d = self.days_overdue
        if d <= 30: return AgingBucket.CURRENT
        if d <= 60: return AgingBucket.EARLY
        if d <= 90: return AgingBucket.LATE
        return AgingBucket.CRITICAL

    @property
    def debtor_tier(self) -> DebtorTier:
        if self.amount > 200_000: return DebtorTier.A
        if self.amount > 25_000:  return DebtorTier.B
        return DebtorTier.C

    @property
    def interest_accrued(self) -> float:
        """Simple interest since due date."""
        return self.amount * self.interest_rate * max(0, self.days_overdue) / 365

    @property
    def total_outstanding(self) -> float:
        return self.amount + self.interest_accrued

    def to_dict(self) -> dict:
        return {
            "receivable_id":   self.receivable_id,
            "debtor_name":     self.debtor_name,
            "debtor_vpa":      self.debtor_vpa,
            "invoice_number":  self.invoice_number,
            "amount":          round(self.amount, 2),
            "currency":        self.currency,
            "due_date":        self.due_date.isoformat(),
            "days_overdue":    self.days_overdue,
            "aging_bucket":    self.aging_bucket.value,
            "debtor_tier":     self.debtor_tier.value,
            "status":          self.status.value,
            "interest_accrued":round(self.interest_accrued, 2),
            "total_outstanding": round(self.total_outstanding, 2),
            "promise_id":      self.promise_id,
            "actions_count":   len(self.actions),
            "actions":         [a.to_dict() for a in self.actions[-5:]],  # last 5
        }


# ── Chaser ────────────────────────────────────────────────────────────────────

# Message templates by bucket and tier
CHASE_TEMPLATES: Dict[AgingBucket, Dict[DebtorTier, dict]] = {
    AgingBucket.CURRENT: {
        DebtorTier.C: {
            "channel": "whatsapp",
            "message": "Hi {name}! Invoice #{inv} for ₹{amount:,.0f} was due on {due}. Please pay at: https://rzp.io/l/inv-{id}  Reply if any issues.",
        },
        DebtorTier.B: {
            "channel": "email+whatsapp",
            "message": "Dear {name}, this is a friendly reminder that Invoice #{inv} (₹{amount:,.0f}) was due on {due}. Kindly arrange payment at your earliest. Pay: https://rzp.io/l/inv-{id}",
        },
        DebtorTier.A: {
            "channel": "call+email",
            "message": "Dear {name}, your account manager will reach out regarding Invoice #{inv} (₹{amount:,.0f} due {due}). Please ensure payment or contact us to discuss terms.",
        },
    },
    AgingBucket.EARLY: {
        DebtorTier.C: {
            "channel": "whatsapp+ivr",
            "message": "⚠️ Invoice #{inv} is now {days} days overdue (₹{amount:,.0f}). Please settle immediately to avoid interest charges (18% p.a.). Pay: https://rzp.io/l/inv-{id}",
        },
        DebtorTier.B: {
            "channel": "ar_specialist",
            "message": "NOTICE: Invoice #{inv} for ₹{amount:,.0f} is {days} days past due. Interest of ₹{interest:,.0f} is accruing. Our AR team will contact you within 24 hours. Pay now: https://rzp.io/l/inv-{id}",
        },
        DebtorTier.A: {
            "channel": "dedicated_manager",
            "message": "Formal Notice — Invoice #{inv} (₹{amount:,.0f}) is {days} days overdue. Total outstanding including interest: ₹{total:,.0f}. Your dedicated recovery manager has been assigned. Please respond within 48 hours.",
        },
    },
    AgingBucket.LATE: {
        DebtorTier.C: {
            "channel": "ivr+sms",
            "message": "URGENT: Invoice #{inv} is {days} days overdue. Total due with interest: ₹{total:,.0f}. Failure to pay within 7 days may result in referral to collections. Pay: https://rzp.io/l/inv-{id}",
        },
        DebtorTier.B: {
            "channel": "legal_notice",
            "message": "FORMAL DEMAND — Invoice #{inv} for ₹{total:,.0f} (including 18% p.a. interest) remains unpaid after {days} days. Legal proceedings may commence within 14 days without payment. Pay: https://rzp.io/l/inv-{id}",
        },
        DebtorTier.A: {
            "channel": "legal+senior_manager",
            "message": "FINAL NOTICE — Invoice #{inv}: ₹{total:,.0f} outstanding for {days} days. Legal team engaged. Please contact your account manager within 48 hours to avoid formal proceedings.",
        },
    },
    AgingBucket.CRITICAL: {
        DebtorTier.C: {
            "channel": "collections",
            "message": "COLLECTIONS REFERRAL — Invoice #{inv} (₹{total:,.0f}) is {days}+ days overdue. Account referred to Razorpay Collections. You will be contacted separately.",
        },
        DebtorTier.B: {
            "channel": "legal",
            "message": "LEGAL ACTION INITIATED — Invoice #{inv} (₹{total:,.0f}, {days} days overdue). Legal proceedings have commenced. All further communication via legal counsel only.",
        },
        DebtorTier.A: {
            "channel": "legal+arbitration",
            "message": "ARBITRATION NOTICE — Invoice #{inv} (₹{total:,.0f}, {days} days overdue). Matter referred to arbitration per contract terms. Legal counsel will contact within 5 business days.",
        },
    },
}

# Hinglish IVR script for Tier C under ₹1L
HINGLISH_IVR = (
    "Namaste! Yeh {merchant} ki taraf se ek zaroori sandesh hai. "
    "Aapka invoice number {inv}, jiska amount ₹{amount:,.0f} tha, "
    "{days} din se unpaid hai. "
    "Kripya jaldi payment karein — late fees lag sakti hain. "
    "Payment link SMS pe bheja gaya hai. Dhanyawad!"
)


class B2BChaser:
    """
    B2B Receivables recovery engine.

    Usage:
        chaser = b2b_chaser  # global singleton
        r = chaser.add_receivable(debtor_name, vpa, phone, invoice, amount, due_date_iso)
        chaser.chase(receivable_id)
        chaser.settle(receivable_id, amount_received)
    """

    def __init__(self):
        self._receivables: Dict[str, Receivable] = {}

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def has_invoice(self, invoice_number: str) -> bool:
        return any(r.invoice_number == invoice_number for r in self._receivables.values())

    def add_receivable(
        self,
        debtor_name:    str,
        debtor_vpa:     str,
        debtor_phone:   str,
        invoice_number: str,
        amount:         float,
        due_date_iso:   str,
        currency:       str = "INR",
    ) -> Receivable:
        existing = next((r for r in self._receivables.values() if r.invoice_number == invoice_number), None)
        if existing:
            return existing

        due = datetime.fromisoformat(due_date_iso).replace(tzinfo=IST)
        rid = "RCV-" + str(uuid.uuid4())[:6].upper()
        r   = Receivable(
            receivable_id  = rid,
            debtor_name    = debtor_name,
            debtor_vpa     = debtor_vpa,
            debtor_phone   = debtor_phone,
            invoice_number = invoice_number,
            amount         = amount,
            currency       = currency,
            due_date       = due,
        )
        self._receivables[rid] = r
        logger.info(
            "Receivable added: %s | %s | ₹%.0f | due=%s | bucket=%s | tier=%s",
            rid, debtor_name, amount, due.strftime("%d %b"), r.aging_bucket.value, r.debtor_tier.value,
        )
        return r

    def settle(self, receivable_id: str, amount_received: float) -> Optional[Receivable]:
        r = self._receivables.get(receivable_id)
        if r:
            r.status = ChaseStatus.SETTLED
            logger.info("Receivable SETTLED: %s | received ₹%.0f vs ₹%.0f due", receivable_id, amount_received, r.total_outstanding)
        return r

    def write_off(self, receivable_id: str) -> Optional[Receivable]:
        r = self._receivables.get(receivable_id)
        if r:
            r.status = ChaseStatus.WRITTEN_OFF
        return r

    def attach_promise(self, receivable_id: str, promise_id: str) -> Optional[Receivable]:
        r = self._receivables.get(receivable_id)
        if r:
            r.promise_id = promise_id
            r.status     = ChaseStatus.PROMISED
        return r

    # ── Chase (the main action) ───────────────────────────────────────────────

    def chase(self, receivable_id: str, merchant: str = "Razorpay Merchant") -> Optional[ChaseAction]:
        """Execute the next appropriate chase action based on aging + tier."""
        r = self._receivables.get(receivable_id)
        if not r:
            logger.warning("Receivable %s not found", receivable_id)
            return None
        if r.status in (ChaseStatus.SETTLED, ChaseStatus.WRITTEN_OFF):
            logger.info("Receivable %s already %s — skipping chase", receivable_id, r.status)
            return None
        if r.status == ChaseStatus.PROMISED:
            logger.info("Receivable %s has active promise — holding chase", receivable_id)
            return None

        bucket = r.aging_bucket
        tier   = r.debtor_tier
        tmpl   = CHASE_TEMPLATES[bucket][tier]
        msg    = tmpl["message"].format(
            name   = r.debtor_name,
            inv    = r.invoice_number,
            amount = r.amount,
            days   = r.days_overdue,
            due    = r.due_date.strftime("%d %b %Y"),
            total  = r.total_outstanding,
            interest = r.interest_accrued,
            id     = r.receivable_id.lower(),
        )

        # For Tier C in early/late buckets, also generate Hinglish IVR
        ivr_msg = None
        if tier == DebtorTier.C and r.amount < 100_000 and bucket in (AgingBucket.EARLY, AgingBucket.LATE):
            ivr_msg = HINGLISH_IVR.format(
                merchant = merchant,
                inv      = r.invoice_number,
                amount   = r.amount,
                days     = r.days_overdue,
            )
            logger.info("[Hinglish IVR → %s] %s", r.debtor_phone, ivr_msg)

        action = ChaseAction(
            action_id   = "ACT-" + str(uuid.uuid4())[:6].upper(),
            action_type = tmpl["channel"].split("+")[0],
            message     = msg,
            channel     = tmpl["channel"],
            executed_at = datetime.now(IST),
        )
        r.actions.append(action)

        if bucket == AgingBucket.CRITICAL:
            r.status = ChaseStatus.ESCALATED

        logger.info(
            "[B2B Chase → %s | %s | Tier-%s | bucket=%s] %s",
            r.debtor_vpa, bucket.value, tier.value, bucket.value, msg[:120],
        )
        return action

    # ── Queries ───────────────────────────────────────────────────────────────

    def all_receivables(self) -> List[Receivable]:
        return sorted(self._receivables.values(), key=lambda r: r.days_overdue, reverse=True)

    def by_bucket(self, bucket: AgingBucket) -> List[Receivable]:
        return [r for r in self._receivables.values() if r.aging_bucket == bucket]

    def stats(self) -> dict:
        all_ = list(self._receivables.values())
        return {
            "total":              len(all_),
            "total_outstanding":  round(sum(r.total_outstanding for r in all_), 2),
            "settled":            sum(1 for r in all_ if r.status == ChaseStatus.SETTLED),
            "escalated":          sum(1 for r in all_ if r.status == ChaseStatus.ESCALATED),
            "buckets": {
                "0-30d":  {"count": len(self.by_bucket(AgingBucket.CURRENT)),  "amount": sum(r.amount for r in self.by_bucket(AgingBucket.CURRENT))},
                "31-60d": {"count": len(self.by_bucket(AgingBucket.EARLY)),    "amount": sum(r.amount for r in self.by_bucket(AgingBucket.EARLY))},
                "61-90d": {"count": len(self.by_bucket(AgingBucket.LATE)),     "amount": sum(r.amount for r in self.by_bucket(AgingBucket.LATE))},
                "90d+":   {"count": len(self.by_bucket(AgingBucket.CRITICAL)), "amount": sum(r.amount for r in self.by_bucket(AgingBucket.CRITICAL))},
            },
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
b2b_chaser = B2BChaser()
