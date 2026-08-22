# 🔄 AI Revenue Recovery Agent

> Find revenue that's slipping away and win it back — with a focus on India's UPI Autopay ecosystem.

An intelligent agent that **detects revenue at risk**, **diagnoses the root cause**, and **executes the right recovery intervention** — from generic payment failures to UPI Autopay mandate failures with salary-cycle-aware retries.

---

## 🎯 What it does

Revenue loss rarely happens in one clean step. A UPI Autopay debit fails at month-end (salary crunch), a mandate gets revoked via PhonePe, or a bank times out for the 3rd time. This agent closes the loop:

```
Razorpay Webhook → Detect Risk → Diagnose (NPCI code) → Intervene → Recover
```

---

## 🚀 Features

| Feature | Description |
|---|---|
| **UPI Autopay Recovery** | 14 NPCI error codes mapped to recovery strategies |
| **Salary-cycle Retry** | Retries U30 failures on 1st–7th of next month (salary window) |
| **Mandate Renewal** | Auto-generates Razorpay magic link for revoked/expired mandates |
| **UPI Collect Request** | Sends UPI push notification to customer's VPA as fallback |
| **WhatsApp Nudge** | Contextual WhatsApp message per failure type |
| **Smart Escalation** | Escalates to support after 3 failed auto-retries |
| **Structured Logging** | IST-timestamped structlog — console (dev) or JSON (prod) |
| **Generic Recovery** | Checkout abandonment, subscription failure, invoice overdue |

---

## 🏗️ Project Structure

```
ai-revenue-recovery-agent/
├── demo.py                     # Generic pipeline demo (no API keys needed)
├── upi_demo.py                 # UPI Autopay 4-scenario demo (no API keys needed)
├── requirements.txt
├── .env.example
│
├── src/
│   ├── config.py               # App config (pydantic-settings, reads .env)
│   │
│   ├── agent/
│   │   ├── detector.py         # RiskType enum + RevenueRisk dataclass
│   │   ├── diagnoser.py        # Root cause diagnosis (UPI + generic paths)
│   │   ├── interventions.py    # Base intervention classes (generic)
│   │   ├── orchestrator.py     # detect → diagnose → intervene loop
│   │   ├── upi_detector.py     # UPI-specific risk detection from webhooks
│   │   ├── retry_scheduler.py  # Salary-cycle-aware retry scheduler (IST)
│   │   └── upi_interventions.py # 5 UPI recovery strategies
│   │
│   ├── integrations/
│   │   └── razorpay_upi.py     # Razorpay webhook parser + API wrappers
│   │
│   ├── models/
│   │   └── upi_models.py       # NPCI error codes, mandate states, event types
│   │
│   └── utils/
│       └── logger.py           # Shared structlog logger (IST + JSON/console)
│
└── tests/
    └── test_upi_recovery.py    # 34 tests — all passing
```

---

## ⚡ Quick Start

### Prerequisites
- Python 3.11+ (tested on 3.14)
- Git

### 1. Clone the repo

```bash
git clone https://github.com/OMGITCODE/AI-Revenue-Recovery.git
cd AI-Revenue-Recovery
```

### 2. Create virtual environment

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up environment (optional for demos)

```bash
cp .env.example .env
# Demos run without any API keys.
# Add RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET to .env for live mode.
```

### 5. Run the demos

```bash
# Windows — set encoding first to render ₹ and emoji correctly
set PYTHONIOENCODING=utf-8

# Generic payment recovery demo (payment failures, checkout abandonment, etc.)
python -X utf8 demo.py

# UPI Autopay failure recovery demo (4 real-world scenarios)
python -X utf8 upi_demo.py
```

### 6. Run the tests

```bash
python -m pytest tests/ -v
```

---

## 🔌 Live Razorpay Integration

To connect to real Razorpay webhooks, add to `.env`:

```env
RAZORPAY_KEY_ID=rzp_live_xxxxxxxxxxxx
RAZORPAY_KEY_SECRET=your_secret_here
RAZORPAY_WEBHOOK_SECRET=your_webhook_secret

LOG_LEVEL=INFO          # DEBUG for verbose output
LOG_FORMAT=json         # json for structured log aggregators (default: console)
```

Then parse incoming webhooks:

```python
from src.integrations.razorpay_upi import parse_upi_webhook, verify_webhook_signature
from src.agent.upi_detector import UPIAutopayDetector
from src.agent.upi_interventions import (
    SmartRetryIntervention, UPICollectIntervention,
    MandateRenewalIntervention, WhatsAppNudgeIntervention, EscalationIntervention
)

# Verify + parse
assert verify_webhook_signature(raw_body, signature, webhook_secret)
event = parse_upi_webhook(payload)

# Detect + intervene
detector = UPIAutopayDetector()
risk = await detector.detect_from_upi_event(event)

interventions = [
    SmartRetryIntervention(),
    UPICollectIntervention(),
    MandateRenewalIntervention(),
    WhatsAppNudgeIntervention(),
    EscalationIntervention(),
]
for iv in interventions:
    if iv.can_handle(risk):
        result = await iv.execute(risk)
        print(result.message)
```

---

## 🧪 Tests

```bash
python -m pytest tests/test_upi_recovery.py -v
# 34 passed in ~0.2s
```

Covers: NPCI error code properties, retry scheduler (salary window, backoff, limit reset), detector severity logic, intervention routing, and 5 end-to-end pipeline scenarios.

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.
