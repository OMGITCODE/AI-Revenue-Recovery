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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from api.store import store
from api.simulator import SCENARIOS, run_scenario, run_custom_webhook

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


@app.post("/api/reset")
async def reset():
    store.reset()
    return {"status": "reset"}


@app.post("/api/custom")
async def custom_case(request: Request):
    """Single custom UPI event from form. Body: {vpa, bank, amount, failure_code, retry_attempt}"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    from api.simulator import run_custom_form
    ev = await run_custom_form(body)
    if not ev:
        raise HTTPException(status_code=422, detail="Could not process — check failure code")
    return ev.to_dict()


@app.post("/api/upload")
async def upload_cases(request: Request):
    """Bulk JSON upload. Accepts a JSON array (or single object) of custom events."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON file")
    from api.simulator import run_custom_form
    items = body if isinstance(body, list) else [body]
    if len(items) > 100:
        raise HTTPException(status_code=400, detail="Max 100 events per upload")
    results, errors = [], []
    for i, item in enumerate(items):
        try:
            ev = await run_custom_form(item)
            if ev:
                results.append(ev.to_dict())
            else:
                errors.append({"index": i, "error": "no risk detected"})
        except Exception as e:
            errors.append({"index": i, "error": str(e)})
        await asyncio.sleep(0.15)
    return {"processed": len(results), "errors": errors, "events": results}



# ── SSE Stream ────────────────────────────────────────────────────────────────

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
