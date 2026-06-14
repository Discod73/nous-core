"""
NOUS Swarm — Promotion Pipeline (Fase 1, lokal).
Henter PRIVATE facts, anonymiserer via qwen3:14b, gemmer til SQLite queue.
"""
import json
import logging
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from wing_config import get_wings_for_swarm_type, NEVER_SWARM

log = logging.getLogger("swarm.promotion")

QDRANT_URL   = "http://localhost:6333"
ARBITER_URL  = "http://localhost:8010"
OLLAMA_URL   = os.environ.get("NOUS_OLLAMA_URL", "http://localhost:11434")
LLM_MODEL    = "qwen3:14b"
EMBED_MODEL  = "nomic-embed-text"
WINGS_FILE   = Path(os.environ.get("NOUS_WINGS_FILE", "/srv/nous/config/wings.json"))
DB_PATH      = Path("/mnt/nous-data/swarm_queue.db")
SCROLL_LIMIT = 256

ANON_PROMPT = """Du er en anonymiserings-agent. Omskriv følgende fact så den bliver generelt \
anvendelig uden at afsløre hvem den handler om.

Regler:
- Fjern ALLE egennavne (personer, steder, organisationer, institutioner)
- Fjern specifikke datoer — behold kun år eller årstid hvis relevant
- Fjern relationer ("min datter", "min bror") — erstat med generiske termer
- Bevar den juridiske, faktuelle eller praktiske kerne af informationen
- Hvis informationen er for personspecifik til at anonymisere sikkert → returner kun teksten "IKKE_ANONYMISERBAR"
- Svar KUN med den anonymiserede tekst eller "IKKE_ANONYMISERBAR" — intet andet

Original fact:
{fact_text}"""


# ── SQLite ────────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS promotion_queue (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at         TEXT    DEFAULT (datetime('now')),
            status             TEXT    DEFAULT 'pending',
            original_point_id  TEXT    NOT NULL,
            original_wing      TEXT    NOT NULL,
            original_text      TEXT    NOT NULL,
            anonymized_text    TEXT,
            confidence         REAL    DEFAULT 0.0,
            reviewed_at        TEXT,
            swarm_point_id     TEXT
        )
    """)
    conn.commit()
    return conn


def _already_queued(conn: sqlite3.Connection, point_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM promotion_queue WHERE original_point_id = ?", (point_id,)
    ).fetchone()
    return row is not None


def _insert_queue(
    conn: sqlite3.Connection,
    point_id: str,
    wing: str,
    original_text: str,
    anonymized_text: str | None,
    confidence: float,
    status: str,
) -> None:
    conn.execute(
        """INSERT INTO promotion_queue
           (original_point_id, original_wing, original_text, anonymized_text, confidence, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (point_id, wing, original_text, anonymized_text, confidence, status),
    )
    conn.commit()


# ── Kandidat-hentning ─────────────────────────────────────────────────────────

def _private_wings(swarm_type: str = "global") -> list[dict]:
    data = json.loads(WINGS_FILE.read_text(encoding="utf-8"))
    allowed = set(get_wings_for_swarm_type(swarm_type))
    return [
        w for w in data.get("wings", [])
        if w.get("scope") in ("PRIVATE", "SECRET")
        and w["name"] not in NEVER_SWARM
        and w["name"] in allowed
    ]


def _get_candidate_facts(max_facts: int, swarm_type: str = "global") -> list[dict]:
    """Henter facts fra PRIVATE wings der er aktiveret for swarm_type."""
    candidates = []
    for wing in _private_wings(swarm_type):
        if len(candidates) >= max_facts:
            break
        collection = wing["collection"]
        offset = None
        while len(candidates) < max_facts:
            body: dict = {
                "limit": SCROLL_LIMIT,
                "with_payload": True,
                "with_vector": False,
                "filter": {
                    "must": [{"key": "type", "match": {"value": "fact"}}],
                    "must_not": [{"key": "swarm_reviewed", "match": {"value": True}}],
                },
            }
            if offset:
                body["offset"] = offset
            try:
                r = httpx.post(
                    f"{QDRANT_URL}/collections/{collection}/points/scroll",
                    json=body, timeout=15.0,
                )
                r.raise_for_status()
            except Exception as e:
                log.warning(f"  Scroll fejl ({collection}): {e}")
                break
            result = r.json().get("result", {})
            for pt in result.get("points", []):
                text = pt["payload"].get("text", "").strip()
                if text:
                    candidates.append({
                        "id":         str(pt["id"]),
                        "wing":       wing["name"],
                        "collection": collection,
                        "text":       text,
                    })
                    if len(candidates) >= max_facts:
                        break
            offset = result.get("next_page_offset")
            if not offset:
                break
    return candidates


# ── Anonymisering ─────────────────────────────────────────────────────────────

def _anonymize(fact_text: str) -> str | None:
    prompt = ANON_PROMPT.format(fact_text=fact_text[:3000])
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=180.0,
        )
        r.raise_for_status()
        return r.json()["message"]["content"].strip()
    except Exception as e:
        log.warning(f"  Anonymisering fejl: {e}")
        return None


# ── Confidence scoring ────────────────────────────────────────────────────────

_LEGAL_TERMS = re.compile(r"\b(lov|paragraf|bekendtgørelse|§|serviceloven|retsplejeloven)\b", re.I)
_SENSITIVE_NUMS = re.compile(r"\b\d{6}[-\s]?\d{4}\b|\b\d{8}\b|\b\d{10}\b|\b\+?45\s*\d{8}\b")


def _score_confidence(original: str, anonymized: str) -> float:
    score = 0.5
    orig_len  = len(original)
    anon_len  = len(anonymized)
    if orig_len > 0 and (orig_len - anon_len) / orig_len > 0.5:
        score += 0.2
    if _SENSITIVE_NUMS.search(anonymized):
        score -= 0.3
    if _LEGAL_TERMS.search(anonymized):
        score += 0.1
    return round(max(0.0, min(1.0, score)), 3)


# ── Markér reviewed i Qdrant ──────────────────────────────────────────────────

def _mark_reviewed(collection: str, point_id: str) -> None:
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/payload",
            json={
                "payload": {"swarm_reviewed": True},
                "points":  [point_id],
            },
            timeout=10.0,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning(f"  Kunne ikke markere {point_id} som reviewed: {e}")


# ── Hoved-batch ───────────────────────────────────────────────────────────────

def run_promotion_batch(max_facts: int = 20, swarm_type: str = "global") -> None:
    log.info(f"Swarm promotion batch starter (max {max_facts} facts, swarm_type={swarm_type})…")
    candidates = _get_candidate_facts(max_facts, swarm_type)
    log.info(f"  {len(candidates)} kandidat-facts fundet")

    conn = _db()
    processed = skipped = queued = not_anon = 0

    for fact in candidates:
        point_id = fact["id"]

        if _already_queued(conn, point_id):
            skipped += 1
            continue

        processed += 1
        original_text = fact["text"]
        anon_text = _anonymize(original_text)

        if anon_text is None:
            _insert_queue(conn, point_id, fact["wing"], original_text, None, 0.0, "pending")
            _mark_reviewed(fact["collection"], point_id)
            skipped += 1
            continue

        if anon_text == "IKKE_ANONYMISERBAR":
            _insert_queue(conn, point_id, fact["wing"], original_text, None, 0.0, "not_anonymizable")
            _mark_reviewed(fact["collection"], point_id)
            not_anon += 1
            log.debug(f"  Ikke anonymiserbar: {point_id[:8]}")
            continue

        confidence = _score_confidence(original_text, anon_text)
        _insert_queue(conn, point_id, fact["wing"], original_text, anon_text, confidence, "pending")
        _mark_reviewed(fact["collection"], point_id)
        queued += 1
        log.info(f"  Kø: {point_id[:8]} (conf={confidence}, wing={fact['wing']})")

    conn.close()
    log.info(
        f"Promotion batch færdig — {processed} behandlet, "
        f"{queued} i kø, {not_anon} ikke-anonymiserbare, {skipped} sprunget over"
    )
