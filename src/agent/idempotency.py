"""
Idempotency & Concurrency Lock Layer for Webhooks and Recovery Operations
========================================================================
Prevents:
  1. Duplicate Webhook Execution: Gateways (Razorpay/Stripe) often retry
     failed/slow webhooks. An event processed once will NOT trigger duplicate
     SMS/WhatsApp nudges, mandate renewals, or bank debits.
  2. Race Conditions / Concurrent Customer State Corruption: Serializes
     incoming parallel events for the same customer (VPA) using an async mutex lock.
"""

import asyncio
import hashlib
import json
import time
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from ..utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class IdempotencyRecord:
    """Stores metadata and cached response of an executed webhook event."""
    event_id: str
    vpa: str
    status: str
    created_at: float = field(default_factory=time.time)
    response_payload: Optional[Dict[str, Any]] = None
    touches_count: int = 1


class IdempotencyManager:
    """
    In-memory, TTL-backed event idempotency cache.
    Pluggable architecture ready for Redis in multi-instance deployments.
    """

    def __init__(self, ttl_seconds: int = 86400):  # 24-hour default TTL
        self._ttl = ttl_seconds
        self._records: Dict[str, IdempotencyRecord] = {}
        self._lock = asyncio.Lock()
        self._duplicate_count: int = 0

    def compute_event_id(self, payload: dict, headers: Optional[dict] = None) -> str:
        """
        Extracts event ID from standard webhook headers, payload ID, or SHA256 content hash.
        Supports Razorpay, Stripe, and generic webhook schemas.
        """
        headers = headers or {}
        # 1. Check common gateway event headers
        for h in ("x-razorpay-event-id", "x-webhook-id", "stripe-event-id", "idempotency-key"):
            val = headers.get(h) or headers.get(h.upper()) or headers.get(h.title())
            if val:
                return str(val)

        # 2. Check payload top-level ID
        if isinstance(payload, dict):
            if "event_id" in payload:
                return str(payload["event_id"])
            if "id" in payload:
                return str(payload["id"])

        # 3. Deterministic SHA256 fallback on serialized payload
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return "hash_" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]

    async def try_acquire(self, event_id: str, vpa: str = "") -> tuple[bool, Optional[IdempotencyRecord]]:
        """
        Atomically reserve the idempotency key in a single step (Reserve-then-Process).
        
        Returns:
            (is_duplicate, record)
            - If is_duplicate is True: Event already processed or in-flight (reject duplicate).
            - If is_duplicate is False: Successfully claimed reservation with status="in_progress".
        """
        self._cleanup_expired()
        async with self._lock:
            record = self._records.get(event_id)
            if record is not None:
                record.touches_count += 1
                self._duplicate_count += 1
                logger.info(
                    "Idempotency: Duplicate webhook detected for event_id=%s (status=%s, touches=%d)",
                    event_id,
                    record.status,
                    record.touches_count,
                )
                return True, record

            # Claim reservation atomically
            new_record = IdempotencyRecord(
                event_id=event_id,
                vpa=vpa,
                status="in_progress",
                created_at=time.time(),
            )
            self._records[event_id] = new_record
            return False, new_record

    async def release_reservation(self, event_id: str):
        """Release reservation if processing failed so subsequent retries can be attempted."""
        async with self._lock:
            record = self._records.get(event_id)
            if record and record.status == "in_progress":
                del self._records[event_id]

    async def is_duplicate(self, event_id: str) -> bool:
        """Check if an event ID was already processed within the TTL window."""
        self._cleanup_expired()
        async with self._lock:
            record = self._records.get(event_id)
            if record is not None:
                record.touches_count += 1
                self._duplicate_count += 1
                logger.info(
                    "Idempotency: Duplicate webhook detected for event_id=%s (touches=%d)",
                    event_id,
                    record.touches_count,
                )
                return True
            return False

    async def get_cached_response(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve the cached output of an already processed event."""
        async with self._lock:
            record = self._records.get(event_id)
            return record.response_payload if record else None

    async def record_processed(
        self,
        event_id: str,
        vpa: str = "",
        status: str = "processed",
        response_payload: Optional[Dict[str, Any]] = None,
    ) -> IdempotencyRecord:
        """Store newly processed event in the idempotency ledger (updating in_progress reservation)."""
        async with self._lock:
            record = self._records.get(event_id)
            if record is not None:
                record.status = status
                record.response_payload = response_payload
                if vpa:
                    record.vpa = vpa
            else:
                record = IdempotencyRecord(
                    event_id=event_id,
                    vpa=vpa,
                    status=status,
                    created_at=time.time(),
                    response_payload=response_payload,
                )
                self._records[event_id] = record
            logger.info("Idempotency: Recorded event_id=%s for vpa=%s", event_id, vpa)
            return record

    def _cleanup_expired(self):
        """Remove records older than the TTL window."""
        now = time.time()
        expired = [k for k, v in self._records.items() if now - v.created_at > self._ttl]
        for k in expired:
            del self._records[k]

    def get_stats(self) -> dict:
        """Return idempotency tracking metrics."""
        return {
            "total_unique_events": len(self._records),
            "duplicates_blocked": self._duplicate_count,
            "ttl_seconds": self._ttl,
        }

    def clear(self):
        """Reset idempotency cache."""
        self._records.clear()
        self._duplicate_count = 0


class CustomerConcurrencyLock:
    """
    Per-customer async mutex manager to prevent parallel race conditions
    when multiple events arrive simultaneously for the same VPA or debtor.
    """

    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def lock_for(self, key: str) -> asyncio.Lock:
        """Get or create an async lock for a specific customer VPA or invoice ID."""
        key = str(key).strip().lower() or "global"
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    def active_locks_count(self) -> int:
        return len(self._locks)

    def clear(self):
        self._locks.clear()


# Singletons
idempotency_manager = IdempotencyManager()
customer_locks = CustomerConcurrencyLock()
