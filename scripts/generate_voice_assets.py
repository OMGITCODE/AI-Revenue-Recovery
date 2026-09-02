"""
Voice Asset Generator for RecoverIQ
====================================
Synthesizes broadcast-quality Indian Neural MP3 audio clips for RecoverIQ's
Outbound Voice AI Channel Preview using Microsoft Edge Neural TTS.

Zero API keys required. Run once as a build step; audio files are committed
directly to `assets/audio/` so that demo day has ZERO runtime network dependency.

Scenarios:
1. StartupXYZ (₹12,500 · Tier C 45d overdue)
   - Hinglish: hi-IN-MadhurNeural
   - Indian English: en-IN-NeerjaNeural
2. Mega Retail (₹84,200 · Tier C Late 75d overdue · MSMED Act Section 16)
   - Hinglish: hi-IN-MadhurNeural
   - Indian English: en-IN-PrabhatNeural
3. Rahul Sharma (₹999 · Cart Drop-off Recovery)
   - Hinglish: hi-IN-SwaraNeural
   - Indian English: en-IN-NeerjaNeural
4. Telecom Ringback Chime (400Hz + 425Hz Indian standard tone)
"""

import asyncio
import math
import struct
import wave
from pathlib import Path
import edge_tts

ROOT = Path(__file__).resolve().parent.parent
AUDIO_DIR = ROOT / "assets" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ── Voice Dialogue Scripts (Statutorily Accurate MSMED Section 16) ─────────────

SCRIPTS = {
    # Scenario 1: StartupXYZ (Tier C Early Overdue, ₹12,500)
    "b2b_early_hinglish": {
        "text": (
            "Namaste Rohan ji. Yeh RecoverIQ automated system se ek zaroori update hai. "
            "Aapka invoice INV-2026-003, amount baarah hazaar paanch sau rupaye, abhi pending hai. "
            "Kripya aaj hi payment complete karein taaki services uninterrupted rahein. "
            "Direct payment link aapke registered WhatsApp aur email par bhej diya gaya hai. "
            "Shukriya aur aapka din shubh ho."
        ),
        "voice": "hi-IN-MadhurNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
    },
    "b2b_early_english": {
        "text": (
            "Hello Rohan. This is an automated reminder from RecoverIQ on behalf of your vendor. "
            "Your invoice INV-2026-003 for rupees twelve thousand five hundred is currently overdue. "
            "Please clear this pending balance today to maintain uninterrupted software access. "
            "A secure payment link has been dispatched to your registered WhatsApp and email. "
            "Thank you and have a productive day."
        ),
        "voice": "en-IN-NeerjaNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
    },

    # Scenario 2: Mega Retail (Tier C Late Overdue, ₹84,200 · MSMED Section 16)
    "b2b_late_hinglish": {
        "text": (
            "Namaste Amit ji. Yeh RecoverIQ se Mega Retail ke pending invoice INV-2026-004 ke regarding ek zaroori alert hai. "
            "Aapka amount chaurasi hazaar do sau rupaye ab pachhattar din overdue ho chuka hai. "
            "MSMED Act Section 16 ke tahet, RBI bank rate ke teen guna monthly compounding interest accrue ho raha hai. "
            "Formal legal notice dispatch hone se pehle kripya aaj hi payment complete karein. "
            "Payment link aapke WhatsApp par available hai. Shukriya."
        ),
        "voice": "hi-IN-MadhurNeural",
        "rate": "-3%",
        "pitch": "-2Hz",
    },
    "b2b_late_english": {
        "text": (
            "Hello Amit. This is an urgent notice from RecoverIQ regarding Mega Retail's pending invoice INV-2026-004. "
            "Your balance of rupees eighty-four thousand two hundred is now seventy-five days overdue. "
            "Under Section 16 of the MSMED Act, statutory penal interest is accruing at three times the RBI bank rate, compounded monthly. "
            "To avoid formal escalation and recovery proceedings, please clear this invoice today via the secure WhatsApp payment link. "
            "Thank you."
        ),
        "voice": "en-IN-PrabhatNeural",
        "rate": "-3%",
        "pitch": "-2Hz",
    },

    # Scenario 3: Rahul Sharma (Cart Recovery, ₹999)
    "cart_recovery_hinglish": {
        "text": (
            "Namaste Rahul ji! RecoverIQ checkout assistant se call hai. "
            "Aapka annual subscription plan lagbhag complete ho gaya tha, par payment complete nahi ho payi. "
            "Humne aapke liye ek special instant discount link WhatsApp par bheja hai. "
            "Bas ek tap mein UPI se payment karke apna subscription turant activate karein. "
            "Shukriya!"
        ),
        "voice": "hi-IN-SwaraNeural",
        "rate": "+2%",
        "pitch": "+1Hz",
    },
    "cart_recovery_english": {
        "text": (
            "Hi Rahul! This is RecoverIQ checkout assistant following up on your pending subscription order. "
            "We noticed your payment of nine hundred and ninety-nine rupees was interrupted before completion. "
            "We have sent an instant 1-tap UPI payment link with an exclusive revival discount directly to your WhatsApp. "
            "Simply tap the link to complete your setup in seconds. "
            "Thank you!"
        ),
        "voice": "en-IN-NeerjaNeural",
        "rate": "+2%",
        "pitch": "+1Hz",
    },
}

def generate_telecom_ringback(filename: Path):
    """
    Synthesizes Indian standard dual-frequency ringback chime (400Hz + 425Hz)
    Duration: 1.5 seconds of clean tone followed by brief fadeout.
    Saved as standard audio file.
    """
    sample_rate = 44100
    duration = 1.5
    num_samples = int(sample_rate * duration)
    wav_path = filename.with_suffix(".wav")

    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)       # Mono
        wav_file.setsampwidth(2)      # 16-bit
        wav_file.setframerate(sample_rate)

        for i in range(num_samples):
            t = i / sample_rate
            # 400Hz + 425Hz telecom standard dual tone
            val1 = math.sin(2 * math.pi * 400 * t)
            val2 = math.sin(2 * math.pi * 425 * t)
            combined = 0.5 * (val1 + val2)

            # Fade in first 50ms, fade out last 100ms
            if t < 0.05:
                envelope = t / 0.05
            elif t > (duration - 0.1):
                envelope = (duration - t) / 0.1
            else:
                envelope = 1.0

            sample = int(combined * envelope * 20000)
            sample = max(-32767, min(32767, sample))
            wav_file.writeframes(struct.pack("<h", sample))

    # Also make telecom_ringback.mp3 (simple header copy or direct wav serve)
    # If pydub/ffmpeg is not available, we can write a clean minimal MP3 or keep wav alongside
    # Let's synthesize an actual MP3 via edge-tts for a short chime phrase or write mp3 bytes
    print(f"  [OK] Generated {wav_path.name} ({wav_path.stat().st_size} bytes)")
    return wav_path


async def synthesize_all():
    print("=" * 72)
    print("RecoverIQ Voice Asset Generator (Microsoft Edge Neural TTS)")
    print("=" * 72)

    for key, spec in SCRIPTS.items():
        out_file = AUDIO_DIR / f"{key}.mp3"
        print(f"Synthesizing '{key}' with voice '{spec['voice']}'...")
        comm = edge_tts.Communicate(
            text=spec["text"],
            voice=spec["voice"],
            rate=spec["rate"],
            pitch=spec["pitch"],
        )
        await comm.save(str(out_file))
        size_kb = out_file.stat().st_size / 1024
        print(f"  [OK] Saved {out_file.name} ({size_kb:.1f} KB)")

    # Telecom sound effect: Generate ringback
    print("Generating telecom ringback chime...")
    ringback_wav = AUDIO_DIR / "telecom_ringback.wav"
    generate_telecom_ringback(ringback_wav)

    # Also synthesize an MP3 audio chime using edge-tts whisper/tone
    ringback_mp3 = AUDIO_DIR / "telecom_ringback.mp3"
    comm = edge_tts.Communicate(
        text="Tring tring. Tring tring.",
        voice="hi-IN-MadhurNeural",
        rate="+50%",
        pitch="+15Hz",
    )
    await comm.save(str(ringback_mp3))
    print(f"  [OK] Saved {ringback_mp3.name} ({ringback_mp3.stat().st_size / 1024:.1f} KB)")

    print("=" * 72)
    print(f"All 7 voice assets successfully generated in: {AUDIO_DIR}")
    print("=" * 72)

if __name__ == "__main__":
    asyncio.run(synthesize_all())
