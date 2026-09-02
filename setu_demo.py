"""
Setu Account Aggregator (AA) Simulator — Local CLI Demo
=========================================================
Demonstrates RBI-compliant digital consent and verified balance check
across real-world failure scenarios from upi_failures_dataset.json.

Usage:
    python setu_demo.py
    python setu_demo.py --vpa rahul@oksbi --amount 999 --code U30
"""

import sys
import argparse
from pathlib import Path

# Ensure project root is in sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.integrations.setu_aa import setu_aa

SEP = "─" * 72
DSEP = "═" * 72

DATASET_PERSONAS = [
    {
        "title": "Scenario 1: Rahul Sharma — Insufficient Funds (U30)",
        "vpa": "rahul@oksbi",
        "bank": "SBI",
        "amount": 999.0,
        "failure_code": "U30",
        "context": "Classic month-end salary crunch. Traditional aggregators retry blindly."
    },
    {
        "title": "Scenario 2: Priya Mehta — Mandate Revoked (BT01)",
        "vpa": "priya@okhdfcbank",
        "bank": "HDFC",
        "amount": 1499.0,
        "failure_code": "BT01",
        "context": "Customer cancelled mandate. Verify balance to confirm intent-driven churn."
    },
    {
        "title": "Scenario 3: Arjun Nair — Technical Timeout (TM)",
        "vpa": "arjun@okicici",
        "bank": "ICICI",
        "amount": 4500.0,
        "failure_code": "TM",
        "context": "Bank switch timeout. Verify funds to confirm transient technical failure."
    },
    {
        "title": "Scenario 4: Kavita Joshi — Mandate Cap Exceeded (U29)",
        "vpa": "kavita@okkotak",
        "bank": "Kotak Mahindra Bank",
        "amount": 3499.0,
        "failure_code": "U29",
        "context": "Payment exceeded recurring limit. Verify liquidity before limit uplift nudge."
    },
    {
        "title": "Scenario 5: Vikram Patel — Expired Mandate (BT02)",
        "vpa": "vikram@ybl",
        "bank": "Yes Bank",
        "amount": 2999.0,
        "failure_code": "BT02",
        "context": "Mandate reached validity end date. Verify liquidity for renewal link."
    },
]


def run_single(vpa: str, amount: float, bank: str, failure_code: str, title: str = "", context: str = ""):
    if title:
        print(f"\n{SEP}")
        print(f"  📌  {title}")
        if context:
            print(f"      {context}")
        print(SEP)

    print(f"  Step 1: Requesting 1-tap RBI Digital Consent...")
    consent = setu_aa.request_consent(vpa=vpa, purpose="Recurring payment recovery balance check")
    print(f"          ✓ Consent Session Created : {consent.consent_id}")
    print(f"          ✓ Digital Consent URL     : {consent.consent_url}")
    print(f"          ✓ Consent Status          : {consent.status.upper()} (Sandbox Auto-Approved)")

    print(f"\n  Step 2: Fetching Verified Balance via Setu AA Bridge...")
    result = setu_aa.fetch_balance(
        consent=consent,
        amount_due=amount,
        bank=bank,
        failure_code=failure_code,
    )
    print(f"          ✓ Verified Account Balance: ₹{result.balance:,.2f}")
    print(f"          ✓ Amount Due for Debit    : ₹{result.amount_due:,.2f}")

    status_badge = "✅ FUNDS AVAILABLE" if result.funds_available else "⚠️  INSUFFICIENT FUNDS"
    print(f"          ✓ Liquidity Signal        : {status_badge}")

    print(f"\n  Step 3: RecoverIQ AI Decision Engine Action...")
    print(f"          🤖 {result.note}")


def main():
    parser = argparse.ArgumentParser(description="Run Setu Account Aggregator Simulator locally")
    parser.add_argument("--vpa", type=str, default=None, help="Customer UPI VPA (e.g. rahul@oksbi)")
    parser.add_argument("--amount", type=float, default=None, help="Amount due in INR")
    parser.add_argument("--bank", type=str, default="SBI", help="Customer bank name")
    parser.add_argument("--code", type=str, default="U30", help="Failure code (U30, TM, BT01, etc.)")

    args = parser.parse_args()

    print(f"\n{DSEP}")
    print("   🏦  RecoverIQ — Setu Account Aggregator (AA) Simulator (Local)")
    print("   Consent-Native Balance Verification for UPI Autopay Recovery")
    print(f"{DSEP}")

    if args.vpa and args.amount:
        run_single(
            vpa=args.vpa,
            amount=args.amount,
            bank=args.bank,
            failure_code=args.code,
            title=f"Custom Run: {args.vpa} (₹{args.amount:,.2f})",
        )
    else:
        print(f"  Running 5 canonical failure scenarios from upi_failures_dataset.json:\n")
        for sc in DATASET_PERSONAS:
            run_single(
                vpa=sc["vpa"],
                amount=sc["amount"],
                bank=sc["bank"],
                failure_code=sc["failure_code"],
                title=sc["title"],
                context=sc["context"],
            )

    print(f"\n{DSEP}")
    print("  ✨  All scenarios verified through Setu AA sandbox.")
    print("      Dashboard UI is also live at: http://127.0.0.1:8000/")
    print(f"{DSEP}\n")


if __name__ == "__main__":
    main()
