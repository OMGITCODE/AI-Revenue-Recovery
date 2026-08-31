"""
Promise-to-Pay (P2P) Tracker.

Tracks explicit customer commitments to pay by a specific date.
Integrates with the DecisionEngine — an active P2P suppresses nudges
so we never harass a customer who already made a commitment.
Resolves customer identity so promises created under any alias (VPA, customer ID, phone)
consistently inform the customer's behavioral trust score and suppress redundant nudges.

States:
    pending   → customer has promised, awaiting payment
    fulfilled → payment received before deadline
    broken    → deadline passed, no payment (triggers B2B chaser tier-up)
    expired   → cancelled by agent/system

Storage: In-memory dict keyed by promise_id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set

from ..utils.logger import get_logger
from .customer_identity import customer_identity_registry, normalize_identifier

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


# ── Model ─────────────────────────────────────────────────────────────────────

class PromiseStatus(str, Enum):
    PENDING   = "pending"
    FULFILLED = "fulfilled"
    BROKEN    = "broken"
    EXPIRED   = "expired"


@dataclass
class PromiseToPay:
    """A single customer payment commitment."""
    promise_id:    str
    vpa:           str
    amount:        float
    bank:          str
    failure_code:  str
    promised_at:   datetime
    deadline:      datetime
    customer_id:   str           = ""
    status:        PromiseStatus = PromiseStatus.PENDING
    channel:       str           = "whatsapp"   # how the promise was captured
    notes:         str           = ""
    fulfilled_at:  Optional[datetime] = None
    broken_at:     Optional[datetime] = None

    @property
    def is_overdue(self) -> bool:
        return (
            self.status == PromiseStatus.PENDING
            and datetime.now(IST) > self.deadline
        )

    @property
    def hours_until_deadline(self) -> float:
        delta = self.deadline - datetime.now(IST)
        return delta.total_seconds() / 3600

    def to_dict(self) -> dict:
        return {
            "promise_id":            self.promise_id,
            "vpa":                   self.vpa,
            "customer_id":           self.customer_id,
            "amount":                self.amount,
            "bank":                  self.bank,
            "failure_code":          self.failure_code,
            "promised_at":           self.promised_at.isoformat(),
            "deadline":              self.deadline.isoformat(),
            "status":                self.status.value,
            "channel":               self.channel,
            "notes":                 self.notes,
            "is_overdue":            self.is_overdue,
            "hours_until_deadline":  round(self.hours_until_deadline, 1),
            "fulfilled_at":          self.fulfilled_at.isoformat() if self.fulfilled_at else None,
            "broken_at":             self.broken_at.isoformat() if self.broken_at else None,
        }


# ── Tracker (singleton in-memory store) ──────────────────────────────────────

class PromiseToPayTracker:
    """
    Central registry for P2P promises with cross-alias customer identity resolution.
    """

    def __init__(self):
        self._store: Dict[str, PromiseToPay] = {}

    @property
    def _promises(self) -> Dict[str, PromiseToPay]:
        return self._store

    def _matches_person(self, p: PromiseToPay, identifier: str) -> bool:
        """True if the promise belongs to the same person as identifier."""
        if not identifier:
            return False
        if p.vpa == identifier or p.customer_id == identifier:
            return True
        return (
            customer_identity_registry.is_same_person(p.vpa, identifier)
            or (bool(p.customer_id) and customer_identity_registry.is_same_person(p.customer_id, identifier))
        )

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(
        self,
        vpa:           str,
        amount:        float,
        bank:          str           = "UPI",
        failure_code:  str           = "U30",
        deadline_hours: float        = 48,
        channel:       str           = "whatsapp",
        notes:         str           = "",
        customer_id:   str           = "",
        phone:         str           = "",
    ) -> PromiseToPay:
        """Record a new customer promise, linking any provided aliases."""
        if vpa or customer_id or phone:
            customer_identity_registry.resolve_canonical_id(vpa, customer_id, phone)

        # Check existing pending promise for the same person and amount
        existing = next(
            (
                p for p in self._store.values()
                if self._matches_person(p, vpa or customer_id or phone)
                and abs(p.amount - amount) < 1
                and p.status == PromiseStatus.PENDING
            ),
            None,
        )
        if existing:
            return existing

        now = datetime.now(IST)
        p = PromiseToPay(
            promise_id   = str(uuid.uuid4())[:8].upper(),
            vpa          = vpa or f"user_{customer_id or phone or 'anon'}@upi",
            customer_id  = customer_id,
            amount       = amount,
            bank         = bank,
            failure_code = failure_code,
            promised_at  = now,
            deadline     = now + timedelta(hours=deadline_hours),
            channel      = channel,
            notes        = notes,
        )
        self._store[p.promise_id] = p
        logger.info(
            "P2P created: %s | vpa=%s | cust=%s | ₹%.0f | deadline=%s",
            p.promise_id, p.vpa, p.customer_id, amount, p.deadline.strftime("%d %b %H:%M IST"),
        )
        return p

    def fulfill(self, promise_id: str) -> Optional[PromiseToPay]:
        """Mark a promise as fulfilled (payment received)."""
        p = self._store.get(promise_id)
        if not p:
            logger.warning("P2P fulfill: promise %s not found", promise_id)
            return None
        if p.status == PromiseStatus.FULFILLED:
            return p
        p.status       = PromiseStatus.FULFILLED
        p.fulfilled_at = datetime.now(IST)
        logger.info("P2P fulfilled: %s | vpa=%s | ₹%.0f", promise_id, p.vpa, p.amount)
        return p

    def mark_broken(self, promise_id: str) -> Optional[PromiseToPay]:
        """Mark a promise as broken (deadline passed, no payment)."""
        p = self._store.get(promise_id)
        if not p:
            return None
        if p.status == PromiseStatus.BROKEN:
            return p
        p.status    = PromiseStatus.BROKEN
        p.broken_at = datetime.now(IST)
        logger.warning(
            "P2P BROKEN: %s | vpa=%s | ₹%.0f — escalating to B2B chaser",
            promise_id, p.vpa, p.amount,
        )
        return p

    def expire(self, promise_id: str) -> Optional[PromiseToPay]:
        p = self._store.get(promise_id)
        if p:
            p.status = PromiseStatus.EXPIRED
        return p

    # ── Queries ───────────────────────────────────────────────────────────────

    def has_active(self, identifier: str, amount: float) -> bool:
        """True if customer has a pending promise for this amount across any alias."""
        return any(
            self._matches_person(p, identifier) and abs(p.amount - amount) < 1 and p.status == PromiseStatus.PENDING
            for p in self._store.values()
        )

    def active_promises_for_vpa(self, identifier: str) -> List[PromiseToPay]:
        """Returns all currently pending promises for a given customer identifier / alias."""
        return [p for p in self._store.values() if self._matches_person(p, identifier) and p.status == PromiseStatus.PENDING]

    def get(self, promise_id: str) -> Optional[PromiseToPay]:
        return self._store.get(promise_id)

    def all_promises(self) -> List[PromiseToPay]:
        return sorted(self._store.values(), key=lambda p: p.promised_at, reverse=True)

    def pending(self) -> List[PromiseToPay]:
        return [p for p in self._store.values() if p.status == PromiseStatus.PENDING]

    def broken(self) -> List[PromiseToPay]:
        return [p for p in self._store.values() if p.status == PromiseStatus.BROKEN]

    def fulfill_active(self, identifier: str, amount: Optional[float] = None) -> Optional[PromiseToPay]:
        """Fulfill any active pending promise for a customer across aliases upon successful recovery."""
        for p in list(self._store.values()):
            if self._matches_person(p, identifier) and p.status == PromiseStatus.PENDING:
                if amount is None or abs(p.amount - amount) < 1:
                    return self.fulfill(p.promise_id)
        return None

    def payer_trust_score(self, identifier: str) -> float:
        """
        Compute a CRED-style trust score (0.0 – 1.0) from this payer's
        Promise-to-Pay history across all their linked identifiers/aliases.
        """
        if not identifier:
            return 0.5

        history = [p for p in self._store.values() if self._matches_person(p, identifier)]
        if not history:
            return 0.5   # neutral — no data

        # Sort newest-first for recency weighting
        history.sort(key=lambda p: p.promised_at, reverse=True)

        weighted_fulfilled = 0.0
        weighted_total     = 0.0
        broken_cnt         = 0
        fulfilled_cnt      = 0
        pending_cnt        = 0

        for i, p in enumerate(history):
            weight = 2.0 if i == 0 else 1.0   # most-recent counts double
            if p.status == PromiseStatus.FULFILLED:
                weighted_fulfilled += weight
                weighted_total += weight
                fulfilled_cnt += 1
            elif p.status == PromiseStatus.BROKEN:
                weighted_total += weight
                broken_cnt += 1
            elif p.status == PromiseStatus.PENDING:
                weighted_fulfilled += weight * 0.50
                weighted_total += weight
                pending_cnt += 1

        base_rate = (weighted_fulfilled / weighted_total) if weighted_total > 0 else 0.50
        # Penalise broken promises, boost verified fulfillment track record
        score = base_rate - (broken_cnt * 0.15) + (fulfilled_cnt * 0.05)
        score = round(max(0.05, min(0.98, score)), 2)

        logger.debug(
            "TrustScore ident=%s history=%d fulfilled=%d broken=%d pending=%d score=%.2f",
            identifier, len(history), fulfilled_cnt, broken_cnt, pending_cnt, score,
        )
        return score

    # ── Sweep (call periodically) ─────────────────────────────────────────────

    def check_broken(self) -> List[PromiseToPay]:
        """
        Sweep pending promises; mark overdue ones as broken.
        Returns newly broken promises so caller can trigger B2B chaser.
        """
        newly_broken = []
        for p in list(self._store.values()):
            if p.is_overdue:
                self.mark_broken(p.promise_id)
                newly_broken.append(p)
        return newly_broken

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        all_ = list(self._store.values())
        return {
            "total":     len(all_),
            "pending":   sum(1 for p in all_ if p.status == PromiseStatus.PENDING),
            "fulfilled": sum(1 for p in all_ if p.status == PromiseStatus.FULFILLED),
            "broken":    sum(1 for p in all_ if p.status == PromiseStatus.BROKEN),
            "expired":   sum(1 for p in all_ if p.status == PromiseStatus.EXPIRED),
            "amount_at_risk":     sum(p.amount for p in all_ if p.status == PromiseStatus.PENDING),
            "amount_recovered":   sum(p.amount for p in all_ if p.status == PromiseStatus.FULFILLED),
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
promise_tracker = PromiseToPayTracker()
