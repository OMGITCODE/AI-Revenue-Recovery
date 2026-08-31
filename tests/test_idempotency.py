"""
tests/test_idempotency.py — Idempotency & Concurrency Lock Test Suite
====================================================================
Tests:
  1. Event ID computation from headers, payload ID, and SHA256 fallback.
  2. Duplicate webhook detection within TTL window.
  3. Cached response return on duplicate submissions.
  4. Per-customer async mutex concurrency serialization.
  5. API endpoints /api/webhook and /api/idempotency.
"""

import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from src.agent.idempotency import IdempotencyManager, CustomerConcurrencyLock, idempotency_manager, customer_locks
from api.main import app


class TestIdempotencyManager:
    """Unit tests for the IdempotencyManager class."""

    @pytest.mark.asyncio
    async def test_compute_event_id_from_header(self):
        mgr = IdempotencyManager()
        headers = {"x-razorpay-event-id": "evt_test_123"}
        payload = {"dummy": "data"}
        event_id = mgr.compute_event_id(payload, headers)
        assert event_id == "evt_test_123"

    @pytest.mark.asyncio
    async def test_compute_event_id_from_payload(self):
        mgr = IdempotencyManager()
        payload = {"event_id": "evt_payload_456", "amount": 999}
        event_id = mgr.compute_event_id(payload, {})
        assert event_id == "evt_payload_456"

    @pytest.mark.asyncio
    async def test_compute_event_id_hash_fallback(self):
        mgr = IdempotencyManager()
        payload = {"customer": "test@upi", "amount": 500}
        event_id = mgr.compute_event_id(payload, {})
        assert event_id.startswith("hash_")

    @pytest.mark.asyncio
    async def test_duplicate_rejection_and_stats(self):
        mgr = IdempotencyManager(ttl_seconds=3600)
        event_id = "evt_order_789"

        # 1st call: not duplicate
        assert not await mgr.is_duplicate(event_id)

        # Record it as processed
        await mgr.record_processed(event_id=event_id, vpa="user@upi", status="processed", response_payload={"success": True})

        # 2nd call: duplicate detected
        assert await mgr.is_duplicate(event_id)

        # Cached response retrieval
        cached = await mgr.get_cached_response(event_id)
        assert cached == {"success": True}

        # Stats check
        stats = mgr.get_stats()
        assert stats["total_unique_events"] == 1
        assert stats["duplicates_blocked"] == 1


class TestConcurrencyLocks:
    """Unit tests for CustomerConcurrencyLock manager."""

    @pytest.mark.asyncio
    async def test_per_customer_lock_serialization(self):
        locks = CustomerConcurrencyLock()
        execution_order = []

        async def worker(customer_vpa: str, worker_id: int, delay: float):
            lock = await locks.lock_for(customer_vpa)
            async with lock:
                execution_order.append(f"start_{worker_id}")
                await asyncio.sleep(delay)
                execution_order.append(f"end_{worker_id}")

        # Launch two workers for the same customer concurrently
        await asyncio.gather(
            worker("user@oksbi", 1, 0.05),
            worker("user@oksbi", 2, 0.01),
        )

        # Worker 2 must wait for Worker 1 to finish before starting
        assert execution_order == ["start_1", "end_1", "start_2", "end_2"]
        assert locks.active_locks_count() == 1


class TestWebhookIdempotencyAPI:
    """Integration test verifying /api/webhook idempotency over HTTP."""

    @pytest.mark.asyncio
    async def test_webhook_duplicate_post_rejected(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Reset state
            await client.post("/api/reset")

            sample_webhook = {
                "entity": "event",
                "account_id": "acc_test",
                "event": "payment.failed",
                "contains": ["payment"],
                "payload": {
                    "payment": {
                        "entity": {
                            "id": "pay_test_idempotent_001",
                            "amount": 99900,
                            "currency": "INR",
                            "status": "failed",
                            "method": "upi",
                            "vpa": "idempotent_user@oksbi",
                            "error_code": "BAD_REQUEST_ERROR",
                            "error_description": "Payment failed due to insufficient funds",
                            "error_reason": "payment_failed",
                            "notes": {"failure_code": "U30", "bank": "SBI"},
                        }
                    }
                }
            }

            headers = {"X-Razorpay-Event-Id": "evt_idempotency_test_001"}

            # First Webhook POST -> Processed normally (200 OK)
            res1 = await client.post("/api/webhook", json=sample_webhook, headers=headers)
            assert res1.status_code == 200
            data1 = res1.json()
            assert data1.get("customer_vpa") == "idempotent_user@oksbi"

            # Second Webhook POST with same Event ID -> Duplicate Ignored (200 OK with duplicate_ignored flag)
            res2 = await client.post("/api/webhook", json=sample_webhook, headers=headers)
            assert res2.status_code == 200
            data2 = res2.json()
            assert data2.get("status") == "duplicate_ignored"
            assert data2.get("event_id") == "evt_idempotency_test_001"

            # Verify idempotency metrics endpoint
            stats_res = await client.get("/api/idempotency")
            assert stats_res.status_code == 200
            stats = stats_res.json()
            assert stats["total_unique_events"] >= 1
            assert stats["duplicates_blocked"] >= 1

    @pytest.mark.asyncio
    async def test_concurrent_webhook_duplicate_race_safety(self):
        """Stress-test concurrent identical webhooks fired simultaneously in parallel."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/reset")

            payload = {
                "entity": "event",
                "event": "payment.failed",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": "pay_concurrent_001",
                            "amount": 149900,
                            "vpa": "parallel_user@okhdfc",
                            "notes": {"failure_code": "U30", "bank": "HDFC"},
                        }
                    }
                }
            }
            headers = {"X-Razorpay-Event-Id": "evt_concurrent_race_001"}

            # Fire 5 identical requests concurrently
            responses = await asyncio.gather(*[
                client.post("/api/webhook", json=payload, headers=headers)
                for _ in range(5)
            ])

            statuses = [r.status_code for r in responses]
            assert all(s == 200 for s in statuses)

            data = [r.json() for r in responses]
            # Exactly ONE request should be the main processed execution
            processed = [d for d in data if d.get("status") != "duplicate_ignored"]
            duplicates = [d for d in data if d.get("status") == "duplicate_ignored"]

            assert len(processed) == 1
            assert len(duplicates) == 4


class TestModuleDeduplication:
    """Unit tests verifying duplicate protections across stateful modules."""

    @pytest.mark.asyncio
    async def test_event_store_deduplication(self):
        from api.store import EventStore, RecoveryEvent
        store = EventStore()
        ev1 = RecoveryEvent(
            id="EVT-DUP-001",
            timestamp="12:00:00",
            event_type="mandate.execution.failed",
            failure_code="U30",
            failure_reason="Insufficient funds",
            customer_id="CUST-001",
            customer_vpa="user@oksbi",
            bank="SBI",
            amount=999.0,
            severity="high",
            interventions=["smart_retry"],
            intervention_msgs=["Scheduled"],
            scheduled_at=None,
            action_url=None,
            success=True,
        )
        # Add once
        await store.add_event(ev1)
        assert len(store.get_events()) == 1
        assert store.get_stats()["total_events"] == 1

        # Add duplicate event with same ID
        ev1_updated = RecoveryEvent(
            id="EVT-DUP-001",
            timestamp="12:00:01",
            event_type="mandate.execution.failed",
            failure_code="U30",
            failure_reason="Insufficient funds",
            customer_id="CUST-001",
            customer_vpa="user@oksbi",
            bank="SBI",
            amount=999.0,
            severity="high",
            interventions=["smart_retry"],
            intervention_msgs=["Updated message"],
            scheduled_at=None,
            action_url=None,
            success=True,
        )
        await store.add_event(ev1_updated)
        # Length and total_events should NOT duplicate
        assert len(store.get_events()) == 1
        assert store.get_stats()["total_events"] == 1
        assert store.get_events()[0]["intervention_msgs"] == ["Updated message"]

    def test_recovery_ledger_debounce_deduplication(self):
        from src.agent.recovery_ledger import RecoveryLedger
        ledger = RecoveryLedger()
        e1 = ledger.log(
            event_type="detect",
            vpa="user@oksbi",
            amount=999.0,
            reasoning="U30 detected on SBI",
            confidence=0.85,
        )
        e2 = ledger.log(
            event_type="detect",
            vpa="user@oksbi",
            amount=999.0,
            reasoning="U30 detected on SBI",
            confidence=0.85,
        )
        # Rapid duplicate call must return the exact same ledger entry
        assert e1.ledger_id == e2.ledger_id
        assert len(ledger.all_entries()) == 1

    def test_promise_tracker_deduplication(self):
        from src.agent.promise_tracker import PromiseToPayTracker
        tracker = PromiseToPayTracker()
        p1 = tracker.create("rahul@oksbi", 999.0, "SBI", "U30", deadline_hours=24)
        p2 = tracker.create("rahul@oksbi", 999.0, "SBI", "U30", deadline_hours=48)
        assert p1.promise_id == p2.promise_id
        assert len(tracker.all_promises()) == 1

    def test_checkout_recovery_deduplication(self):
        from src.agent.checkout_recovery import CheckoutRecoveryAgent
        agent = CheckoutRecoveryAgent()
        s1 = agent.record_drop_off("user@okaxis", "+91-9999999999", 1499.0, "Merchant A", "payment_page_exit")
        s2 = agent.record_drop_off("user@okaxis", "+91-9999999999", 1499.0, "Merchant A", "payment_page_exit")
        assert s1.session_id == s2.session_id
        assert len(agent.all_sessions()) == 1

    def test_b2b_chaser_deduplication(self):
        from src.agent.b2b_chaser import B2BChaser
        chaser = B2BChaser()
        r1 = chaser.add_receivable("Debtor Inc", "debtor@okicici", "+91-9800000001", "INV-DUP-01", 50000.0, "2026-07-01")
        r2 = chaser.add_receivable("Debtor Inc", "debtor@okicici", "+91-9800000001", "INV-DUP-01", 50000.0, "2026-07-01")
        assert r1.receivable_id == r2.receivable_id
        assert len(chaser.all_receivables()) == 1

        # Chase throttling within 60s
        act1 = chaser.chase(r1.receivable_id)
        act2 = chaser.chase(r1.receivable_id)
        assert act1.action_id == act2.action_id
        assert len(r1.actions) == 1
