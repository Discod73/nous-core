"""
NOUS Swarm Agent — P2P knowledge sharing (Fase 3: Familia + credits + wing-config).
Port 8020
"""
import hashlib
import json
import logging
import os
import re as _re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
from node_identity import get_node_id
from peers import (
    add_peer, get_all_peers, get_active_peers, peer_by_ip,
    ping_peer, remove_peer, update_last_seen,
)
from familia import (
    create_group, add_member_to_group, delete_group,
    get_group_by_id, get_group_for_peer, get_all_groups,
    encrypt_fact, try_decrypt,
)
from credits import add_credit, get_balance, get_credit_summary, get_priority_for_peer
from wing_config import get_wings_for_swarm_type, NEVER_SWARM

log = logging.getLogger("swarm_agent")
logging.basicConfig(level=logging.INFO)

QDRANT_URL   = os.environ.get("NOUS_QDRANT_URL",   "http://localhost:6333")
ARBITER_URL  = os.environ.get("NOUS_ARBITER_URL",  "http://localhost:8010")
OLLAMA_URL   = os.environ.get("NOUS_OLLAMA_URL",   "http://localhost:11434")
EMBED_MODEL  = os.environ.get("NOUS_EMBED_MODEL",  "nomic-embed-text")
SWARM_PORT   = int(os.environ.get("NOUS_SWARM_PORT", "8020"))
WINGS_FILE   = Path(os.environ.get("NOUS_WINGS_FILE", "/srv/nous/config/wings.json"))

SWARM_OUT_COL = "swarm_outgoing"
SWARM_IN_COL  = "swarm_incoming"

# ── Compute sharing config ────────────────────────────────────────────────────
COMPUTE_ENABLED    = os.environ.get("SWARM_COMPUTE_ENABLED",    "false").lower() == "true"
COMPUTE_MAX_TOKENS = int(os.environ.get("SWARM_COMPUTE_MAX_TOKENS", "10000"))
COMPUTE_HOURS      = os.environ.get("SWARM_COMPUTE_HOURS",      "22-06")
COMPUTE_MODEL      = os.environ.get("SWARM_COMPUTE_MODEL",      "qwen2.5:7b")

_token_usage: dict[str, int] = {}

# ── PII heuristics ───────────────────────────────────────────────────────────
_PII_MARKERS = (
    "cpr", "cpr-nr", "personnummer", "pers.nr",
    "kontonummer", "bankkonto", "nemkonto",
    "adgangskode", "password", "kodeord",
)
_CPR_RE = _re.compile(r"\b\d{6}[-\s]?\d{4}\b")


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_node_id()
    yield


app = FastAPI(title="NOUS Swarm Agent", version="3.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Pydantic models ───────────────────────────────────────────────────────────

class AddPeerRequest(BaseModel):
    tailscale_ip: str
    label: str
    swarm_type: str = "familia"
    port: int = 8020


class PeerIngestFact(BaseModel):
    text: str
    source_node: str
    fact_id: str
    approved_at: str = ""
    original_wing: str = ""
    confidence: float = 0.5
    swarm_type: str = "familia"
    encrypted: bool = False


class PeerIngestRequest(BaseModel):
    node_id: str
    facts: list[PeerIngestFact]


class InferRequest(BaseModel):
    prompt: str
    max_tokens: int = 512


# ── Wing helpers ──────────────────────────────────────────────────────────────

def _wing_map() -> dict[str, str]:
    """Navn → collection fra wings.json."""
    try:
        data = json.loads(WINGS_FILE.read_text(encoding="utf-8"))
        return {w["name"]: w["collection"] for w in data.get("wings", [])}
    except Exception:
        return {}


# ── Qdrant helpers ────────────────────────────────────────────────────────────

def _qdrant_count(collection: str) -> int:
    try:
        r = httpx.get(f"{QDRANT_URL}/collections/{collection}", timeout=5.0)
        return r.json().get("result", {}).get("points_count", 0)
    except Exception:
        return 0


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _fact_exists_in_incoming(text_hash: str) -> bool:
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{SWARM_IN_COL}/points/scroll",
            json={
                "limit": 1,
                "with_payload": False,
                "with_vector": False,
                "filter": {"must": [{"key": "text_hash", "match": {"value": text_hash}}]},
            },
            timeout=10.0,
        )
        r.raise_for_status()
        return bool(r.json().get("result", {}).get("points"))
    except Exception:
        return False


async def _embed(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text[:4096]},
        )
        r.raise_for_status()
        return r.json()["embedding"]


async def _write_to_incoming(fact: PeerIngestFact, source_label: str) -> None:
    import uuid as _uuid
    vector = await _embed(fact.text)
    point_id = str(_uuid.uuid4())
    payload = {
        "text":          fact.text,
        "type":          "fact",
        "scope":         "SWARM",
        "source":        "peer_sync",
        "source_node":   fact.source_node,
        "source_label":  source_label,
        "text_hash":     _text_hash(fact.text),
        "verified":      False,
        "original_wing": fact.original_wing,
        "confidence":    fact.confidence,
        "swarm_type":    fact.swarm_type,
        "received_at":   datetime.now(timezone.utc).isoformat(),
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{ARBITER_URL}/arbiter/write/sync",
            json={
                "wing":      "swarm_incoming",
                "scope":     "SWARM",
                "operation": "upsert",
                "points":    [{"id": point_id, "vector": vector, "payload": payload}],
                "source":    "peer_sync",
            },
        )
        r.raise_for_status()


async def _fetch_peer_facts(peer: dict, group_id: str | None = None) -> list[dict]:
    url = f"http://{peer['tailscale_ip']}:{peer['port']}/swarm/outgoing/facts"
    if group_id:
        url += f"?group_id={group_id}"
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.json().get("facts", [])


async def _push_facts_to_peer(peer: dict, facts: list[dict]) -> None:
    if not facts:
        return
    node_id = get_node_id()
    url = f"http://{peer['tailscale_ip']}:{peer['port']}/swarm/ingest"
    payload = PeerIngestRequest(
        node_id=node_id,
        facts=[PeerIngestFact(**f) for f in facts],
    )
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(url, json=payload.model_dump())
        r.raise_for_status()


def _get_our_outgoing_facts() -> list[dict]:
    """Facts fra swarm_outgoing — anonymiseret, til plain sync."""
    facts = []
    offset = None
    node_id = get_node_id()
    while True:
        body: dict = {"limit": 100, "with_payload": True, "with_vector": False}
        if offset:
            body["offset"] = offset
        try:
            r = httpx.post(
                f"{QDRANT_URL}/collections/{SWARM_OUT_COL}/points/scroll",
                json=body, timeout=15.0,
            )
            r.raise_for_status()
        except Exception:
            break
        result = r.json().get("result", {})
        for pt in result.get("points", []):
            p = pt["payload"]
            facts.append({
                "fact_id":       str(pt["id"]),
                "text":          p.get("text", ""),
                "source_node":   node_id,
                "approved_at":   p.get("approved_at", ""),
                "original_wing": p.get("original_wing", ""),
                "confidence":    p.get("confidence", 0.5),
                "swarm_type":    p.get("swarm_type", ""),
                "encrypted":     False,
            })
        offset = result.get("next_page_offset")
        if not offset:
            break
    return facts


def _get_facts_from_wings(allowed_wings: list[str]) -> list[dict]:
    """Facts direkte fra private wings — til Familia krypteret sync."""
    node_id = get_node_id()
    wmap = _wing_map()
    facts = []
    for wing_name in allowed_wings:
        if wing_name in NEVER_SWARM:
            continue
        collection = wmap.get(wing_name)
        if not collection:
            continue
        offset = None
        while True:
            body: dict = {
                "limit": 100,
                "with_payload": True,
                "with_vector": False,
                "filter": {"must": [{"key": "type", "match": {"value": "fact"}}]},
            }
            if offset:
                body["offset"] = offset
            try:
                r = httpx.post(
                    f"{QDRANT_URL}/collections/{collection}/points/scroll",
                    json=body, timeout=15.0,
                )
                r.raise_for_status()
            except Exception:
                break
            result = r.json().get("result", {})
            for pt in result.get("points", []):
                text = pt["payload"].get("text", "").strip()
                if text:
                    facts.append({
                        "fact_id":       str(pt["id"]),
                        "text":          text,
                        "source_node":   node_id,
                        "approved_at":   pt["payload"].get("approved_at", ""),
                        "original_wing": wing_name,
                        "confidence":    pt["payload"].get("confidence", 0.5),
                        "swarm_type":    "familia",
                        "encrypted":     False,
                    })
            offset = result.get("next_page_offset")
            if not offset:
                break
    return facts


# ── Compute helpers ───────────────────────────────────────────────────────────

def _is_allowed_compute_hour() -> bool:
    parts = COMPUTE_HOURS.split("-")
    if len(parts) != 2:
        return False
    start, end = int(parts[0]), int(parts[1])
    h = datetime.now().hour
    return (h >= start or h < end) if start > end else (start <= h < end)


def _compute_budget_exceeded() -> bool:
    key = datetime.now().strftime("%Y%m%d%H")
    return _token_usage.get(key, 0) >= COMPUTE_MAX_TOKENS


def _track_tokens(n: int) -> None:
    key = datetime.now().strftime("%Y%m%d%H")
    _token_usage[key] = _token_usage.get(key, 0) + n
    for k in list(_token_usage):
        if k < key:
            del _token_usage[k]


def _contains_pii(text: str) -> bool:
    t = text.lower()
    if _CPR_RE.search(text):
        return True
    return any(m in t for m in _PII_MARKERS)


# ── Sync helpers ──────────────────────────────────────────────────────────────

async def _sync_plain(peer: dict, our_facts: list[dict]) -> dict:
    """Standard udfkrypteret sync (global/work peers)."""
    entry: dict = {
        "peer": peer["label"], "ip": peer["tailscale_ip"],
        "received": 0, "pushed": len(our_facts),
        "encrypted": False, "error": None,
    }
    try:
        peer_facts = await _fetch_peer_facts(peer)
        new_count = 0
        for f in peer_facts:
            h = _text_hash(f.get("text", ""))
            if not _fact_exists_in_incoming(h):
                try:
                    await _write_to_incoming(
                        PeerIngestFact(
                            text=f["text"],
                            source_node=f.get("source_node", peer["node_id"]),
                            fact_id=f.get("fact_id", ""),
                            approved_at=f.get("approved_at", ""),
                            original_wing=f.get("original_wing", ""),
                            confidence=f.get("confidence", 0.5),
                            swarm_type=peer.get("swarm_type", ""),
                        ),
                        source_label=peer["label"],
                    )
                    add_credit("consume_fact", peer["node_id"])
                    new_count += 1
                except Exception as e:
                    log.warning(f"Write fejl (fact fra {peer['label']}): {e}")
        entry["received"] = new_count

        await _push_facts_to_peer(peer, our_facts)
        for _ in our_facts:
            add_credit("contribute_fact", peer["node_id"])

        update_last_seen(peer["node_id"])
        log.info(f"Sync {peer['label']}: {new_count} modtaget, {len(our_facts)} sendt")
    except Exception as e:
        entry["error"] = str(e)
        log.warning(f"Sync fejl ({peer['label']}): {e}")
    return entry


async def _sync_familia(peer: dict, group: dict) -> dict:
    """Krypteret Familia sync med PSK."""
    psk = group["psk"]
    allowed_wings = group["allowed_wings"]
    entry: dict = {
        "peer": peer["label"], "ip": peer["tailscale_ip"],
        "received": 0, "pushed": 0,
        "encrypted": True, "group": group["name"], "error": None,
    }
    try:
        # Pull peer's krypterede facts for denne gruppe
        peer_facts = await _fetch_peer_facts(peer, group_id=group["group_id"])
        new_count = 0
        for f in peer_facts:
            if f.get("encrypted"):
                plain = try_decrypt(f["text"], psk)
                if plain is None:
                    log.warning(f"Dekrypterings-fejl for fact fra {peer['label']}")
                    continue
                f = {**f, "text": plain, "encrypted": False}
            h = _text_hash(f["text"])
            if not _fact_exists_in_incoming(h):
                try:
                    await _write_to_incoming(
                        PeerIngestFact(
                            text=f["text"],
                            source_node=f.get("source_node", peer["node_id"]),
                            fact_id=f.get("fact_id", ""),
                            approved_at=f.get("approved_at", ""),
                            original_wing=f.get("original_wing", ""),
                            confidence=f.get("confidence", 0.5),
                            swarm_type="familia",
                        ),
                        source_label=peer["label"],
                    )
                    add_credit("consume_fact", peer["node_id"])
                    new_count += 1
                except Exception as e:
                    log.warning(f"Write fejl (familia fact fra {peer['label']}): {e}")
        entry["received"] = new_count

        # Push vores krypterede facts til peer
        our_facts = _get_facts_from_wings(allowed_wings)
        encrypted_facts = [
            {**f, "text": encrypt_fact(f["text"], psk), "encrypted": True}
            for f in our_facts
        ]
        await _push_facts_to_peer(peer, encrypted_facts)
        for _ in encrypted_facts:
            add_credit("contribute_fact", peer["node_id"])
        entry["pushed"] = len(encrypted_facts)

        update_last_seen(peer["node_id"])
        log.info(f"Familia sync {peer['label']} ({group['name']}): {new_count} modtaget, {len(encrypted_facts)} sendt")
    except Exception as e:
        entry["error"] = str(e)
        log.warning(f"Familia sync fejl ({peer['label']}): {e}")
    return entry


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/swarm/health")
def health():
    peers = get_all_peers()
    groups = get_all_groups()
    return {
        "node_id":           get_node_id(),
        "version":           "3.0",
        "phase":             3,
        "offerings": {
            "knowledge": True,
            "compute":   COMPUTE_ENABLED,
            "familia":   len(groups) > 0,
        },
        "swarm_facts_count": _qdrant_count(SWARM_OUT_COL),
        "peers":             len(peers),
        "groups":            len(groups),
        "credit_balance":    get_balance(),
    }


@app.post("/swarm/search")
def swarm_search(q: str = Query(...), limit: int = Query(5, le=20)):
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": q[:4096]},
            timeout=30.0,
        )
        r.raise_for_status()
        vector = r.json()["embedding"]
    except Exception as e:
        return {"error": f"Embedding fejl: {e}", "results": []}
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{SWARM_OUT_COL}/points/search",
            json={"vector": vector, "limit": limit, "with_payload": True},
            timeout=15.0,
        )
        r.raise_for_status()
        hits = r.json().get("result", [])
    except Exception as e:
        return {"error": f"Søgning fejl: {e}", "results": []}
    return {
        "results": [
            {"score": round(h["score"], 3), "text": h["payload"].get("text", "")}
            for h in hits
        ]
    }


@app.get("/swarm/outgoing/facts")
def get_outgoing_facts(group_id: str = Query(None)):
    """
    Eksponér facts til peers.
    group_id: returnér krypterede Familia-facts fra allowed_wings.
    Ingen group_id: returnér anonymiserede swarm_outgoing facts.
    """
    node_id = get_node_id()
    if group_id:
        group = get_group_by_id(group_id)
        if not group:
            raise HTTPException(404, f"Gruppe {group_id} ikke fundet")
        psk = group["psk"]
        facts = _get_facts_from_wings(group["allowed_wings"])
        encrypted = [
            {**f, "text": encrypt_fact(f["text"], psk), "encrypted": True}
            for f in facts
        ]
        return {"node_id": node_id, "facts": encrypted}
    return {"node_id": node_id, "facts": _get_our_outgoing_facts()}


@app.post("/swarm/ingest")
async def ingest_from_peer(body: PeerIngestRequest, request: Request):
    """Modtag viden fra en peer og gem i swarm_incoming."""
    client_ip = request.client.host if request.client else "unknown"
    peer = peer_by_ip(client_ip)
    if not peer:
        raise HTTPException(403, f"Ukendt peer IP: {client_ip}")
    if not peer.get("trusted"):
        raise HTTPException(403, f"Peer {peer['label']} er ikke trusted")
    if peer["node_id"] != body.node_id:
        raise HTTPException(403, "node_id matcher ikke registreret peer")

    group = get_group_for_peer(body.node_id)
    psk = group["psk"] if group else None

    new_count = 0
    for fact in body.facts:
        fact_text = fact.text
        if fact.encrypted:
            if psk is None:
                log.warning(f"Krypteret fact fra peer {peer['label']} men ingen gruppe fundet")
                continue
            plain = try_decrypt(fact_text, psk)
            if plain is None:
                log.warning(f"Kunne ikke dekryptere fact fra {peer['label']}")
                continue
            fact_text = plain

        h = _text_hash(fact_text)
        if _fact_exists_in_incoming(h):
            continue
        try:
            await _write_to_incoming(
                PeerIngestFact(
                    text=fact_text,
                    source_node=fact.source_node,
                    fact_id=fact.fact_id,
                    approved_at=fact.approved_at,
                    original_wing=fact.original_wing,
                    confidence=fact.confidence,
                    swarm_type=fact.swarm_type,
                ),
                source_label=peer["label"],
            )
            add_credit("consume_fact", peer["node_id"])
            new_count += 1
        except Exception as e:
            log.warning(f"Ingest fejl for fact fra {peer['label']}: {e}")

    update_last_seen(peer["node_id"])
    log.info(f"Ingest fra {peer['label']}: {new_count} nye facts")
    return {"ok": True, "new_facts": new_count, "peer": peer["label"]}


@app.post("/swarm/sync")
async def sync_with_peers():
    """Sync med alle aktive peers. Familia-peers: krypteret PSK-sync."""
    peers = get_active_peers()
    if not peers:
        return {"ok": True, "message": "Ingen aktive peers", "results": []}

    our_facts = _get_our_outgoing_facts()
    results = []

    for peer in peers:
        group = get_group_for_peer(peer["node_id"])
        if group:
            result = await _sync_familia(peer, group)
        else:
            result = await _sync_plain(peer, our_facts)
        results.append(result)

    return {"ok": True, "synced_at": datetime.now(timezone.utc).isoformat(), "results": results}


# ── Peer management ───────────────────────────────────────────────────────────

@app.get("/swarm/peers")
def list_peers():
    peers = get_all_peers()
    result = []
    for p in peers:
        online = ping_peer(p, timeout=3.0)
        priority = get_priority_for_peer(p["node_id"])
        result.append({**p, "online": online, "priority": priority})
    return {"peers": result}


@app.post("/swarm/peers/add")
def api_add_peer(body: AddPeerRequest):
    peer = add_peer(body.tailscale_ip, body.label, body.swarm_type, body.port)
    online = ping_peer(peer, timeout=3.0)
    return {"ok": True, "peer": peer, "online": online}


@app.delete("/swarm/peers/{node_id}")
def api_remove_peer(node_id: str):
    if not remove_peer(node_id):
        raise HTTPException(404, f"Peer {node_id} ikke fundet")
    return {"ok": True, "removed": node_id}


# ── Familia gruppe management ─────────────────────────────────────────────────

class CreateGroupBody(BaseModel):
    name: str
    group_type: str = "familia"
    allowed_wings: list[str] = []


class AddMemberBody(BaseModel):
    node_id: str
    label: str


@app.get("/swarm/groups")
def api_get_groups():
    return {"groups": get_all_groups()}


@app.post("/swarm/groups")
def api_create_group(body: CreateGroupBody):
    group = create_group(body.name, body.group_type, body.allowed_wings)
    return {"ok": True, "group": group}


@app.post("/swarm/groups/{group_id}/members")
def api_add_member(group_id: str, body: AddMemberBody):
    group = add_member_to_group(group_id, body.node_id, body.label)
    if group is None:
        raise HTTPException(404, f"Gruppe {group_id} ikke fundet")
    return {"ok": True, "group": group}


@app.delete("/swarm/groups/{group_id}")
def api_delete_group(group_id: str):
    if not delete_group(group_id):
        raise HTTPException(404, f"Gruppe {group_id} ikke fundet")
    return {"ok": True, "deleted": group_id}


# ── Credits ───────────────────────────────────────────────────────────────────

@app.get("/swarm/credits")
def api_get_credits():
    return get_credit_summary()


@app.get("/swarm/peer/priority/{node_id}")
def api_peer_priority(node_id: str):
    return {"node_id": node_id, "priority": get_priority_for_peer(node_id)}


# ── Wing-config ───────────────────────────────────────────────────────────────

from wing_config import get_all_wing_config, set_wing_config


class WingConfigBody(BaseModel):
    wing: str
    config: dict[str, bool]


@app.get("/swarm/wing-config")
def api_get_wing_config():
    return {"wings": get_all_wing_config()}


@app.post("/swarm/wing-config")
def api_set_wing_config(body: WingConfigBody):
    try:
        set_wing_config(body.wing, body.config)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "wing": body.wing, "config": body.config}


# ── Compute sharing ───────────────────────────────────────────────────────────

@app.post("/swarm/infer")
async def swarm_infer(body: InferRequest):
    if not COMPUTE_ENABLED:
        raise HTTPException(503, "Compute sharing ikke aktiveret på denne node")
    if not _is_allowed_compute_hour():
        raise HTTPException(503, f"Compute sharing kun i timer {COMPUTE_HOURS}")
    if _compute_budget_exceeded():
        raise HTTPException(429, f"Token-budget ({COMPUTE_MAX_TOKENS}/time) opbrugt")
    if _contains_pii(body.prompt):
        raise HTTPException(400, "Prompt indeholder potentielt personlige data")

    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model":    COMPUTE_MODEL,
                    "messages": [{"role": "user", "content": body.prompt}],
                    "stream":   False,
                    "options":  {"num_predict": min(body.max_tokens, 1024)},
                },
            )
            r.raise_for_status()
        data = r.json()
        text = data["message"]["content"]
        tokens = data.get("eval_count", len(text) // 4)
        _track_tokens(tokens)
        add_credit("contribute_compute", multiplier=max(1, tokens // 1000))
        return {"response": text, "model": COMPUTE_MODEL, "tokens": tokens}
    except Exception as e:
        raise HTTPException(503, f"Inference fejl: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SWARM_PORT)
