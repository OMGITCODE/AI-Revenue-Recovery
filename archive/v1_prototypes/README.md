# Archive: V1 Initial Prototypes

This directory preserves earlier V1 conceptual prototypes created during initial architecture exploration prior to developing the specialized Indian FinTech UPI Autopay recovery engine.

### Production AI Engines:
All active, production-grade intelligence resides in `src/agent/`:
- `upi_detector.py`: Root-cause diagnosis across 14 NPCI response codes.
- `decision_engine.py`: Deterministic RBI & TRAI Guardrails (GR1–GR9).
- `bandit.py`: Contextual Multi-Armed Bandit using Bayesian Thompson Sampling ($\theta \sim \text{Beta}(\alpha, \beta)$).
- `upi_interventions.py`: Multi-channel recovery dispatchers.
- `recovery_ledger.py`: Append-only regulatory audit ledger.
- `promise_tracker.py`: Promise-to-Pay tracking and dynamic trust scoring.
- `whatsapp_inbound.py`: Real-time 2-way Hinglish conversational NLP intent classifier.
- `checkout_recovery.py`: Drop-off cart recovery.
- `b2b_chaser.py`: B2B invoice dunning scheduler.
- `retry_scheduler.py`: Salary-cycle aware retry scheduler (1st–7th IST).
- `idempotency.py`: Atomic concurrency locks and deduplication caches.
