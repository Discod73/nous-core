#!/usr/bin/env python3
"""
NOUS Night Pipeline — processer alle dokumenter i Qdrant wings.
Genererer domain-aware summaries, ekstraher strukturerede facts,
og skriver til Kuzu knowledge graph.

Venv: /srv/nous/app/.venv (har kuzu + qdrant-client + httpx)
Kører dagligt kl 02:30 via nous-night-pipeline.timer
"""
import base64
import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import kuzu

# Fact extractor v2 — importeres her for at undgå cirkulære imports
if "/srv/nous/scripts" not in sys.path:
    sys.path.insert(0, "/srv/nous/scripts")
from fact_extractor import extract_facts_from_document

# ── Endpoints & konfiguration ─────────────────────────────────────────────────
QDRANT_URL   = "http://localhost:6333"
OLLAMA_URL   = "http://localhost:11434"
EMBED_MODEL  = "nomic-embed-text"
LLM_14B      = "qwen3:14b"
LLM_FALLBACK = "qwen2.5:7b"

WINGS_FILE       = Path("/srv/nous/config/wings.json")
MODEL_ROLES_FILE = Path("/mnt/nous-data/model_roles.json")
KUZU_DB_PATH     = Path("/mnt/nous-data/kuzu.db")
LOG_FILE     = Path("/mnt/nous-data/logs/night_pipeline.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

MAX_DOC_CHARS        = 32_000
MAX_CROSS_CHARS      = 60_000
SCROLL_PAGESIZE      = 256
CROSS_ANALYSIS_WINGS = {"boernesag", "fbf"}
MAX_CROSS_SUMMARIES  = 30   # Øget fra 20 → 30
MAX_CROSS_FACTS      = 0    # Cross-analyse bruger kun summaries
MAX_INCONS_SUMMARIES = 10
BATCH_SIZE_FACTS     = 25   # Øget fra 20 → 25
INCONSISTENCY_TIMEOUT = 180.0
CROSS_TIMEOUT         = 300.0
ANALYSIS_TIMEOUT      = 600.0  # Beholdes som fallback
INCONSISTENCY_STATE_FILE = Path("/mnt/nous-data/logs/inconsistency_state.json")

# ── Medie-analyse (Gemma 4 multimodal via llama.cpp) ─────────────────────────
NX_LLAVA_URL        = "http://YOUR_NX_HOST:8181"
MEDIA_ANALYSIS_FILE = Path("/mnt/nous-data/analyzed_media.json")
MAX_MEDIA_PER_NIGHT = 10
MEDIA_TIMEOUT       = 300.0
MEDIA_IMAGE_EXTS    = {".jpg", ".jpeg", ".png", ".gif"}
MEDIA_VIDEO_EXTS    = {".mp4", ".mov", ".avi"}
MEDIA_WINGS_DIRS: dict[str, list[Path]] = {
    "boernesag": [Path("/home/nous/incoming/boernesag"), Path("/home/nous/incoming/secret")],
    "fbf":       [Path("/home/nous/incoming/fbf")],
}

# ── Lydfilanalyse (faster-whisper via speaches) ───────────────────────────────
NX_SPEACHES_URL      = "http://YOUR_NX_HOST:8182"
ANALYZED_AUDIO_FILE  = Path("/mnt/nous-data/analyzed_audio.json")
MAX_AUDIO_PER_NIGHT  = 10
AUDIO_TIMEOUT        = 300.0
AUDIO_EXTS           = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
AUDIO_WINGS_DIRS     = MEDIA_WINGS_DIRS  # samme incoming-mapper

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("night_pipeline")


# ── Domain-prompts ────────────────────────────────────────────────────────────
def build_prompt(wing: str, source_file: str, text: str) -> str:
    header = f"DOKUMENT: {source_file}\n\n=== TEKST START ===\n{text}\n=== TEKST SLUT ===\n\n"

    if wing == "boernesag":
        instruction = """Du er juridisk analytiker specialiseret i forældreansvarssager og forældrefjendtliggørelse (parental alienation).

Analyser dokumentet og ekstraher KUN:
- Juridiske afgørelser (dato, myndighed, resultat, lovgrundlag §)
- Konkrete handlinger der indikerer forældrefjendtliggørelse eller koordinerede historier
- Direkte citater fra barn eller parter
- Brud på samværsaftaler med dato og beskrivelse
- Myndighedernes vurderinger og hvilke facts de baserede dem på

Ignorer: generel baggrundstekst, statistik der ikke vedrører denne specifikke sag, hyggepræget indhold.
Vær præcis og faktabaseret. Opfind intet.

Returner PRÆCIST dette JSON (ingen tekst udenfor):
{
  "summary": "Analytisk opsummering af dokumentets relevans for sagen (2-5 sætninger)",
  "facts": [
    {
      "dato": "YYYY-MM-DD eller null",
      "kilde": "<source_file>",
      "entiteter": ["Myndighed/person"],
      "fact_type": "afgørelse|handling|citat|samværsbrud|myndighedsvurdering",
      "indhold": "Præcis beskrivelse af det konkrete fact",
      "lovgrundlag": ["§50 SEL"],
      "alienation_indicator": false
    }
  ],
  "alienation_indicators": ["Konkret observeret mønster hvis relevant"]
}"""

    elif wing == "fbf":
        instruction = """Ekstraher strukturerede facts fra dette dokument.

Fokuser på: betalingsdatoer, beløb, krav, frister, afgørelser, myndighedskontakt, manglende betalinger, renter.
Ignorer alt andet.

Returner PRÆCIST dette JSON:
{
  "summary": "Kortfattet opsummering af dokumentets økonomi/administrative indhold",
  "facts": [
    {
      "dato": "YYYY-MM-DD eller null",
      "kilde": "<source_file>",
      "entiteter": ["relevante parter"],
      "fact_type": "betaling|krav|afgørelse|frist|manglende_betaling",
      "indhold": "Præcis beskrivelse med beløb og datoer",
      "lovgrundlag": [],
      "alienation_indicator": false
    }
  ],
  "alienation_indicators": []
}"""

    elif wing == "jura":
        instruction = """Ekstraher juridiske referencepunkter fra dette dokument.

Fokuser på: lovparagraffer med præcist indhold, afgørelsespræcedenser, relevante domme, definitioner.

Returner PRÆCIST dette JSON:
{
  "summary": "Kortfattet beskrivelse af dokumentets juridiske relevans og anvendelseområde",
  "facts": [],
  "alienation_indicators": []
}"""

    elif wing in ("familie", "nous_projekt"):
        instruction = """Ekstraher praktiske informationer fra dette dokument.

Fokuser på: rutiner, præferencer, aftaler, upcoming events, opgaver, praktiske informationer.
Bevar hverdagspræget indhold — det er nyttigt for en familieassistent.

Returner PRÆCIST dette JSON:
{
  "summary": "Kortfattet beskrivelse af dokumentets praktiske indhold",
  "facts": [],
  "alienation_indicators": []
}"""

    elif wing == "dans_profil":
        instruction = """Du bygger et levende portræt af ejeren til hans/hendes nærmeste.

Ekstraher KUN verificerede facts:
- Konkrete livshistorier med dato/kontekst
- Værdier demonstreret gennem HANDLING (ikke abstrakte)
- Holdninger til specifikke emner
- Humor, faglig stolthed (lastbil, kran, have, madlavning)
- Relationer til nære familiemedlemmer

ABSOLUT REGEL: Opfind aldrig minder eller citater. Hvis ikke belæg i teksten — skriv det ikke.

Returner PRÆCIST dette JSON:
{
  "summary": "Kortfattet portræt-opsummering baseret KUN på hvad der fremgår af teksten",
  "facts": [],
  "alienation_indicators": []
}"""

    else:
        instruction = """Lav en kortfattet opsummering af dokumentets indhold.

Returner PRÆCIST dette JSON:
{
  "summary": "Kortfattet opsummering",
  "facts": [],
  "alienation_indicators": []
}"""

    return "Svar udelukkende på dansk.\n\n" + header + instruction


# ── Kuzu schema ───────────────────────────────────────────────────────────────
KUZU_SCHEMA = [
    "CREATE NODE TABLE IF NOT EXISTS Person (naam STRING, PRIMARY KEY (naam))",
    "CREATE NODE TABLE IF NOT EXISTS Myndighed (naam STRING, PRIMARY KEY (naam))",
    "CREATE NODE TABLE IF NOT EXISTS Afgorelse (id STRING, dato STRING, fact_type STRING, indhold STRING, lovgrundlag STRING, kilde STRING, alienation_indicator BOOLEAN, PRIMARY KEY (id))",
    "CREATE NODE TABLE IF NOT EXISTS Dokument (source_file STRING, wing STRING, PRIMARY KEY (source_file))",
    "CREATE REL TABLE IF NOT EXISTS TRAF_AFGORELSE (FROM Myndighed TO Afgorelse)",
    "CREATE REL TABLE IF NOT EXISTS PART_I_SAG (FROM Person TO Afgorelse)",
    "CREATE REL TABLE IF NOT EXISTS BARN_AF (FROM Person TO Person)",
    "CREATE REL TABLE IF NOT EXISTS FRA_DOKUMENT (FROM Afgorelse TO Dokument)",
]

KNOWN_PERSONS    = {"ejer", "modpart", "barn"}
KNOWN_MYNDIGHED  = {"Ankestyrelsen", "Statsforvaltningen", "Familiestyrelsen", "Fogedretten", "Familieretten", "Kommunen", "FBF"}


def kuzu_init() -> kuzu.Connection:
    KUZU_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db   = kuzu.Database(str(KUZU_DB_PATH), max_db_size=1 * 1024 ** 3)
    conn = kuzu.Connection(db)
    for stmt in KUZU_SCHEMA:
        try:
            conn.execute(stmt)
        except Exception as e:
            log.warning(f"Kuzu schema: {e}")
    return conn


def kuzu_write_facts(conn: kuzu.Connection, facts: list, source_file: str, wing: str) -> None:
    for fact in facts:
        fact_id = str(uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"{source_file}:{fact.get('dato','')}:{fact.get('indhold','')[:80]}"
        ))
        indhold   = fact.get("indhold", "")[:500]
        dato      = fact.get("dato") or ""
        fact_type = fact.get("fact_type", "ukendt")
        lovgrund  = json.dumps(fact.get("lovgrundlag", []), ensure_ascii=False)
        kilde     = source_file
        alien     = bool(fact.get("alienation_indicator", False))

        try:
            # Dokument node
            conn.execute(
                "MERGE (d:Dokument {source_file: $sf}) SET d.wing = $wing",
                {"sf": source_file, "wing": wing},
            )
            # Afgørelse node
            conn.execute(
                "MERGE (a:Afgorelse {id: $id}) SET a.dato = $dato, a.fact_type = $ft, "
                "a.indhold = $ind, a.lovgrundlag = $lg, a.kilde = $kilde, a.alienation_indicator = $alien",
                {"id": fact_id, "dato": dato, "ft": fact_type, "ind": indhold,
                 "lg": lovgrund, "kilde": kilde, "alien": alien},
            )
            # Relation: afgørelse → dokument
            conn.execute(
                "MATCH (a:Afgorelse {id: $id}), (d:Dokument {source_file: $sf}) "
                "MERGE (a)-[:FRA_DOKUMENT]->(d)",
                {"id": fact_id, "sf": source_file},
            )
            # Entiteter
            for ent in fact.get("entiteter", []):
                ent = ent.strip()
                if not ent:
                    continue
                if any(p.lower() in ent.lower() for p in KNOWN_PERSONS) or len(ent.split()) <= 4:
                    conn.execute(
                        "MERGE (p:Person {naam: $naam})", {"naam": ent}
                    )
                    conn.execute(
                        "MATCH (p:Person {naam: $naam}), (a:Afgorelse {id: $id}) "
                        "MERGE (p)-[:PART_I_SAG]->(a)",
                        {"naam": ent, "id": fact_id},
                    )
                if any(m.lower() in ent.lower() for m in KNOWN_MYNDIGHED):
                    conn.execute(
                        "MERGE (m:Myndighed {naam: $naam})", {"naam": ent}
                    )
                    conn.execute(
                        "MATCH (m:Myndighed {naam: $naam}), (a:Afgorelse {id: $id}) "
                        "MERGE (m)-[:TRAF_AFGORELSE]->(a)",
                        {"naam": ent, "id": fact_id},
                    )
        except Exception as e:
            log.warning(f"  Kuzu write fejl (fact {fact_id[:8]}): {e}")


# ── Qdrant helpers ────────────────────────────────────────────────────────────
def embed(text: str) -> list:
    r = httpx.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text[:8192]},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["embedding"]


ARBITER_URL = "http://localhost:8010"

def upsert_point(wing: str, scope: str, point_id: str, vector: list, payload: dict) -> None:
    r = httpx.post(
        f"{ARBITER_URL}/arbiter/write/sync",
        json={
            "wing": wing, "scope": scope, "operation": "upsert",
            "points": [{"id": point_id, "vector": vector, "payload": payload}],
            "source": "night_pipeline",
        },
        timeout=90.0,
    )
    r.raise_for_status()


def new_facts_v2_exist(collection: str, source_file: str) -> bool:
    """Returnerer True hvis source_file allerede har v2-facts (fact_schema_version=2)."""
    r = httpx.post(
        f"{QDRANT_URL}/collections/{collection}/points/scroll",
        json={
            "filter": {"must": [
                {"key": "source_file",        "match": {"value": source_file}},
                {"key": "type",               "match": {"value": "fact"}},
                {"key": "fact_schema_version","match": {"value": 2}},
            ]},
            "limit": 1,
            "with_payload": False,
            "with_vector": False,
        },
        timeout=10.0,
    )
    return bool(r.json().get("result", {}).get("points"))


def summary_exists(collection: str, source_file: str) -> bool:
    r = httpx.post(
        f"{QDRANT_URL}/collections/{collection}/points/scroll",
        json={
            "filter": {"must": [
                {"key": "source_file", "match": {"value": source_file}},
                {"key": "type",        "match": {"value": "summary"}},
            ]},
            "limit": 1,
            "with_payload": False,
            "with_vector": False,
        },
        timeout=10.0,
    )
    return bool(r.json().get("result", {}).get("points"))


def get_all_source_files(collection: str) -> list:
    files: dict[str, int] = {}
    offset = None
    while True:
        body: dict = {
            "limit": SCROLL_PAGESIZE,
            "with_payload": ["source_file", "chunk_index", "type"],
            "with_vector": False,
            "filter": {"must_not": [
                {"key": "type", "match": {"any": ["summary", "fact"]}},
            ]},
        }
        if offset:
            body["offset"] = offset
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/scroll",
            json=body, timeout=20.0,
        )
        result = r.json().get("result", {})
        for pt in result.get("points", []):
            sf = pt["payload"].get("source_file")
            if sf:
                files[sf] = files.get(sf, 0) + 1
        offset = result.get("next_page_offset")
        if not offset:
            break
    return list(files.keys())


def get_chunks_for_file(collection: str, source_file: str) -> list:
    chunks: list[tuple[int, str]] = []
    offset = None
    while True:
        body: dict = {
            "limit": SCROLL_PAGESIZE,
            "with_payload": ["chunk_index", "text"],
            "with_vector": False,
            "filter": {"must": [
                {"key": "source_file", "match": {"value": source_file}},
                {"must_not": [{"key": "type", "match": {"any": ["summary", "fact"]}}]},
            ]},
        }
        if offset:
            body["offset"] = offset
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/scroll",
            json=body, timeout=20.0,
        )
        result = r.json().get("result", {})
        for pt in result.get("points", []):
            idx  = pt["payload"].get("chunk_index", 0)
            text = pt["payload"].get("text", "")
            if text:
                chunks.append((idx, text))
        offset = result.get("next_page_offset")
        if not offset:
            break
    chunks.sort(key=lambda x: x[0])
    return [t for _, t in chunks]


def _get_day_model() -> str:
    try:
        if MODEL_ROLES_FILE.exists():
            roles = json.loads(MODEL_ROLES_FILE.read_text(encoding="utf-8"))
            day = roles.get("day", "")
            if day:
                return day
    except Exception:
        pass
    return LLM_FALLBACK


_DEFAULT_NIGHT_PARAMS = {"temperature": 0.7, "num_ctx": 8192, "num_gpu": 99}


def resolve_model() -> tuple[str, dict]:
    """Returnerer (model_name, params) fra model_roles.json."""
    try:
        if MODEL_ROLES_FILE.exists():
            roles = json.loads(MODEL_ROLES_FILE.read_text(encoding="utf-8"))
            night = roles.get("night", "")
            if night:
                log.info(f"Nat-model fra model_roles.json: {night}")
                params = {**_DEFAULT_NIGHT_PARAMS, **roles.get("night_params", {})}
                return night, params
    except Exception as e:
        log.warning(f"Kunne ikke læse model_roles.json: {e}")
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        for m in r.json().get("models", []):
            name = m.get("name", "")
            if "qwen3" in name.lower() and "14b" in name.lower():
                return name, dict(_DEFAULT_NIGHT_PARAMS)
    except Exception:
        pass
    log.info(f"  Nat-model ikke tilgængelig — bruger fallback {LLM_FALLBACK}")
    return LLM_FALLBACK, dict(_DEFAULT_NIGHT_PARAMS)


def call_llm(model: str, prompt: str, timeout: float = 120.0, params: dict | None = None) -> str:
    options = {"temperature": 0.1}
    if params:
        options["temperature"] = params.get("temperature", 0.1)
        if "num_ctx" in params:
            options["num_ctx"] = params["num_ctx"]
        if "num_gpu" in params:
            options["num_gpu"] = params["num_gpu"]
    r = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": options,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


def call_llm_with_retry(model: str, prompt: str, label: str = "", base_timeout: float = 120.0, params: dict | None = None) -> str:
    timeouts = [base_timeout, base_timeout * 2, base_timeout * 4]
    last_exc: Exception = RuntimeError("ingen forsøg")
    for attempt, t in enumerate(timeouts, 1):
        try:
            log.debug(f"  LLM forsøg {attempt}/3 (timeout {t:.0f}s){f' — {label}' if label else ''}")
            return call_llm(model, prompt, timeout=t, params=params)
        except Exception as e:
            last_exc = e
            log.warning(f"  LLM forsøg {attempt}/3 fejlede ({t:.0f}s): {e}")
    raise last_exc


def extract_json(text: str) -> dict:
    # Find JSON-blok i LLM-output (kan have tekst før/efter)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        # Prøv at rense og parse igen
        cleaned = re.sub(r",\s*([}\]])", r"\1", m.group())
        try:
            return json.loads(cleaned)
        except Exception:
            return {}


# ── Cross-document analyse ───────────────────────────────────────────────────
def get_summaries_and_facts(
    collection: str,
    wing: str,
    max_summaries: int = MAX_CROSS_SUMMARIES,
    max_facts: int = MAX_CROSS_FACTS,
) -> tuple[list[str], list[str]]:
    summaries_raw: list[tuple[str, str]] = []
    facts_raw:     list[tuple[str, str]] = []

    for type_filter, target, max_count in [
        ("summary", summaries_raw, max_summaries),
        ("fact",    facts_raw,     max_facts),
    ]:
        if max_count == 0:
            continue
        offset = None
        while True:
            body: dict = {
                "limit": SCROLL_PAGESIZE,
                "with_payload": ["text", "source_file", "dato", "timestamp"],
                "with_vector": False,
                "filter": {"must": [
                    {"key": "type", "match": {"value": type_filter}},
                    {"key": "wing", "match": {"value": wing}},
                ]},
            }
            if offset:
                body["offset"] = offset
            r = httpx.post(
                f"{QDRANT_URL}/collections/{collection}/points/scroll",
                json=body, timeout=20.0,
            )
            result = r.json().get("result", {})
            for pt in result.get("points", []):
                text      = pt["payload"].get("text", "")
                sf        = pt["payload"].get("source_file", "")
                dato      = pt["payload"].get("dato", "")
                timestamp = pt["payload"].get("timestamp", "")
                if text:
                    prefix = f"[{sf}" + (f" / {dato}" if dato else "") + "] " if sf else ""
                    target.append((timestamp, prefix + text))
            offset = result.get("next_page_offset")
            if not offset:
                break

    summaries_raw.sort(key=lambda x: x[0], reverse=True)
    facts_raw.sort(key=lambda x: x[0], reverse=True)
    summaries = [t for _, t in summaries_raw[:max_summaries]]
    facts     = [t for _, t in facts_raw[:max_facts]]
    return summaries, facts


def cross_analysis_exists(collection: str, wing: str, date_str: str) -> bool:
    r = httpx.post(
        f"{QDRANT_URL}/collections/{collection}/points/scroll",
        json={
            "filter": {"must": [
                {"key": "type",      "match": {"value": "cross_analysis"}},
                {"key": "wing",      "match": {"value": wing}},
                {"key": "run_date",  "match": {"value": date_str}},
            ]},
            "limit": 1,
            "with_payload": False,
            "with_vector": False,
        },
        timeout=10.0,
    )
    return bool(r.json().get("result", {}).get("points"))


def build_cross_analysis_prompt(wing: str, summaries: list[str], facts: list[str]) -> str:
    combined = "=== SUMMARIES ===\n" + "\n\n".join(summaries)
    if facts:
        combined += "\n\n=== FACTS ===\n" + "\n".join(facts)
    combined = combined[:MAX_CROSS_CHARS]

    if wing == "boernesag":
        system = (
            "Svar udelukkende på dansk.\n\n"
            "Du er juridisk analytiker specialiseret i forældreansvarssager og forældrefjendtliggørelse.\n"
            "Du har nu adgang til summaries og facts fra ALLE dokumenter i denne sag.\n\n"
            "Analyser på tværs af dokumenterne og identificer:\n"
            "- Mønstre der indikerer systematisk forældrefjendtliggørelse\n"
            "- Koordinerede historier mellem modparten og barnet over tid\n"
            "- Modstridende udsagn i forskellige dokumenter\n"
            "- Tidslinje over eskalering af adfærd\n"
            "- Myndighedernes reaktioner og om de tog facts til efterretning\n"
            "- Juridiske muligheder baseret på det samlede billede\n\n"
            "Vær konkret. Referer til specifikke dokumenter og datoer. Opfind intet."
        )
    elif wing == "fbf":
        system = (
            "Svar udelukkende på dansk.\n\n"
            "Du er økonomisk analytiker specialiseret i børnebidragssager.\n"
            "Du har nu adgang til summaries og facts fra ALLE dokumenter i denne sag.\n\n"
            "Analyser på tværs af dokumenterne og identificer:\n"
            "- Samlede beløb og udestående krav\n"
            "- Tidslinje over betalinger og manglende betalinger\n"
            "- Mønstre i myndighedernes afgørelser\n"
            "- Juridiske muligheder baseret på det samlede billede\n\n"
            "Vær konkret. Referer til specifikke dokumenter og datoer. Opfind intet."
        )
    else:
        system = "Svar udelukkende på dansk.\n\nAnalyser på tværs af alle dokumenter og giv en samlet vurdering af de vigtigste mønstre og konklusioner."

    return system + "\n\n" + combined


INCONSISTENCY_SYSTEM = """Svar udelukkende på dansk.

Du er juridisk analytiker specialiseret i forældreansvarssager.
Analyser ALLE dokumenter og identificer:

1. MODSTRIDENDE UDSAGN FRA MODPARTEN:
   - Hvad sagde modparten til myndighed X vs myndighed Y om samme emne?
   - Hvad sagde modparten på tidspunkt A vs tidspunkt B?
   - Hvor afviger modpartens udsagn fra dokumenterede facts?
   - Citér konkret fra dokumenterne med kilde og dato.

2. MØNSTRE I MYNDIGHEDSMØDER:
   - Identificer møder hvor modparten angriber ejeren overfor myndighedspersoner
   - Notér hvis myndighedspersonen beskytter eller validerer modpartens angreb frem for at forholde sig neutralt

3. BARNET SOM INSTRUMENT:
   - Tilfælde hvor barnets udsagn ligner modpartens narrativ mistænkeligt meget
   - Tidspunkter hvor barnets holdning ændrer sig i takt med modpartens eskalering

4. EJERENS KONSEKVENTE LINJE:
   - Hvad siger ejeren konsekvent på tværs af dokumenter?
   - Hvor stemmer ejerens udsagn overens med objektive facts og vidneudsagn?

Vær konkret. Referer til specifikke dokumenter, datoer og citater. Opfind intet."""


def inconsistency_analysis_exists(collection: str, date_str: str) -> bool:
    r = httpx.post(
        f"{QDRANT_URL}/collections/{collection}/points/scroll",
        json={
            "filter": {"must": [
                {"key": "type",     "match": {"value": "inconsistency_analysis"}},
                {"key": "wing",     "match": {"value": "boernesag"}},
                {"key": "run_date", "match": {"value": date_str}},
            ]},
            "limit": 1,
            "with_payload": False,
            "with_vector": False,
        },
        timeout=10.0,
    )
    return bool(r.json().get("result", {}).get("points"))


def build_inconsistency_prompt(summaries: list[str], facts: list[str]) -> str:
    combined = (
        "=== SUMMARIES ===\n" + "\n\n".join(summaries) +
        "\n\n=== FACTS ===\n" + "\n".join(facts)
    )[:MAX_CROSS_CHARS]
    return INCONSISTENCY_SYSTEM + "\n\n" + combined


def load_inconsistency_state() -> dict:
    try:
        if INCONSISTENCY_STATE_FILE.exists():
            return json.loads(INCONSISTENCY_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Kunne ikke læse inkonsistens-state: {e}")
    return {}


def save_inconsistency_state(state: dict) -> None:
    try:
        INCONSISTENCY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        INCONSISTENCY_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"Kunne ikke gemme inkonsistens-state: {e}")


def get_facts_since(collection: str, wing: str, since_iso: str | None) -> list[str]:
    """Henter facts for wing; filtrerer til nyere end since_iso, sorterer efter fact_type så
    samme typer batches sammen i inkonsistens-analysen (Gemini's clustering-anbefaling)."""
    facts_raw: list[tuple[str, str, str]] = []  # (timestamp, text, fact_type)
    offset = None
    while True:
        body: dict = {
            "limit": SCROLL_PAGESIZE,
            "with_payload": ["text", "source_file", "dato", "timestamp", "fact_type"],
            "with_vector": False,
            "filter": {"must": [
                {"key": "type", "match": {"value": "fact"}},
                {"key": "wing", "match": {"value": wing}},
            ]},
        }
        if offset:
            body["offset"] = offset
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/scroll",
            json=body, timeout=20.0,
        )
        result = r.json().get("result", {})
        for pt in result.get("points", []):
            text      = pt["payload"].get("text", "")
            sf        = pt["payload"].get("source_file", "")
            dato      = pt["payload"].get("dato", "")
            timestamp = pt["payload"].get("timestamp", "")
            ft        = pt["payload"].get("fact_type", "observation")
            if text:
                prefix = f"[{sf}" + (f" / {dato}" if dato else "") + "] " if sf else ""
                facts_raw.append((timestamp, prefix + text, ft))
        offset = result.get("next_page_offset")
        if not offset:
            break

    if since_iso:
        facts_raw = [(ts, t, ft) for ts, t, ft in facts_raw if ts > since_iso]

    # Primær sort: fact_type (clustering) — sekundær: timestamp
    facts_raw.sort(key=lambda x: (x[2], x[0]))
    return [t for _, t, _ in facts_raw]


def run_inconsistency_analysis(collection: str, scope: str, model: str, params: dict | None = None) -> None:
    log.info("  Inkonsistens-analyse for boernesag starter...")

    state    = load_inconsistency_state()
    last_run = state.get("boernesag")

    summaries, _ = get_summaries_and_facts(
        collection, "boernesag",
        max_summaries=MAX_INCONS_SUMMARIES,
        max_facts=0,
    )
    all_facts = get_facts_since(collection, "boernesag", last_run)

    if last_run and not all_facts:
        log.info(f"  Ingen nye facts siden {last_run[:10]} — springer inkonsistens-analyse over")
        return

    if not summaries and not all_facts:
        log.warning("  Ingen summaries/facts fundet — springer inkonsistens-analyse over")
        return

    trimmed_facts = [f[:500] for f in all_facts]
    batches = (
        [trimmed_facts[i : i + BATCH_SIZE_FACTS] for i in range(0, len(trimmed_facts), BATCH_SIZE_FACTS)]
        if trimmed_facts else [[]]
    )
    log.info(
        f"  {len(summaries)} summaries, {len(trimmed_facts)} facts (max 500 tegn) "
        f"→ {len(batches)} batch(es) af max {BATCH_SIZE_FACTS}"
    )

    batch_results: list[str] = []
    for i, batch in enumerate(batches, 1):
        prompt = build_inconsistency_prompt(summaries, batch)
        log.info(f"  Batch {i}/{len(batches)}: {len(batch)} facts ({len(prompt)} tegn)")
        try:
            result = call_llm(model, prompt, timeout=INCONSISTENCY_TIMEOUT, params=params)
            text = result.strip()
            if text:
                batch_results.append(text)
        except Exception as e:
            log.warning(
                f"  Batch {i}/{len(batches)} timeout/fejl ({INCONSISTENCY_TIMEOUT:.0f}s) — springer over: {e}"
            )

    if not batch_results:
        log.error("  Alle inkonsistens-analyse batches fejlede")
        return

    analysis_text = (
        f"\n\n{'─' * 40}\n\n".join(batch_results)
        if len(batch_results) > 1 else batch_results[0]
    )

    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now      = datetime.now(timezone.utc).isoformat()
    point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{collection}:boernesag:inconsistency_analysis:{today}"))
    try:
        vec = embed(analysis_text[:8192])
        upsert_point("boernesag", scope, point_id, vec, {
            "type":        "inconsistency_analysis",
            "wing":        "boernesag",
            "scope":       scope,
            "text":        analysis_text,
            "run_date":    today,
            "timestamp":   now,
            "doc_count":   len(summaries),
            "fact_count":  len(all_facts),
            "batch_count": len(batch_results),
        })
        log.info(
            f"  Inkonsistens-analyse gemt ({len(analysis_text)} tegn, "
            f"{len(batch_results)}/{len(batches)} batches OK)"
        )
        state["boernesag"] = now
        save_inconsistency_state(state)
    except Exception as e:
        log.error(f"  Inkonsistens-analyse gem fejl: {e}")


def run_cross_analysis(collection: str, wing: str, scope: str, model: str, params: dict | None = None) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info(f"  Cross-analyse for {wing} starter...")
    summaries, facts = get_summaries_and_facts(
        collection, wing,
        max_summaries=MAX_CROSS_SUMMARIES,
        max_facts=MAX_CROSS_FACTS,
    )
    if not summaries and not facts:
        log.warning(f"  Ingen summaries/facts fundet — springer cross-analyse over ({wing})")
        return

    log.info(f"  {len(summaries)} summaries, {len(facts)} facts (max {MAX_CROSS_SUMMARIES}/{MAX_CROSS_FACTS})")

    raw = None
    attempts = [
        (summaries, facts),
        (summaries[: max(len(summaries) // 2, 1)], facts[: max(len(facts) // 2, 1)]),
    ]
    for s_slice, f_slice in attempts:
        prompt = build_cross_analysis_prompt(wing, s_slice, f_slice)
        log.info(f"  Sender prompt: {len(s_slice)} summaries ({len(prompt)} tegn)")
        try:
            raw = call_llm(model, prompt, timeout=CROSS_TIMEOUT, params=params)
            break
        except Exception as e:
            log.warning(f"  Cross-analyse timeout/fejl ({len(s_slice)}s, {CROSS_TIMEOUT:.0f}s): {e}")

    if raw is None:
        log.error(f"  Cross-analyse ({wing}) — alle forsøg fejlede, springer over")
        return

    analysis_text = raw.strip()
    if not analysis_text:
        log.warning(f"  Tom cross-analyse output for {wing}")
        return

    now      = datetime.now(timezone.utc).isoformat()
    point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{collection}:{wing}:cross_analysis:{today}"))
    try:
        vec = embed(analysis_text[:8192])
        upsert_point(wing, scope, point_id, vec, {
            "type":       "cross_analysis",
            "wing":       wing,
            "scope":      scope,
            "text":       analysis_text,
            "run_date":   today,
            "timestamp":  now,
            "doc_count":  len(summaries),
            "fact_count": len(facts),
        })
        log.info(f"  Cross-analyse gemt ({len(analysis_text)} tegn)")
    except Exception as e:
        log.error(f"  Cross-analyse gem fejl ({wing}): {e}")


# ── Dokument-processering ─────────────────────────────────────────────────────
def process_document(
    collection: str,
    source_file: str,
    wing: str,
    scope: str,
    model: str,
    kuzu_conn: kuzu.Connection,
    params: dict | None = None,
) -> bool:
    # Hent og saml tekst
    chunks = get_chunks_for_file(collection, source_file)
    if not chunks:
        log.warning(f"  Ingen chunks fundet for {source_file!r}")
        return False

    full_text = "\n\n".join(chunks)[:MAX_DOC_CHARS]

    # LLM-kald med retry (120s → 240s → 480s)
    prompt = build_prompt(wing, source_file, full_text)
    try:
        raw = call_llm_with_retry(model, prompt, label=source_file, params=params)
    except Exception as e:
        log.error(f"  LLM fejl for {source_file!r} — alle 3 forsøg fejlede: {e}")
        return False

    data = extract_json(raw)
    if not data:
        # Brug rå tekst som summary hvis JSON-parsing fejler
        log.warning(f"  JSON-parsing fejlede for {source_file!r} — gemmer rå output som summary")
        data = {"summary": raw[:2000], "facts": [], "alienation_indicators": []}

    summary_text = data.get("summary", "").strip()
    if not summary_text:
        log.warning(f"  Tom summary for {source_file!r} — springer over")
        return False

    now = datetime.now(timezone.utc).isoformat()

    # Gem summary-punkt
    summary_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{collection}:{source_file}:summary"))
    try:
        vec = embed(summary_text)
        upsert_point(wing, scope, summary_id, vec, {
            "type":        "summary",
            "source_file": source_file,
            "wing":        wing,
            "scope":       scope,
            "text":        summary_text,
            "timestamp":   now,
        })
        log.info(f"  Summary gemt ({len(summary_text)} tegn)")
    except Exception as e:
        log.error(f"  Fejl ved gem summary: {e}")
        return False

    # Kuzu graph (bruger stadig domain-facts fra build_prompt til graph-struktur)
    domain_facts = data.get("facts", [])
    alienation   = data.get("alienation_indicators", [])
    for ai in alienation:
        if ai.strip():
            domain_facts.append({
                "dato": None, "kilde": source_file, "entiteter": [],
                "fact_type": "alienation_indicator", "indhold": ai.strip(),
                "lovgrundlag": [], "alienation_indicator": True,
            })
    if wing in ("boernesag", "fbf") and domain_facts:
        try:
            kuzu_write_facts(kuzu_conn, domain_facts, source_file, wing)
        except Exception as e:
            log.warning(f"  Kuzu fejl: {e}")

    # V2-facts via fact_extractor — universelt schema, gemmes i Qdrant via Arbiter
    v2_facts = extract_facts_from_document(full_text, source_file)
    for i, fact in enumerate(v2_facts):
        fact_id = str(uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"{collection}:{source_file}:v2fact:{i}:{fact['fact_text'][:60]}"
        ))
        fact_text_emb = fact["fact_text"]
        if fact.get("date"):
            fact_text_emb = f"{fact['date']} — {fact_text_emb}"
        try:
            vec = embed(fact_text_emb)
            upsert_point(wing, scope, fact_id, vec, {
                "type":               "fact",
                "fact_schema_version": 2,
                "source_file":        source_file,
                "wing":               wing,
                "scope":              scope,
                "text":               fact["fact_text"],
                "fact_type":          fact["fact_type"],
                "actor":              fact.get("actor") or "",
                "dato":               fact.get("date") or "",
                "confidence":         fact["confidence"],
                "timestamp":          now,
            })
        except Exception as e:
            log.warning(f"  V2-fact {i} gem fejl: {e}")

    if v2_facts:
        log.info(f"  {len(v2_facts)} v2-facts gemt")

    return True


# ── Backfill v2-facts for eksisterende dokumenter ────────────────────────────
def backfill_facts_pass(wings: list, model: str) -> None:
    """
    Ekstraher v2-facts for dokumenter der har summary men ingen v2-facts endnu.
    Kører automatisk som del af night pipeline — idempotent.
    """
    log.info("\n── Backfill v2-facts ──")
    backfilled = skipped = 0

    for wing_entry in wings:
        wing       = wing_entry["name"]
        collection = wing_entry["collection"]
        scope      = wing_entry["scope"]

        if scope == "SWARM":
            continue  # Swarm-wings har ikke dokument-chunks

        try:
            source_files = get_all_source_files(collection)
        except Exception as e:
            log.warning(f"  {wing}: kunne ikke hente source_files: {e}")
            continue

        for sf in source_files:
            if not summary_exists(collection, sf):
                continue  # Endnu ikke processeret
            if new_facts_v2_exist(collection, sf):
                skipped += 1
                continue

            log.info(f"  Backfill v2-facts: {wing}/{sf!r}")
            chunks = get_chunks_for_file(collection, sf)
            if not chunks:
                continue

            full_text = "\n\n".join(chunks)
            now       = datetime.now(timezone.utc).isoformat()
            v2_facts  = extract_facts_from_document(full_text, sf)

            for i, fact in enumerate(v2_facts):
                fact_id = str(uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"{collection}:{sf}:v2fact:{i}:{fact['fact_text'][:60]}"
                ))
                emb_text = fact["fact_text"]
                if fact.get("date"):
                    emb_text = f"{fact['date']} — {emb_text}"
                try:
                    vec = embed(emb_text)
                    upsert_point(wing, scope, fact_id, vec, {
                        "type":                "fact",
                        "fact_schema_version": 2,
                        "source_file":         sf,
                        "wing":                wing,
                        "scope":               scope,
                        "text":                fact["fact_text"],
                        "fact_type":           fact["fact_type"],
                        "actor":               fact.get("actor") or "",
                        "dato":                fact.get("date") or "",
                        "confidence":          fact["confidence"],
                        "timestamp":           now,
                    })
                except Exception as e:
                    log.warning(f"  Backfill fact {i} fejl: {e}")

            backfilled += 1
            time.sleep(5)

    log.info(f"  Backfill færdig — {backfilled} backfilled, {skipped} sprunget over (v2 eksisterer)")


# ── Medie-analyse: hjælpefunktioner ──────────────────────────────────────────
def load_analyzed_media() -> dict:
    try:
        if MEDIA_ANALYSIS_FILE.exists():
            return json.loads(MEDIA_ANALYSIS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Kunne ikke læse analyzed_media.json: {e}")
    return {}


def save_analyzed_media(data: dict) -> None:
    try:
        MEDIA_ANALYSIS_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEDIA_ANALYSIS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"Kunne ikke gemme analyzed_media.json: {e}")


def media_analysis_exists_qdrant(collection: str, source_file: str) -> bool:
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/scroll",
            json={
                "filter": {"must": [
                    {"key": "source_file", "match": {"value": source_file}},
                    {"key": "type",        "match": {"value": "image_analysis"}},
                ]},
                "limit": 1,
                "with_payload": False,
                "with_vector": False,
            },
            timeout=10.0,
        )
        return bool(r.json().get("result", {}).get("points"))
    except Exception:
        return False


def call_llava_completion(image_bytes: bytes, prompt_text: str) -> str:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    r = httpx.post(
        f"{NX_LLAVA_URL}/completion",
        json={
            "prompt": f"[img-1]\n{prompt_text}",
            "image_data": [{"data": img_b64, "id": 1}],
            "n_predict": 512,
            "temperature": 0.1,
        },
        timeout=MEDIA_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("content", "").strip()


def extract_video_frames(video_path: Path) -> list[bytes]:
    """Udtræk første, midterste og sidste frame som JPEG-bytes via ffmpeg."""
    frames: list[bytes] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(video_path)],
                capture_output=True, text=True, timeout=30,
            )
            duration = float(probe.stdout.strip())
        except Exception:
            duration = 0.0

        frame_specs: list[tuple[str, list[str]]] = [
            ("first", ["ffmpeg", "-i", str(video_path), "-vframes", "1",
                       "-q:v", "2", str(tmp / "frame_first.jpg"), "-y"]),
        ]
        if duration > 4:
            mid = str(duration / 2)
            frame_specs.append((
                "mid", ["ffmpeg", "-ss", mid, "-i", str(video_path),
                        "-vframes", "1", "-q:v", "2", str(tmp / "frame_mid.jpg"), "-y"],
            ))
        frame_specs.append((
            "last", ["ffmpeg", "-sseof", "-0.5", "-i", str(video_path),
                     "-vframes", "1", "-q:v", "2", str(tmp / "frame_last.jpg"), "-y"],
        ))

        for label, cmd in frame_specs:
            out = tmp / f"frame_{label}.jpg"
            try:
                subprocess.run(cmd, capture_output=True, timeout=60)
                if out.exists():
                    frames.append(out.read_bytes())
            except Exception as e:
                log.warning(f"  ffmpeg frame-udtræk ({label}) fejl: {e}")

    return frames


def scan_qdrant_for_media(collection: str) -> list[str]:
    """Find source_files i Qdrant med file_type: image eller video."""
    source_files: list[str] = []
    all_exts = MEDIA_IMAGE_EXTS | MEDIA_VIDEO_EXTS
    offset = None
    while True:
        body: dict = {
            "limit": SCROLL_PAGESIZE,
            "with_payload": ["source_file"],
            "with_vector": False,
            "filter": {"should": [
                {"key": "file_type", "match": {"any": ["image", "video"]}},
            ]},
        }
        if offset:
            body["offset"] = offset
        try:
            r = httpx.post(
                f"{QDRANT_URL}/collections/{collection}/points/scroll",
                json=body, timeout=10.0,
            )
            result = r.json().get("result", {})
            for pt in result.get("points", []):
                sf = pt["payload"].get("source_file", "")
                if sf and Path(sf).suffix.lower() in all_exts:
                    source_files.append(sf)
            offset = result.get("next_page_offset")
            if not offset:
                break
        except Exception as e:
            log.warning(f"  Qdrant medie-scan fejl ({collection}): {e}")
            break
    return source_files


def run_media_analysis(wings: list) -> None:
    log.info("\n── Medie-analyse (Gemma 4 multimodal) ──")

    analyzed = load_analyzed_media()
    wing_lookup = {w["name"]: w for w in wings}
    all_exts   = MEDIA_IMAGE_EXTS | MEDIA_VIDEO_EXTS

    # Saml kandidater: (wing_name, collection, filepath, scope)
    candidates: list[tuple[str, str, Path, str]] = []
    seen_names: set[str] = set()

    for wing_name, incoming_dirs in MEDIA_WINGS_DIRS.items():
        if wing_name not in wing_lookup:
            log.warning(f"  Wing {wing_name!r} ikke i wings.json — springer over")
            continue
        entry      = wing_lookup[wing_name]
        collection = entry["collection"]
        scope      = entry["scope"]

        # 1) Qdrant-scan (for fremtidig kompatibilitet når ingest.py håndterer billeder)
        for sf in scan_qdrant_for_media(collection):
            if sf in seen_names:
                continue
            for d in incoming_dirs:
                hits = list(d.rglob(sf))
                if hits:
                    candidates.append((wing_name, collection, hits[0], scope))
                    seen_names.add(sf)
                    break

        # 2) Direkte filsystem-scan
        for d in incoming_dirs:
            if not d.exists():
                continue
            for fp in d.rglob("*"):
                if fp.is_file() and fp.suffix.lower() in all_exts and fp.name not in seen_names:
                    candidates.append((wing_name, collection, fp, scope))
                    seen_names.add(fp.name)

    log.info(f"  {len(candidates)} mediefiler fundet")

    MEDIA_PROMPT = (
        "Do not think. Beskriv præcist hvad du ser. "
        "Hvad er dokumenteret? Er der tekst, personer, steder eller hændelser? "
        "Svar på dansk."
    )
    count = 0

    for wing_name, collection, filepath, scope in candidates:
        if count >= MAX_MEDIA_PER_NIGHT:
            log.info(f"  Maks {MAX_MEDIA_PER_NIGHT} mediefiler nået — stopper")
            break

        filename = filepath.name

        if filename in analyzed:
            log.debug(f"  Skip (allerede analyseret): {filename}")
            continue

        if media_analysis_exists_qdrant(collection, filename):
            log.info(f"  Skip (analyse i Qdrant): {filename}")
            analyzed[filename] = datetime.now(timezone.utc).isoformat()
            continue

        log.info(f"  Analyserer: {filename} ({wing_name})")
        ext = filepath.suffix.lower()

        try:
            if ext in MEDIA_IMAGE_EXTS:
                analysis_text = call_llava_completion(filepath.read_bytes(), MEDIA_PROMPT)

            elif ext in MEDIA_VIDEO_EXTS:
                frames = extract_video_frames(filepath)
                if not frames:
                    log.warning(f"  Ingen frames ekstraheret fra {filename}")
                    continue
                frame_texts: list[str] = []
                for i, frame_bytes in enumerate(frames, 1):
                    try:
                        desc = call_llava_completion(frame_bytes, MEDIA_PROMPT)
                        if desc:
                            frame_texts.append(f"Frame {i}/{len(frames)}: {desc}")
                    except Exception as e:
                        log.warning(f"  Frame {i} analyse fejl: {e}")
                if not frame_texts:
                    log.warning(f"  Ingen frame-analyser lykkedes for {filename}")
                    continue
                analysis_text = "\n\n".join(frame_texts)
            else:
                continue

        except Exception as e:
            log.warning(f"  Medie-analyse fejl ({filename}): {e}")
            continue

        if not analysis_text:
            log.warning(f"  Tom analyse for {filename}")
            continue

        now      = datetime.now(timezone.utc).isoformat()
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{collection}:{filename}:image_analysis"))
        try:
            vec = embed(analysis_text[:8192])
            upsert_point(wing_name, scope, point_id, vec, {
                "type":        "image_analysis",
                "source_file": filename,
                "wing":        wing_name,
                "scope":       scope,
                "text":        analysis_text,
                "timestamp":   now,
            })
            log.info(f"  Analyse gemt ({len(analysis_text)} tegn)")
            analyzed[filename] = now
            count += 1
        except Exception as e:
            log.error(f"  Gem analyse fejl ({filename}): {e}")

    save_analyzed_media(analyzed)
    log.info(f"  Medie-analyse færdig — {count} analyseret, {len(candidates) - count} sprunget over")


# ── Lydfilanalyse: hjælpefunktioner ──────────────────────────────────────────
def load_analyzed_audio() -> dict:
    try:
        if ANALYZED_AUDIO_FILE.exists():
            return json.loads(ANALYZED_AUDIO_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Kunne ikke læse analyzed_audio.json: {e}")
    return {}


def save_analyzed_audio(data: dict) -> None:
    try:
        ANALYZED_AUDIO_FILE.parent.mkdir(parents=True, exist_ok=True)
        ANALYZED_AUDIO_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"Kunne ikke gemme analyzed_audio.json: {e}")


def audio_transcription_exists_qdrant(collection: str, source_file: str) -> bool:
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/scroll",
            json={
                "filter": {"must": [
                    {"key": "source_file", "match": {"value": source_file}},
                    {"key": "type",        "match": {"value": "audio_transcription"}},
                ]},
                "limit": 1,
                "with_payload": False,
                "with_vector": False,
            },
            timeout=10.0,
        )
        return bool(r.json().get("result", {}).get("points"))
    except Exception:
        return False


def transcribe_audio_file(filepath: Path) -> str:
    """Send lydfil til speaches (faster-whisper) på NX:8182 og returnér transskription."""
    suffix = filepath.suffix.lower().lstrip(".")
    mime_map = {
        "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
        "ogg": "audio/ogg", "flac": "audio/flac", "aac": "audio/aac",
    }
    mime = mime_map.get(suffix, "audio/mpeg")
    with open(filepath, "rb") as f:
        r = httpx.post(
            f"{NX_SPEACHES_URL}/v1/audio/transcriptions",
            files={"file": (filepath.name, f, mime)},
            data={"model": "large-v3", "language": "da", "response_format": "json"},
            timeout=AUDIO_TIMEOUT,
        )
    r.raise_for_status()
    result = r.json()
    return (result.get("text") or "").strip()


def run_audio_analysis(wings: list) -> None:
    log.info("\n── Lydfilanalyse (faster-whisper via speaches) ──")

    analyzed  = load_analyzed_audio()
    wing_lookup = {w["name"]: w for w in wings}
    count     = 0

    candidates: list[tuple[str, str, Path, str]] = []
    seen_names: set[str] = set()

    for wing_name, incoming_dirs in AUDIO_WINGS_DIRS.items():
        if wing_name not in wing_lookup:
            continue
        entry      = wing_lookup[wing_name]
        collection = entry["collection"]
        scope      = entry["scope"]

        for d in incoming_dirs:
            if not d.exists():
                continue
            for fp in d.rglob("*"):
                if fp.is_file() and fp.suffix.lower() in AUDIO_EXTS and fp.name not in seen_names:
                    candidates.append((wing_name, collection, fp, scope))
                    seen_names.add(fp.name)

    log.info(f"  {len(candidates)} lydfiler fundet")

    for wing_name, collection, filepath, scope in candidates:
        if count >= MAX_AUDIO_PER_NIGHT:
            log.info(f"  Maks {MAX_AUDIO_PER_NIGHT} lydfiler nået — stopper")
            break

        filename = filepath.name

        if filename in analyzed:
            log.debug(f"  Skip (allerede transskriberet): {filename}")
            continue

        if audio_transcription_exists_qdrant(collection, filename):
            log.info(f"  Skip (transskription i Qdrant): {filename}")
            analyzed[filename] = datetime.now(timezone.utc).isoformat()
            continue

        log.info(f"  Transskriberer: {filename} ({wing_name}, {filepath.stat().st_size // 1024} KB)")
        try:
            text = transcribe_audio_file(filepath)
        except Exception as e:
            log.warning(f"  Speaches fejl ({filename}): {e}")
            continue

        if not text:
            log.warning(f"  Tom transskription for {filename}")
            continue

        now      = datetime.now(timezone.utc).isoformat()
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{collection}:{filename}:audio_transcription"))
        try:
            vec = embed(text[:8192])
            upsert_point(wing_name, scope, point_id, vec, {
                "type":        "audio_transcription",
                "source_file": filename,
                "wing":        wing_name,
                "scope":       scope,
                "text":        text,
                "sprog":       "da",
                "timestamp":   now,
            })
            log.info(f"  Transskription gemt ({len(text)} tegn)")
            analyzed[filename] = now
            count += 1
        except Exception as e:
            log.error(f"  Gem transskription fejl ({filename}): {e}")

    save_analyzed_audio(analyzed)
    log.info(f"  Lydfilanalyse færdig — {count} transskriberet, {len(candidates) - count} sprunget over")


# ── NX fan-kontrol ───────────────────────────────────────────────────────────
NX_HOST     = "nous@YOUR_NX_HOST"
NX_FAN_PATH = "/sys/devices/platform/pwm-fan/hwmon/hwmon1/pwm1"
NX_FAN_MAX  = "255"
NX_FAN_NORM = "154"


def nx_fan(pwm: str) -> None:
    try:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             NX_HOST, f"echo {pwm} | sudo tee {NX_FAN_PATH}"],
            capture_output=True, timeout=10,
        )
        log.info(f"NX fan sat til {pwm}")
    except Exception as e:
        log.warning(f"NX fan-kontrol fejlede: {e}")


def release_day_model() -> None:
    day_model = _get_day_model()
    log.info(f"Frigiver GPU: sender KEEP_ALIVE=0 til {day_model}...")
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": day_model, "prompt": "", "keep_alive": 0, "stream": False},
            timeout=30.0,
        )
        r.raise_for_status()
        log.info(f"GPU frigivet ({day_model} unloaded)")
    except Exception as e:
        log.warning(f"release_day_model fejlede: {e}")
    time.sleep(5)


def warmup_day_model() -> None:
    day_model = _get_day_model()
    log.info(f"Varmer dagmodel op: sender KEEP_ALIVE=-1 + prompt til {day_model}...")
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": day_model, "prompt": "hej", "keep_alive": -1, "stream": False},
            timeout=120.0,
        )
        r.raise_for_status()
        log.info(f"Dagmodel varm ({day_model} loaded, keep_alive=-1)")
    except Exception as e:
        log.warning(f"warmup_day_model fejlede: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    start = datetime.now()
    log.info("=" * 60)
    log.info(f"NOUS Night Pipeline starter — {start.strftime('%Y-%m-%d %H:%M')}")

    release_day_model()
    nx_fan(NX_FAN_MAX)

    try:
        wings_data = json.loads(WINGS_FILE.read_text(encoding="utf-8"))
        wings      = wings_data.get("wings", [])

        model, night_params = resolve_model()
        log.info(f"LLM-model: {model}, params: {night_params}")

        kuzu_conn = kuzu_init()
        log.info("Kuzu graph DB initialiseret")

        total_processed = 0
        total_skipped   = 0

        for wing_entry in wings:
            wing       = wing_entry["name"]
            collection = wing_entry["collection"]
            scope      = wing_entry["scope"]

            log.info(f"\n── Wing: {wing} ({collection}) ──")

            try:
                source_files = get_all_source_files(collection)
            except Exception as e:
                log.error(f"  Kunne ikke hente source_files: {e}")
                continue

            log.info(f"  {len(source_files)} dokumenter fundet")

            for sf in source_files:
                if summary_exists(collection, sf):
                    log.debug(f"  Skip (summary eksisterer): {sf!r}")
                    total_skipped += 1
                    continue

                log.info(f"  Processerer: {sf!r}")
                ok = process_document(collection, sf, wing, scope, model, kuzu_conn, params=night_params)
                if ok:
                    total_processed += 1
                time.sleep(10)

        # Cross-document analyse (kører kun søndage — samme kadence som scraper)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        is_sunday = datetime.now(timezone.utc).weekday() == 6
        if not is_sunday:
            log.info("\n── Cross-analyse: springer over (kører kun søndage) ──")
        else:
            for wing_entry in wings:
                wing = wing_entry["name"]
                if wing not in CROSS_ANALYSIS_WINGS:
                    continue
                collection = wing_entry["collection"]
                scope      = wing_entry["scope"]
                log.info(f"\n── Cross-analyse: {wing} ──")
                if cross_analysis_exists(collection, wing, today):
                    log.info(f"  Cross-analyse allerede kørt i dag — springer over")
                    continue
                run_cross_analysis(collection, wing, scope, model, params=night_params)

        # Inkonsistens-analyse (kun boernesag, kører efter cross-analyse)
        for wing_entry in wings:
            if wing_entry["name"] != "boernesag":
                continue
            collection = wing_entry["collection"]
            scope      = wing_entry["scope"]
            log.info("\n── Inkonsistens-analyse: boernesag ──")
            if inconsistency_analysis_exists(collection, today):
                log.info("  Inkonsistens-analyse allerede kørt i dag — springer over")
                continue
            run_inconsistency_analysis(collection, scope, model, params=night_params)

        # Backfill v2-facts for eksisterende dokumenter
        backfill_facts_pass(wings, model)

        # Billede- og videoanalyse via Gemma 4 multimodal
        run_media_analysis(wings)

        # Lydfilanalyse via faster-whisper (speaches på NX:8182)
        run_audio_analysis(wings)

        # Swarm promotion batch (kører efter inkonsistens-analyse)
        log.info("\n── Swarm Promotion Batch ──")
        try:
            sys.path.insert(0, "/srv/nous/swarm")
            from promotion import run_promotion_batch
            run_promotion_batch(max_facts=20)
        except Exception as e:
            log.error(f"  Swarm promotion fejl: {e}")

        elapsed = (datetime.now() - start).total_seconds()
        log.info(f"\n{'=' * 60}")
        log.info(f"Færdig — {total_processed} processeret, {total_skipped} sprunget over")
        log.info(f"Samlet tid: {elapsed:.0f}s")
        log.info("=" * 60)
    finally:
        nx_fan(NX_FAN_NORM)
        log.info("NX fan tilbage til normal")
        warmup_day_model()


if __name__ == "__main__":
    main()
