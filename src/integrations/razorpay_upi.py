"""
Razorpay UPI Autopay Integration.

Wraps Razorpay API calls for UPI Autopay / e-Mandate operations.
In production, set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env.

All methods are async-compatible and return typed objects from upi_models.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any

from ..models.upi_models import (
    MandateFrequency,
    MandateState,
    UPIAutopayEvent,
    UPIFailureCode,
    UPIMandate,
)

logger = logging.getLogger(__name__)

# Razorpay webhook event types we care about
WATCHED_EVENTS = {
    "subscription.charged",           # recurring debit attempt (check status for failure)
    "mandate.execution.failed",       # explicit execution failure
    "mandate.revoked",                # customer revoked via UPI app
    "mandate.expired",                # mandate crossed validity date
    "mandate.paused",                 # bank / customer paused mandate
    "payment.failed",                 # fallback — generic payment failure
}


# ── Signature Verification ────────────────────────────────────────────────────

def verify_webhook_signature(
    payload_body: bytes,
    razorpay_signature: str,
    webhook_secret: str,
) -> bool:
    """
    Verify that the webhook payload came from Razorpay.

    Razorpay signs webhooks with HMAC-SHA256 using the webhook secret.
    Reference: https://razorpay.com/docs/webhooks/validate-test/
    """
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, razorpay_signature)


# ── Event Parser ──────────────────────────────────────────────────────────────

def parse_upi_webhook(payload: dict) -> UPIAutopayEvent | None:
    """
    Parse a raw Razorpay webhook payload into a typed UPIAutopayEvent.

    Returns None if the event is not a UPI Autopay failure event.

    Args:
        payload: Decoded JSON payload from Razorpay webhook POST body.

    Returns:
        UPIAutopayEvent if the event is relevant, else None.
    """
    event_type = payload.get("event", "")
    if event_type not in WATCHED_EVENTS:
        logger.debug("Ignoring non-UPI event: %s", event_type)
        return None

    # Navigate Razorpay's nested payload structure
    entity = _extract_entity(payload, event_type)
    if not entity:
        logger.warning("Could not extract entity from payload: %s", event_type)
        return None

    mandate_data = entity.get("mandate", {}) or entity.get("recurring_details", {}) or {}
    failure_code = _parse_failure_code(entity)
    mandate = _build_mandate(entity, mandate_data)

    return UPIAutopayEvent(
        event_id=payload.get("id", f"evt_{datetime.now().timestamp():.0f}"),
        event_type=event_type,
        payment_id=entity.get("id") if event_type.startswith("payment") else None,
        mandate=mandate,
        failure_code=failure_code,
        failure_message=entity.get("error_description", failure_code.human_reason),
        debit_amount=_parse_amount(entity.get("amount", 0)),
        currency=entity.get("currency", "INR"),
        occurred_at=datetime.fromtimestamp(payload.get("created_at", datetime.now().timestamp())),
        retry_attempt=int(entity.get("attempts", 0)),
        raw_payload=payload,
    )


def _extract_entity(payload: dict, event_type: str) -> dict:
    """Pull the primary entity from the nested Razorpay payload."""
    entities = payload.get("payload", {})
    if "payment" in entities:
        return entities["payment"].get("entity", {})
    if "subscription" in entities:
        return entities["subscription"].get("entity", {})
    if "mandate" in entities:
        return entities["mandate"].get("entity", {})
    # Fallback: return top-level payload
    return payload


def _parse_failure_code(entity: dict) -> UPIFailureCode:
    """Extract NPCI failure code from various Razorpay field names."""
    code_str = (
        entity.get("error_code")
        or entity.get("error_reason")
        or entity.get("gateway_error_code")
        or "UNKNOWN"
    )
    # Razorpay sometimes prefixes gateway codes with "BAD_REQUEST_ERROR" etc.
    # Strip and normalise
    code_str = code_str.upper().strip()
    try:
        return UPIFailureCode(code_str)
    except ValueError:
        logger.debug("Unknown UPI failure code: %s — mapping to UNKNOWN", code_str)
        return UPIFailureCode.UNKNOWN


def _parse_amount(raw: Any) -> float:
    """Razorpay amounts are in paise (1 INR = 100 paise)."""
    try:
        return float(raw) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _build_mandate(entity: dict, mandate_data: dict) -> UPIMandate:
    """Construct a UPIMandate from Razorpay entity fields."""
    state_raw = mandate_data.get("status", entity.get("status", "active"))
    try:
        state = MandateState(state_raw.lower())
    except ValueError:
        state = MandateState.ACTIVE  # assume active if unknown

    freq_raw = mandate_data.get("frequency", "monthly")
    try:
        freq = MandateFrequency(freq_raw.lower())
    except ValueError:
        freq = MandateFrequency.MONTHLY

    return UPIMandate(
        mandate_id=mandate_data.get("id", entity.get("subscription_id", "MND-UNKNOWN")),
        customer_id=entity.get("customer_id", "CUST-UNKNOWN"),
        customer_vpa=_extract_vpa(entity),
        amount=_parse_amount(mandate_data.get("max_amount", entity.get("amount", 0))),
        frequency=freq,
        state=state,
        bank_name=_extract_bank(entity),
        bank_ifsc=entity.get("bank", {}).get("ifsc", "UNKNOWN") if isinstance(entity.get("bank"), dict) else "UNKNOWN",
        created_at=datetime.fromtimestamp(mandate_data.get("created_at", datetime.now().timestamp())),
        expiry_date=datetime.fromtimestamp(mandate_data.get("end_at", datetime.now().timestamp())),
        last_debit_at=None,
        failure_count=int(entity.get("attempts", 0)),
        metadata={"raw_mandate": mandate_data},
    )


def _extract_vpa(entity: dict) -> str:
    """Try multiple known locations for the customer VPA."""
    return (
        entity.get("vpa")
        or entity.get("upi", {}).get("vpa", "")
        or entity.get("customer_vpa", "customer@upi")
    )


def _extract_bank(entity: dict) -> str:
    """Heuristically extract bank name from VPA or bank field."""
    vpa = _extract_vpa(entity)
    if "@" in vpa:
        handle = vpa.split("@")[-1].lower()
        VPA_BANK_MAP = {
            "oksbi": "SBI", "sbi": "SBI",
            "okhdfcbank": "HDFC", "hdfc": "HDFC",
            "okicici": "ICICI", "icici": "ICICI",
            "okaxis": "Axis", "axis": "Axis",
            "paytm": "Paytm Payments Bank",
            "ybl": "Yes Bank", "axl": "Axis",
            "ibl": "IDBI", "upi": "NPCI",
        }
        return VPA_BANK_MAP.get(handle, handle.upper())
    return entity.get("bank_name", "Unknown Bank")


# ── Mock API Calls (replace with real razorpay SDK in production) ─────────────

async def fetch_mandate(mandate_id: str, api_key: str = "", api_secret: str = "") -> dict:
    """
    Fetch live mandate details from Razorpay API.

    Production: GET https://api.razorpay.com/v1/payments/mandate/{mandate_id}
    Returns raw Razorpay mandate entity dict.

    Note: In demo mode (no keys), returns a mock response.
    """
    if not api_key:
        logger.info("Demo mode: returning mock mandate for %s", mandate_id)
        return {
            "id": mandate_id,
            "status": "active",
            "frequency": "monthly",
            "max_amount": 99900,  # ₹999 in paise
            "customer_id": "cust_demo",
        }
    # Production path (requires razorpay-python SDK or httpx)
    raise NotImplementedError("Production Razorpay API not configured. Set RAZORPAY_KEY_ID.")


async def trigger_collect_request(
    customer_vpa: str,
    amount_inr: float,
    note: str,
    api_key: str = "",
    api_secret: str = "",
) -> dict:
    """
    Send a UPI collect request to the customer's VPA.

    Production: POST https://api.razorpay.com/v1/payments/create/upi
    The customer gets a notification in their UPI app to approve.

    Returns: {"payment_id": "...", "status": "pending", ...}
    """
    if not api_key:
        logger.info("Demo mode: mock collect request to %s for ₹%.2f", customer_vpa, amount_inr)
        return {
            "payment_id": f"pay_demo_{customer_vpa.replace('@', '_')}",
            "status": "pending",
            "vpa": customer_vpa,
            "amount": amount_inr,
            "note": note,
        }
    raise NotImplementedError("Production Razorpay API not configured.")


async def generate_mandate_renewal_link(
    customer_id: str,
    plan_id: str,
    amount_inr: float,
    api_key: str = "",
    api_secret: str = "",
) -> str:
    """
    Generate a Razorpay magic link for the customer to re-register their UPI mandate.

    Production: POST https://api.razorpay.com/v1/subscriptions
    Returns a payment link URL.
    """
    if not api_key:
        mock_link = f"https://rzp.io/l/demo-mandate-{customer_id}"
        logger.info("Demo mode: mock renewal link → %s", mock_link)
        return mock_link
    raise NotImplementedError("Production Razorpay API not configured.")
