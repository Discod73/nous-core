#!/usr/bin/env python3
"""
NOUS Memory Arbiter — eneste autoriserede writer til Qdrant.
Port 8010

Alle services sender write-intents her via HTTP.
Arbiteren er den eneste der kalder Qdrant write-endpoints.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, field_validator

from intent_bus import (
    get_intent,
    get_pending,
    get_recent,
    init_db,
    insert_intent,
    update_status,
)
from scope_rules import ScopeError, validate

QDRANT_URL       = os.environ.get("NOUS_QDRANT_URL", "http://localhost:6333")
ARBITER_PORT     = int(os.environ.get("NOUS_ARBITER_PORT", "8010"))
MAX_POINT_BYTES  = 1024 * 1024  # 1 MB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("arbiter")

# Per-intent asyncio events for /write/sync
_sync_events: dict[int, asyncio.Event] = {}


# ── Pydantic models ───────────────────────────────────────────────────────────

class WriteRequest(BaseModel):
    wing:      str
    scope:     str
    operation: str   # upsert | delete
    points:    list[dict]
    source:    str

    @field_validator("operation")
    @classmethod
    def _valid_op(cls, v: str) -> str:
        if v not in ("upsert", "delete"):
            raise ValueError("operation skal være 'upsert' eller 'delete'")
        return v

    @field_validator("scope")
    @classmethod
    def _valid_scope(cls, v: str) -> str:
        valid = {"SECRET", "PRIVATE", "SWARM", "PUBLIC"}
        if v not in valid:
            raise ValueError(f"Ugyldigt scope '{v}'")
        return v


# ── Intent processor ──────────────────────────────────────────────────────────

async def _process_intent(intent: dict) -> None:
    intent_id = intent["id"]
    try:
        payload    = json.loads(intent["payload"])
        collection = validate(intent["wing"], intent["scope"])

        if intent["operation"] == "upsert":
            for pt in payload.get("points", []):
                size = len(json.dumps(pt).encode())
                if size > MAX_POINT_BYTES:
                    raise ValueError(
                        f"Point {pt.get('id', '?')} overstiger 1 MB ({size} bytes)"
                    )
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.put(
                    f"{QDRANT_URL}/collections/{collection}/points",
                    json={"points": payload["points"]},
                )
                r.raise_for_status()

        elif intent["operation"] == "delete":
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{QDRANT_URL}/collections/{collection}/points/delete",
                    json={"points": payload["points"]},
                )
                r.raise_for_status()

        await update_status(intent_id, "done")
        log.info(
            "intent %d done  wing=%s scope=%s op=%s points=%d",
            intent_id, intent["wing"], intent["scope"],
            intent["operation"], len(payload.get("points", [])),
        )

    except ScopeError as e:
        await update_status(intent_id, "failed", str(e))
        log.warning("intent %d AFVIST (scope): %s", intent_id, e)
    except Exception as e:
        await update_status(intent_id, "failed", str(e))
        log.error("intent %d FEJL: %s", intent_id, e)
    finally:
        ev = _sync_events.pop(intent_id, None)
        if ev:
            ev.set()


# ── Background worker ─────────────────────────────────────────────────────────

async def _worker() -> None:
    log.info("Worker startet — poller pending intents hvert 0.5s")
    while True:
        try:
            pending = await get_pending(limit=10)
            for intent in pending:
                # Mark processing before yielding — prevents double-pickup
                await update_status(intent["id"], "processing")
                asyncio.create_task(_process_intent(intent))
        except Exception as e:
            log.error("Worker fejl: %s", e)
        await asyncio.sleep(0.5)


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task = asyncio.create_task(_worker())
    log.info("Memory Arbiter klar — port %d", ARBITER_PORT)
    yield
    task.cancel()


app = FastAPI(title="NOUS Memory Arbiter", version="1.0", lifespan=lifespan)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/arbiter/write", status_code=202)
async def write_async(req: WriteRequest):
    """Kø en write-intent og returnér straks (async)."""
    try:
        validate(req.wing, req.scope)
    except ScopeError as e:
        raise HTTPException(403, str(e))

    intent_id = await insert_intent(
        wing=req.wing, scope=req.scope,
        operation=req.operation,
        payload={"points": req.points},
        source=req.source,
    )
    return {"intent_id": intent_id, "status": "pending", "queued_points": len(req.points)}


@app.post("/arbiter/write/sync")
async def write_sync(req: WriteRequest):
    """Kø en write-intent og vent til den er completed i Qdrant."""
    try:
        validate(req.wing, req.scope)
    except ScopeError as e:
        raise HTTPException(403, str(e))

    intent_id = await insert_intent(
        wing=req.wing, scope=req.scope,
        operation=req.operation,
        payload={"points": req.points},
        source=req.source,
    )

    # Register event atomically (no await between here and registration)
    ev = asyncio.Event()
    _sync_events[intent_id] = ev

    # Worker may have already processed it during insert_intent await
    intent = await get_intent(intent_id)
    if intent and intent["status"] in ("done", "failed"):
        _sync_events.pop(intent_id, None)
        if intent["status"] == "failed":
            raise HTTPException(500, f"Write fejlede: {intent['error']}")
        return {"intent_id": intent_id, "status": "done", "queued_points": len(req.points)}

    try:
        await asyncio.wait_for(ev.wait(), timeout=60.0)
    except asyncio.TimeoutError:
        _sync_events.pop(intent_id, None)
        raise HTTPException(504, f"Timeout: intent {intent_id} ikke færdig inden 60s")

    intent = await get_intent(intent_id)
    if intent["status"] == "failed":
        raise HTTPException(500, f"Write fejlede: {intent['error']}")
    return {"intent_id": intent_id, "status": "done", "queued_points": len(req.points)}


@app.get("/arbiter/status/{intent_id}")
async def get_status(intent_id: int):
    intent = await get_intent(intent_id)
    if not intent:
        raise HTTPException(404, f"Intent {intent_id} ikke fundet")
    return intent


@app.get("/arbiter/health")
async def health():
    pending_count = 0
    qdrant_ok     = False
    try:
        pending_count = len(await get_pending(limit=9999))
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{QDRANT_URL}/healthz")
            qdrant_ok = r.status_code == 200
    except Exception:
        pass
    return {
        "status":       "ok",
        "queue_depth":  pending_count,
        "qdrant_ok":    qdrant_ok,
        "timestamp":    datetime.utcnow().isoformat(),
    }


@app.get("/arbiter/audit")
async def audit(request: Request):
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Audit endpoint er kun tilgængeligt lokalt")
    intents = await get_recent(100)
    return {"intents": intents, "count": len(intents)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=ARBITER_PORT)
