"""
FastAPI application — AI Revenue Recovery Agent
Serves the dashboard + REST API + SSE live stream.
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from api.store import store
from api.simulator import SCENARIOS, run_scenario, run_custom_webhook, run_custom_scenario
from src.agent.decision_engine import DecisionEngine
from src.agent.promise_tracker import promise_tracker
from src.agent.checkout_recovery import checkout_agent, DropOffReason
from src.agent.b2b_chaser import b2b_chaser, AgingBucket

_decision_engine = DecisionEngine()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AI Revenue Recovery Agent",
    description="UPI Autopay failure detection and recovery",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve dashboard static files
DASHBOARD = ROOT / "dashboard"
app.mount("/static", StaticFiles(directory=str(DASHBOARD)), name="static")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "ai-revenue-recovery-agent"}


@app.get("/api/stats")
async def get_stats():
    return store.get_stats()


@app.get("/api/events")
async def get_events(limit: int = 50):
    return store.get_events(limit)


@app.get("/api/scenarios")
async def get_scenarios():
    return [
        {"key": k, "name": v["name"], "amount": v["amount"],
         "bank": v["bank"], "code": v["failure_code"].value}
        for k, v in SCENARIOS.items()
    ]


@app.post("/api/simulate/{scenario_key}")
async def simulate(scenario_key: str):
    if scenario_key not in SCENARIOS and scenario_key != "all":
        raise HTTPException(status_code=404, detail=f"Unknown scenario: {scenario_key}")

    if scenario_key == "all":
        results = []
        for key in SCENARIOS:
            ev = await run_scenario(key)
            if ev:
                results.append(ev.to_dict())
            await asyncio.sleep(0.3)  # slight delay for visual effect
        return {"processed": len(results), "events": results}

    ev = await run_scenario(scenario_key)
    if not ev:
        raise HTTPException(status_code=500, detail="Scenario failed to run")
    return ev.to_dict()


@app.post("/api/webhook")
async def webhook(request: Request):
    """Accept a raw Razorpay-style webhook payload."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    ev = await run_custom_webhook(payload)
    if not ev:
        raise HTTPException(status_code=422, detail="Could not parse webhook payload")
    return ev.to_dict()


class CustomScenarioRequest(BaseModel):
    """Form payload for a user-defined scenario."""
    failure_code:   str   = Field(..., example="U30")
    vpa:            str   = Field(..., example="user@oksbi")
    bank:           str   = Field(..., example="SBI")
    amount:         float = Field(..., gt=0, example=999.0)
    mandate_state:  str   = Field(default="active", example="active")
    retry_attempt:  int   = Field(default=0, ge=0)
    scenario_name:  str   = Field(default="Custom Scenario")


@app.post("/api/custom")
async def custom_scenario(payload: CustomScenarioRequest):
    """Run a user-created scenario through the full agent pipeline."""
    ev = await run_custom_scenario(payload.dict())
    if not ev:
        raise HTTPException(status_code=422, detail="Could not process custom scenario")
    return ev.to_dict()


@app.post("/api/reset")
async def reset():
    store.reset()
    return {"status": "reset"}


# ── Decision Engine (Guardrails) ───────────────────────────────────────────────

class DecideRequest(BaseModel):
    failure_code:  str   = "U30"
    mandate_state: str   = "active"
    amount:        float = 999.0
    retry_count:   int   = 0
    vpa:           str   = ""

@app.post("/api/decide")
async def decide(req: DecideRequest):
    """Run the guardrails decision engine for a given failure scenario."""
    has_promise = promise_tracker.has_active(req.vpa, req.amount) if req.vpa else False
    decision = _decision_engine.evaluate(
        failure_code  = req.failure_code,
        mandate_state = req.mandate_state,
        amount        = req.amount,
        retry_count   = req.retry_count,
        has_promise   = has_promise,
    )
    return decision.to_dict()


# ── Promise-to-Pay ────────────────────────────────────────────────────────────────────

class PromiseRequest(BaseModel):
    vpa:            str
    amount:         float
    bank:           str   = ""
    failure_code:   str   = "U30"
    deadline_hours: float = 48
    channel:        str   = "whatsapp"
    notes:          str   = ""

@app.post("/api/promises")
async def create_promise(req: PromiseRequest):
    p = promise_tracker.create(
        vpa           = req.vpa,
        amount        = req.amount,
        bank          = req.bank,
        failure_code  = req.failure_code,
        deadline_hours= req.deadline_hours,
        channel       = req.channel,
        notes         = req.notes,
    )
    return p.to_dict()

@app.get("/api/promises")
async def list_promises():
    return {
        "stats":    promise_tracker.stats(),
        "promises": [p.to_dict() for p in promise_tracker.all_promises()],
    }

@app.post("/api/promises/{promise_id}/fulfill")
async def fulfill_promise(promise_id: str):
    p = promise_tracker.fulfill(promise_id)
    if not p:
        raise HTTPException(404, f"Promise {promise_id} not found")
    return p.to_dict()

@app.post("/api/promises/{promise_id}/break")
async def break_promise(promise_id: str):
    p = promise_tracker.mark_broken(promise_id)
    if not p:
        raise HTTPException(404, f"Promise {promise_id} not found")
    return p.to_dict()


# ── Checkout Drop-off Recovery ───────────────────────────────────────────────────

class CheckoutDropRequest(BaseModel):
    customer_vpa:    str
    customer_phone:  str   = ""
    cart_amount:     float
    merchant:        str   = "Demo Merchant"
    drop_off_reason: str   = "unknown"
    language:        str   = "hinglish"

@app.post("/api/checkout/drop")
async def checkout_drop(req: CheckoutDropRequest):
    session = checkout_agent.record_drop_off(
        customer_vpa    = req.customer_vpa,
        customer_phone  = req.customer_phone,
        cart_amount     = req.cart_amount,
        merchant        = req.merchant,
        drop_off_reason = req.drop_off_reason,
        language        = req.language,
    )
    return session.to_dict()

@app.get("/api/checkout")
async def list_checkout_sessions():
    return {
        "stats":    checkout_agent.stats(),
        "sessions": [s.to_dict() for s in checkout_agent.all_sessions()],
    }

@app.post("/api/checkout/{session_id}/recover")
async def checkout_recovered(session_id: str):
    s = checkout_agent.mark_recovered(session_id)
    if not s:
        raise HTTPException(404, f"Session {session_id} not found")
    return s.to_dict()


# ── B2B Receivables Chaser ────────────────────────────────────────────────────────

class ReceivableRequest(BaseModel):
    debtor_name:    str
    debtor_vpa:     str
    debtor_phone:   str   = ""
    invoice_number: str
    amount:         float
    due_date:       str   # ISO 8601: "2026-07-01"
    currency:       str   = "INR"

@app.post("/api/b2b/receivables")
async def add_receivable(req: ReceivableRequest):
    r = b2b_chaser.add_receivable(
        debtor_name    = req.debtor_name,
        debtor_vpa     = req.debtor_vpa,
        debtor_phone   = req.debtor_phone,
        invoice_number = req.invoice_number,
        amount         = req.amount,
        due_date_iso   = req.due_date,
        currency       = req.currency,
    )
    return r.to_dict()

@app.post("/api/b2b/receivables/{receivable_id}/chase")
async def chase_receivable(receivable_id: str):
    action = b2b_chaser.chase(receivable_id)
    if action is None:
        raise HTTPException(404, f"Receivable {receivable_id} not found or already closed")
    return action.to_dict()

@app.post("/api/b2b/receivables/{receivable_id}/settle")
async def settle_receivable(receivable_id: str, amount_received: float = 0):
    r = b2b_chaser.settle(receivable_id, amount_received)
    if not r:
        raise HTTPException(404, detail="Receivable not found")
    return r.to_dict()

@app.get("/api/b2b")
async def b2b_dashboard():
    return {
        "stats":       b2b_chaser.stats(),
        "receivables": [r.to_dict() for r in b2b_chaser.all_receivables()],
    }


# ── Seed demo data on startup ───────────────────────────────────────────────────────

@app.on_event("startup")
async def seed_demo_data():
    """Pre-populate demo data for judges to see immediately."""
    # B2B Receivables across all 4 aging buckets
    b2b_chaser.add_receivable("Infosys BPO",       "infosys@okhdfc",  "+91-9800000001", "INV-2026-001", 185000, "2026-08-10")
    b2b_chaser.add_receivable("TechCorp Pvt Ltd",  "techcorp@oksbi",  "+91-9800000002", "INV-2026-002",  42000, "2026-07-25")
    b2b_chaser.add_receivable("StartupXYZ",         "startup@okaxis",  "+91-9800000003", "INV-2026-003",  12500, "2026-06-30")
    b2b_chaser.add_receivable("Mega Retail Ltd",   "megaretail@ybl",  "+91-9800000004", "INV-2026-004", 320000, "2026-05-15")
    b2b_chaser.add_receivable("CloudSoft India",   "cloudsoft@okicici","+91-9800000005", "INV-2026-005",  8900,  "2026-08-20")

    # Chase all of them
    for r in b2b_chaser.all_receivables():
        b2b_chaser.chase(r.receivable_id)

    # Promise-to-Pay examples
    promise_tracker.create("rahul@oksbi",  999,   "SBI",  "U30",  deadline_hours=24,  notes="Customer called and promised by 5 PM")
    promise_tracker.create("priya@hdfc",   499,   "HDFC", "BT01", deadline_hours=48,  notes="Re-registration link sent; promised to complete")
    promise_tracker.create("vikram@ybl",   3200,  "Yes Bank", "BT02", deadline_hours=72, notes="Insurance premium; customer on travel")

    # Checkout drop-offs
    checkout_agent.record_drop_off("meera@okaxis",   "+91-9700000001", 2499,  "FashionHub",  "payment_page_exit",   "hinglish")
    checkout_agent.record_drop_off("ankit@oksbi",    "+91-9700000002", 899,   "ElectroMart", "otp_timeout",        "hinglish")
    checkout_agent.record_drop_off("sunita@okicici", "+91-9700000003", 15999, "LuxeStore",   "upi_intent_abandoned","english")
    checkout_agent.record_drop_off("raj@paytm",      "+91-9700000004", 349,   "FoodExpress", "bank_error_exit",    "hinglish")


# ── SSE Stream ───────────────────────────────────────────────────────────────────────

@app.get("/api/stream")
async def stream(request: Request):
    """
    Server-Sent Events endpoint.
    Browser connects once; server pushes every new event as JSON.
    """
    queue = store.subscribe()

    async def generator():
        # Send current stats immediately on connect
        yield {"event": "stats", "data": json.dumps(store.get_stats())}
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {"event": "recovery_event", "data": json.dumps(event_data)}
                    # Also push updated stats
                    yield {"event": "stats", "data": json.dumps(store.get_stats())}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            store.unsubscribe(queue)

    return EventSourceResponse(generator())
