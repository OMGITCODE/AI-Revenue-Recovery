"""Revenue Risk Models.

Defines the core data models, risk types, and severity levels for revenue recovery detection.
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
