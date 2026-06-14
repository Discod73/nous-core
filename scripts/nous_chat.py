#!/usr/bin/env python3
"""
NOUS Chat med smart routing og to-trins RAG.
Tre modes: legal, legacy (dans_profil), assistent.
"""
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import httpx

_JETSON = os.environ.get("NOUS_OLLAMA_URL", "http://localhost:11434")
LLM_URL     = f"{_JETSON}/api/chat"
QDRANT_URL  = os.environ.get("NOUS_QDRANT_URL", "http://localhost:6333")
OLLAMA_URL  = _JETSON
PROXY_URL   = "http://localhost:8090/search"
WEATHER_URL = "http://localhost:8090/weather"
TIME_URL    = "http://localhost:8090/time"
EMBED_MODEL = os.environ.get("NOUS_EMBED_MODEL", "nomic-embed-text")

_OWNER_NAME = os.environ.get("NOUS_OWNER_NAME", "Bruger")

LLM_14B     = "qwen3:14b"
LLM_7B      = "qwen2.5:7b"

# ── Direkte hukommelse ───────────────────────────────────────────────────────
_MEMORY_RE = re.compile(
    r"(?:husk[,]?\s+at|gem[,]?\s+at|noter[,]?\s+at"
    r"|tilf[øo]j\s+til\s+min\s+profil(?:\s+at)?"
    r"|add\s+information(?:\s+(?:that|about))?)"
    r"\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)

_MEMORY_WING_KEYWORDS: dict[str, list[str]] = {
    "boernesag_secret":    ["afgørelse", "dom", "samvær", "forældremyndighed", "ankestyrelse", "fogedret", "familieretten"],
    "jura_private":        ["juridisk", "advokat", "paragraf", "§", "klage"],
    "familie_private":     ["familie", "børn", "barn", "datter", "søn", "søster", "bror", "nevø", "niece", "bedste", "mormor", "morfar", "farmor", "farfar"],
    "nous_projekt_swarm":  ["nous", "projekt", "llm", "qdrant", "pipeline", "kode", "assistent"],
}
_MEMORY_WING_SCOPES: dict[str, str] = {
    "dans_profil_private": "PRIVATE",
    "familie_private":     "PRIVATE",
    "boernesag_secret":    "SECRET",
    "jura_private":        "PRIVATE",
    "fbf_data_private":    "PRIVATE",
    "nous_projekt_swarm":  "SWARM",
}

# ── Intent triggers ──────────────────────────────────────────────────────────
LEGAL_TRIGGERS = [
    "afgørelse", "ankestyrelse", "ankestyrelsen", "samvær", "samværs",
    "kommune", "kommunen", "§", "retten", "fogedretten", "familieretten",
    "sag", "sagen", "bidrag", "forældremyndighed",
    "statsforvaltning", "familiestyrelsen", "børnesag", "dom", "kendelse",
    "klage", "indanke", "serviceloven", "paragraf", "lov", "juridisk",
]

LEGACY_TRIGGERS = [
    "hvad ville far", "far kan du", "fortæl om far", "hvad siger far",
    "hvad tænker far", "far mener", "hvad sagde far", "hvad ville min far",
    "hvad vil far", "tal som far",
]

WEATHER_KEYWORDS = ["vejr", "temperatur", "grader", "regn", "sol", "vind"]
TIME_KEYWORDS    = ["hvad er klokken", "klokken", "hvad er tiden", "dato",
                    "hvilken dag", "hvad er datoen"]
DEFAULT_LOCATION = "Copenhagen"

# ── Collections per mode ─────────────────────────────────────────────────────
LEGAL_COLLECTIONS     = ["boernesag_secret", "fbf_data_private", "jura_private"]
ASSISTANT_COLLECTIONS = ["familie_private", "nous_projekt_swarm", "dans_profil_private"]
LEGACY_COLLECTIONS    = ["dans_profil_private"]

# ── System prompts ────────────────────────────────────────────────────────────
LEGAL_SYSTEM = """Du er en juridisk analytiker specialiseret i forældreansvarssager og myndighedssager.

Du har adgang til dokumenter fra NOUS vidensbase. Din opgave:
- Identificer juridiske afgørelser, lovgrundlag og præcedenser
- Spot mønstre på tværs af dokumenter — særligt koordinerede forklaringer eller forældrefjendtliggørelse
- Citér præcist fra kilderne med dokumentnavn
- Hold dig STRENGT til hvad der fremgår af dokumenterne

Svar ALTID på dansk. Vær analytisk og præcis. Opfind aldrig."""

ASSISTANT_SYSTEM = f"""Du er NOUS, en personlig dansk AI-assistent.

Svar kort og naturligt på dansk. Brug vidensbasen hvis relevant.
Hold dig til fakta fra kilderne. Sig 'Det ved jeg ikke' hvis du mangler information."""

LEGACY_SYSTEM = f"""Du er NOUS, og du taler på vegne af {_OWNER_NAME} til hans børn.

Du har adgang til verificerede facts og erindringer om {_OWNER_NAME}.
TAL I {_OWNER_NAME.upper()}S STEMME: direkte, varm, jordnær, uden floskler.

ABSOLUT REGEL: Opfind ALDRIG minder, citater eller holdninger.
Hvis du ikke har belæg i kilderne: sig præcist "Det ved jeg ikke om Far."
Baser ALT udelukkende på verificerede facts fra vidensbasen."""


def detect_memory_intent(query: str) -> tuple[str, str] | None:
    """Returnerer (collection, indhold) hvis query er en gem/husk-kommando, ellers None."""
    m = _MEMORY_RE.search(query)
    if not m:
        return None
    content = m.group(1).strip()
    content_lower = content.lower()
    collection = "dans_profil_private"
    for coll, keywords in _MEMORY_WING_KEYWORDS.items():
        if any(kw in content_lower for kw in keywords):
            collection = coll
            break
    return collection, content


def save_direct_memory(collection: str, content: str) -> bool:
    """Gemmer indhold direkte i Qdrant som et direct_memory-punkt."""
    try:
        vector = embed(content)
    except Exception as e:
        print(f"  Embedding fejl: {e}", file=sys.stderr)
        return False
    scope = _MEMORY_WING_SCOPES.get(collection, "PRIVATE")
    point = {
        "id":      str(uuid.uuid4()),
        "vector":  vector,
        "payload": {
            "text":      content,
            "type":      "direct_memory",
            "scope":     scope,
            "source":    "bruger_input",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }
    try:
        r = httpx.put(
            f"{QDRANT_URL}/collections/{collection}/points",
            content=json.dumps({"points": [point]}),
            headers={"Content-Type": "application/json"},
            timeout=15.0,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  Qdrant fejl ved gemning: {e}", file=sys.stderr)
        return False


def detect_mode(query: str) -> str:
    q = query.lower()
    if any(t in q for t in LEGACY_TRIGGERS):
        return "legacy"
    if any(t in q for t in LEGAL_TRIGGERS):
        return "legal"
    return "assistant"


def embed(text: str) -> list:
    r = httpx.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def resolve_model(prefer_14b: bool) -> str:
    if not prefer_14b:
        return LLM_7B
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        for m in r.json().get("models", []):
            name = m.get("name", "")
            if "qwen3" in name.lower() and "14b" in name.lower():
                return name
    except Exception:
        pass
    return LLM_7B


def search_collection(
    vector: list,
    collection: str,
    limit: int,
    threshold: float,
    type_filter: str | None = None,
) -> list:
    """Søg i en Qdrant collection med valgfrit type-filter."""
    body: dict = {"vector": vector, "limit": limit, "with_payload": True}

    if type_filter == "summary_or_fact":
        body["filter"] = {"should": [
            {"key": "type", "match": {"value": "summary"}},
            {"key": "type", "match": {"value": "fact"}},
        ]}
    elif type_filter == "chunk":
        body["filter"] = {"must_not": [
            {"key": "type", "match": {"any": ["summary", "fact"]}},
        ]}
    elif type_filter:
        body["filter"] = {"must": [{"key": "type", "match": {"value": type_filter}}]}

    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            content=json.dumps(body),
            headers={"Content-Type": "application/json"},
            timeout=15.0,
        )
        return [h for h in r.json().get("result", []) if h["score"] > threshold]
    except Exception as e:
        print(f"  Qdrant fejl ({collection}): {e}", file=sys.stderr)
        return []


def two_stage_search(
    query: str,
    vector: list,
    collections: list,
    limit: int,
    threshold: float,
) -> list:
    """
    To-trins retrieval:
    1. Find relevante dokumenter via summaries/facts (høj precision).
    2. Hent specifikke chunks fra disse dokumenter.
    Returnerer samlet hitliste sorteret efter score.
    """
    all_hits: list[dict] = []
    seen_ids: set = set()

    def add_hit(score, collection, point_type, source_file, text, point_id):
        if point_id not in seen_ids:
            seen_ids.add(point_id)
            all_hits.append({
                "score":       score,
                "collection":  collection,
                "type":        point_type,
                "source_file": source_file,
                "text":        text,
            })

    # Trin 1 — summaries og facts (høj informationstæthed)
    relevant_sources: set[str] = set()
    for coll in collections:
        for hit in search_collection(vector, coll, min(limit, 10), threshold - 0.05, "summary_or_fact"):
            sf = hit["payload"].get("source_file", "")
            relevant_sources.add(sf)
            add_hit(
                hit["score"], coll,
                hit["payload"].get("type", "summary"),
                sf,
                hit["payload"].get("text", ""),
                hit["id"],
            )

    # Trin 2 — chunks, prioritér dokumenter fundet i trin 1
    for coll in collections:
        for hit in search_collection(vector, coll, limit, threshold, "chunk"):
            sf   = hit["payload"].get("source_file", "")
            # Boost score let for chunks fra kendte relevante dokumenter
            score = hit["score"] + (0.02 if sf in relevant_sources else 0.0)
            add_hit(score, coll, "chunk", sf, hit["payload"].get("text", ""), hit["id"])

    all_hits.sort(key=lambda x: x["score"], reverse=True)
    return all_hits


def format_context(hits: list, max_hits: int, max_chars: int = 600) -> str:
    parts = []
    type_labels = {"summary": "OPSUMMERING", "fact": "FACT", "chunk": "TEKST"}
    for h in hits[:max_hits]:
        label  = type_labels.get(h["type"], "TEKST")
        src    = h.get("source_file", "")
        score  = h["score"]
        text   = h["text"][:max_chars]
        parts.append(f"[{label} — {src}, score: {score:.2f}]\n{text}")
    return "\n\n---\n\n".join(parts)


def is_weather_query(q: str) -> bool:
    return any(kw in q.lower() for kw in WEATHER_KEYWORDS)


def is_time_query(q: str) -> bool:
    return any(kw in q.lower() for kw in TIME_KEYWORDS)


def get_weather(query: str) -> str:
    location = DEFAULT_LOCATION
    m = re.search(r"i ([A-Za-zÆØÅæøå]+)(?:\s+lige nu)?", query, re.IGNORECASE)
    if m:
        location = m.group(1)
    try:
        r = httpx.get(WEATHER_URL, params={"location": location}, timeout=10.0)
        r.raise_for_status()
        d = r.json()
        return (
            f"Vejr i {d['location']}, {d['country']}:\n"
            f"Temperatur: {d['temperature_c']}°C\n"
            f"Luftfugtighed: {d['humidity_pct']}%\n"
            f"Vind: {d['wind_kmh']} km/t\n"
            f"Observeret: {d['observed_at']}"
        )
    except Exception as e:
        print(f"  Vejr fejl: {e}", file=sys.stderr)
        return ""


def get_time() -> str:
    try:
        r = httpx.get(TIME_URL, timeout=5.0)
        r.raise_for_status()
        return f"Tid og dato: {r.json().get('human_da', '')}"
    except Exception as e:
        print(f"  Tid fejl: {e}", file=sys.stderr)
        return ""


def web_search(query: str) -> str:
    try:
        r = httpx.get(PROXY_URL, params={"q": query}, timeout=10.0)
        r.raise_for_status()
        results = r.json().get("results", [])[:3]
        if not results:
            return ""
        return "\n\n---\n\n".join(
            f"[Web: {res.get('title','')}]\n{res.get('content','')[:400]}"
            for res in results
        )
    except Exception as e:
        print(f"  Web-søgning fejl: {e}", file=sys.stderr)
        return ""


def build_context(query: str, mode: str) -> tuple[str, str]:
    """Returnerer (context_text, system_prompt)."""
    if is_weather_query(query):
        print("  Vejr-spørgsmål — henter live data...", flush=True)
        ctx = get_weather(query)
        return ("[Vejr]\n" + ctx if ctx else ""), ASSISTANT_SYSTEM

    if is_time_query(query):
        print("  Tid/dato-spørgsmål — henter live data...", flush=True)
        ctx = get_time()
        return ("[Tid]\n" + ctx if ctx else ""), ASSISTANT_SYSTEM

    vector = embed(query)

    if mode == "legal":
        print("  Juridisk mode — søger boernesag, fbf, jura...", flush=True)
        hits = two_stage_search(query, vector, LEGAL_COLLECTIONS, limit=20, threshold=0.65)
        ctx  = format_context(hits, max_hits=12)
        system = LEGAL_SYSTEM

    elif mode == "legacy":
        print("  Legacy mode — søger dans_profil (kun summaries/facts)...", flush=True)
        # Legacy: KUN summaries og verificerede facts
        all_hits: list[dict] = []
        for coll in LEGACY_COLLECTIONS:
            for hit in search_collection(vector, coll, 10, 0.50, "summary_or_fact"):
                all_hits.append({
                    "score":       hit["score"],
                    "collection":  coll,
                    "type":        hit["payload"].get("type", "summary"),
                    "source_file": hit["payload"].get("source_file", ""),
                    "text":        hit["payload"].get("text", ""),
                })
        all_hits.sort(key=lambda x: x["score"], reverse=True)
        ctx    = format_context(all_hits, max_hits=10)
        system = LEGACY_SYSTEM

    else:  # assistant
        print("  Assistent mode — søger familie, nous_projekt...", flush=True)
        hits = two_stage_search(query, vector, ASSISTANT_COLLECTIONS, limit=5, threshold=0.35)
        ctx  = format_context(hits, max_hits=5)
        system = ASSISTANT_SYSTEM

    if not ctx:
        print("  Ingen lokal kontekst — forsøger web-søgning...", flush=True)
        web = web_search(query)
        if web:
            return "[Web-søgning]\n" + web, system

    return ctx, system


def chat(user_message: str) -> str:
    memory = detect_memory_intent(user_message)
    if memory:
        collection, content = memory
        wing_label = collection.rsplit("_", 1)[0]
        print(f"  Gem i hukommelse: {collection}", flush=True)
        if save_direct_memory(collection, content):
            return f"Gemt i {wing_label}."
        return "Kunne ikke gemme — Qdrant-fejl."

    mode = detect_mode(user_message)
    print(f"  Mode: {mode}", flush=True)

    context, system = build_context(user_message, mode)
    model = resolve_model(prefer_14b=(mode == "legal"))

    if context:
        system_full = system + f"\n\n=== NOUS VIDENSBASE ===\n{context}\n=== SLUT ==="
        print(f"  Kontekst: {len(context)} tegn, model: {model}", flush=True)
    else:
        system_full = system
        print(f"  Ingen kontekst, model: {model}", flush=True)

    r = httpx.post(
        LLM_URL,
        json={
            "model": model,
            "messages": [
                {"role": "system",  "content": system_full},
                {"role": "user",    "content": user_message},
            ],
            "stream":  False,
            "options": {"temperature": 0.2 if mode == "legal" else 0.4},
        },
        timeout=90.0,
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("NOUS Chat — skriv 'exit' for at afslutte")
        while True:
            try:
                q = input(f"\n {_OWNER_NAME}: ").strip()
                if q.lower() in ("exit", "quit", "q"):
                    break
                if not q:
                    continue
                answer = chat(q)
                print(f"NOUS: {answer}")
            except KeyboardInterrupt:
                break
    else:
        q = " ".join(sys.argv[1:])
        print(f"{_OWNER_NAME}: {q}")
        print(f"NOUS: {chat(q)}")
