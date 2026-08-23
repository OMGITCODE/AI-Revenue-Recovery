"""Revenue Risk Detector.

Monitors payment events and identifies revenue at risk
across different failure modes.
"""

from enum import Enum
from dataclasses import dataclass
from datetime import datetime


class RiskType(Enum):
    """Types of revenue risk detected."""
    PAYMENT_FAILURE             = "payment_failure"
    CHECKOUT_ABANDONMENT        = "checkout_abandonment"
    SUBSCRIPTION_FAILURE        = "subscription_failure"
    INVOICE_OVERDUE             = "invoice_overdue"
    MANDATE_FAILURE             = "mandate_failure"
    UPI_AUTOPAY_MANDATE_FAILURE = "upi_autopay_mandate_failure"  # UPI Autopay / e-Mandate specific


class RiskSeverity(Enum):
    """Severity levels for detected risks."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class RevenueRisk:
    """Represents a detected revenue risk event."""
    id: str
    risk_type: RiskType
    severity: RiskSeverity
    amount: float
    currency: str
    customer_id: str
    detected_at: datetime
    metadata: dict | None = None

    @property
    def is_critical(self) -> bool:
        return self.severity == RiskSeverity.CRITICAL


class RevenueRiskDetector:
    """Detects revenue at risk from payment events and signals."""

    def __init__(self):
        self._handlers: dict[RiskType, list] = {}

    def register_handler(self, risk_type: RiskType, handler):
        """Register a handler for a specific risk type."""
        if risk_type not in self._handlers:
            self._handlers[risk_type] = []
        self._handlers[risk_type].append(handler)

    async def detect(self, event: dict) -> RevenueRisk | None:
        """Analyze an incoming event and detect revenue risk.

        Args:
            event: Raw event from payment gateway or CRM.

        Returns:
            RevenueRisk if risk detected, None otherwise.
        """
        event_type = event.get("event_type")
        if not event_type:
            return None

        risk_type_map = {
            "payment.failed": RiskType.PAYMENT_FAILURE,
            "checkout.abandoned": RiskType.CHECKOUT_ABANDONMENT,
            "subscription.failed": RiskType.SUBSCRIPTION_FAILURE,
            "invoice.overdue": RiskType.INVOICE_OVERDUE,
            "mandate.failed": RiskType.MANDATE_FAILURE,
            "upi_autopay.failed": RiskType.UPI_AUTOPAY_MANDATE_FAILURE,
        }

        risk_type = risk_type_map.get(event_type)
        if not risk_type:
            return None

        amount = float(event.get("amount", 0))
        severity = RiskSeverity.HIGH if amount >= 5000 else RiskSeverity.MEDIUM
        
        return RevenueRisk(
            id=event.get("id", f"evt_{int(datetime.utcnow().timestamp())}"),
            risk_type=risk_type,
            severity=severity,
            amount=amount,
            currency=event.get("currency", "INR"),
            customer_id=event.get("customer_id", "cust_unknown"),
            detected_at=datetime.utcnow(),
            metadata=event.get("metadata", {})
        )

    async def process_risk(self, risk: RevenueRisk):
        """Process a detected risk through registered handlers."""
        handlers = self._handlers.get(risk.risk_type, [])
        for handler in handlers:
            await handler(risk)
