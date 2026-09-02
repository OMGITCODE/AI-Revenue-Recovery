# RecoverIQ: What Broke at 2 AM, and How We Got Out

> *"The hardest problems in building an AI payment recovery agent aren't the AI models. They are the unglamorous edge cases where naive assumptions collide with messy, real-world financial rails."*

This document is the unfiltered, chronological engineering log of RecoverIQ. It documents what broke during development, why it broke, how we diagnosed it under pressure, and how we engineered our way out.

---

## Chronological War Stories

```mermaid
flowchart LR
    S1["1. Day 1<br/>Windows cp1252 &<br/>Salary Trap"] --> S2["2. Benchmark<br/>Throwing Out<br/>Fake 100%"]
    S2 --> S3["3. Ledger<br/>Ghost Revenue &<br/>Stats Inflation"]
    S3 --> S4["4. Identity<br/>Split VPAs &<br/>300x Spend Spike"]
    S4 --> S5["5. Lifecycles<br/>Proactive vs Reactive<br/>Disconnect"]
    S5 --> S6["6. NLP Safety<br/>Fail-Safe LLM &<br/>Regex Net"]
    S6 --> S7["7. Finish Line<br/>Judge Rotation<br/>TTL Idempotency"]
```

---

### 1. Day 1: The Windows `cp1252` Crash & The Month-End "Salary Trap"

* **What broke:**
  On Day 1, our real-time dashboard stream completely refused to boot on Windows. The default Windows console pipe initializes with legacy `cp1252` character encoding. The moment our backend emitted the Indian Rupee symbol (`₹`), Hindi words in Hinglish messages, or status emojis over Server-Sent Events (SSE), Python choked with:
  ```text
  UnicodeEncodeError: 'charmap' codec can't encode character '\u20b9' in position 42
  ```
  At the exact same time, our first retry prototype failed its first realistic test. We simulated an OTT subscription for a salaried employee that failed on August 28th due to `U30` (Insufficient Funds). Standard payment gateways naively retry on $D+1, D+2, D+3$. Our agent did the same: it fired retries on August 29th, 30th, and 31st.
  All three retries bounced. The customer was hit with ₹25 bank bounce fees for each attempt, the mandate exhausted its NPCI 3-attempt lifetime cap, and the subscription was permanently canceled on August 31st—**exactly 12 hours before the customer's monthly salary landed on September 1st.** Our "smart" agent was acting like a blunt instrument, destroying customer goodwill.

* **How we got out:**
  1. We engineered an ASGI [`Utf8CharsetMiddleware`](api/main.py) inside FastAPI to guarantee that all HTTP responses, JS, CSS, and SSE streams explicitly serve `charset=utf-8`. We also configured `PYTHONIOENCODING=utf-8` and `-X utf8` runtime flags across all batch scripts and test suites.
  2. We threw out fixed-interval retries and wrote [`UPIRetryScheduler`](src/agent/retry_scheduler.py). If an Autopay failure occurs between the 26th and month-end with reason code `U30`, the agent halts all retries immediately. It places the mandate into a protective hold and reschedules execution specifically for the **1st to 7th of the following month at 10:00 AM IST** (aligning with standard Indian corporate salary disbursements).

---

### 2. The Benchmark Reality Check: Throwing Out the Fake 100% Number

* **What broke:**
  In our earliest benchmark script, our recovery pipeline reported an eye-popping "100% recovery rate." It looked incredible on a slide deck, but when we looked closely at the code, it was completely rigged: deterministic tests assumed every WhatsApp nudge was opened, every discount was accepted, and no customer ever disputed a charge. 
  In the real world, customers change phones, bank switches go down, and people face genuine financial emergencies. Claiming a 100% recovery rate in a fintech submission is an instant red flag that tells any experienced reviewer the team never tested against reality.

* **How we got out:**
  We completely scrapped the cherry-picked benchmark and rebuilt [`benchmark.py`](benchmark.py) from the ground up:
  1. Built a **Monte Carlo simulator running $N=50$ independent iterations** using seeded pseudo-random number generators for 100% mathematical reproducibility.
  2. Calibrated channel recovery probabilities against published Indian fintech benchmarks (Razorpay, NPCI, Juspay).
  3. Added an explicit **20% pessimistic sensitivity haircut** to prove our unit economics hold up even if real-world macroeconomic conditions deteriorate.
  4. The result: RecoverIQ delivers an honest, defensible **75.8% ± 4.9% recovery rate** (vs. 11.7% fixed baseline), recovering ₹447,296 ± ₹65,872 across 60 real-world scenarios. We traded a fake 100% for a number that stands up under audit.

---

### 3. The "Ghost Revenue" Crisis: Why Our Dashboard Kept Lying to Us

* **What broke:**
  Late in integration testing, we noticed our dashboard's "Total Recovered Revenue" counter was steadily inflating. When we investigated, we found that whenever someone refreshed the page, clicked "Simulate Payment" twice in quick succession, or when a network hiccup caused a webhook retry, our backend generated a new random UUID.
  The system was treating the same ₹12,500 overdue invoice as two separate recoveries, recording ₹25,000 in the audit ledger! Even worse, our multi-armed bandit algorithm was learning from this fake success signal, unnaturally favoring the UPI collect channel over other valid interventions.

* **How we got out:**
  We tore out incremental counters and instituted **authoritative domain-state idempotency**:
  1. We bound simulated executions to deterministic scenario IDs (`EVT-SIM-{CODE}`) rather than random UUIDs.
  2. In [`RecoveryLedger`](src/agent/recovery_ledger.py), we enforced a 5-second rapid debounce on `(event_type, vpa, amount, reasoning)` tuples.
  3. In [`B2BChaser.settle`](src/agent/b2b_chaser.py), we added domain-level guards: if `receivable.status == 'settled'`, the API immediately rejects the duplicate with `ALREADY_SETTLED` and refuses to write duplicate revenue entries.
  4. In [`EventStore`](api/store.py), we converted all dashboard KPI metrics from loose accumulators to dynamically computed aggregations over unique, active events.

---

### 4. The 300x Spike & Split-Identity Trap

* **What broke:**
  Indian customers frequently use different Virtual Payment Addresses (VPAs) across apps—e.g., `rahul@oksbi` on Google Pay, `rahul@okhdfcbank` on PhonePe, and a 10-digit mobile number on Paytm. Early in development, our agent treated these as three separate people. Because each profile had its own touch counter, the agent sent 6 messages to the same customer in a single day, **exceeding our internal anti-harassment cap of 3 outbound touchpoints per day** (`DAILY_CONTACT_CAP = 3`).
  Worse still: a customer who typically paid ₹100/month for cloud backup had an accidental ₹45,000 enterprise tier charge fail on their personal VPA. Naive automated retries would have blindly fired debit attempts for ₹45,000, risking severe overdraft penalties and account depletion.

* **How we got out:**
  1. We built the **Canonical Customer Identity Graph** ([`CustomerIdentityRegistry`](src/agent/customer_identity.py)). It resolves and merges fragmented VPAs, phone numbers, and customer IDs into a single identity (`cust:rahul@oksbi`). Cumulative daily touches, compliance holds, and spend histories are now unified across all aliases.
  2. We engineered **Guardrail 10 (`GR10`)** inside [`SpendPatternTracker`](src/agent/spend_pattern.py). The tracker builds a rolling statistical baseline (mean, range, standard deviation) for each customer. If an incoming transaction spikes more than 9x above their historical mean, automated retries are permanently blocked, and the transaction is safely routed to interactive customer authorization.

---

### 5. The "Two Separate Worlds" Disconnect (Building the Force-Lapse Bridge)

* **What broke:**
  We had built two sophisticated subsystems:
  1. A **proactive mandate scanner** that scans recurring subscriptions and nudges customers 72 hours before expiry ($T-72\text{h}$) with a 1-click renewal link.
  2. A **reactive recovery agent** that diagnoses bank failure codes and chases revenue when an expired mandate error (`BT02`) occurs.
  
  During end-to-end testing, we found an architectural hole: *the two systems didn't talk to each other.* If a customer received a 72-hour proactive renewal nudge and ignored it, the mandate reached its expiration date, was marked `LAPSED` in a silo, and stopped there. The reactive recovery agent was never notified! We had two half-systems pretending to be an integrated product.

* **How we got out:**
  We built the **Force-Lapse Event Bridge** (`POST /api/mandates/force-lapse/{id}`) inside [`api/main.py`](api/main.py). 
  When a proactive mandate crosses its expiration threshold without renewal, the bridge automatically translates the customer profile, bank, and plan details into a genuine `BT02` (Mandate Expired) failure event. It injects this event directly into the central event bus, triggering the reactive recovery pipeline, initiating conversational WhatsApp outreach, updating the Thompson Sampling bandit, and logging the escalation to the audit ledger. The two disconnected features became one seamless closed loop.

---

### 6. Why We Never Trust an LLM Alone for Financial Compliance

* **What broke:**
  To handle conversational WhatsApp replies (*"Bhai kal salary aate hi transfer kar dunga"* vs. *"Job chali gayi, hospital emergency hai"*), we initially routed inbound text directly to an LLM. 
  Under stress testing, pure LLM routing proved dangerous: network latency occasionally spiked to 4+ seconds, occasional API rate limits dropped customer messages, and slight prompt drift risked misclassifying a genuine medical hardship plea as a generic dispute. In consumer debt recovery, misclassifying or ignoring a financial hardship plea is a critical compliance violation.

* **How we got out:**
  We implemented a **Fail-Safe Two-Tier Intent Architecture** ([`src/agent/whatsapp_inbound.py`](src/agent/whatsapp_inbound.py)):
  - **Tier 1 (LLM Contextual Engine):** Evaluates multi-turn conversational history using Google Gemini 3.6 Flash / GPT-4o-mini to parse complex colloquial idioms and date shifts.
  - **Tier 2 (Deterministic Heuristic Net):** If the LLM call times out (>2500ms), errors, or hits a rate limit, the system instantly and silently falls back to a deterministic regex engine.
  - Evaluated against our held-out test suite of 30 colloquial messages (`GET /api/classifier/eval`), the deterministic baseline achieves **93.3% accuracy**, while guaranteeing **100% recall on vulnerable categories (`HARDSHIP` and `WRONG_NUMBER`)**. We get the contextual intelligence of an LLM with the zero-downtime safety of deterministic code.

---

### 7. The Final 15-Minute Judge Test: Fixing the TTL Idempotency Window

* **What broke:**
  Right before shipping our Dynamic UPI QR code generator, an external code review revealed a critical edge case in our payment simulation logic:
  ```python
  # The flawed implementation:
  _qr_payment_cache = TTLCache(maxsize=1000, ttl=300)  # 5-minute rolling window
  ```
  In hackathon judging, panels rotate in shifts spaced 10 to 15 minutes apart. If Judge A clicked "Simulate Customer Paid" on an overdue B2B invoice (`INV-2026-003`), the payment succeeded. But when Judge B arrived 15 minutes later and tested the exact same scenario, the 300-second cache had expired! Judge B was able to "pay" the already-settled invoice a second time, writing duplicate recovery revenue to the ledger and invalidating our demo's integrity.

* **How we got out:**
  We eliminated time-expiring caches for financial state checks entirely. In `POST /api/upi/simulate-payment` ([`api/main.py`](api/main.py)), we anchored idempotency directly to the backend domain object:
  ```python
  # Authoritative Domain-State Check:
  r_obj = next((r for r in b2b_chaser.all_receivables() if r.receivable_id == ref or r.invoice_number == ref), None)
  if r_obj and r_obj.status == "settled":
      return {"status": "already_settled", "message": f"Invoice {r_obj.invoice_number} was already settled."}
  ```
  We also cross-reference the append-only [`RecoveryLedger`](src/agent/recovery_ledger.py) entries for permanent deduplication. Once an invoice is settled, it remains settled permanently—whether Judge B tests it 15 minutes later or 3 days later.

---

## The Core Lesson

Real fintech systems cannot rely on happy-path demos. By writing **194 automated tests across 13 test files**, rejecting fake benchmark metrics, enforcing statutory RBI and TRAI guardrails, and hardening every state transition against double-counting, RecoverIQ transformed from a prototype into a resilient, production-grade autonomous agent.
