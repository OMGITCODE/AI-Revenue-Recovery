# 🔄 AI Revenue Recovery Agent

> Find revenue that's slipping away and win it back.

An intelligent agent that **detects revenue at risk**, **determines the right intervention**, and **executes a bounded recovery workflow** — from payment failures and checkout abandonment to overdue receivables.

---

## 🎯 Problem

Revenue loss rarely happens in one clean step. A payment degrades, a checkout gets abandoned, a subscription fails, or an invoice goes overdue. This agent closes the loop from detecting the problem to diagnosing it, choosing the right intervention, and recovering the money.

## 🚀 Features

| Feature | Description |
|---|---|
| **Payment Degradation Recovery** | Detect payment degradation → root cause → recovery action |
| **Checkout Drop-off Recovery** | Re-engage users who abandoned checkout |
| **Failed-Subscription Recovery** | Automatically retry and recover failed subscriptions |
| **B2B Receivables Chaser** | Chase overdue B2B invoices intelligently |
| **Mandate Retry Sequencer** | Smart retry sequences for mandate/authorization failures |
| **Hinglish Voice Recovery** | Voice-based recovery flows in Hinglish |
| **Promise-to-Pay Tracker** | Track and follow up on payment promises |

## 🏗️ Project Structure

```
ai-revenue-recovery-agent/
├── src/
│   ├── agent/              # Core agent logic
│   │   ├── __init__.py
│   │   ├── detector.py     # Revenue risk detection
│   │   ├── diagnoser.py    # Root cause diagnosis
│   │   ├── interventions.py # Recovery interventions
│   │   └── orchestrator.py # Workflow orchestration
│   ├── integrations/       # External service integrations
│   │   ├── __init__.py
│   │   ├── payment.py      # Payment gateway integrations
│   │   ├── crm.py          # CRM integrations
│   │   └── notifications.py # Email/SMS/Voice channels
│   ├── models/             # Data models
│   │   ├── __init__.py
│   │   └── schemas.py      # Pydantic schemas
│   └── utils/              # Utilities
│       ├── __init__.py
│       └── config.py       # Configuration management
├── tests/                  # Test suite
│   └── __init__.py
├── docs/                   # Documentation
├── .env.example            # Environment variable template
├── .gitignore
├── requirements.txt
├── pyproject.toml
└── README.md
```

## ⚡ Quick Start

### Prerequisites
- Python 3.11+
- pip or uv

### Installation

```bash
# Clone the repository
git clone https://github.com/<your-username>/ai-revenue-recovery-agent.git
cd ai-revenue-recovery-agent

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your configuration
```

### Running the Agent

```bash
python -m src.agent.orchestrator
```

## 🧪 Running Tests

```bash
pytest tests/ -v
```

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

## 🤝 Contributing

Contributions are welcome! Please open an issue or submit a pull request.
