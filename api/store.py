"""
In-memory event store for the recovery dashboard.
Holds the last 100 processed events and running stats.
Thread-safe via asyncio — no DB needed.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections import deque
from typing import Any
import asyncio

from src.agent.customer_identity import customer_identity_registry, normalize_identifier

IST = timezone(timedelta(hours=5, minutes=30))

@dataclass
class RecoveryEvent:
    """A single processed recovery event — stored and streamed to dashboard."""
    id:                 str
    timestamp:          str          # IST formatted
    event_type:         str          # mandate.execution.failed etc.
    failure_code:       str          # U30, BT01, TM etc.
    failure_reason:     str          # human readable
    customer_id:        str
    customer_vpa:       str
    bank:               str
    amount:             float
    severity:           str          # critical / high / medium / low
    interventions:      list[str]    # intervention types that fired
    intervention_msgs:  list[str]    # messages from each intervention
    scheduled_at:       str | None   # for retry interventions
    action_url:         str | None   # renewal link etc.
    success:            bool
    scenario_name:      str = ""     # e.g. "U30 – Insufficient Funds"
    trust_score:        float = 0.5  # payer P2P trust score (0.0–1.0)
    aa_check:           str   = ""   # AA balance result summary string
    status:             str   = "recovered" # recovered / escalated / failed
    amount_recovered:   float = 0.0
    pattern_spike_ratio: float = 1.0  # ratio to historical baseline mean/median
    is_pattern_critical: bool  = False # True if sudden upward critical spike
    pattern_summary:     str   = ""   # Plain-English summary of spend pattern check
    pattern_baseline:    str   = ""   # e.g. "₹100 (range ₹89–₹149)"

    def to_dict(self) -> dict:
        return {
            "id":                  self.id,
            "timestamp":           self.timestamp,
            "event_type":          self.event_type,
            "failure_code":        self.failure_code,
            "failure_reason":      self.failure_reason,
            "customer_id":         self.customer_id,
            "customer_vpa":        self.customer_vpa,
            "bank":                self.bank,
            "amount":              self.amount,
            "severity":            self.severity,
            "interventions":       self.interventions,
            "intervention_msgs":   self.intervention_msgs,
            "scheduled_at":        self.scheduled_at,
            "action_url":          self.action_url,
            "success":             self.success,
            "scenario_name":       self.scenario_name,
            "trust_score":         self.trust_score,
            "aa_check":            self.aa_check,
            "status":              self.status,
            "amount_recovered":    self.amount_recovered,
            "pattern_spike_ratio": round(self.pattern_spike_ratio, 2),
            "is_pattern_critical": self.is_pattern_critical,
            "pattern_summary":     self.pattern_summary,
            "pattern_baseline":    self.pattern_baseline,
        }


@dataclass
class Stats:
    total_events:       int   = 0
    total_recovered:    float = 0.0
    successful:         int   = 0
    failed:             int   = 0
    retries_scheduled:  int   = 0
    renewals_sent:      int   = 0
    escalations:        int   = 0
    whatsapp_sent:      int   = 0
    upi_collects:       int   = 0

    @property
    def success_rate(self) -> float:
        if self.total_events == 0:
            return 0.0
        return round(self.successful / self.total_events * 100, 1)

    def to_dict(self) -> dict:
        return {
            "total_events":      self.total_events,
            "total_recovered":   round(self.total_recovered, 2),
            "successful":        self.successful,
            "failed":            self.failed,
            "success_rate":      self.success_rate,
            "retries_scheduled": self.retries_scheduled,
            "renewals_sent":     self.renewals_sent,
            "escalations":       self.escalations,
            "whatsapp_sent":     self.whatsapp_sent,
            "upi_collects":      self.upi_collects,
        }


class EventStore:
    """Thread-safe in-memory store for events and stats."""

    def __init__(self, maxlen: int = 100):
        self._events: deque[RecoveryEvent] = deque(maxlen=maxlen)
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def add_event(self, event: RecoveryEvent) -> None:
        async with self._lock:
            # Deduplicate by event ID — if already exists, update in-place without double-counting stats
            for idx, prev in enumerate(self._events):
                if prev.id == event.id:
                    self._events[idx] = event
                    for q in self._subscribers:
                        await q.put(event.to_dict())
                    return

            self._events.appendleft(event)

        # Notify all SSE subscribers
        for q in self._subscribers:
            await q.put(event.to_dict())

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def get_events(self, limit: int = 50) -> list[dict]:
        return [e.to_dict() for e in list(self._events)[:limit]]

    def get_events_for_customer(self, identifier: str) -> list[dict]:
        """Return all events for a specific person across all known aliases."""
        if not identifier:
            return []
        matches = []
        for e in self._events:
            if (
                e.customer_vpa == identifier
                or e.customer_id == identifier
                or customer_identity_registry.is_same_person(e.customer_vpa, identifier)
                or customer_identity_registry.is_same_person(e.customer_id, identifier)
            ):
                matches.append(e.to_dict())
        return matches

    def get_stats(self) -> dict:
        stats = Stats()
        for event in self._events:
            stats.total_events += 1
            if event.success:
                stats.successful += 1
                stats.total_recovered += (event.amount_recovered if event.amount_recovered > 0 else event.amount)
            else:
                stats.failed += 1

            for iv in event.interventions:
                if iv == "smart_retry":    stats.retries_scheduled += 1
                if iv == "mandate_renewal":stats.renewals_sent     += 1
                if iv == "escalation":     stats.escalations       += 1
                if iv == "whatsapp_nudge": stats.whatsapp_sent     += 1
                if iv == "upi_collect":    stats.upi_collects      += 1
        return stats.to_dict()

    def reset(self) -> None:
        self._events.clear()


# Global singleton
store = EventStore()
