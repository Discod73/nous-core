#!/usr/bin/env python3
"""
NOUS Memory Arbiter — eneste autoriserede writer til Qdrant.
Port 8010

Alle services sender write-intents her via HTTP.
Arbiteren er den eneste der kalder Qdrant write-endpoints.

──── GBrain source-tag konvention ────────────────────────────────────────────
Facts afledt af graf-inferens (Kuzu GBrain) SKAL tagges med to felter:

  1. WriteRequest.source = GBRAIN_SOURCE_TAG  ("gbrain_inference")
     → Spores i intent_bus.db; viser hvem der kaldte arbiteren.

  2. Qdrant-point payload["source"] = GBRAIN_SOURCE_TAG
     → Lagres i Qdrant; gør det muligt at filtrere/forklare inferens-facts
       adskilt fra direkte dokument-facts og LLM-summaries.

Brugs-eksempel (i night_pipeline eller swarm_agent):
    upsert_point(wing, scope, point_id, vector, {
        "type":   "inferred_fact",
        "source": GBRAIN_SOURCE_TAG,   # obligatorisk for alle gbrain-afledte punkter
        "text":   "...",
        ...
    })

Alle andre write-kald bør IKKE sætte source=GBRAIN_SOURCE_TAG i payloaden.
──────────────────────────────────────────────────────────────────────────────
"""
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, field_validator

# Audit log lives at the NOUS root — add it to path so both the arbiter service
# (which runs with CWD=/srv/nous/arbiter) and test imports can find it.
_NOUS_ROOT = str(Path(__file__).resolve().parent.parent)
if _NOUS_ROOT not in sys.path:
    sys.path.insert(0, _NOUS_ROOT)
from audit_log import log_event as _audit

from intent_bus import (
    get_cold_points,
    get_intent,
    get_pending,
    get_recent,
    get_shadow_baseline_date,
    get_shadow_stats,
    init_db,
    insert_intent,
    log_shadow_prediction,
    purge_old_heat,
    record_heat,
    update_status,
    HEAT_WINDOW_DAYS,
)
from scope_rules import ScopeError, validate
from curator_shadow import (
    load_curator,
    predict as curator_predict,
    error_direction,
    THRESHOLDS,
)

QDRANT_URL       = os.environ.get("NOUS_QDRANT_URL", "http://localhost:6333")
ARBITER_PORT     = int(os.environ.get("NOUS_ARBITER_PORT", "8010"))
MAX_POINT_BYTES  = 1024 * 1024  # 1 MB
WINGS_FILE       = Path("/srv/nous/config/wings.json")

# Obligatorisk kilde-tag for alle Kuzu GBrain-inferens-afledte Qdrant-punkter.
# Sættes i BÅDE WriteRequest.source og point.payload["source"].
GBRAIN_SOURCE_TAG = "gbrain_inference"

# Arkiv-collection navn (scope PRIVATE — aldrig slet herfra).
ARCHIVE_WING       = "private_archive"
ARCHIVE_COLLECTION = "private_archive_private"
ARCHIVE_SCOPE      = "PRIVATE"

# Heat-arkivering: kolde punkter med score under tærskel i 180-dages vindue.
ARCHIVE_HEAT_THRESHOLD = 0.02

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


class HeatRecordRequest(BaseModel):
    collection: str
    point_ids:  list[str]


# ── Arkiv-hjælpefunktioner ────────────────────────────────────────────────────

def _load_private_wings() -> list[dict]:
    """Returnér kun PRIVATE-wings fra wings.json — aldrig SECRET, SWARM eller PUBLIC.
    Hardkodet guard: arkivering må aldrig ramme SECRET-data.
    """
    data = json.loads(WINGS_FILE.read_text(encoding="utf-8"))
    return [
        w for w in data.get("wings", [])
        if w.get("scope") == "PRIVATE"
        and not w.get("is_archive", False)
    ]


async def _ensure_archive_collection() -> None:
    """Opret private_archive_private-collection i Qdrant hvis den ikke eksisterer."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{QDRANT_URL}/collections/{ARCHIVE_COLLECTION}")
            if r.status_code == 200:
                return
            # Opret — vector size 768 (nomic-embed-text)
            r2 = await client.put(
                f"{QDRANT_URL}/collections/{ARCHIVE_COLLECTION}",
                json={"vectors": {"size": 768, "distance": "Cosine"}},
            )
            r2.raise_for_status()
            log.info("Arkiv-collection '%s' oprettet", ARCHIVE_COLLECTION)
    except Exception as e:
        log.warning("Kunne ikke sikre arkiv-collection: %s", e)


async def _archive_cold_points(dry_run: bool = False) -> dict:
    """Flyt kolde PRIVATE-punkter til private_archive_private.

    Hard guard: ALDRIG SECRET-collections (kontrolleret via _load_private_wings()).
    Slet ALDRIG fra arkiv-collectionen. Kold = heat_score < ARCHIVE_HEAT_THRESHOLD.
    """
    private_wings = _load_private_wings()
    if not private_wings:
        return {"archived": 0, "wings_scanned": 0}

    await _ensure_archive_collection()
    await purge_old_heat(HEAT_WINDOW_DAYS)

    total_archived = 0

    for wing in private_wings:
        collection = wing["collection"]
        # Hent varme punkt-IDs i vinduet (de der har haft accesses)
        warm_ids = await get_cold_points(collection)  # returnerer VARME ids

        # Scroll alle punkter i collectionen
        offset = None
        archived_this_wing = 0
        while True:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{QDRANT_URL}/collections/{collection}/points/scroll",
                    json={
                        "limit": 100,
                        "with_payload": True,
                        "with_vector": True,
                        "offset": offset,
                    },
                )
            r.raise_for_status()
            result = r.json().get("result", {})
            points = result.get("points", [])

            cold_points = [p for p in points if str(p["id"]) not in warm_ids]

            if cold_points and not dry_run:
                # Kopiér til arkiv
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r2 = await client.put(
                        f"{QDRANT_URL}/collections/{ARCHIVE_COLLECTION}/points",
                        json={"points": [
                            {
                                "id": p["id"],
                                "vector": p["vector"],
                                "payload": {
                                    **p.get("payload", {}),
                                    "archived_from": collection,
                                    "archived_at": datetime.now(timezone.utc).isoformat(),
                                    "archived_scope": ARCHIVE_SCOPE,
                                },
                            }
                            for p in cold_points
                        ]},
                    )
                    r2.raise_for_status()
                # Slet fra kilde (data bevaret i arkiv)
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r3 = await client.post(
                        f"{QDRANT_URL}/collections/{collection}/points/delete",
                        json={"points": [p["id"] for p in cold_points]},
                    )
                    r3.raise_for_status()
                archived_this_wing += len(cold_points)

            offset = result.get("next_page_offset")
            if not offset:
                break

        if archived_this_wing:
            log.info(
                "Arkivering: %d kolde punkter flyttet fra '%s' → '%s'",
                archived_this_wing, collection, ARCHIVE_COLLECTION,
            )
        total_archived += archived_this_wing

    return {
        "archived":      total_archived,
        "wings_scanned": len(private_wings),
        "dry_run":       dry_run,
    }


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

            n_pts = len(payload.get("points", []))
            _audit(
                "WRITE", intent["wing"], intent["scope"],
                intent.get("source"),
                f"upsert {n_pts} point(s) via {intent.get('source', '?')}",
            )

            # Shadow-log Curator v1-forudsigelse for hvert punkt med tekst.
            # Fejler stille — shadow-fejl stopper ALDRIG en skrivning.
            wing_actual  = intent["wing"]
            scope_actual = intent["scope"]
            is_test_intent = intent.get("source", "").lower().startswith("test")
            for pt in payload.get("points", []):
                text = pt.get("payload", {}).get("text", "")
                if not text:
                    continue
                point_id = str(pt.get("id", ""))
                try:
                    pred_wing, pred_scope, conf = curator_predict(text)
                    if not pred_wing:
                        continue
                    direction = error_direction(scope_actual, pred_scope)
                    await log_shadow_prediction(
                        point_id=point_id,
                        actual_wing=wing_actual,
                        actual_scope=scope_actual,
                        predicted_wing=pred_wing,
                        predicted_scope=pred_scope,
                        confidence=conf,
                        direction=direction,
                        is_test=is_test_intent,
                    )
                except Exception as shadow_err:
                    log.debug("Shadow-log fejl (point %s): %s", point_id, shadow_err)

        elif intent["operation"] == "delete":
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{QDRANT_URL}/collections/{collection}/points/delete",
                    json={"points": payload["points"]},
                )
                r.raise_for_status()

            n_pts = len(payload.get("points", []))
            _audit(
                "WRITE", intent["wing"], intent["scope"],
                intent.get("source"),
                f"delete {n_pts} point(s) via {intent.get('source', '?')}",
            )

        await update_status(intent_id, "done")
        log.info(
            "intent %d done  wing=%s scope=%s op=%s points=%d",
            intent_id, intent["wing"], intent["scope"],
            intent["operation"], len(payload.get("points", [])),
        )

    except ScopeError as e:
        await update_status(intent_id, "failed", str(e))
        log.warning("intent %d AFVIST (scope): %s", intent_id, e)
        _audit("SCOPE_VIOLATION", intent.get("wing", "?"), intent.get("scope", "?"),
               intent.get("source"), str(e)[:100])
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
    await _ensure_archive_collection()
    load_curator()   # Fejler stille — shadow-logging deaktiveres automatisk
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
        _audit("SCOPE_VIOLATION", req.wing, req.scope, req.source, str(e)[:100])
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
        _audit("SCOPE_VIOLATION", req.wing, req.scope, req.source, str(e)[:100])
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


@app.post("/arbiter/heat/record", status_code=202)
async def heat_record(req: HeatRecordRequest):
    """Registrér at disse Qdrant-point IDs blev tilgået (søgeresultat, RAG-kontekst osv.).

    Kaldes af api/main.py ved hvert søgeresultat der returneres til brugeren.
    Heat-scoren bruges af /arbiter/archive/run til at identificere kolde punkter.
    """
    await record_heat(req.collection, req.point_ids)
    return {"recorded": len(req.point_ids), "collection": req.collection}


@app.get("/curator/shadow-report")
async def curator_shadow_report():
    """Curator v1 shadow-mode rapport — kun observationsdata, ingen automatisk handling.

    Rapporterer fejlrate per scope per retning for 7 dage, 30 dage og al tid.
    Dan vurderer tallene manuelt efter 6-ugers baseline-periode (2026-07-03 → ca. 2026-08-14).

    Fejlretninger:
      dangerous    — modellen foreslog bredere deling end korrekt
      safe         — modellen foreslog snævrere deling end korrekt
      correct      — korrekt scope (wing kan stadig være forkert)
      scope_unknown — forudsagt wing ikke i wings.json
    """
    stats_7d   = await get_shadow_stats(days=7)
    stats_30d  = await get_shadow_stats(days=30)
    stats_all  = await get_shadow_stats(days=None)
    baseline   = await get_shadow_baseline_date()

    return {
        "baseline_since": baseline,
        "baseline_ends":  "2026-08-14",
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "thresholds":     THRESHOLDS,
        "periods": {
            "7d":      stats_7d,
            "30d":     stats_30d,
            "all_time": stats_all,
        },
    }


@app.post("/arbiter/archive/run")
async def archive_run(request: Request, dry_run: bool = False):
    """Flyt kolde PRIVATE-punkter (heat < 0.02 over 180 dage) til private_archive.

    HARDKODET SIKKERHEDSGUARD:
    - Kun PRIVATE-collections behandles — SECRET er udelukket på kodeniveau
    - Arkiv-collectionen (private_archive_private) slettes aldrig fra
    - dry_run=true viser hvad der VILLE blive arkiveret uden at gøre det

    Kald typisk fra night_pipeline eller manuelt. Ikke automat.
    """
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Archive endpoint er kun tilgængeligt lokalt")

    result = await _archive_cold_points(dry_run=dry_run)
    log.info(
        "Arkivering kørt: %d punkter arkiveret, %d wings skannet (dry_run=%s)",
        result["archived"], result["wings_scanned"], dry_run,
    )
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=ARBITER_PORT)
