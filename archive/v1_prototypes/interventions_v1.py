"""V1 Prototype: Recovery Interventions (Archived).

Preserved for reference. Production interventions are in src/agent/upi_interventions.py.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from src.agent.diagnoser import Diagnosis


class InterventionType(Enum):
    """Types of recovery interventions."""
    PAYMENT_RETRY = "payment_retry"
    CARD_UPDATE_REQUEST = "card_update_request"
    CHECKOUT_REMINDER = "checkout_reminder"
    DUNNING_EMAIL = "dunning_email"
    SMS_NUDGE = "sms_nudge"
    VOICE_CALL = "voice_call"
    DISCOUNT_OFFER = "discount_offer"
    INVOICE_REMINDER = "invoice_reminder"
    ESCALATION = "escalation"


@dataclass
class InterventionResult:
    """Result of executing an intervention."""
    intervention_type: InterventionType
    success: bool
    amount_recovered: float
    message: str
    next_action: InterventionType | None = None


class BaseIntervention(ABC):
    """Base class for all recovery interventions."""

    @abstractmethod
    async def execute(self, diagnosis: Diagnosis) -> InterventionResult:
        ...

    @abstractmethod
    def can_handle(self, diagnosis: Diagnosis) -> bool:
        ...
