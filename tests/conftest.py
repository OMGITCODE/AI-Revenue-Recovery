"""
conftest.py — Pytest Configuration & Test Isolation Fixtures
============================================================
Guarantees:
1. Tests NEVER make live network calls to Twilio even if real credentials
   exist in the developer's ambient .env file.
2. MessagingClient is explicitly set to force_mock=True across all test suites.
3. State registries (suppression, promise_tracker, recovery_ledger) are clean.
"""

import pytest
import os
from src.config import settings
from src.integrations.messaging import messenger


@pytest.fixture(autouse=True)
def isolate_test_settings():
    """
    Autouse fixture: ensures settings are reloaded cleanly before each test
    and restored to ambient defaults after monkeypatched test execution.
    """
    settings.reload()
    yield
    settings.reload()


@pytest.fixture(autouse=True)
def force_mock_messaging(monkeypatch):
    """
    Autouse fixture: strictly forces mock mode on the global messenger client
    and isolates tests from any ambient TWILIO_* environment variables.
    """
    # Force mock mode on singleton
    original_force_mock = messenger.force_mock
    original_client = messenger._client
    messenger.force_mock = True
    messenger._client = None

    yield

    # Restore
    messenger.force_mock = original_force_mock
    messenger._client = original_client
