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
    "mandate.execution.failed": (RiskType.MANDATE_FAILURE,      RiskSeverity.MEDIUM),
    "mandate.revoked":          (RiskType.MANDATE_FAILURE,      RiskSeverity.CRITICAL),
    "mandate.expired":          (RiskType.MANDATE_FAILURE,      RiskSeverity.CRITICAL),
    "mandate.paused":           (RiskType.MANDATE_FAILURE,      RiskSeverity.LOW),
    "subscription.charged":     (RiskType.SUBSCRIPTION_FAILURE, RiskSeverity.MEDIUM),
    "payment.failed":           (RiskType.PAYMENT_FAILURE,      RiskSeverity.MEDIUM),
}

# ── Failure code severity overrides ──────────────────────────────────────────
_CODE_SEVERITY_OVERRIDE: dict[UPIFailureCode, RiskSeverity] = {
    UPIFailureCode.BT01: RiskSeverity.CRITICAL,  # Revoked mandate — dead
    UPIFailureCode.BT02: RiskSeverity.CRITICAL,  # Expired mandate — dead
    UPIFailureCode.BA:   RiskSeverity.CRITICAL,  # Account closed — dead
    UPIFailureCode.XB:   RiskSeverity.CRITICAL,  # Account blocked — dead
    UPIFailureCode.U30:  RiskSeverity.MEDIUM,    # Insufficient funds — recoverable
    UPIFailureCode.TM:   RiskSeverity.LOW,       # Technical timeout — transient
    UPIFailureCode.TE:   RiskSeverity.LOW,       # Transaction expired
    UPIFailureCode.U13:  RiskSeverity.LOW,       # Paused — customer action needed
    UPIFailureCode.U69:  RiskSeverity.MEDIUM,    # Daily limit hit
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

        from .spend_pattern import spend_pattern_tracker

        risk_type, _ = mapping
        cust_id = upi_event.mandate.customer_id if upi_event.mandate else ""

        # 1. Terminal unrecoverable failure codes & retry exhaustion are always CRITICAL
        if upi_event.failure_code in (
            UPIFailureCode.BT01, UPIFailureCode.BT02,
            UPIFailureCode.BA, UPIFailureCode.XB
        ):
            severity = RiskSeverity.CRITICAL
            pattern_analysis = spend_pattern_tracker.analyze(
                vpa=upi_event.customer_vpa,
                current_amount=upi_event.debit_amount,
                customer_id=cust_id,
            )
        elif upi_event.retry_attempt >= 2:
            # 3rd+ retry attempt exhausted
            severity = RiskSeverity.CRITICAL
            pattern_analysis = spend_pattern_tracker.analyze(
                vpa=upi_event.customer_vpa,
                current_amount=upi_event.debit_amount,
                customer_id=cust_id,
            )
        else:
            # 2. Spend Pattern & Anomaly Analysis for this customer profile
            # Evaluates transaction against customer's personalized unified spending profile
            pattern_analysis = spend_pattern_tracker.analyze(
                vpa=upi_event.customer_vpa,
                current_amount=upi_event.debit_amount,
                customer_id=cust_id,
            )
            severity = pattern_analysis.severity

        # Automatically record this transaction into customer history so subsequent
        # transactions for this user evaluate against their rolling spending profile
        spend_pattern_tracker.record_transaction(
            vpa=upi_event.customer_vpa,
            amount=upi_event.debit_amount,
            customer_id=cust_id,
        )

        risk = RevenueRisk(
            id=f"UPI-RISK-{upi_event.event_id}",
            risk_type=risk_type,
            severity=severity,
            amount=upi_event.debit_amount,
            currency=upi_event.currency,
            customer_id=cust_id,
            detected_at=upi_event.occurred_at,
            metadata={
                # UPI-specific context carried forward to diagnoser & interventions
                "upi_event":        upi_event,
                "failure_code":     upi_event.failure_code,
                "customer_vpa":     upi_event.customer_vpa,
                "bank_name":        upi_event.bank_name,
                "mandate_id":       upi_event.mandate.mandate_id if upi_event.mandate else "",
                "mandate_state":    upi_event.mandate.state if upi_event.mandate else MandateState.ACTIVE,
                "retry_attempt":    upi_event.retry_attempt,
                "is_recoverable":   upi_event.failure_code.is_recoverable,
                "requires_renewal":  upi_event.failure_code.requires_mandate_renewal,
                "pattern_analysis": pattern_analysis,
            },
        )

        logger.info(
            "UPI risk detected: %s | code=%s | severity=%s | amount=₹%.2f | vpa=%s | cust=%s | spike_ratio=%.2fx | is_spike_critical=%s",
            risk.id, upi_event.failure_code.value,
            severity.value, upi_event.debit_amount, upi_event.customer_vpa, cust_id,
            pattern_analysis.spike_ratio, pattern_analysis.is_critical,
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
