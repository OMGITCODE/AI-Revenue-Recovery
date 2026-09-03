"""
Test 2-Way Conversational WhatsApp Inbound Feature Locally
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx

samples = [
    ("PROMISE", "Bhai kal pakka pay kar dunga abhi salary aane wali hai", "rahul@oksbi", "+919876543210", 999.0),
    ("ALREADY_PAID", "Mera account se paise kat gaye hain check your bank statement", "priya@okhdfcbank", "+919811122233", 499.0),
    ("DISPUTE", "Maine ye service cancel kar di thi fraud mat karo refund chahiye", "vikram@ybl", "+919822233344", 1500.0),
    ("HARDSHIP", "Meri job chali gayi hai aur hospital emergency hai abhi paise nahi hain", "anita@paytm", "+919833344455", 299.0),
    ("WRONG_NUMBER", "Galat number hai bhai stop messaging me not my account", "stranger@upi", "+919999999999", 100.0)
]

print("=" * 80)
print("📱 Testing 2-Way Conversational WhatsApp Inbound Feature Locally")
print("=" * 80)

# Check if live server is running, otherwise fall back to in-process TestClient
use_test_client = False
try:
    with httpx.Client(timeout=1.5) as probe:
        res = probe.get("http://localhost:8000/api/health")
        if res.status_code != 200:
            use_test_client = True
except Exception:
    use_test_client = True

if use_test_client:
    print("  [Mode: In-Process FastAPI TestClient (no running server needed)]")
    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)
    def dispatch_post(url, **kwargs):
        endpoint = url.replace("http://localhost:8000", "")
        return client.post(endpoint, **kwargs)
else:
    print("  [Mode: Live HTTP against http://localhost:8000]")
    def dispatch_post(url, **kwargs):
        return httpx.post(url, **kwargs)

for expected, msg, vpa, phone, amount in samples:
    try:
        r = dispatch_post("http://localhost:8000/api/webhook/whatsapp/inbound", json={
            "from_phone": phone,
            "customer_vpa": vpa,
            "message": msg,
            "amount": amount
        }, timeout=10.0)
        data = r.json()
        print(f"\n💬 [Incoming Message]: \"{msg}\"")
        print(f"   👤 Sender: {vpa} ({phone}) | Amount: ₹{amount}")
        print(f"   🎯 Intent: {data['intent'].upper()} (Confidence: {data['confidence']:.0%})")
        print(f"   🔍 Reasoning: {data['reasoning']}")
        print(f"   ⚡ Action Taken: {data['action_taken']}")
        print(f"   🤖 Hinglish AI Response: \"{data['reply_text']}\"")
        print("-" * 80)
    except Exception as e:
        print(f"Error testing '{msg}': {e}")
