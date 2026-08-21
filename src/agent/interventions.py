"""Recovery Interventions.

Defines and executes recovery actions to win back lost revenue.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from .diagnoser import Diagnosis


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
        """Execute the intervention based on diagnosis.

        Args:
            diagnosis: The diagnosis determining what action to take.

        Returns:
            InterventionResult indicating success/failure.
        """
        ...

    @abstractmethod
    def can_handle(self, diagnosis: Diagnosis) -> bool:
        """Check if this intervention can handle the given diagnosis."""
        ...


class PaymentRetryIntervention(BaseIntervention):
    """Retry a failed payment with smart timing."""

    async def execute(self, diagnosis: Diagnosis) -> InterventionResult:
        # TODO: Implement smart payment retry with exponential backoff
        raise NotImplementedError

    def can_handle(self, diagnosis: Diagnosis) -> bool:
        from .diagnoser import RootCause
        return diagnosis.root_cause in (
            RootCause.INSUFFICIENT_FUNDS,
            RootCause.NETWORK_ERROR,
        )


class CheckoutReminderIntervention(BaseIntervention):
    """Send a reminder for abandoned checkouts."""

    async def execute(self, diagnosis: Diagnosis) -> InterventionResult:
        # TODO: Implement checkout reminder via email/SMS
        raise NotImplementedError

    def can_handle(self, diagnosis: Diagnosis) -> bool:
        from .diagnoser import RootCause
        return diagnosis.root_cause == RootCause.USER_ABANDONED
