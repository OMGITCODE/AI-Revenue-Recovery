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
        attempt = diagnosis.context.get("retry_attempt", 0)
        max_retries = 3
        
        if attempt >= max_retries:
            return InterventionResult(
                intervention_type=InterventionType.PAYMENT_RETRY,
                success=False,
                amount_recovered=0.0,
                message=f"Max retries ({max_retries}) reached. Escalating.",
                next_action=InterventionType.ESCALATION
            )
            
        backoff_hours = 2 ** attempt
        
        return InterventionResult(
            intervention_type=InterventionType.PAYMENT_RETRY,
            success=True,
            amount_recovered=diagnosis.risk.amount if diagnosis.risk else 0.0,
            message=f"Smart payment retry scheduled in {backoff_hours} hours (Attempt {attempt + 1})."
        )

    def can_handle(self, diagnosis: Diagnosis) -> bool:
        from .diagnoser import RootCause
        return diagnosis.root_cause in (
            RootCause.INSUFFICIENT_FUNDS,
            RootCause.NETWORK_ERROR,
        )


class CheckoutReminderIntervention(BaseIntervention):
    """Send a reminder for abandoned checkouts."""

    async def execute(self, diagnosis: Diagnosis) -> InterventionResult:
        return InterventionResult(
            intervention_type=InterventionType.CHECKOUT_REMINDER,
            success=True,
            amount_recovered=0.0,
            message="Checkout reminder sent to customer via SMS and Email.",
            next_action=None
        )

    def can_handle(self, diagnosis: Diagnosis) -> bool:
        from .diagnoser import RootCause
        return diagnosis.root_cause == RootCause.USER_ABANDONED
