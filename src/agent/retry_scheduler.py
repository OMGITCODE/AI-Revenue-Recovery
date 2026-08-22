"""
UPI Autopay Retry Scheduler.

Determines the optimal retry datetime for a failed UPI Autopay debit,
based on the NPCI error code, bank-specific cooling periods,
and Indian salary-cycle patterns.

All datetimes are returned in IST (Asia/Kolkata, UTC+5:30).
No external dependencies — uses only stdlib datetime.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from ..models.upi_models import UPIFailureCode

logger = logging.getLogger(__name__)

# IST = UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

# ── Bank-specific cooling periods (hours) ─────────────────────────────────────
# Some banks block back-to-back retries. We respect their minimum gap.
BANK_COOLING_HOURS: dict[str, int] = {
    "SBI":   24,   # SBI is strict — 24h between retries
    "HDFC":  12,
    "ICICI": 12,
    "Axis":  6,
    "Yes Bank": 6,
    "Paytm Payments Bank": 4,
    "DEFAULT": 4,
}

# Maximum automatic retries before escalation
MAX_AUTO_RETRIES = 3

# Salary window: 1st to 7th of each month is when most Indian salaried
# employees receive their salary. We target 10:00 AM IST within this window.
SALARY_WINDOW_START_DAY = 1
SALARY_WINDOW_END_DAY   = 7
SALARY_RETRY_HOUR_IST   = 10   # 10:00 AM IST — most people have checked balance by then


class RetryDecision(NamedTuple):
    """Result returned by the scheduler for a single failure."""
    should_retry:       bool
    scheduled_at:       datetime | None   # None if should_retry is False
    strategy:           str               # Human-readable reason
    requires_renewal:   bool              # True if mandate must be re-created
    max_retries_hit:    bool              # True if we've given up on auto-retry


# ── Main Scheduler ────────────────────────────────────────────────────────────

class UPIRetryScheduler:
    """
    Salary-cycle-aware retry scheduler for UPI Autopay failures.

    Usage:
        scheduler = UPIRetryScheduler()
        decision = scheduler.schedule(
            failure_code=UPIFailureCode.U30,
            bank_name="SBI",
            attempt_number=0,
            failure_time=datetime.now(IST),
        )
    """

    def schedule(
        self,
        failure_code: UPIFailureCode,
        bank_name: str,
        attempt_number: int,
        failure_time: datetime | None = None,
    ) -> RetryDecision:
        """
        Decide if and when to retry a failed UPI Autopay debit.

        Args:
            failure_code:   NPCI error code from the failed execution.
            bank_name:      Bank name (from VPA handle or mandate data).
            attempt_number: 0 = first failure, 1+ = previous retries.
            failure_time:   When the failure occurred (defaults to now IST).

        Returns:
            RetryDecision with schedule details.
        """
        now = failure_time or datetime.now(IST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)

        # ── Gate: mandate is dead — no retry, needs renewal ───────────────────
        if failure_code.requires_mandate_renewal:
            return RetryDecision(
                should_retry=False,
                scheduled_at=None,
                strategy=f"Mandate unrecoverable ({failure_code.value}: {failure_code.human_reason}). "
                          f"Customer must re-register mandate.",
                requires_renewal=True,
                max_retries_hit=False,
            )

        # ── Gate: max retries hit ─────────────────────────────────────────────
        if attempt_number >= MAX_AUTO_RETRIES:
            return RetryDecision(
                should_retry=False,
                scheduled_at=None,
                strategy=f"Max {MAX_AUTO_RETRIES} auto-retries exhausted. Escalating to support.",
                requires_renewal=False,
                max_retries_hit=True,
            )

        # ── Code-specific retry strategy ──────────────────────────────────────
        if failure_code == UPIFailureCode.U30:
            return self._salary_window_retry(now, bank_name, attempt_number)

        if failure_code in {UPIFailureCode.U69, UPIFailureCode.U66, UPIFailureCode.U64}:
            return self._next_day_retry(now, bank_name, failure_code)

        if failure_code in {UPIFailureCode.TM, UPIFailureCode.TE, UPIFailureCode.RB}:
            return self._exponential_backoff_retry(now, bank_name, attempt_number)

        if failure_code == UPIFailureCode.U13:
            return self._paused_mandate_retry(now, bank_name)

        # Catch-all for UNKNOWN
        return self._exponential_backoff_retry(now, bank_name, attempt_number)

    # ── Strategy: Salary Window ───────────────────────────────────────────────

    def _salary_window_retry(
        self, now: datetime, bank_name: str, attempt: int
    ) -> RetryDecision:
        """
        U30 (Insufficient Funds) — retry during next salary credit window.

        Indian salaried employees typically receive salary on 1st–7th of the month.
        We schedule within that window, offset by attempt number to spread load.
        """
        retry_day_offset = attempt  # attempt 0 → 1st, attempt 1 → 3rd, etc.
        target_day = SALARY_WINDOW_START_DAY + (retry_day_offset * 2)

        # If target day is beyond the salary window, retry on 1st of month after next
        if target_day > SALARY_WINDOW_END_DAY:
            target_day = SALARY_WINDOW_START_DAY

        # Find the next occurrence of that day in the calendar
        retry_dt = _next_occurrence_of_day(now, target_day, SALARY_RETRY_HOUR_IST)

        # Apply bank cooling period
        cooling = BANK_COOLING_HOURS.get(bank_name, BANK_COOLING_HOURS["DEFAULT"])
        min_retry = now + timedelta(hours=cooling)
        if retry_dt < min_retry:
            retry_dt = min_retry.replace(
                hour=SALARY_RETRY_HOUR_IST, minute=0, second=0, microsecond=0
            )
            if retry_dt < min_retry:
                retry_dt += timedelta(days=1)

        return RetryDecision(
            should_retry=True,
            scheduled_at=retry_dt,
            strategy=(
                f"Insufficient funds (U30). Retrying on {retry_dt.strftime('%d %b %Y at %I:%M %p IST')} "
                f"during salary credit window (attempt {attempt + 1}/{MAX_AUTO_RETRIES})."
            ),
            requires_renewal=False,
            max_retries_hit=False,
        )

    # ── Strategy: Next Day ────────────────────────────────────────────────────

    def _next_day_retry(
        self, now: datetime, bank_name: str, code: UPIFailureCode
    ) -> RetryDecision:
        """
        U69/U66/U64 (Limit Exceeded) — retry after midnight reset.
        UPI daily limits reset at midnight. Retry at 06:00 AM IST next day.
        """
        retry_dt = (now + timedelta(days=1)).replace(
            hour=6, minute=0, second=0, microsecond=0
        )
        return RetryDecision(
            should_retry=True,
            scheduled_at=retry_dt,
            strategy=(
                f"Transaction limit exceeded ({code.value}). "
                f"Retrying at {retry_dt.strftime('%d %b %Y at %I:%M %p IST')} "
                f"after daily limit resets at midnight."
            ),
            requires_renewal=False,
            max_retries_hit=False,
        )

    # ── Strategy: Exponential Backoff ─────────────────────────────────────────

    def _exponential_backoff_retry(
        self, now: datetime, bank_name: str, attempt: int
    ) -> RetryDecision:
        """
        TM/TE/RB (Technical Errors) — exponential backoff.
        Pattern: 2h → 6h → 24h
        """
        BACKOFF_HOURS = [2, 6, 24]
        hours = BACKOFF_HOURS[min(attempt, len(BACKOFF_HOURS) - 1)]

        cooling = BANK_COOLING_HOURS.get(bank_name, BANK_COOLING_HOURS["DEFAULT"])
        hours = max(hours, cooling)

        retry_dt = now + timedelta(hours=hours)

        return RetryDecision(
            should_retry=True,
            scheduled_at=retry_dt,
            strategy=(
                f"Technical error. Retrying in {hours}h "
                f"at {retry_dt.strftime('%d %b %Y at %I:%M %p IST')} "
                f"(attempt {attempt + 1}/{MAX_AUTO_RETRIES})."
            ),
            requires_renewal=False,
            max_retries_hit=False,
        )

    # ── Strategy: Paused Mandate ──────────────────────────────────────────────

    def _paused_mandate_retry(self, now: datetime, bank_name: str) -> RetryDecision:
        """
        U13 (Mandate Paused) — notify customer to un-pause, retry in 48h.
        """
        retry_dt = now + timedelta(hours=48)
        return RetryDecision(
            should_retry=True,
            scheduled_at=retry_dt,
            strategy=(
                f"Mandate is paused (U13). Notifying customer to un-pause. "
                f"Retrying at {retry_dt.strftime('%d %b %Y at %I:%M %p IST')}."
            ),
            requires_renewal=False,
            max_retries_hit=False,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _next_occurrence_of_day(from_dt: datetime, target_day: int, hour: int) -> datetime:
    """
    Return the next datetime where day-of-month == target_day at the given hour (IST).

    If target_day has already passed this month, returns it in the next month.
    If target_day is today but the hour hasn't arrived yet, returns today.
    """
    import calendar

    candidate = from_dt.replace(day=1)  # start from 1st to safely add months

    # Try current month first
    _, days_in_month = calendar.monthrange(from_dt.year, from_dt.month)
    safe_day = min(target_day, days_in_month)
    candidate = from_dt.replace(day=safe_day, hour=hour, minute=0, second=0, microsecond=0)

    if candidate > from_dt:
        return candidate

    # Move to next month
    if from_dt.month == 12:
        next_year, next_month = from_dt.year + 1, 1
    else:
        next_year, next_month = from_dt.year, from_dt.month + 1

    _, days_in_next_month = calendar.monthrange(next_year, next_month)
    safe_day = min(target_day, days_in_next_month)
    return from_dt.replace(
        year=next_year, month=next_month, day=safe_day,
        hour=hour, minute=0, second=0, microsecond=0
    )
