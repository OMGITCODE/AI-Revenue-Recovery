"""
batch_run.py — Batch Dataset Runner for RecoverIQ
==================================================
Reads data/upi_failures_dataset.json and fires every scenario
through the /api/custom endpoint with a small delay between each.

Usage:
    python data/batch_run.py                  # run all 40 events
    python data/batch_run.py --delay 0.5      # slower (0.5s between events)
    python data/batch_run.py --limit 10       # only first 10 events
    python data/batch_run.py --code U30       # only events with failure_code U30
    python data/batch_run.py --reset          # clear dashboard first, then run all
"""

import argparse
import json
import time
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("[!] 'requests' not found. Installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

BASE_URL = "http://127.0.0.1:8000"
DATASET  = Path(__file__).parent / "upi_failures_dataset.json"


def fmt_inr(amount):
    return f"₹{amount:,.0f}"


def run_all(scenarios, delay: float = 0.3):
    passed = failed = 0
    total = len(scenarios)

    print(f"\n{'='*60}")
    print(f"  RecoverIQ — Batch Runner  ({total} scenarios)")
    print(f"{'='*60}\n")

    for i, sc in enumerate(scenarios, 1):
        name   = sc.get("scenario_name", "Unnamed")
        code   = sc.get("failure_code", "?")
        bank   = sc.get("bank", "?")
        amount = sc.get("amount", 0)

        print(f"[{i:02d}/{total}] {name}")
        print(f"        Code={code}  Bank={bank}  Amount={fmt_inr(amount)}")

        try:
            r = requests.post(f"{BASE_URL}/api/custom", json=sc, timeout=10)
            if r.status_code == 200:
                ev = r.json()
                ivs = ", ".join(ev.get("interventions", [])) or "none"
                sev = ev.get("severity", "?")
                print(f"        ✓ Processed  severity={sev}  interventions=[{ivs}]")
                passed += 1
            else:
                detail = r.json().get("detail", r.text)
                print(f"        ✗ Error {r.status_code}: {detail}")
                failed += 1
        except requests.exceptions.ConnectionError:
            print(f"        ✗ Cannot connect to {BASE_URL}. Is the server running?")
            print("          Run: python -m uvicorn api.main:app --port 8000")
            sys.exit(1)
        except Exception as e:
            print(f"        ✗ Exception: {e}")
            failed += 1

        if i < total:
            time.sleep(delay)

    print(f"\n{'='*60}")
    print(f"  Done — {passed} passed  {failed} failed  out of {total}")
    print(f"  Dashboard: {BASE_URL}/")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="RecoverIQ batch runner")
    parser.add_argument("--delay",  type=float, default=0.3,  help="Seconds between events (default 0.3)")
    parser.add_argument("--limit",  type=int,   default=None, help="Only run the first N events")
    parser.add_argument("--code",   type=str,   default=None, help="Filter by failure_code (e.g. U30)")
    parser.add_argument("--reset",  action="store_true",      help="Clear dashboard before running")
    args = parser.parse_args()

    # Load dataset
    if not DATASET.exists():
        print(f"[!] Dataset not found at {DATASET}")
        sys.exit(1)

    with open(DATASET, encoding="utf-8") as f:
        scenarios = json.load(f)

    # Optional filters
    if args.code:
        scenarios = [s for s in scenarios if s.get("failure_code", "").upper() == args.code.upper()]
        print(f"[i] Filtered to {len(scenarios)} events with code={args.code.upper()}")

    if args.limit:
        scenarios = scenarios[:args.limit]
        print(f"[i] Limited to first {len(scenarios)} events")

    # Optional reset
    if args.reset:
        try:
            r = requests.post(f"{BASE_URL}/api/reset", timeout=5)
            if r.status_code == 200:
                print("[i] Dashboard cleared.")
            else:
                print(f"[!] Reset failed: {r.status_code}")
        except Exception as e:
            print(f"[!] Reset error: {e}")

    run_all(scenarios, delay=args.delay)


if __name__ == "__main__":
    main()
