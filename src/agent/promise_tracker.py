"""
Promise-to-Pay (P2P) Tracker.

Tracks explicit customer commitments to pay by a specific date.
Integrates with the DecisionEngine — an active P2P suppresses nudges
so we never harass a customer who already made a commitment.

States:
    pending   → customer has promised, awaiting payment
    fulfilled → payment received before deadline
    broken    → deadline passed, no payment (triggers B2B chaser tier-up)
    expired   → cancelled by agent/system

Storage: In-memory dict keyed by (vpa, amount).
In production, swap for Redis / Postgres.
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
    Central registry for P2P promises.

    Usage:
        tracker = promise_tracker          # global singleton
        p = tracker.create(vpa, amount, bank, failure_code, deadline_hours=48)
        tracker.fulfill(promise_id)
        tracker.check_broken()             # call periodically to sweep overdue
        is_active = tracker.has_active(vpa, amount)
    """

    def __init__(self):
        self._store: Dict[str, PromiseToPay] = {}

    # Alias so api/main.py reset can do: promise_tracker._promises.clear()
    @property
    def _promises(self) -> Dict[str, PromiseToPay]:
        return self._store

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(
        self,
        vpa:           str,
        amount:        float,
        bank:          str,
        failure_code:  str,
        deadline_hours: float = 48,
        channel:       str   = "whatsapp",
        notes:         str   = "",
    ) -> PromiseToPay:
        """Record a new customer promise."""
        existing = next((p for p in self._store.values() if p.vpa == vpa and abs(p.amount - amount) < 1 and p.status == PromiseStatus.PENDING), None)
        if existing:
            return existing

        now = datetime.now(IST)
        p = PromiseToPay(
            promise_id   = str(uuid.uuid4())[:8].upper(),
            vpa          = vpa,
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
            "P2P created: %s | vpa=%s | ₹%.0f | deadline=%s",
            p.promise_id, vpa, amount, p.deadline.strftime("%d %b %H:%M IST"),
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

    def has_active(self, vpa: str, amount: float) -> bool:
        """True if customer has a pending promise for this amount."""
        return any(
            p.vpa == vpa and abs(p.amount - amount) < 1 and p.status == PromiseStatus.PENDING
            for p in self._store.values()
        )

    def active_promises_for_vpa(self, vpa: str) -> List[PromiseToPay]:
        """Returns all currently pending promises for a given VPA."""
        return [p for p in self._store.values() if p.vpa == vpa and p.status == PromiseStatus.PENDING]

    def get(self, promise_id: str) -> Optional[PromiseToPay]:
        return self._store.get(promise_id)

    def all_promises(self) -> List[PromiseToPay]:
        return sorted(self._store.values(), key=lambda p: p.promised_at, reverse=True)

    def pending(self) -> List[PromiseToPay]:
        return [p for p in self._store.values() if p.status == PromiseStatus.PENDING]

    def broken(self) -> List[PromiseToPay]:
        return [p for p in self._store.values() if p.status == PromiseStatus.BROKEN]

    def payer_trust_score(self, vpa: str) -> float:
        """
        Compute a CRED-style trust score (0.0 – 1.0) from this payer's
        Promise-to-Pay history.  A higher score means the customer has a
        strong track record of keeping commitments — the Decision Engine
        uses this to be *less* aggressive with retries (they'll self-cure)
        or *more* lenient with retry timing.

        Algorithm:
          - No history          → neutral 0.5  (benefit of the doubt)
          - fulfilled / total   → base rate
          - Recency-weighted:   recent promises count 2×
          - Broken promises     subtract 0.15 each (capped at floor 0.05)

        In production: feed this into Thompson Sampling as a prior.
        """
        history = [p for p in self._store.values() if p.vpa == vpa]
        if not history:
            return 0.5   # neutral — no data

        # Sort newest-first for recency weighting
        history.sort(key=lambda p: p.promised_at, reverse=True)

        weighted_fulfilled = 0.0
        weighted_total     = 0.0
        for i, p in enumerate(history):
            weight = 2.0 if i == 0 else 1.0   # most-recent counts double
            weighted_total += weight
            if p.status == PromiseStatus.FULFILLED:
                weighted_fulfilled += weight

        base_rate  = weighted_fulfilled / weighted_total if weighted_total else 0.5
        # Penalise broken promises
        broken_cnt = sum(1 for p in history if p.status == PromiseStatus.BROKEN)
        score      = base_rate - (broken_cnt * 0.15)
        score      = round(max(0.05, min(1.0, score)), 2)

        logger.debug(
            "TrustScore vpa=%s history=%d fulfilled=%.1f broken=%d score=%.2f",
            vpa, len(history), weighted_fulfilled, broken_cnt, score,
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
