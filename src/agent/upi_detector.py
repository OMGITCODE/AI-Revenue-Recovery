"""
UPI Autopay Detector.

Extends the base RevenueRiskDetector to recognise Razorpay UPI Autopay
webhook events and convert them into typed RevenueRisk objects.
"""

from __future__ import annotations

from datetime import datetime

from ..models.risk_models import RevenueRisk, RiskSeverity, RiskType
from ..models.upi_models import UPIAutopayEvent, UPIFailureCode, MandateState
from ..utils.logger import get_logger

logger = get_logger(__name__)

# ── Razorpay event type → (RiskType, base severity) ──────────────────────────
_EVENT_RISK_MAP: dict[str, tuple[RiskType, RiskSeverity]] = {
    "mandate.execution.failed": (RiskType.MANDATE_FAILURE,      RiskSeverity.HIGH),
    "mandate.revoked":          (RiskType.MANDATE_FAILURE,      RiskSeverity.CRITICAL),
    "mandate.expired":          (RiskType.MANDATE_FAILURE,      RiskSeverity.HIGH),
    "mandate.paused":           (RiskType.MANDATE_FAILURE,      RiskSeverity.MEDIUM),
    "subscription.charged":     (RiskType.SUBSCRIPTION_FAILURE, RiskSeverity.CRITICAL),
    "payment.failed":           (RiskType.PAYMENT_FAILURE,      RiskSeverity.HIGH),
}

# ── Failure code severity overrides ──────────────────────────────────────────
_CODE_SEVERITY_OVERRIDE: dict[UPIFailureCode, RiskSeverity] = {
    UPIFailureCode.BT01: RiskSeverity.CRITICAL,  # Revoked — highest priority
    UPIFailureCode.BT02: RiskSeverity.CRITICAL,  # Expired mandate
    UPIFailureCode.BA:   RiskSeverity.CRITICAL,  # Account closed
    UPIFailureCode.U30:  RiskSeverity.HIGH,
    UPIFailureCode.TM:   RiskSeverity.MEDIUM,    # Technical — likely transient
    UPIFailureCode.U13:  RiskSeverity.MEDIUM,    # Paused — customer action needed
}


class UPIAutopayDetector:
    """
    Detects UPI Autopay failure risks from parsed Razorpay webhook events.

    Usage:
        detector = UPIAutopayDetector()
        risk = await detector.detect_from_upi_event(upi_event)
    """

    async def detect_from_upi_event(
        self, upi_event: UPIAutopayEvent
    ) -> RevenueRisk | None:
        """
        Convert a parsed UPIAutopayEvent into a RevenueRisk.

        Args:
            upi_event: Parsed and validated UPI event.

        Returns:
            RevenueRisk if this event represents revenue at risk, else None.
        """
        mapping = _EVENT_RISK_MAP.get(upi_event.event_type)
        if mapping is None:
            logger.debug("No risk mapping for UPI event: %s", upi_event.event_type)
            return None

        risk_type, base_severity = mapping

        # Apply failure-code severity override
        severity = _CODE_SEVERITY_OVERRIDE.get(upi_event.failure_code, base_severity)

        # Escalate to CRITICAL if this is the 3rd+ attempt
        if upi_event.retry_attempt >= 2 and severity != RiskSeverity.CRITICAL:
            severity = RiskSeverity.CRITICAL

        risk = RevenueRisk(
            id=f"UPI-RISK-{upi_event.event_id}",
            risk_type=risk_type,
            severity=severity,
            amount=upi_event.debit_amount,
            currency=upi_event.currency,
            customer_id=upi_event.mandate.customer_id,
            detected_at=upi_event.occurred_at,
            metadata={
                # UPI-specific context carried forward to diagnoser & interventions
                "upi_event":       upi_event,
                "failure_code":    upi_event.failure_code,
                "customer_vpa":    upi_event.customer_vpa,
                "bank_name":       upi_event.bank_name,
                "mandate_id":      upi_event.mandate.mandate_id,
                "mandate_state":   upi_event.mandate.state,
                "retry_attempt":   upi_event.retry_attempt,
                "is_recoverable":  upi_event.failure_code.is_recoverable,
                "requires_renewal": upi_event.failure_code.requires_mandate_renewal,
            },
        )

        logger.info(
            "UPI risk detected: %s | code=%s | severity=%s | amount=₹%.2f | vpa=%s",
            risk.id, upi_event.failure_code.value,
            severity.value, upi_event.debit_amount, upi_event.customer_vpa,
        )
        return risk

    async def detect_from_raw_event(self, raw_event: dict) -> RevenueRisk | None:
        """
        Convenience: parse a raw Razorpay webhook dict and detect risk in one call.

        Args:
            raw_event: Raw JSON-decoded Razorpay webhook payload.
        """
        from ..integrations.razorpay_upi import parse_upi_webhook
        upi_event = parse_upi_webhook(raw_event)
        if upi_event is None:
            return None
        return await self.detect_from_upi_event(upi_event)
