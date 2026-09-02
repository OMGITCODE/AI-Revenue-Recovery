"""
test_voice_ai.py — Outbound Voice AI Outreach & IVR Integration Tests
=====================================================================
Validates:
1. GET /api/voice/scenarios: Public exact path, returns 3 scenarios (StartupXYZ, Mega Retail, Rahul Sharma).
2. Dual-dialect synthesis metadata: Hinglish & Indian English with valid subtitle cue timestamps.
3. Physical audio file existence: All referenced MP3s exist on disk and are non-empty.
4. Security middleware enforcement: POST /api/voice/call/{receivable_id} is strictly protected
   when RECOVERIQ_API_KEY is configured, and open in default zero-key development mode.
5. Ledger unit cost accounting: Correctly logs channel="ivr" and deducts ₹1.50 unit cost.
6. Event store streaming: Appends RecoveryEvent to in-memory store for real-time SSE.
7. MessagingClient.send_voice_call: Validates mock mode execution and graceful live fallback.
"""

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from api.main import app, ROOT
from api.store import store
from src.config import settings
from src.agent.recovery_ledger import ledger as recovery_ledger
from src.integrations.messaging import MessagingClient, MessageResult, messenger

client = TestClient(app)


class TestVoiceScenariosCatalog:
    def test_get_voice_scenarios_public_access(self):
        """Public exact path: must return 200 without requiring API key."""
        res = client.get("/api/voice/scenarios")
        assert res.status_code == 200
        data = res.json()
        assert "scenarios" in data
        assert len(data["scenarios"]) == 3

        ids = [s["id"] for s in data["scenarios"]]
        assert "startup_xyz" in ids
        assert "mega_retail" in ids
        assert "cart_rahul" in ids

    def test_dialects_and_cue_timestamps(self):
        """Verifies dual dialects and strictly increasing subtitle cue timestamps."""
        res = client.get("/api/voice/scenarios")
        scenarios = res.json()["scenarios"]

        for s in scenarios:
            assert "dialects" in s
            assert "hinglish" in s["dialects"]
            assert "english" in s["dialects"]

            for dialect_key in ("hinglish", "english"):
                d = s["dialects"][dialect_key]
                assert "audio_url" in d
                assert "voice" in d
                assert "duration_sec" in d
                assert d["duration_sec"] > 0
                assert len(d["cues"]) >= 3

                last_end = 0.0
                for cue in d["cues"]:
                    assert "start" in cue and "end" in cue and "text" in cue
                    assert cue["start"] >= 0.0
                    assert cue["end"] > cue["start"]
                    assert len(cue["text"].strip()) > 0
                    assert cue["start"] >= last_end - 0.2  # Monotonic with tiny tolerance
                    last_end = cue["end"]

    def test_audio_files_exist_on_disk(self):
        """Verifies all referenced MP3 files exist in assets/audio/ and are non-empty."""
        res = client.get("/api/voice/scenarios")
        scenarios = res.json()["scenarios"]
        assets_dir = ROOT / "assets" / "audio"

        assert assets_dir.exists()

        # Telecom ringback tone must exist
        ringback_file = assets_dir / "telecom_ringback.mp3"
        assert ringback_file.exists()
        assert ringback_file.stat().st_size > 5000

        for s in scenarios:
            for dialect_key in ("hinglish", "english"):
                url = s["dialects"][dialect_key]["audio_url"]
                filename = Path(url).name
                audio_path = assets_dir / filename
                assert audio_path.exists(), f"Missing audio asset: {audio_path}"
                assert audio_path.stat().st_size > 10000, f"Audio asset is too small: {audio_path}"


class TestVoiceCallSecurityAndAuth:
    def test_voice_call_open_in_development_mode(self):
        """When RECOVERIQ_API_KEY is empty, calls succeed for zero-friction evaluation."""
        orig = settings.recoveriq_api_key
        try:
            settings.recoveriq_api_key = ""
            res = client.post("/api/voice/call/INV-2026-003")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "connected"
            assert data["channel"] == "ivr"
            assert data["channel_cost"] == 1.50
        finally:
            settings.recoveriq_api_key = orig

    def test_voice_call_strictly_protected_when_key_configured(self):
        """When RECOVERIQ_API_KEY is configured, requests without X-API-Key are rejected with 401."""
        orig = settings.recoveriq_api_key
        try:
            settings.recoveriq_api_key = "rec_sec_test_998877"

            # 1. Without header -> 401 Unauthorized
            res_unauth = client.post("/api/voice/call/INV-2026-003")
            assert res_unauth.status_code == 401
            assert "Unauthorized" in res_unauth.json()["detail"]

            # 2. With incorrect header -> 401 Unauthorized
            res_bad = client.post("/api/voice/call/INV-2026-003", headers={"X-API-Key": "wrong_key"})
            assert res_bad.status_code == 401

            # 3. With valid header -> 200 OK
            res_auth = client.post(
                "/api/voice/call/INV-2026-003",
                headers={"X-API-Key": "rec_sec_test_998877"},
            )
            assert res_auth.status_code == 200
            assert res_auth.json()["status"] == "connected"
        finally:
            settings.recoveriq_api_key = orig


class TestVoiceCallLedgerAndEventStore:
    def test_voice_call_logs_to_ledger_and_store(self):
        """Verifies ₹1.50 IVR cost deduction in recovery ledger and store event dispatch."""
        recovery_ledger.reset()
        initial_cost = recovery_ledger.overall_roi()["total_cost"]

        payload = {
            "debtor_name": "StartupXYZ (Rohan Sharma)",
            "amount": 12500.0,
            "vpa": "startup@okaxis",
            "notes": "Hinglish IVR call dispatched",
        }
        res = client.post("/api/voice/call/INV-2026-003", json=payload)
        assert res.status_code == 200
        data = res.json()

        assert data["channel"] == "ivr"
        assert data["channel_cost"] == 1.50
        assert "ledger_id" in data
        assert data["ledger_id"] is not None

        # Ledger total cost must increase by exactly ₹1.50
        new_cost = recovery_ledger.overall_roi()["total_cost"]
        assert round(new_cost - initial_cost, 2) == 1.50

        # Event store must have received the event
        events = store.get_events(limit=10)
        matching_events = [e for e in events if e.get("failure_code") == "IVR_CHASE"]
        assert len(matching_events) >= 1
        ev = matching_events[0]
        assert ev["customer_vpa"] == "startup@okaxis"
        assert ev["amount"] == 12500.0
        assert "ivr_outreach" in ev["interventions"]


class TestMessagingClientVoiceCall:
    def test_send_voice_call_mock_mode(self):
        """MessagingClient in mock mode logs and returns safe MessageResult."""
        m = MessagingClient(force_mock=True)
        res = m.send_voice_call(to="+919800000003", script="Namaste Rohan ji. Yeh RecoverIQ se call hai.")

        assert isinstance(res, MessageResult)
        assert res.channel == "ivr"
        assert res.to == "+919800000003"
        assert "Namaste" in res.body
        assert res.sent is False
        assert res.mode == "mock"
        assert res.error is None

    def test_send_voice_call_live_fallback(self):
        """Live mode with broken client falls back gracefully without raising exceptions."""
        m = MessagingClient(force_mock=False)
        m._client = object()  # Dummy object lacking .calls
        m.sms_from = "+14155238886"

        res = m.send_voice_call(to="+919800000003", script="Test call")
        assert res.channel == "ivr"
        assert res.sent is False
        assert res.mode == "mock"
        assert res.error is not None
