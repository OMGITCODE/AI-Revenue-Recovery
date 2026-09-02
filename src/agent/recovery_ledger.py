"""
Recovery Audit Ledger.

Every routing / retry / guardrail / intervention / escalation decision
gets ONE plain-English line of reasoning PLUS a confidence score.

This is:
  - the audit trail regulators can read
  - the narration track for the 5-minute pitch video
  - proof that the agent "knows why" it did what it did

Storage: append-only in-memory list (swap for Postgres in prod).

Schema per entry:
  ledger_id       — short unique ID
  ts              — IST timestamp
  event_type      — decide | intervene | guardrail | escalate | recover | p2p | checkout | b2b
  vpa             — customer VPA (or debtor ID for B2B)
  amount          — ₹ at stake
  reasoning       — plain-English one-liner (what the agent decided and why)
  confidence      — 0.0–1.0 (how sure the agent is this is the right call)
  outcome         — pending | success | failure | skipped
  channel_cost    — ₹ cost of the intervention channel (for ROI calc)
  amount_recovered— ₹ actually recovered (0 until confirmed)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from ..utils.logger import get_logger

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Channel costs (₹ per action) ──────────────────────────────────────────────
# Source: standard Indian market rates
CHANNEL_COSTS = {
    "whatsapp":        0.50,   # WhatsApp Business API per message
    "sms":             0.15,   # Transactional SMS (DLT-registered)
    "ivr":             1.50,   # IVR / voice call per attempt
    "email":           0.05,   # Transactional email
    "smart_retry":     0.00,   # Automated retry — no marginal cost
    "upi_collect":     0.25,   # UPI collect request fee
    "mandate_renewal": 0.50,   # Magic link generation + WhatsApp
    "escalation":      25.00,  # Human agent cost per touchpoint
    "legal":           500.00, # Legal notice drafting (estimated)
    "ar_specialist":   150.00, # AR specialist per touchpoint
}

# ── Ledger entry ──────────────────────────────────────────────────────────────

@dataclass
class LedgerEntry:
    ledger_id:        str
    ts:               datetime
    event_type:       str      # decide | intervene | guardrail | escalate | recover | b2b | checkout | p2p
    vpa:              str
    amount:           float
    reasoning:        str      # plain English, one sentence
    confidence:       float    # 0.0 – 1.0
    outcome:          str      = "pending"   # pending | success | failure | skipped
    channel:          str      = ""
    channel_cost:     float    = 0.0
    amount_recovered: float    = 0.0
    recovery_type:    str      = "reactive"  # "reactive" (post-failure recovery) | "proactive" (pre-failure churn prevention)

    @property
    def roi(self) -> float:
        """Net return on this intervention."""
        return self.amount_recovered - self.channel_cost

    def to_dict(self) -> dict:
        return {
            "ledger_id":        self.ledger_id,
            "ts":               self.ts.strftime("%H:%M:%S"),
            "ts_full":          self.ts.isoformat(),
            "event_type":       self.event_type,
            "recovery_type":    self.recovery_type,
            "vpa":              self.vpa,
            "amount":           self.amount,
            "reasoning":        self.reasoning,
            "confidence":       round(self.confidence, 2),
            "outcome":          self.outcome,
            "channel":          self.channel,
            "channel_cost":     round(self.channel_cost, 2),
            "amount_recovered": round(self.amount_recovered, 2),
            "roi":              round(self.roi, 2),
        }


# ── Ledger store ──────────────────────────────────────────────────────────────

class RecoveryLedger:
    """
    Append-only audit ledger for every agent decision.

    Usage:
        ledger.log(event_type, vpa, amount, reasoning, confidence, channel, recovery_type="reactive")
        ledger.mark_outcome(ledger_id, outcome, amount_recovered)
        ledger.roi_by_channel()   → dict of channel → {cost, recovered, roi, recovery_type}
        ledger.overall_roi()      → dict of separated and combined ROI metrics
    """

    def __init__(self, max_entries: int = 500):
        self._entries: List[LedgerEntry] = []
        self._max    = max_entries

    def reset(self) -> None:
        self._entries.clear()

    # ── Write ─────────────────────────────────────────────────────────────────

    def log(
        self,
        event_type:    str,
        vpa:           str,
        amount:        float,
        reasoning:     str,
        confidence:    float,
        channel:       str   = "",
        outcome:       str   = "pending",
        recovery_type: str   = "reactive",
    ) -> LedgerEntry:
        # Auto-infer proactive if explicitly mandate_renewal or reasoning indicates pre-emptive expiry action
        if recovery_type == "reactive" and (channel == "mandate_renewal" or "proactive" in reasoning.lower() or "bt02_prevented" in reasoning.lower()):
            recovery_type = "proactive"

        now = datetime.now(IST)
        for prev in reversed(self._entries[-20:]):
            if (
                prev.event_type == event_type
                and prev.vpa == vpa
                and abs(prev.amount - amount) < 0.01
                and prev.reasoning == reasoning
                and getattr(prev, "recovery_type", "reactive") == recovery_type
                and (now - prev.ts).total_seconds() < 5.0
            ):
                return prev
        cost = CHANNEL_COSTS.get(channel.lower().split("+")[0], 0.0)
        entry = LedgerEntry(
            ledger_id     = str(uuid.uuid4())[:8].upper(),
            ts            = now,
            event_type    = event_type,
            recovery_type = recovery_type,
            vpa           = vpa,
            amount        = amount,
            reasoning     = reasoning,
            confidence    = max(0.0, min(1.0, confidence)),
            outcome       = outcome,
            channel       = channel,
            channel_cost  = cost,
        )
        self._entries.append(entry)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]   # trim oldest

        logger.info(
            "[LEDGER %s][%s] %s | %s | conf=%.2f | %s",
            entry.ledger_id, recovery_type.upper(), event_type.upper(), vpa, confidence, reasoning
        )
        return entry

    def mark_outcome(
        self,
        ledger_id:        str,
        outcome:          str,
        amount_recovered: float = 0.0,
    ) -> Optional[LedgerEntry]:
        for e in reversed(self._entries):
            if e.ledger_id == ledger_id:
                e.outcome          = outcome
                e.amount_recovered = amount_recovered
                return e
        return None

    # ── Read ──────────────────────────────────────────────────────────────────

    def recent(self, n: int = 50) -> List[LedgerEntry]:
        return list(reversed(self._entries[-n:]))

    def all_entries(self) -> List[LedgerEntry]:
        return list(reversed(self._entries))

    # ── ROI analytics ─────────────────────────────────────────────────────────

    def roi_by_channel(self) -> dict:
        """
        Returns per-channel ROI breakdown:
          { channel: { count, total_cost, total_recovered, net_roi, avg_confidence, recovery_type } }
        """
        summary: dict = {}
        for e in self._entries:
            ch = e.channel or "unknown"
            rec_type = getattr(e, "recovery_type", "reactive")
            if ch not in summary:
                summary[ch] = {
                    "count":           0,
                    "total_cost":      0.0,
                    "total_recovered": 0.0,
                    "net_roi":         0.0,
                    "avg_confidence":  0.0,
                    "recovery_type":   rec_type,
                    "_conf_sum":       0.0,
                }
            s = summary[ch]
            s["count"]           += 1
            s["total_cost"]      += e.channel_cost
            s["total_recovered"] += e.amount_recovered
            s["net_roi"]         = round(s["total_recovered"] - s["total_cost"], 2)
            s["_conf_sum"]       += e.confidence
            s["avg_confidence"]  = round(s["_conf_sum"] / s["count"], 2)

        # Remove internal helper key
        for s in summary.values():
            s.pop("_conf_sum", None)
        return summary

    def overall_roi(self) -> dict:
        """
        Returns comprehensive ROI breakdown separating:
          1. Reactive recovery: ₹ recovered after actual transaction failure occurred (e.g. smart retries, collect)
          2. Proactive protection: ₹ protected before failure ever happened (e.g. T-72h mandate expiry renewal)
          3. Combined headline impact: total net value delivered to merchant
        """
        reactive_entries  = [e for e in self._entries if getattr(e, "recovery_type", "reactive") == "reactive"]
        proactive_entries = [e for e in self._entries if getattr(e, "recovery_type", "reactive") == "proactive"]

        total_cost      = sum(e.channel_cost     for e in self._entries)
        total_recovered = sum(e.amount_recovered for e in self._entries)
        total_at_stake  = sum(e.amount           for e in self._entries if e.outcome != "skipped")
        success_count   = sum(1 for e in self._entries if e.outcome == "success")
        total_actioned  = sum(1 for e in self._entries if e.outcome in ("success", "failure"))
        avg_conf        = (
            sum(e.confidence for e in self._entries) / len(self._entries)
            if self._entries else 0.0
        )

        # ── Reactive metrics (post-failure recovery) ──────────────────────────
        reactive_cost      = sum(e.channel_cost     for e in reactive_entries)
        reactive_recovered = sum(e.amount_recovered for e in reactive_entries)
        reactive_at_stake  = sum(e.amount           for e in reactive_entries if e.outcome != "skipped")
        reactive_success   = sum(1 for e in reactive_entries if e.outcome == "success")
        reactive_actioned  = sum(1 for e in reactive_entries if e.outcome in ("success", "failure"))
        reactive_rate      = round(reactive_success / reactive_actioned * 100, 1) if reactive_actioned else 0.0

        # ── Proactive metrics (pre-failure churn prevention) ──────────────────
        proactive_cost      = sum(e.channel_cost     for e in proactive_entries)
        proactive_protected = sum(e.amount_recovered for e in proactive_entries)
        proactive_at_stake  = sum(e.amount           for e in proactive_entries if e.outcome != "skipped")
        proactive_success   = sum(1 for e in proactive_entries if e.outcome == "success")
        proactive_actioned  = sum(1 for e in proactive_entries if e.outcome in ("success", "failure"))
        proactive_rate      = round(proactive_success / proactive_actioned * 100, 1) if proactive_actioned else 0.0

        return {
            "total_entries":        len(self._entries),
            "total_cost":           round(total_cost, 2),
            "total_recovered":      round(total_recovered, 2),
            "net_roi":              round(total_recovered - total_cost, 2),
            "total_at_stake":       round(total_at_stake, 2),
            "recovery_rate_pct":    round(success_count / total_actioned * 100, 1) if total_actioned else 0.0,
            "avg_confidence":       round(avg_conf, 2),
            # Explicit two-part separation
            "reactive_recovered":   round(reactive_recovered, 2),
            "reactive_cost":        round(reactive_cost, 2),
            "reactive_net_roi":     round(reactive_recovered - reactive_cost, 2),
            "reactive_at_stake":    round(reactive_at_stake, 2),
            "reactive_rate_pct":    reactive_rate,
            "proactive_protected":  round(proactive_protected, 2),
            "proactive_cost":       round(proactive_cost, 2),
            "proactive_net_roi":    round(proactive_protected - proactive_cost, 2),
            "proactive_at_stake":   round(proactive_at_stake, 2),
            "proactive_rate_pct":   proactive_rate,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
ledger = RecoveryLedger()
