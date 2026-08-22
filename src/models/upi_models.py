"""
UPI Autopay Models.

Covers the full lifecycle of a UPI Autopay / e-Mandate:
mandate creation → confirmation → execution → failure / recovery.

Error codes follow NPCI UPI 2.0 spec and Razorpay webhook schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# ── NPCI / Bank Error Codes ───────────────────────────────────────────────────

class UPIFailureCode(Enum):
    """
    NPCI UPI error codes returned on Autopay execution failure.
    Reference: NPCI UPI Recurring Mandate spec v2.0
    """
    # Funds / Limit issues
    U30  = "U30"   # Insufficient funds
    U69  = "U69"   # Daily transaction limit exceeded
    U66  = "U66"   # Weekly limit exceeded
    U64  = "U64"   # Monthly limit exceeded

    # Mandate issues
    BT01 = "BT01"  # Mandate revoked by customer
    BT02 = "BT02"  # Mandate expired
    U13  = "U13"   # Mandate paused
    U29  = "U29"   # Mandate amount exceeded

    # Bank / Account issues
    BA   = "BA"    # Beneficiary account closed / frozen
    XB   = "XB"    # Bank account blocked
    AM   = "AM"    # Account mismatch

    # Technical errors
    TM   = "TM"    # Technical / timeout error
    TE   = "TE"    # Transaction expired
    RB   = "RB"    # Response back from bank — generic decline

    # Unknown / catch-all
    UNKNOWN = "UNKNOWN"

    @property
    def is_recoverable(self) -> bool:
        """True if an automatic retry may succeed."""
        return self in {
            UPIFailureCode.U30,
            UPIFailureCode.U69,
            UPIFailureCode.U66,
            UPIFailureCode.U64,
            UPIFailureCode.U13,
            UPIFailureCode.TM,
            UPIFailureCode.TE,
            UPIFailureCode.UNKNOWN,
        }

    @property
    def requires_mandate_renewal(self) -> bool:
        """True if the mandate itself is dead and must be re-created."""
        return self in {
            UPIFailureCode.BT01,
            UPIFailureCode.BT02,
            UPIFailureCode.BA,
            UPIFailureCode.XB,
            UPIFailureCode.AM,
        }

    @property
    def human_reason(self) -> str:
        """Plain-English reason for the failure."""
        _MAP = {
            "U30":  "Insufficient funds in bank account",
            "U69":  "Daily UPI transaction limit exceeded",
            "U66":  "Weekly UPI transaction limit exceeded",
            "U64":  "Monthly UPI transaction limit exceeded",
            "BT01": "UPI Autopay mandate revoked by customer",
            "BT02": "UPI Autopay mandate has expired",
            "U13":  "Mandate is currently paused",
            "U29":  "Debit amount exceeds mandate limit",
            "BA":   "Bank account closed or frozen",
            "XB":   "Bank account is blocked",
            "AM":   "Account mismatch",
            "TM":   "Technical / timeout error from bank",
            "TE":   "Transaction expired before bank response",
            "RB":   "Bank declined the transaction",
            "UNKNOWN": "Unknown failure",
        }
        return _MAP.get(self.value, "Unknown failure")


# ── Mandate Lifecycle ─────────────────────────────────────────────────────────

class MandateState(Enum):
    """States of a UPI Autopay mandate."""
    CREATED   = "created"    # Mandate request sent, awaiting customer approval
    CONFIRMED = "confirmed"  # Customer approved via UPI app
    ACTIVE    = "active"     # Successfully debited at least once
    PAUSED    = "paused"     # Temporarily paused (by customer or bank)
    REVOKED   = "revoked"    # Cancelled by customer — requires renewal
    EXPIRED   = "expired"    # Mandate validity period ended
    FAILED    = "failed"     # Creation or confirmation failed


class MandateFrequency(Enum):
    """Debit frequency of a recurring mandate."""
    DAILY       = "daily"
    WEEKLY      = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY     = "monthly"
    BIMONTHLY   = "bimonthly"
    QUARTERLY   = "quarterly"
    HALFYEARLY  = "halfyearly"
    YEARLY      = "yearly"
    AS_PRESENTED = "as_presented"  # On-demand / presenter-initiated


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class UPIMandate:
    """Represents a UPI Autopay mandate and its current state."""
    mandate_id:        str
    customer_id:       str
    customer_vpa:      str            # e.g. "rahul@oksbi"
    amount:            float          # Max debit amount per cycle (INR)
    frequency:         MandateFrequency
    state:             MandateState
    bank_name:         str            # e.g. "SBI", "HDFC", "ICICI"
    bank_ifsc:         str
    created_at:        datetime
    expiry_date:       datetime
    last_debit_at:     datetime | None = None
    failure_count:     int = 0
    metadata:          dict = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.state in {MandateState.CONFIRMED, MandateState.ACTIVE}

    @property
    def is_renewable(self) -> bool:
        return self.state in {MandateState.REVOKED, MandateState.EXPIRED}


@dataclass
class UPIAutopayEvent:
    """
    Enriched event from a Razorpay UPI Autopay webhook.
    Parsed from raw webhook payload before entering the pipeline.
    """
    event_id:          str
    event_type:        str           # e.g. "mandate.execution.failed"
    payment_id:        str | None
    mandate:           UPIMandate
    failure_code:      UPIFailureCode
    failure_message:   str
    debit_amount:      float         # Actual amount attempted (may differ from mandate max)
    currency:          str = "INR"
    occurred_at:       datetime = field(default_factory=datetime.now)
    retry_attempt:     int = 0       # 0 = first attempt, 1+ = retries
    raw_payload:       dict = field(default_factory=dict)

    @property
    def is_first_failure(self) -> bool:
        return self.retry_attempt == 0

    @property
    def customer_vpa(self) -> str:
        return self.mandate.customer_vpa

    @property
    def bank_name(self) -> str:
        return self.mandate.bank_name


# ── Recovery Context (passed between pipeline stages) ─────────────────────────

@dataclass
class UPIRecoveryContext:
    """
    Carries all context needed across the detect → diagnose → intervene pipeline
    for a single UPI Autopay failure event.
    """
    event:             UPIAutopayEvent
    failure_code:      UPIFailureCode
    is_recoverable:    bool
    requires_renewal:  bool
    suggested_actions: list[str] = field(default_factory=list)
    scheduled_retry:   datetime | None = None
    notes:             str = ""
