"""
mandate_expiry.py — Proactive Mandate Expiry Interceptor (T-72h Prevention)
=============================================================================
Detects active UPI Autopay mandates nearing their validity expiration date (24–72 hours
prior to lapse) and dispatches proactive 1-click renewal magic links via WhatsApp/SMS.

Prevents NPCI BT02 ("Mandate Expired") transaction failures before they ever occur,
eliminating subscription interruptions, bank decline fees, and churn.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from ..utils.logger import get_logger
from ..integrations.razorpay_upi import generate_mandate_renewal_link
from ..integrations.messaging import messenger
from .recovery_ledger import ledger as recovery_ledger
from .customer_identity import customer_identity_registry, normalize_identifier

logger = get_logger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class ExpiringMandate:
    """Represents an active UPI Autopay mandate approaching validity expiration."""
    mandate_id:        str
    customer_id:       str
    customer_vpa:      str
    customer_name:     str
    amount:            float
    plan_name:         str
    bank_name:         str
    expiry_date:       datetime
    status:            str = "PENDING"  # PENDING | NUDGED | RENEWED
    renewal_link:      Optional[str] = None
    nudged_at:         Optional[datetime] = None
    renewed_at:        Optional[datetime] = None
    created_at:        datetime = field(default_factory=lambda: datetime.now(IST))

    def hours_remaining(self) -> float:
        """Calculate hours remaining until mandate expires."""
        now = datetime.now(IST)
        delta = self.expiry_date - now
        return max(0.0, delta.total_seconds() / 3600.0)

    def days_remaining(self) -> float:
        """Calculate days remaining until mandate expires."""
        return self.hours_remaining() / 24.0

    def to_dict(self) -> dict:
        return {
            "mandate_id":        self.mandate_id,
            "customer_id":       self.customer_id,
            "customer_vpa":      self.customer_vpa,
            "customer_name":     self.customer_name,
            "amount":            self.amount,
            "plan_name":         self.plan_name,
            "bank_name":         self.bank_name,
            "expiry_date":       self.expiry_date.strftime("%Y-%m-%d %H:%M IST"),
            "hours_remaining":   round(self.hours_remaining(), 1),
            "days_remaining":    round(self.days_remaining(), 1),
            "status":            self.status,
            "renewal_link":      self.renewal_link,
            "nudged_at":         self.nudged_at.strftime("%Y-%m-%d %H:%M IST") if self.nudged_at else None,
            "renewed_at":        self.renewed_at.strftime("%Y-%m-%d %H:%M IST") if self.renewed_at else None,
            "created_at":        self.created_at.strftime("%Y-%m-%d %H:%M IST"),
        }


class MandateExpiryScanner:
    """
    Scans for active UPI Autopay mandates approaching expiry (T-72h window),
    generates 1-click renewal magic links, and logs pre-empted recoveries.
    """

    def __init__(self):
        self._mandates: Dict[str, ExpiringMandate] = {}
        self._seed_expiring_mandates()

    def _seed_expiring_mandates(self):
        """Seed realistic active mandates from dataset file or fallback defaults."""
        now = datetime.now(IST)
        dataset_path = Path(__file__).resolve().parent.parent.parent / "data" / "expiring_mandates_dataset.json"

        if dataset_path.exists():
            try:
                with open(dataset_path, "r", encoding="utf-8") as f:
                    records = json.load(f)
                for r in records:
                    hrs = float(r.get("expiry_hours_from_now", 48.0))
                    m = ExpiringMandate(
                        mandate_id=r["mandate_id"],
                        customer_id=r["customer_id"],
                        customer_vpa=r["customer_vpa"],
                        customer_name=r.get("customer_name", "Customer"),
                        amount=float(r["amount"]),
                        plan_name=r.get("plan_name", "Subscription Plan"),
                        bank_name=r.get("bank_name", "HDFC"),
                        expiry_date=now + timedelta(hours=hrs),
                    )
                    self._mandates[m.mandate_id] = m
                return
            except Exception as e:
                logger.warning("Failed to load expiring mandates dataset: %s", e)

        archetypes = [
            ExpiringMandate(
                mandate_id="mand_sbi_exp_001",
                customer_id="cust-sbi-001",
                customer_vpa="rahul@oksbi",
                customer_name="Rahul Sharma",
                amount=999.0,
                plan_name="Hotstar VIP Annual",
                bank_name="SBI",
                expiry_date=now + timedelta(hours=36),
            ),
            ExpiringMandate(
                mandate_id="mand_hdfc_exp_002",
                customer_id="cust-hdfc-002",
                customer_vpa="priya@okhdfcbank",
                customer_name="Priya Patel",
                amount=1499.0,
                plan_name="Notion Team SaaS",
                bank_name="HDFC",
                expiry_date=now + timedelta(hours=18),
            ),
            ExpiringMandate(
                mandate_id="mand_ybl_exp_003",
                customer_id="cust-ybl-005",
                customer_vpa="vikram@ybl",
                customer_name="Vikram Singh",
                amount=2999.0,
                plan_name="Cult.fit Pass Elite",
                bank_name="Yes Bank",
                expiry_date=now + timedelta(hours=64),
            ),
        ]
        for m in archetypes:
            self._mandates[m.mandate_id] = m

    def find_expiring_mandates(self, within_hours: int = 72) -> List[ExpiringMandate]:
        """
        Return active mandates that expire within the given lookahead window (default 72 hours).
        Filters out already renewed mandates.
        """
        results = []
        for m in self._mandates.values():
            hrs = m.hours_remaining()
            if 0 < hrs <= within_hours and m.status != "RENEWED":
                results.append(m)
        return sorted(results, key=lambda x: x.expiry_date)

    def get_all_mandates(self) -> List[ExpiringMandate]:
        """Return all tracked mandates."""
        return sorted(list(self._mandates.values()), key=lambda x: x.expiry_date)

    def get_mandate(self, mandate_id: str) -> Optional[ExpiringMandate]:
        """Lookup mandate by ID."""
        return self._mandates.get(mandate_id)

    def register_mandate(
        self,
        mandate_id: str,
        customer_id: str,
        customer_vpa: str,
        customer_name: str,
        amount: float,
        plan_name: str,
        bank_name: str,
        expiry_date: datetime,
    ) -> ExpiringMandate:
        """Register a new mandate into the proactive scanner."""
        mandate = ExpiringMandate(
            mandate_id=mandate_id,
            customer_id=customer_id,
            customer_vpa=customer_vpa,
            customer_name=customer_name,
            amount=amount,
            plan_name=plan_name,
            bank_name=bank_name,
            expiry_date=expiry_date,
        )
        self._mandates[mandate_id] = mandate
        logger.info("Registered mandate %s for %s expiring at %s", mandate_id, customer_vpa, expiry_date)
        return mandate

    async def dispatch_proactive_nudge(self, mandate_id: str) -> Optional[ExpiringMandate]:
        """
        Dispatches an interactive 1-click WhatsApp/SMS renewal magic link to the customer
        before the mandate expires, logging the preventive action into RecoveryLedger.
        """
        m = self._mandates.get(mandate_id)
        if not m:
            logger.warning("Mandate %s not found for proactive nudge", mandate_id)
            return None

        # Generate 1-click renewal magic link
        link = await generate_mandate_renewal_link(
            customer_id=m.customer_id,
            plan_id=m.plan_name.lower().replace(" ", "_"),
            amount_inr=m.amount,
        )
        m.renewal_link = link
        m.status = "NUDGED"
        m.nudged_at = datetime.now(IST)

        # Dispatch personalized WhatsApp nudge
        hours_left = int(m.hours_remaining())
        msg = (
            f"Namaste {m.customer_name}! 🔔 Aapka {m.plan_name} ka UPI Autopay mandate ({m.mandate_id}) "
            f"agle {hours_left} ghante mein expire ho raha hai. "
            f"Service uninterrupted rakhne ke liye 1-click mein renew karein: {link}"
        )

        try:
            await messenger.send_whatsapp(
                to_phone="+919800000001",
                body=msg,
                customer_vpa=m.customer_vpa,
            )
        except Exception as e:
            logger.warning("Failed to send proactive WhatsApp nudge: %s", e)

        # Record proactive prevention audit trail in RecoveryLedger
        entry = recovery_ledger.log(
            event_type="intervene",
            vpa=m.customer_vpa,
            amount=m.amount,
            reasoning=f"Proactive 1-click renewal magic link dispatched {m.hours_remaining():.1f}h before BT02 mandate expiry",
            confidence=0.92,
            channel="whatsapp",
            outcome="success",
        )

        logger.info(
            "[PREVENTION] Proactive renewal link sent to %s for %s | Link: %s | Ledger: %s",
            m.customer_vpa, m.mandate_id, link, entry.ledger_id,
        )
        return m

    async def simulate_proactive_renewal(self, mandate_id: str) -> Optional[ExpiringMandate]:
        """
        Simulates customer completing the proactive 1-click mandate renewal before expiry,
        logging the confirmed pre-empted revenue recovery in RecoveryLedger.
        """
        m = self._mandates.get(mandate_id)
        if not m:
            logger.warning("Mandate %s not found for renewal simulation", mandate_id)
            return None

        m.status = "RENEWED"
        m.renewed_at = datetime.now(IST)

        # Record confirmed pre-empted recovery in RecoveryLedger
        entry = recovery_ledger.log(
            event_type="recover",
            vpa=m.customer_vpa,
            amount=m.amount,
            reasoning=f"Customer completed proactive 1-click mandate renewal — ₹{m.amount:.2f} protected from BT02 churn",
            confidence=1.0,
            channel="mandate_renewal",
            outcome="success",
        )
        recovery_ledger.mark_outcome(entry.ledger_id, outcome="success", amount_recovered=m.amount)

        logger.info(
            "[PREVENTION SUCCESS] Mandate %s renewed proactively | ₹%.2f protected from BT02 churn",
            m.mandate_id, m.amount,
        )
        return m

    def get_stats(self) -> dict:
        """Summary statistics of proactive expiry prevention."""
        expiring = self.find_expiring_mandates(within_hours=72)
        nudged = sum(1 for m in self._mandates.values() if m.status == "NUDGED")
        renewed = sum(1 for m in self._mandates.values() if m.status == "RENEWED")
        revenue_protected = sum(m.amount for m in self._mandates.values() if m.status == "RENEWED")
        revenue_at_risk = sum(m.amount for m in expiring if m.status != "RENEWED")

        return {
            "total_mandates_tracked": len(self._mandates),
            "expiring_within_72h": len(expiring),
            "nudges_dispatched": nudged,
            "renewals_completed": renewed,
            "revenue_at_risk": revenue_at_risk,
            "revenue_protected": revenue_protected,
            "prevention_rate_pct": round((renewed / len(self._mandates) * 100), 1) if self._mandates else 0.0,
        }

    def reset(self):
        """Reset state for tests."""
        self._mandates.clear()
        self._seed_expiring_mandates()


mandate_expiry_scanner = MandateExpiryScanner()
