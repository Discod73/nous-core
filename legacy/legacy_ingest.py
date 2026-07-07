#!/usr/bin/env python3
"""
NOUS Legacy — ingest interview-svar til legacy-wing via Memory Arbiter.
Svar gemmes ORDRET. Ingen parafrasering. Ingen redigering.
Wing styres via is_legacy_wing: true i config/wings.json.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from questions import CATEGORY_LABELS

def _legacy_wing() -> str:
    try:
        data = json.loads(Path("/srv/nous/config/wings.json").read_text())
        entry = next((w for w in data.get("wings", []) if w.get("is_legacy_wing")), None)
        return entry["name"] if entry else ""
    except Exception:
        return ""

_LEGACY_WING = _legacy_wing()

OLLAMA_URL  = os.environ.get("NOUS_OLLAMA_URL",  "http://localhost:11434")
EMBED_MODEL = os.environ.get("NOUS_EMBED_MODEL", "nomic-embed-text")
ARBITER_URL = os.environ.get("NOUS_ARBITER_URL", "http://localhost:8010")


def embed_text(text: str) -> list[float]:
    r = httpx.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text[:8192]},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def ingest_answer_to_legacy(
    question_id: str,
    question: str,
    answer: str,
    category: str,
    parent_question_id: str | None = None,
) -> dict:
    """
    Gem svar i legacy-wing via Memory Arbiter.
    Svar gemmes ORDRET — aldrig parafraseret eller ændret.
    Point-ID er deterministisk baseret på question_id (idempotent ved genindsamling).
    """
    cat_label = CATEGORY_LABELS.get(category, category)
    text = (
        f"[LEGACY — {cat_label.upper()}]\n\n"
        f"Spørgsmål: {question}\n\n"
        f"Dans svar: {answer}"
    )
    vector = embed_text(text)
    point_id = f"legacy_{question_id}"

    payload = {
        "text":        text,
        "type":        "legacy_answer",
        "category":    category,
        "question_id": question_id,
        "source_file": "interview",
        "wing":        _LEGACY_WING,
        "scope":       "PRIVATE",
        "question":    question,
        "answer":      answer,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    if parent_question_id:
        payload["parent_question_id"] = parent_question_id

    r = httpx.post(
        f"{ARBITER_URL}/arbiter/write/sync",
        json={
            "wing":      _LEGACY_WING,
            "scope":     "PRIVATE",
            "operation": "upsert",
            "points":    [{"id": point_id, "vector": vector, "payload": payload}],
            "source":    "legacy_interview",
        },
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()
