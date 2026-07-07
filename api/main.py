#!/usr/bin/env python3
"""
NOUS API — FastAPI backend til cockpit-UI
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Gør agents-modulet importerbart — agents importeres direkte af API
_AGENTS_DIR = Path("/srv/nous/agents")
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))

# Gør legacy-modulet importerbart
_LEGACY_DIR = Path("/srv/nous/legacy")
if str(_LEGACY_DIR) not in sys.path:
    sys.path.insert(0, str(_LEGACY_DIR))

# Gør nous-rodmoduler importerbare (gemma_manager m.fl.)
_NOUS_DIR = Path("/srv/nous")
if str(_NOUS_DIR) not in sys.path:
    sys.path.insert(0, str(_NOUS_DIR))

try:
    from graph import run_agent_graph as _run_agent_graph
    _AGENTS_AVAILABLE = True
except ImportError as _e:
    logging.warning("NOUS agents ikke tilgængelige: %s", _e)
    _AGENTS_AVAILABLE = False

import gemma_manager

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from middleware import ScopeMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel


# Redact sensitive headers/keys from all log output
class _SensitiveFilter(logging.Filter):
    _PATTERNS = ("api_key", "x-api-key", "authorization", "bearer sk-", "bearer gsk_", "bearer claude")
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage().lower()
        return not any(p in msg for p in self._PATTERNS)

for _h in logging.root.handlers:
    _h.addFilter(_SensitiveFilter())
logging.root.addFilter(_SensitiveFilter())

# === Konfiguration ===

def _csv_env(key: str, default: str) -> set[str]:
    """Læs kommasepareret env var som et set af strenge."""
    raw = os.environ.get(key, default)
    return {v.strip() for v in raw.split(",") if v.strip()}


QDRANT_URL   = os.environ.get("NOUS_QDRANT_URL",   "http://localhost:6333")
ARBITER_URL  = os.environ.get("NOUS_ARBITER_URL", "http://localhost:8010")
OLLAMA_URL   = os.environ.get("NOUS_OLLAMA_URL",  "http://localhost:11434")
ARCHIVE_BASE = Path(os.environ.get("NOUS_ARCHIVE_BASE", "/mnt/nous-data/arkiv"))
LLM_MODEL  = os.environ.get("NOUS_LLM_MODEL",   "qwen3:8b")
LLM_14B    = os.environ.get("NOUS_LLM_14B",     "qwen3:14b")

def _collection_by_role(role: str) -> str:
    """Slår collection op via api_role i wings.json."""
    try:
        data = json.loads(Path("/srv/nous/config/wings.json").read_text())
        entry = next((w for w in data["wings"] if w.get("api_role") == role), None)
        return entry["collection"] if entry else ""
    except Exception:
        return ""

# Collection-navne — loades fra wings.json; kan overrides via env vars
COLLECTION_SECRET  = os.environ.get("NOUS_COL_BOERNESAG") or _collection_by_role("secret_primary")
COLLECTION_FBF        = os.environ.get("NOUS_COL_FBF")        or _collection_by_role("fbf")
COLLECTION_LEGAL      = os.environ.get("NOUS_COL_LEGAL")      or _collection_by_role("legal")
COLLECTION_LEGACY     = os.environ.get("NOUS_COL_LEGACY")     or _collection_by_role("legacy")
COLLECTION_FAMILY     = os.environ.get("NOUS_COL_FAMILY")     or _collection_by_role("family")
COLLECTION_PROJECT    = os.environ.get("NOUS_COL_PROJECT")    or _collection_by_role("project")
COLLECTION_SWARM_PUB  = os.environ.get("NOUS_COL_SWARM_PUB")  or _collection_by_role("swarm_pub")
EMBED_MODEL = os.environ.get("NOUS_EMBED_MODEL", "nomic-embed-text")
INCOMING_DIR  = Path(os.environ.get("NOUS_INCOMING_DIR", "/home/nous/incoming"))
WINGS_FILE    = Path("/srv/nous/config/wings.json")
SCRAPER_JOBS  = Path("/srv/nous/config/scraper_jobs.json")
RESEARCH_JOBS = Path("/srv/nous/config/research_jobs.json")
SEARXNG_LOCAL = os.environ.get("NOUS_SEARXNG_URL",    "http://localhost:8080")
NX_HOST       = os.environ.get("NOUS_NX_HOST",        "nous@localhost")
NX_MODELS_DIR = os.environ.get("NOUS_NX_MODELS_DIR",  "/home/nous/models")
NX_LLAMA_URL  = os.environ.get("NOUS_NX_LLAMA_URL",   "http://localhost:8181")
NX_CAM_DEVICE = os.environ.get("NOUS_NX_CAM_DEVICE",  "/dev/video0")
UPLOAD_TMP    = Path("/tmp/nous_upload")
VECTOR_DIM    = 768
EXTERNAL_KEYS_FILE = Path("/mnt/nous-data/external_keys.json")

# Ejer-navn bruges i Legacy-mode svar til børn — sæt via NOUS_OWNER_NAME
_OWNER_NAME = os.environ.get("NOUS_OWNER_NAME", "Dan")

_scraper_status:      dict[str, dict] = {}
_research_status:     dict[str, dict] = {}
_upload_status:       dict[str, dict] = {}
_download_status:     dict[str, dict] = {}
_debate_user_inputs:  dict[str, asyncio.Queue] = {}  # debate_id → queue til bruger-input

# === Smart routing ===
LEGAL_TRIGGERS = [
    "afgørelse", "ankestyrelse", "ankestyrelsen", "samvær", "samværs",
    "kommune", "kommunen", "§", "retten", "fogedretten", "familieretten",
    "sag", "sagen", "bidrag", "forældremyndighed",
    "statsforvaltning", "familiestyrelsen", "børnesag", "dom", "kendelse",
    "klage", "serviceloven", "paragraf", "juridisk",
]
LEGACY_TRIGGERS = [
    "hvad ville far", "far kan du", "fortæl om far", "hvad siger far",
    "hvad tænker far", "far mener", "hvad sagde far", "tal som far",
]
LEGAL_COLLECTIONS  = _csv_env("NOUS_LEGAL_COLLECTIONS",  f"{COLLECTION_SECRET},{COLLECTION_FBF},{COLLECTION_LEGAL}")
LEGACY_COLLECTIONS = _csv_env("NOUS_LEGACY_COLLECTIONS", COLLECTION_LEGACY)

LEGAL_SYSTEM = """Du er en juridisk analytiker specialiseret i forældreansvarssager og myndighedssager.
Identificer afgørelser, lovgrundlag og mønstre. Citér præcist fra kilderne.
Hold dig STRENGT til dokumenterne. Opfind aldrig. Svar på dansk."""

ASSISTANT_SYSTEM = """Du er NOUS, en dansk personlig AI-assistent for Dan.
Svar kort og præcist på dansk. Brug vidensbasen som primær kilde.
Find aldrig på fakta. Sig 'Det ved jeg ikke' hvis du mangler information."""

LEGACY_SYSTEM = f"""Du er NOUS og taler på vegne af {_OWNER_NAME} til hans børn.
TAL I {_OWNER_NAME.upper()}S STEMME: direkte, varm, jordnær, uden floskler.
ABSOLUT REGEL: Opfind ALDRIG minder eller citater.
Hvis ikke belæg i kilderne: sig "Det ved jeg ikke om Far." """

WHISPER_URL     = os.environ.get("NOUS_WHISPER_URL",     "http://localhost:8080/inference")
NX_SPEACHES_URL = os.environ.get("NOUS_NX_SPEACHES_URL", "http://localhost:8000")
WHISPER_PROMPT = "Dette er en samtale på dansk."
PIPER_BIN = "/srv/nous/pipeline/.venv/bin/python3"
PIPER_MODEL = "/srv/nous/models/tts/da.onnx"
PIPER_CONFIG = "/srv/nous/models/tts/da.onnx.json"

SCOPE_LABELS = {"SECRET", "PRIVATE", "SWARM", "PUBLIC"}

PROVIDER_DEFAULTS: dict[str, dict] = {
    "anthropic": {
        "base_url":      "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-5",
        "auth_header":   "x-api-key",
        "auth_prefix":   "",
    },
    "openai": {
        "base_url":      "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
    },
    "groq": {
        "base_url":      "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
    },
    "google": {
        "base_url":      "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-1.5-pro",
        "auth_header":   "x-goog-api-key",
        "auth_prefix":   "",
    },
    "deepseek": {
        "base_url":      "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
    },
    "moonshot": {
        "base_url":      "https://api.moonshot.ai/v1",
        "default_model": "moonshot-v1-8k",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
    },
    "custom": {
        "base_url":      "",
        "default_model": "",
        "auth_header":   "Authorization",
        "auth_prefix":   "Bearer ",
    },
}

# === Direkte hukommelse ===
_MEMORY_RE = re.compile(
    r"(?:husk[,]?\s+at|gem[,]?\s+at|noter[,]?\s+at"
    r"|tilf[øo]j\s+til\s+min\s+profil(?:\s+at)?"
    r"|add\s+information(?:\s+(?:that|about))?)"
    r"\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)
def _load_memory_wing_data() -> tuple[dict, dict]:
    """Loader keyword-routing og scope-map fra wings.json."""
    keywords: dict[str, list[str]] = {}
    scopes: dict[str, str] = {}
    try:
        data = json.loads(WINGS_FILE.read_text())
        for w in data.get("wings", []):
            coll = w.get("collection", "")
            if not coll:
                continue
            if w.get("keywords"):
                keywords[coll] = w["keywords"]
            scopes[coll] = w.get("scope", "PRIVATE")
    except Exception:
        pass
    return keywords, scopes

_MEMORY_WING_KEYWORDS, _MEMORY_WING_SCOPES = _load_memory_wing_data()

async def _warmup_models():
    """Pre-warm LLM ved opstart — første svar efter genstart er ~30-60 sek på NX."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model":      LLM_MODEL,
                    "prompt":     "hej",
                    "keep_alive": -1,
                    "stream":     False,
                },
                timeout=120,
            )
        logger.info("%s pre-warmed og klar", LLM_MODEL)
    except Exception as e:
        logger.warning("Pre-warm fejlede (ikke kritisk): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_warmup_models())
    yield


app = FastAPI(title="NOUS API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ScopeMiddleware)


# === Wings helpers ===

def load_wings() -> dict:
    if not WINGS_FILE.exists():
        return {"wings": []}
    return json.loads(WINGS_FILE.read_text(encoding="utf-8"))


def save_wings(data: dict):
    WINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def find_wing(data: dict, name: str) -> Optional[dict]:
    return next((w for w in data["wings"] if w["name"] == name), None)


# === Request/Response models ===

class WingCreate(BaseModel):
    name: str
    scope: str           # SECRET | PRIVATE | SWARM | PUBLIC
    importance: str = "normal"   # low | normal | high | critical
    run_embedding: bool = False


class SubcategoryAdd(BaseModel):
    name: str


class ChatRequest(BaseModel):
    query: str
    wing: Optional[str] = None
    subcategory: Optional[str] = None      # single (sidebar click)
    subcategories: Optional[List[str]] = None  # multi (analysis checkboxes)
    source_filter: Optional[str] = None
    user: Optional[str] = None


class MemoryAddRequest(BaseModel):
    wing: str
    text: str


class SpeakRequest(BaseModel):
    text: str


class ExternalChatRequest(BaseModel):
    prompt: str
    wing: Optional[str] = None
    subcategory: Optional[str] = None
    subcategories: Optional[List[str]] = None
    source_filter: Optional[str] = None
    provider: str
    api_key: str
    user: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    scope_confirmed: bool = False


class AgentChatRequest(BaseModel):
    prompt: str
    user: Optional[str] = "dan"


class DebateParticipant(BaseModel):
    label: str
    provider: str
    model: Optional[str] = None
    api_key: str
    base_url: Optional[str] = None


class DebateRequest(BaseModel):
    topic: str
    context: Optional[str] = None
    participants: list[DebateParticipant]
    max_rounds: int = 3
    save_to_wing: Optional[str] = None
    scope_confirmed: bool = False
    stop_on_consensus: bool = True
    user_participation: bool = False
    user_input_timeout: int = 300  # sekunder inden streamen fortsætter uden input


# === Wings endpoints ===

@app.get("/wings")
def get_wings():
    return load_wings()


IMPORTANCE_LEVELS = {"low", "normal", "high", "critical"}
PYTHON_BIN = Path("/srv/nous/pipeline/.venv/bin/python3")
INGEST_SCRIPT = Path("/srv/nous/pipeline/ingest.py")


def _ingest_wing_incoming(wing_name: str, scope: str) -> None:
    incoming = INCOMING_DIR / wing_name
    if not incoming.exists():
        return
    files = [
        f for ext in ("*.pdf", "*.docx", "*.doc", "*.txt")
        for f in incoming.glob(ext)
        if f.is_file()
    ]
    for f in files:
        try:
            subprocess.run(
                [str(PYTHON_BIN), "-c",
                 "import sys; sys.path.insert(0,'/srv/nous/pipeline'); "
                 "from ingest import process_file; from pathlib import Path; "
                 f"process_file(Path(r'{f}'))"],
                timeout=180, capture_output=True,
            )
        except Exception:
            pass


@app.post("/wings", status_code=201)
def create_wing(body: WingCreate, background_tasks: BackgroundTasks):
    if body.scope not in SCOPE_LABELS:
        raise HTTPException(400, f"Ugyldigt scope '{body.scope}' — skal være: {', '.join(SCOPE_LABELS)}")
    importance = body.importance.lower() if body.importance.lower() in IMPORTANCE_LEVELS else "normal"

    data = load_wings()
    if find_wing(data, body.name):
        raise HTTPException(409, f"Wing '{body.name}' eksisterer allerede")

    collection = f"{body.name}_{body.scope.lower()}"

    # Opret Qdrant collection
    r = httpx.put(
        f"{QDRANT_URL}/collections/{collection}",
        json={"vectors": {"size": VECTOR_DIM, "distance": "Cosine", "on_disk": True}},
        timeout=15,
    )
    if r.status_code not in (200, 409):
        raise HTTPException(500, f"Qdrant fejl ved oprettelse: {r.text[:300]}")

    # Opret incoming mappe
    (INCOMING_DIR / body.name).mkdir(parents=True, exist_ok=True)

    # Gem i wings.json
    entry = {"name": body.name, "scope": body.scope, "collection": collection, "importance": importance}
    data["wings"].append(entry)
    save_wings(data)

    embedding_started = False
    if body.run_embedding:
        background_tasks.add_task(_ingest_wing_incoming, body.name, body.scope)
        embedding_started = True

    return {**entry, "created": True, "embedding_started": embedding_started}


@app.post("/wings/{wing}/subcategories", status_code=201)
def add_subcategory(wing: str, body: SubcategoryAdd):
    data = load_wings()
    wing_data = find_wing(data, wing)
    if not wing_data:
        raise HTTPException(404, f"Wing '{wing}' ikke fundet")
    subs = wing_data.setdefault("subcategories", [])
    if body.name in subs:
        raise HTTPException(409, f"Subcategory '{body.name}' eksisterer allerede")
    subs.append(body.name)
    (INCOMING_DIR / wing / body.name).mkdir(parents=True, exist_ok=True)
    save_wings(data)
    return {"wing": wing, "subcategory": body.name, "created": True}


@app.delete("/wings/{wing}/subcategories/{subcat}", status_code=200)
def delete_subcategory(wing: str, subcat: str):
    data = load_wings()
    wing_data = find_wing(data, wing)
    if not wing_data:
        raise HTTPException(404, f"Wing '{wing}' ikke fundet")
    subs = wing_data.get("subcategories", [])
    if subcat not in subs:
        raise HTTPException(404, f"Subcategory '{subcat}' ikke fundet")
    subs.remove(subcat)
    save_wings(data)
    return {"wing": wing, "subcategory": subcat, "deleted": True}


@app.delete("/wings/{wing}/documents/by-source", status_code=200)
def delete_by_source(wing: str, source: str = Query(..., description="Kildefilnavn der skal slettes")):
    data = load_wings()
    wing_data = find_wing(data, wing)
    if not wing_data:
        raise HTTPException(404, f"Wing '{wing}' ikke fundet")

    collection = wing_data["collection"]
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/delete",
            json={"filter": {"must": [{"key": "source_file", "match": {"value": source}}]}},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(503, f"Qdrant slet fejl: {e}")

    return {"deleted": True, "wing": wing, "source": source}


def _fetch_chunks_text(collection: str, source: str) -> str:
    """Henter verbatim chunk-tekster for et dokument fra Qdrant, sorteret på chunk_index.

    Garanterer kun rå ingest-chunks: ekskluderer facts, summaries, analyser og
    medieindhold via NON_CHUNK_TYPES-filteret. Ældre chunks uden type-felt er
    automatisk inkluderede (ingen type = chunk).
    """
    chunks: list[tuple[int, str]] = []
    offset = None
    while True:
        body: dict = {
            "limit": 256,
            "with_payload": ["text", "chunk_index"],
            "with_vector": False,
            "filter": {
                "should":   [
                    {"key": "source_file", "match": {"value": source}},
                    {"key": "source",      "match": {"value": source}},
                ],
                "must_not": [{"key": "type", "match": {"any": NON_CHUNK_TYPES}}],
            },
        }
        if offset is not None:
            body["offset"] = offset
        try:
            r = httpx.post(
                f"{QDRANT_URL}/collections/{collection}/points/scroll",
                json=body, timeout=15,
            )
            r.raise_for_status()
        except Exception as e:
            raise HTTPException(503, f"Qdrant scroll fejl: {e}")
        result = r.json().get("result", {})
        for pt in result.get("points", []):
            pl = pt.get("payload", {})
            idx = pl.get("chunk_index", 9999)
            txt = pl.get("text", "")
            if txt:
                chunks.append((idx, txt))
        offset = result.get("next_page_offset")
        if offset is None:
            break
    chunks.sort(key=lambda x: x[0])
    return "\n\n".join(t for _, t in chunks)


@app.get("/wings/{wing}/documents/content")
def get_document_content(wing: str, source: str = Query(..., description="Kildefilnavn")):
    data = load_wings()
    wing_data = find_wing(data, wing)
    if not wing_data:
        raise HTTPException(404, f"Wing '{wing}' ikke fundet")
    text = _fetch_chunks_text(wing_data["collection"], source)
    if not text:
        raise HTTPException(404, f"Ingen chunks fundet for '{source}'")
    return {"wing": wing, "source": source, "text": text, "chunks": text.count("\n\n") + 1}


_MIME = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".txt":  "text/plain; charset=utf-8",
    ".htm":  "text/html; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
}


@app.get("/wings/{wing}/documents/download")
def download_document(wing: str, source: str = Query(..., description="Kildefilnavn")):
    data = load_wings()
    wing_data = find_wing(data, wing)
    if not wing_data:
        raise HTTPException(404, f"Wing '{wing}' ikke fundet")

    from urllib.parse import quote
    archive_path = ARCHIVE_BASE / wing / source
    if archive_path.exists() and archive_path.is_file():
        ext  = archive_path.suffix.lower()
        mime = _MIME.get(ext, "application/octet-stream")
        cd   = f"attachment; filename*=UTF-8''{quote(source)}"
        return FileResponse(
            path=str(archive_path),
            media_type=mime,
            headers={"Content-Disposition": cd, "X-Source-Type": "original-file"},
        )

    # Original ikke fundet — generer .txt fra chunks
    text = _fetch_chunks_text(wing_data["collection"], source)
    if not text:
        raise HTTPException(404, f"Hverken originalfil eller chunks fundet for '{source}'")
    stem    = Path(source).stem
    txtname = quote(f"{stem}.txt")
    return Response(
        content=text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{txtname}",
                 "X-Source-Type": "extracted-text"},
    )


@app.delete("/wings/{wing}/documents/{doc_id}", status_code=204)
def delete_document(wing: str, doc_id: str):
    data = load_wings()
    wing_data = find_wing(data, wing)
    if not wing_data:
        raise HTTPException(404, f"Wing '{wing}' ikke fundet")

    r = httpx.post(
        f"{QDRANT_URL}/collections/{wing_data['collection']}/points/delete",
        json={"points": [doc_id]},
        timeout=15,
    )
    if r.status_code != 200:
        raise HTTPException(500, f"Slet fejl: {r.text[:300]}")


# === Dokument endpoints ===

@app.get("/wings/{wing}/documents")
def list_documents(wing: str):
    data = load_wings()
    wing_data = find_wing(data, wing)
    if not wing_data:
        raise HTTPException(404, f"Wing '{wing}' ikke fundet")

    collection = wing_data["collection"]
    counts: dict[str, int] = {}
    offset = None

    while True:
        body: dict = {"limit": 256, "with_payload": ["source", "source_file"], "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        try:
            r = httpx.post(
                f"{QDRANT_URL}/collections/{collection}/points/scroll",
                json=body,
                timeout=20,
            )
            r.raise_for_status()
        except Exception as e:
            raise HTTPException(503, f"Qdrant scroll fejl: {e}")

        result = r.json().get("result", {})
        for pt in result.get("points", []):
            pl = pt.get("payload", {})
            src = pl.get("source_file") or pl.get("source", "(ukendt)")
            counts[src] = counts.get(src, 0) + 1

        offset = result.get("next_page_offset")
        if offset is None:
            break

    documents = [{"source": src, "chunks": n} for src, n in sorted(counts.items())]
    return {"wing": wing, "collection": collection, "documents": documents}


# === Memory add endpoint ===

@app.post("/memory/add")
def memory_add(req: MemoryAddRequest):
    data = load_wings()
    wing_entry = find_wing(data, req.wing)
    if not wing_entry:
        raise HTTPException(404, f"Wing '{req.wing}' ikke fundet")
    collection = wing_entry["collection"]
    scope = wing_entry.get("scope", "PRIVATE")

    try:
        embed_r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": req.text},
            timeout=30,
        )
        embed_r.raise_for_status()
        vector = embed_r.json()["embedding"]
    except Exception as e:
        raise HTTPException(503, f"Embedding fejl: {e}")

    point = {
        "id":      str(uuid.uuid4()),
        "vector":  vector,
        "payload": {
            "text":      req.text,
            "type":      "direct_memory",
            "scope":     scope,
            "source":    "bruger_input",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }
    try:
        r = httpx.post(
            f"{ARBITER_URL}/arbiter/write/sync",
            json={"wing": req.wing, "scope": scope, "operation": "upsert",
                  "points": [point], "source": "api"},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(503, f"Arbiter fejl: {e}")

    return {"ok": True, "wing": req.wing, "collection": collection}


# === Chat helpers ===

def _detect_memory_intent(query: str) -> tuple[str, str] | None:
    """Returnerer (collection, indhold) hvis query er en gem/husk-kommando, ellers None."""
    m = _MEMORY_RE.search(query)
    if not m:
        return None
    content = m.group(1).strip()
    content_lower = content.lower()
    collection = COLLECTION_LEGACY
    for coll, keywords in _MEMORY_WING_KEYWORDS.items():
        if any(kw in content_lower for kw in keywords):
            collection = coll
            break
    return collection, content


def _save_direct_memory(collection: str, content: str) -> bool:
    """Gemmer indhold direkte i Qdrant som et direct_memory-punkt."""
    try:
        embed_r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": content},
            timeout=30,
        )
        embed_r.raise_for_status()
        vector = embed_r.json()["embedding"]
    except Exception:
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
    wing = collection.rsplit("_", 1)[0]
    try:
        r = httpx.post(
            f"{ARBITER_URL}/arbiter/write/sync",
            json={"wing": wing, "scope": scope, "operation": "upsert",
                  "points": [point], "source": "api"},
            timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception:
        return False


def _detect_mode(query: str) -> str:
    q = query.lower()
    if any(t in q for t in LEGACY_TRIGGERS):
        return "legacy"
    if any(t in q for t in LEGAL_TRIGGERS):
        return "legal"
    return "assistant"


def _resolve_model(prefer_14b: bool) -> str:
    if not prefer_14b:
        return LLM_MODEL
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        for m in r.json().get("models", []):
            name = m.get("name", "")
            if "qwen3" in name.lower() and "14b" in name.lower():
                return name
    except Exception:
        pass
    return LLM_MODEL


def _qdrant_search(
    vector: list, collection: str, limit: int, threshold: float,
    type_filter: Optional[str] = None,
    source_filter: Optional[str] = None,
    subcategory_filter=None,   # str | List[str] | None
) -> list:
    body: dict = {"vector": vector, "limit": limit, "with_payload": True}
    must_conds = []
    if source_filter:
        must_conds.append({"key": "source_file", "match": {"value": source_filter}})
    if subcategory_filter:
        if isinstance(subcategory_filter, list) and len(subcategory_filter) == 1:
            must_conds.append({"key": "subcategory", "match": {"value": subcategory_filter[0]}})
        elif isinstance(subcategory_filter, list):
            must_conds.append({"key": "subcategory", "match": {"any": subcategory_filter}})
        else:
            must_conds.append({"key": "subcategory", "match": {"value": subcategory_filter}})
    if type_filter == "summary_or_fact":
        type_cond = {"should": [
            {"key": "type", "match": {"value": "summary"}},
            {"key": "type", "match": {"value": "fact"}},
        ]}
        body["filter"] = {"must": must_conds + [type_cond]} if must_conds else {"should": [
            {"key": "type", "match": {"value": "summary"}},
            {"key": "type", "match": {"value": "fact"}},
        ]}
    elif type_filter == "chunk":
        body["filter"] = {"must": must_conds, "must_not": [{"key": "type", "match": {"any": ["summary", "fact"]}}]} if must_conds \
            else {"must_not": [{"key": "type", "match": {"any": ["summary", "fact"]}}]}
    elif must_conds:
        body["filter"] = {"must": must_conds}
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            content=json.dumps(body),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        return [h for h in r.json().get("result", []) if h["score"] > threshold]
    except Exception:
        return []


# === Chat endpoint ===

@app.post("/chat")
def chat(req: ChatRequest):
    memory = _detect_memory_intent(req.query)
    if memory:
        collection, content = memory
        wing_label = collection.rsplit("_", 1)[0]
        ok = _save_direct_memory(collection, content)
        if ok:
            return {"answer": f"Gemt i {wing_label}.", "sources": [], "mode": "memory", "model": None}
        raise HTTPException(503, "Qdrant-fejl ved gemning af hukommelse")

    mode = _detect_mode(req.query)

    # Embed forespørgsel
    try:
        embed_r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": req.query[:8192]},
            timeout=30,
        )
        embed_r.raise_for_status()
        vector = embed_r.json()["embedding"]
    except Exception as e:
        raise HTTPException(503, f"Embedding fejl: {e}")

    # Vælg wings og søge-parametre baseret på mode
    wings_data = load_wings()
    if req.wing:
        wing_entry = find_wing(wings_data, req.wing)
        if not wing_entry:
            raise HTTPException(404, f"Wing '{req.wing}' ikke fundet")
        collections_to_search = [wing_entry]
    else:
        all_colls = {w["collection"] for w in wings_data["wings"]}
        if mode == "legal":
            target = LEGAL_COLLECTIONS
        elif mode == "legacy":
            target = LEGACY_COLLECTIONS
        else:
            target = all_colls - LEGAL_COLLECTIONS - LEGACY_COLLECTIONS
            if not target:
                target = all_colls
        collections_to_search = [w for w in wings_data["wings"] if w["collection"] in target]

    if mode == "legal":
        threshold, limit, system_prompt = 0.65, 20, LEGAL_SYSTEM
    elif mode == "legacy":
        threshold, limit, system_prompt = 0.50, 10, LEGACY_SYSTEM
    else:
        user_lang = _load_ui_prefs().get(req.user or "", {}).get("lang", "da") if req.user else "da"
        if user_lang == "en":
            assistant_sys = ASSISTANT_SYSTEM.replace("Svar kort og præcist på dansk.", "Always respond in English, briefly and precisely.")
        else:
            assistant_sys = ASSISTANT_SYSTEM
        threshold, limit, system_prompt = 0.45, 6, assistant_sys

    # To-trins søgning
    context_parts: list[str] = []
    sources: list[dict] = []
    seen: set = set()
    relevant_sources: set[str] = set()
    type_labels = {"summary": "OPSUMMERING", "fact": "FACT"}
    sub_filter = req.subcategories or ([req.subcategory] if req.subcategory else None)

    for w in collections_to_search:
        coll = w["collection"]
        # Trin 1: summaries og facts
        if mode != "assistant":
            for hit in _qdrant_search(vector, coll, min(limit, 10), threshold - 0.05, "summary_or_fact", req.source_filter, sub_filter):
                if hit["id"] in seen:
                    continue
                seen.add(hit["id"])
                sf   = hit["payload"].get("source_file", "")
                text = hit["payload"].get("text", "")
                ptype = hit["payload"].get("type", "summary")
                relevant_sources.add(sf)
                label = type_labels.get(ptype, "TEKST")
                context_parts.append(f"[{label} — {sf}]\n{text[:600]}")
                sources.append({"wing": w["name"], "score": round(hit["score"], 3),
                                 "id": hit["id"], "type": ptype, "preview": text[:200]})

        # Trin 2: chunks (kun for legacy: spring over)
        if mode != "legacy":
            type_f = "chunk" if mode == "legal" else None
            for hit in _qdrant_search(vector, coll, limit, threshold, type_f, req.source_filter, sub_filter):
                if hit["id"] in seen:
                    continue
                seen.add(hit["id"])
                text = hit["payload"].get("text", "")
                sf   = hit["payload"].get("source_file", "")
                context_parts.append(f"[TEKST — {sf}]\n{text[:600]}")
                sources.append({"wing": w["name"], "score": round(hit["score"], 3),
                                 "id": hit["id"], "type": "chunk", "preview": text[:200]})

    # Byg system prompt
    max_ctx = 12 if mode == "legal" else (10 if mode == "legacy" else 5)
    system = system_prompt
    if context_parts:
        system += "\n\nKontekst fra NOUS vidensbase:\n" + "\n\n---\n\n".join(context_parts[:max_ctx])

    model = _resolve_model(prefer_14b=(mode == "legal"))

    # LLM-kald
    _roles = _load_model_roles()
    _role_key = "night" if mode == "legal" else "day"
    _stored_params = _roles.get(f"{_role_key}_params", _DEFAULT_PARAMS)
    temperature = 0.2 if mode == "legal" else _stored_params.get("temperature", 0.4)
    _llm_options: dict = {
        "temperature": temperature,
        "num_ctx":     _stored_params.get("num_ctx", 8192),
        "num_gpu":     _stored_params.get("num_gpu", 99),
    }
    try:
        llm_r = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": req.query},
                ],
                "options": _llm_options,
            },
            timeout=90,
        )
        llm_r.raise_for_status()
        answer = llm_r.json()["message"]["content"]
    except Exception as e:
        raise HTTPException(503, f"LLM fejl: {e}")

    return {"answer": answer, "sources": sources, "mode": mode, "model": model}


# === Upload endpoint ===

@app.post("/upload/{wing}")
async def upload_file(wing: str, file: UploadFile = File(...), subcategory: Optional[str] = None):
    data = load_wings()
    if not find_wing(data, wing):
        raise HTTPException(404, f"Wing '{wing}' ikke fundet")

    target_dir = INCOMING_DIR / wing / subcategory if subcategory else INCOMING_DIR / wing
    target_dir.mkdir(parents=True, exist_ok=True)

    content = await file.read()
    target_path = target_dir / file.filename
    target_path.write_bytes(content)

    return {
        "filename": file.filename,
        "wing": wing,
        "subcategory": subcategory,
        "path": str(target_path),
        "size": len(content),
    }


# === Ingest status ===

@app.get("/ingest/status")
def ingest_status():
    try:
        result = subprocess.run(
            ["journalctl", "-u", "nous-ingest-watch.service", "-n", "50", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        return {
            "lines": result.stdout.splitlines(),
            "stderr": result.stderr[:300] if result.stderr else None,
        }
    except Exception as e:
        raise HTTPException(500, f"journalctl fejl: {e}")


# === System status ===

@app.get("/status")
def system_status():
    out: dict = {}

    # Qdrant
    try:
        r = httpx.get(QDRANT_URL, timeout=5)
        out["qdrant"] = {"ok": True, "version": r.json().get("version")}
    except Exception as e:
        out["qdrant"] = {"ok": False, "error": str(e)}

    # Collections med punktantal
    try:
        r = httpx.get(f"{QDRANT_URL}/collections", timeout=5)
        cols = {}
        for c in r.json()["result"]["collections"]:
            try:
                info = httpx.get(f"{QDRANT_URL}/collections/{c['name']}", timeout=5)
                cols[c["name"]] = info.json()["result"]["points_count"]
            except Exception:
                cols[c["name"]] = None
        out["collections"] = cols
    except Exception as e:
        out["collections"] = {"error": str(e)}

    # Ollama
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        out["ollama"] = {
            "ok": True,
            "models": [m["name"] for m in r.json().get("models", [])],
        }
    except Exception as e:
        out["ollama"] = {"ok": False, "error": str(e)}

    return out


# === Voice endpoints ===

@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    audio_bytes = await file.read()
    suffix = Path(file.filename or "audio.webm").suffix.lower().lstrip(".")
    mime_map = {
        "webm": "video/webm", "wav": "audio/wav", "mp3": "audio/mpeg",
        "ogg": "audio/ogg",   "m4a": "audio/mp4", "flac": "audio/flac",
    }
    mime = mime_map.get(suffix, "video/webm")
    try:
        resp = httpx.post(
            f"{NX_SPEACHES_URL}/v1/audio/transcriptions",
            files={"file": (file.filename or f"audio.{suffix}", audio_bytes, mime)},
            data={"model": "Systran/faster-whisper-small", "language": "da",
                  "response_format": "json"},
            timeout=60.0,
        )
        resp.raise_for_status()
        text = (resp.json().get("text") or "").strip()
    except Exception as e:
        raise HTTPException(503, f"Whisper fejl: {e}")
    return {"text": text}


@app.post("/speak")
def speak_text(req: SpeakRequest):
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "speak.wav"
        try:
            r = subprocess.run(
                [PIPER_BIN, "-m", "piper", "--model", PIPER_MODEL, "--config", PIPER_CONFIG, "--output_file", str(out_path)],
                input=req.text.encode(),
                capture_output=True,
                timeout=30,
            )
            if r.returncode != 0:
                raise HTTPException(500, f"piper fejl: {r.stderr.decode()[:300]}")
        except subprocess.TimeoutExpired:
            raise HTTPException(500, "piper timeout")

        wav_bytes = out_path.read_bytes()

    return Response(content=wav_bytes, media_type="audio/wav")


@app.post("/audio/transcribe")
async def audio_transcribe(file: UploadFile = File(...), language: str = "da"):
    """Manuel transskription via speaches (faster-whisper) på NX:8182."""
    audio_bytes = await file.read()
    suffix      = Path(file.filename or "audio.wav").suffix.lower().lstrip(".")
    mime_map    = {
        "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
        "ogg": "audio/ogg",  "flac": "audio/flac", "aac": "audio/aac",
        "webm": "video/webm",
    }
    mime = mime_map.get(suffix, "audio/mpeg")
    try:
        resp = httpx.post(
            f"{NX_SPEACHES_URL}/v1/audio/transcriptions",
            files={"file": (file.filename or f"audio.{suffix}", audio_bytes, mime)},
            data={"model": "large-v3", "language": language, "response_format": "json"},
            timeout=300.0,
        )
        resp.raise_for_status()
        text = (resp.json().get("text") or "").strip()
    except Exception as e:
        raise HTTPException(503, f"Speaches fejl: {e}")
    return {"text": text, "language": language}


# === Scraper endpoints ===

def _load_scraper_jobs() -> list:
    if not SCRAPER_JOBS.exists():
        return []
    return json.loads(SCRAPER_JOBS.read_text(encoding="utf-8"))


def _save_scraper_jobs(jobs: list) -> None:
    SCRAPER_JOBS.parent.mkdir(parents=True, exist_ok=True)
    SCRAPER_JOBS.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")


class ScraperJobCreate(BaseModel):
    wing:        str
    queries:     list[str]
    seed_urls:   list[str] = []
    danish_only: bool = True
    schedule:    str  = "weekly_sunday_0100"
    active:      bool = True


@app.get("/scraper/jobs")
def list_scraper_jobs():
    return {"jobs": _load_scraper_jobs()}


@app.post("/scraper/jobs", status_code=201)
def create_scraper_job(body: ScraperJobCreate):
    data = load_wings()
    if not find_wing(data, body.wing):
        raise HTTPException(404, f"Wing '{body.wing}' ikke fundet")

    jobs   = _load_scraper_jobs()
    job_id = f"{body.wing}_{uuid.uuid4().hex[:6]}"
    entry  = {
        "id":          job_id,
        "wing":        body.wing,
        "queries":     body.queries,
        "seed_urls":   body.seed_urls,
        "danish_only": body.danish_only,
        "schedule":    body.schedule,
        "active":      body.active,
    }
    jobs.append(entry)
    _save_scraper_jobs(jobs)
    return entry


@app.delete("/scraper/jobs/{job_id}", status_code=204)
def delete_scraper_job(job_id: str):
    jobs = _load_scraper_jobs()
    new  = [j for j in jobs if j["id"] != job_id]
    if len(new) == len(jobs):
        raise HTTPException(404, f"Job '{job_id}' ikke fundet")
    _save_scraper_jobs(new)


def _run_scraper_bg(job_id: str) -> None:
    _scraper_status[job_id] = {"status": "running", "started": __import__("datetime").datetime.now().isoformat()}
    try:
        result = subprocess.run(
            ["/srv/nous/app/.venv/bin/python3", "/srv/nous/scripts/night_scraper.py", job_id],
            capture_output=True, text=True, timeout=3600,
        )
        _scraper_status[job_id] = {
            "status":   "done" if result.returncode == 0 else "error",
            "finished": __import__("datetime").datetime.now().isoformat(),
            "output":   result.stdout[-2000:] if result.stdout else "",
            "error":    result.stderr[-500:]  if result.stderr else "",
        }
    except Exception as e:
        _scraper_status[job_id] = {"status": "error", "error": str(e)}


@app.post("/scraper/run/{job_id}", status_code=202)
def run_scraper_job(job_id: str, background_tasks: BackgroundTasks):
    jobs = _load_scraper_jobs()
    if not any(j["id"] == job_id for j in jobs):
        raise HTTPException(404, f"Job '{job_id}' ikke fundet")
    if _scraper_status.get(job_id, {}).get("status") == "running":
        raise HTTPException(409, "Job kører allerede")
    background_tasks.add_task(_run_scraper_bg, job_id)
    return {"job_id": job_id, "status": "started"}


@app.get("/scraper/status/{job_id}")
def scraper_job_status(job_id: str):
    if job_id not in _scraper_status:
        return {"job_id": job_id, "status": "idle"}
    return {"job_id": job_id, **_scraper_status[job_id]}


# === Research scraper endpoints ===

def _load_research_jobs() -> list:
    if not RESEARCH_JOBS.exists():
        return []
    return json.loads(RESEARCH_JOBS.read_text(encoding="utf-8"))


def _save_research_jobs(jobs: list) -> None:
    RESEARCH_JOBS.parent.mkdir(parents=True, exist_ok=True)
    RESEARCH_JOBS.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")


class ResearchJobCreate(BaseModel):
    wing:            str
    start_url:       str
    label:           str   = ""
    max_depth:       int   = 2
    max_pages:       int   = 30
    score_threshold: float = 0.3


@app.get("/research/jobs")
def list_research_jobs():
    return {"jobs": _load_research_jobs()}


@app.post("/research/jobs", status_code=201)
def create_research_job(body: ResearchJobCreate):
    data = load_wings()
    if not find_wing(data, body.wing):
        raise HTTPException(404, f"Wing '{body.wing}' ikke fundet")
    jobs   = _load_research_jobs()
    job_id = f"res_{body.wing}_{uuid.uuid4().hex[:6]}"
    entry  = {
        "id":             job_id,
        "wing":           body.wing,
        "start_url":      body.start_url,
        "label":          body.label or body.start_url[:60],
        "max_depth":      max(1, min(body.max_depth, 4)),
        "max_pages":      max(1, min(body.max_pages, 100)),
        "score_threshold": body.score_threshold,
        "created_at":     datetime.now(timezone.utc).isoformat(),
    }
    jobs.append(entry)
    _save_research_jobs(jobs)
    return entry


@app.delete("/research/jobs/{job_id}", status_code=204)
def delete_research_job(job_id: str):
    jobs = _load_research_jobs()
    new  = [j for j in jobs if j["id"] != job_id]
    if len(new) == len(jobs):
        raise HTTPException(404, f"Job '{job_id}' ikke fundet")
    _save_research_jobs(new)


def _run_research_bg(job_id: str) -> None:
    _research_status[job_id] = {
        "status":  "running",
        "started": datetime.now(timezone.utc).isoformat(),
    }
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "/srv/nous/scripts/research_scraper.py", job_id],
            capture_output=True, text=True, timeout=7200,
        )
        _research_status[job_id] = {
            "status":   "done" if result.returncode == 0 else "error",
            "finished": datetime.now(timezone.utc).isoformat(),
            "output":   result.stdout[-3000:] if result.stdout else "",
            "error":    result.stderr[-500:]  if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        _research_status[job_id] = {"status": "error", "error": "Timeout (2 timer)"}
    except Exception as e:
        _research_status[job_id] = {"status": "error", "error": str(e)}


@app.post("/research/run/{job_id}", status_code=202)
def run_research_job(job_id: str, background_tasks: BackgroundTasks):
    jobs = _load_research_jobs()
    if not any(j["id"] == job_id for j in jobs):
        raise HTTPException(404, f"Job '{job_id}' ikke fundet")
    if _research_status.get(job_id, {}).get("status") == "running":
        raise HTTPException(409, "Job kører allerede")
    background_tasks.add_task(_run_research_bg, job_id)
    return {"job_id": job_id, "status": "started"}


@app.get("/research/status/{job_id}")
def research_job_status(job_id: str):
    if job_id not in _research_status:
        return {"job_id": job_id, "status": "idle"}
    return {"job_id": job_id, **_research_status[job_id]}


# === Model Manager ===

MODEL_ROLES_FILE = Path("/mnt/nous-data/model_roles.json")


_DEFAULT_PARAMS = {"temperature": 0.7, "num_ctx": 8192, "num_gpu": 99}
_DEFAULT_ROLES = {
    "day": "qwen3:8b",
    "day_params": dict(_DEFAULT_PARAMS),
    "night": "qwen3:14b",
    "night_params": dict(_DEFAULT_PARAMS),
}


def _load_model_roles() -> dict:
    if not MODEL_ROLES_FILE.exists():
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEFAULT_ROLES.items()}
    data = json.loads(MODEL_ROLES_FILE.read_text(encoding="utf-8"))
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEFAULT_ROLES.items()}
    for k, v in data.items():
        if k in ("day_params", "night_params") and isinstance(v, dict):
            merged[k] = {**_DEFAULT_PARAMS, **v}
        else:
            merged[k] = v
    return merged


def _save_model_roles(roles: dict) -> None:
    MODEL_ROLES_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODEL_ROLES_FILE.write_text(json.dumps(roles, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/models/list")
async def list_models():
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags", timeout=10)
            r.raise_for_status()
        models = [
            {"name": m.get("name", ""), "size_gb": round(m.get("size", 0) / 1e9, 1)}
            for m in r.json().get("models", [])
        ]
        return {"models": models}
    except Exception as e:
        raise HTTPException(502, f"Ollama utilgængelig: {e}")


@app.get("/models/roles")
def get_model_roles():
    return _load_model_roles()


class ModelRoleSet(BaseModel):
    model: str
    role: str  # 'day' | 'night'


@app.post("/models/set-role")
async def set_model_role(body: ModelRoleSet):
    if body.role not in ("day", "night"):
        raise HTTPException(400, "role skal være 'day' eller 'night'")
    roles = _load_model_roles()
    old_day = roles.get("day", "")
    roles[body.role] = body.model
    _save_model_roles(roles)

    if body.role == "day":
        async with httpx.AsyncClient() as client:
            if old_day and old_day != body.model:
                try:
                    await client.post(
                        f"{OLLAMA_URL}/api/generate",
                        json={"model": old_day, "prompt": "", "keep_alive": 0, "stream": False},
                        timeout=15,
                    )
                except Exception:
                    pass
            try:
                await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": body.model, "prompt": "hej", "keep_alive": -1, "stream": False},
                    timeout=120,
                )
            except Exception as e:
                raise HTTPException(502, f"Kunne ikke loade dag-model: {e}")

    return {"ok": True, "role": body.role, "model": body.model}


class ModelParamsSet(BaseModel):
    role: str  # 'day' | 'night'
    temperature: float = 0.7
    num_ctx: int = 8192
    num_gpu: int = 99


@app.post("/models/set-params")
def set_model_params(body: ModelParamsSet):
    if body.role not in ("day", "night"):
        raise HTTPException(400, "role skal være 'day' eller 'night'")
    roles = _load_model_roles()
    roles[f"{body.role}_params"] = {
        "temperature": max(0.0, min(2.0, body.temperature)),
        "num_ctx":     max(512, min(32768, body.num_ctx)),
        "num_gpu":     max(0,   min(100,  body.num_gpu)),
    }
    _save_model_roles(roles)
    return {"ok": True, "role": body.role, "params": roles[f"{body.role}_params"]}


class ModelLoadNow(BaseModel):
    model: str


@app.post("/models/load-now")
async def load_model_now(body: ModelLoadNow):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": body.model, "prompt": "hej", "keep_alive": -1, "stream": False},
                timeout=120,
            )
            r.raise_for_status()
        return {"ok": True, "model": body.model}
    except Exception as e:
        raise HTTPException(502, f"Kunne ikke loade model: {e}")


# === Model search & GGUF upload ===

@app.get("/models/search")
async def search_models(q: str = Query(..., min_length=1, max_length=100)):
    """Søg Ollama library via lokal SearXNG."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{SEARXNG_LOCAL}/search",
                params={"q": f"site:ollama.com/library {q}", "format": "json"},
                headers={"User-Agent": "nous-mm/1.0"},
            )
            r.raise_for_status()
        hits = r.json().get("results", [])
        # If site: operator yields nothing, fall back to broader query
        if len(hits) < 2:
            async with httpx.AsyncClient(timeout=15) as client:
                r2 = await client.get(
                    f"{SEARXNG_LOCAL}/search",
                    params={"q": f"ollama.com library {q}", "format": "json"},
                    headers={"User-Agent": "nous-mm/1.0"},
                )
                if r2.status_code == 200:
                    hits = (hits + r2.json().get("results", []))[:20]
        results, seen = [], set()
        for hit in hits:
            url = hit.get("url", "")
            m = re.match(r"https?://ollama\.com/library/([^/?#]+)", url)
            if not m:
                continue
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            content = hit.get("content", "")
            title   = hit.get("title", "")
            sizes = list(dict.fromkeys(
                re.findall(r"\b\d+(?:\.\d+)?[Bb]\b", title + " " + content)
            ))
            results.append({
                "name":        name,
                "description": content[:220].strip(),
                "sizes":       sizes,
                "url":         url,
            })
        return {"results": results[:10]}
    except Exception as e:
        raise HTTPException(502, f"Modelsøgning fejlede: {e}")


def _safe_model_name(filename: str) -> str:
    name = re.sub(r"\.gguf$", "", filename, flags=re.IGNORECASE)
    name = re.sub(r"[^a-zA-Z0-9_.\-]", "_", name).lower().strip("_")
    return name or "custom_model"


def _do_gguf_upload(job_id: str, tmp_path: Path, filename: str) -> None:
    """Background: rsync fil til NX (eller lokalt) + registrér i Ollama."""
    def upd(phase: str, pct: int, msg: str) -> None:
        _upload_status[job_id] = {"phase": phase, "progress": pct, "msg": msg}

    model_name = _safe_model_name(filename)
    try:
        if NX_HOST:
            # ── rsync Pi → NX ────────────────────────────────────────────
            upd("transfer", 10, f"Overfører til NX…")
            dest = f"{NX_HOST}:{NX_MODELS_DIR}/{filename}"
            proc = subprocess.Popen(
                ["rsync", "-az", "--info=progress2", "--no-inc-recursive",
                 str(tmp_path), dest],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            buf = ""
            while True:
                chunk = proc.stdout.read(256)
                if not chunk and proc.poll() is not None:
                    break
                if chunk:
                    buf = (buf + chunk)[-200:]
                    m = re.findall(r"(\d+)%", buf)
                    if m:
                        pct = min(int(m[-1]), 99)
                        upd("transfer", 10 + pct * 8 // 10, f"Overfører til NX… {pct}%")
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"rsync fejl: {proc.stderr.read()[:300]}")
            model_path = f"{NX_MODELS_DIR}/{filename}"

            # ── Modelfile via SCP → SSH ollama create ────────────────────
            upd("registering", 90, "Registrerer i Ollama…")
            mf_local = Path(f"/tmp/nous_mf_{job_id}.tmp")
            mf_local.write_text(f"FROM {model_path}\n")
            scp = subprocess.run(
                ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 str(mf_local), f"{NX_HOST}:/tmp/nous_mf_{job_id}.tmp"],
                capture_output=True, text=True, timeout=30,
            )
            mf_local.unlink(missing_ok=True)
            if scp.returncode != 0:
                raise RuntimeError(f"SCP modelfile fejl: {scp.stderr[:200]}")
            ssh = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", NX_HOST,
                 f"ollama create '{model_name}:latest' -f /tmp/nous_mf_{job_id}.tmp "
                 f"&& rm -f /tmp/nous_mf_{job_id}.tmp"],
                capture_output=True, text=True, timeout=300,
            )
            if ssh.returncode != 0:
                raise RuntimeError(f"ollama create fejl: {ssh.stderr[:300]}")
        else:
            # ── Single-node: flyt lokalt ─────────────────────────────────
            upd("transfer", 30, "Gemmer lokalt…")
            dest_dir = Path("/home/nous/models")
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_path), str(dest_dir / filename))
            model_path = str(dest_dir / filename)

            upd("registering", 90, "Registrerer i Ollama…")
            mf = Path(f"/tmp/nous_mf_{job_id}.tmp")
            mf.write_text(f"FROM {model_path}\n")
            result = subprocess.run(
                ["ollama", "create", f"{model_name}:latest", "-f", str(mf)],
                capture_output=True, text=True, timeout=300,
            )
            mf.unlink(missing_ok=True)
            if result.returncode != 0:
                raise RuntimeError(f"ollama create fejl: {result.stderr[:300]}")

        upd("done", 100, f"{model_name}:latest klar i Ollama")
    except Exception as e:
        upd("error", 0, str(e))
    finally:
        tmp_path.unlink(missing_ok=True)
        try:
            tmp_path.parent.rmdir()
        except OSError:
            pass


@app.post("/models/upload", status_code=202)
async def upload_model_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    if not file.filename or not file.filename.lower().endswith(".gguf"):
        raise HTTPException(400, "Kun .gguf-filer understøttes")
    safe = re.sub(r"[^a-zA-Z0-9_.\-]", "_", Path(file.filename).name)
    if not safe.lower().endswith(".gguf"):
        safe += ".gguf"

    job_id  = uuid.uuid4().hex[:12]
    tmp_dir = UPLOAD_TMP / job_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / safe

    _upload_status[job_id] = {"phase": "receiving", "progress": 2, "msg": "Modtager fil…"}
    try:
        with open(tmp_path, "wb") as f:
            while True:
                chunk = await file.read(2 * 1024 * 1024)  # 2 MB chunks
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(500, f"Fil-modtagelse fejlede: {e}")

    _upload_status[job_id] = {"phase": "queued", "progress": 5, "msg": "Fil modtaget — starter overførsel…"}
    background_tasks.add_task(_do_gguf_upload, job_id, tmp_path, safe)
    return {"job_id": job_id, "filename": safe}


@app.get("/models/upload-status/{job_id}")
def get_upload_status(job_id: str):
    return _upload_status.get(job_id, {"phase": "not_found", "progress": 0, "msg": "Ukendt job"})


# === Model download (ollama pull via SSH eller lokalt) ===

class ModelDownloadRequest(BaseModel):
    model: str


_SAFE_MODEL_RE = re.compile(r'^[a-zA-Z0-9._:\-/]+$')


def _do_model_download(job_id: str, model: str) -> None:
    def upd(phase: str, pct: int, msg: str) -> None:
        _download_status[job_id] = {"phase": phase, "progress": pct, "msg": msg}

    upd("pulling", 2, f"Starter ollama pull {model}…")
    try:
        if NX_HOST:
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                   NX_HOST, f"ollama pull {model}"]
        else:
            cmd = ["ollama", "pull", model]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        cur_pct = 2
        for line in iter(proc.stdout.readline, ""):
            line = line.strip()
            if not line:
                continue
            m = re.search(r"(\d+)%", line)
            if m:
                cur_pct = min(int(m.group(1)), 99)
            upd("pulling", cur_pct, line[:150])
        proc.wait()
        if proc.returncode != 0:
            upd("error", 0, f"ollama pull fejlede (exit {proc.returncode})")
        else:
            upd("done", 100, f"{model} klar i Ollama")
    except Exception as e:
        upd("error", 0, str(e)[:200])


@app.post("/models/download", status_code=202)
async def download_model(body: ModelDownloadRequest, background_tasks: BackgroundTasks):
    if not _SAFE_MODEL_RE.match(body.model):
        raise HTTPException(400, "Ugyldigt modelnavn")
    job_id = uuid.uuid4().hex[:12]
    _download_status[job_id] = {"phase": "queued", "progress": 0, "msg": f"Downloader {body.model}…"}
    background_tasks.add_task(_do_model_download, job_id, body.model)
    return {"job_id": job_id, "model": body.model}


@app.get("/models/download-status/{job_id}")
def get_download_status(job_id: str):
    return _download_status.get(job_id, {"phase": "not_found", "progress": 0, "msg": "Ukendt job"})


# === Analysis results ===

ANALYSIS_TYPES = ["cross_analysis", "inconsistency_analysis", "summary"]

# Alle non-verbatim typer — bruges i _fetch_chunks_text() for at garantere
# at kun rå ingest-chunks returneres (ikke LLM-facts, analyser eller medieindhold).
# Ældre chunks uden type-felt er også chunks og inkluderes automatisk (ingen type = chunk).
NON_CHUNK_TYPES = [
    "summary", "fact", "cross_analysis", "inconsistency_analysis",
    "image_analysis", "audio_transcription", "inferred_fact",
]


class AnalysisSaveRequest(BaseModel):
    wing:        str
    source_file: str
    text:        str
    type:        str = "summary"


@app.post("/analysis/save", status_code=201)
def save_analysis(req: AnalysisSaveRequest):
    if req.type not in ANALYSIS_TYPES:
        raise HTTPException(400, f"type skal være en af {ANALYSIS_TYPES}")
    data = load_wings()
    wing_entry = find_wing(data, req.wing)
    if not wing_entry:
        raise HTTPException(404, f"Wing '{req.wing}' ikke fundet")
    collection = wing_entry["collection"]
    scope      = wing_entry.get("scope", "PRIVATE")
    try:
        embed_r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": req.text[:8192]},
            timeout=30,
        )
        embed_r.raise_for_status()
        vector = embed_r.json()["embedding"]
    except Exception as e:
        raise HTTPException(503, f"Embedding fejl: {e}")

    point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{collection}:{req.source_file}:{req.type}:manual"))
    point = {
        "id":     point_id,
        "vector": vector,
        "payload": {
            "type":        req.type,
            "source_file": req.source_file,
            "wing":        req.wing,
            "scope":       scope,
            "text":        req.text,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        },
    }
    try:
        r = httpx.post(
            f"{ARBITER_URL}/arbiter/write/sync",
            json={"wing": req.wing, "scope": scope, "operation": "upsert",
                  "points": [point], "source": "analysis_save"},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(503, f"Arbiter fejl: {e}")
    return {"saved": True, "id": point_id, "wing": req.wing, "source_file": req.source_file}


@app.get("/analysis/results")
def get_analysis_results(wing: Optional[str] = None):
    data = load_wings()
    if wing:
        wing_entry = find_wing(data, wing)
        if not wing_entry:
            raise HTTPException(404, f"Wing '{wing}' ikke fundet")
        collections = [wing_entry]
    else:
        collections = data["wings"]

    results = []
    for w in collections:
        coll = w["collection"]
        offset = None
        while True:
            body: dict = {
                "limit": 100,
                "with_payload": True,
                "with_vector": False,
                "filter": {
                    "must": [{"key": "type", "match": {"any": ANALYSIS_TYPES}}]
                },
            }
            if offset is not None:
                body["offset"] = offset
            try:
                r = httpx.post(
                    f"{QDRANT_URL}/collections/{coll}/points/scroll",
                    json=body,
                    timeout=15,
                )
                r.raise_for_status()
            except Exception:
                break
            result = r.json().get("result", {})
            for pt in result.get("points", []):
                payload = pt.get("payload", {})
                results.append({
                    "id": str(pt["id"]),
                    "wing": w["name"],
                    "type": payload.get("type", ""),
                    "text": payload.get("text", ""),
                    "timestamp": payload.get("timestamp", ""),
                    "source": payload.get("source", ""),
                    "source_file": payload.get("source_file", ""),
                })
            offset = result.get("next_page_offset")
            if offset is None:
                break

    results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"results": results}


# === Raw chunk søgning ===

@app.get("/search/chunks")
def search_chunks(
    q: str,
    wing: Optional[str] = None,
    limit: int = Query(12, le=30),
):
    try:
        embed_r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": q[:4096]},
            timeout=30,
        )
        embed_r.raise_for_status()
        vector = embed_r.json()["embedding"]
    except Exception as e:
        raise HTTPException(503, f"Embedding fejl: {e}")

    data = load_wings()
    if wing:
        w = find_wing(data, wing)
        if not w:
            raise HTTPException(404, f"Wing '{wing}' ikke fundet")
        targets = [w]
    else:
        targets = data["wings"]

    results = []
    for w in targets:
        for hit in _qdrant_search(vector, w["collection"], limit, 0.25):
            results.append({
                "wing":   w["name"],
                "score":  round(hit["score"], 3),
                "source": hit["payload"].get("source", hit["payload"].get("source_file", "")),
                "type":   hit["payload"].get("type", "chunk"),
                "text":   hit["payload"].get("text", "")[:600],
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return {"results": results[:limit]}


# === System temperature ===

@app.get("/system/temperature")
def system_temperature():
    from pathlib import Path as _Path
    temps = []
    for zone in sorted(_Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            val = int(zone.read_text().strip())
            temps.append(round(val / 1000, 1))
        except Exception:
            pass
    if not temps:
        return {"celsius": None}
    return {"celsius": round(sum(temps) / len(temps), 1), "zones": temps}


# === Ekstern AI endpoint ===

@app.post("/external/chat")
def external_chat(req: ExternalChatRequest):
    if req.provider not in PROVIDER_DEFAULTS:
        raise HTTPException(400, f"Ukendt provider '{req.provider}'")
    if not req.api_key.strip():
        raise HTTPException(400, "api_key er påkrævet")

    # Scope-validering
    wings_data = load_wings()
    if req.wing:
        wing_entry = find_wing(wings_data, req.wing)
        if not wing_entry:
            raise HTTPException(404, f"Wing '{req.wing}' ikke fundet")
        scope = wing_entry.get("scope", "PRIVATE")
        if scope == "SECRET" and not req.scope_confirmed:
            return JSONResponse(status_code=403, content={"error": "scope_blocked", "scope": "SECRET"})
        if scope == "PRIVATE" and not req.scope_confirmed:
            return JSONResponse(status_code=403, content={"error": "scope_warning", "scope": "PRIVATE"})

    # Embed forespørgsel
    try:
        embed_r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": req.prompt[:8192]},
            timeout=30,
        )
        embed_r.raise_for_status()
        vector = embed_r.json()["embedding"]
    except Exception as e:
        raise HTTPException(503, f"Embedding fejl: {e}")

    # Resolve mode og wings
    mode = _detect_mode(req.prompt)
    if req.wing:
        collections_to_search = [find_wing(wings_data, req.wing)]
    else:
        all_colls = {w["collection"] for w in wings_data["wings"]}
        if mode == "legal":
            target = LEGAL_COLLECTIONS
        elif mode == "legacy":
            target = LEGACY_COLLECTIONS
        else:
            target = all_colls - LEGAL_COLLECTIONS - LEGACY_COLLECTIONS
            if not target:
                target = all_colls
        collections_to_search = [w for w in wings_data["wings"] if w["collection"] in target]

    if mode == "legal":
        threshold, limit, system_prompt = 0.65, 20, LEGAL_SYSTEM
    elif mode == "legacy":
        threshold, limit, system_prompt = 0.50, 10, LEGACY_SYSTEM
    else:
        user_lang = _load_ui_prefs().get(req.user or "", {}).get("lang", "da") if req.user else "da"
        if user_lang == "en":
            assistant_sys = ASSISTANT_SYSTEM.replace("Svar kort og præcist på dansk.", "Always respond in English, briefly and precisely.")
        else:
            assistant_sys = ASSISTANT_SYSTEM
        threshold, limit, system_prompt = 0.45, 6, assistant_sys

    # RAG-søgning (identisk logik som /chat)
    context_parts: list[str] = []
    sources: list[dict] = []
    seen: set = set()
    type_labels = {"summary": "OPSUMMERING", "fact": "FACT"}
    sub_filter = req.subcategories or ([req.subcategory] if req.subcategory else None)

    for w in collections_to_search:
        coll = w["collection"]
        if mode != "assistant":
            for hit in _qdrant_search(vector, coll, min(limit, 10), threshold - 0.05, "summary_or_fact", req.source_filter, sub_filter):
                if hit["id"] in seen:
                    continue
                seen.add(hit["id"])
                sf    = hit["payload"].get("source_file", "")
                text  = hit["payload"].get("text", "")
                ptype = hit["payload"].get("type", "summary")
                label = type_labels.get(ptype, "TEKST")
                context_parts.append(f"[{label} — {sf}]\n{text[:600]}")
                sources.append({"wing": w["name"], "score": round(hit["score"], 3),
                                 "id": hit["id"], "type": ptype, "preview": text[:200]})
        if mode != "legacy":
            type_f = "chunk" if mode == "legal" else None
            for hit in _qdrant_search(vector, coll, limit, threshold, type_f, req.source_filter, sub_filter):
                if hit["id"] in seen:
                    continue
                seen.add(hit["id"])
                text = hit["payload"].get("text", "")
                sf   = hit["payload"].get("source_file", "")
                context_parts.append(f"[TEKST — {sf}]\n{text[:600]}")
                sources.append({"wing": w["name"], "score": round(hit["score"], 3),
                                 "id": hit["id"], "type": "chunk", "preview": text[:200]})

    max_ctx = 12 if mode == "legal" else (10 if mode == "legacy" else 5)
    system = system_prompt
    if context_parts:
        system += "\n\nKontekst fra NOUS vidensbase:\n" + "\n\n---\n\n".join(context_parts[:max_ctx])

    # Byg API-kald — nøgle berøres aldrig af logger
    pconf    = PROVIDER_DEFAULTS[req.provider]
    base_url = (req.base_url or pconf["base_url"]).rstrip("/")
    model    = req.model or pconf["default_model"]
    prefix   = pconf.get("auth_prefix", "")
    _key     = _ascii_header_safe(req.api_key)
    headers  = {
        pconf["auth_header"]: f"{prefix}{_key}" if prefix else _key,
        "Content-Type": "application/json",
    }

    try:
        if req.provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            payload = {
                "model":      model,
                "max_tokens": 2048,
                "system":     system,
                "messages":   [{"role": "user", "content": req.prompt}],
            }
            r = httpx.post(f"{base_url}/messages", headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            answer = r.json()["content"][0]["text"]
        else:
            payload = {
                "model":       model,
                "messages":    [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": req.prompt},
                ],
                "temperature": 0.2 if mode == "legal" else 0.4,
            }
            r = httpx.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            answer = r.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Ekstern API fejl HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(503, f"Ekstern API fejl: {type(e).__name__}")

    return {
        "answer":   answer,
        "sources":  sources,
        "mode":     mode,
        "model":    model,
        "provider": req.provider,
        "external": True,
    }


# === Panel-debat endpoint ===

DEBATE_PARTICIPANT_SYSTEM = """\
Du er deltager i en ekspert-panel debat.
Emne: {topic}
{ctx_line}
Regler:
- Svar direkte og præcist på max 200 ord
- Referer til de andre deltageres pointer hvis relevant
- Vær faglig, ikke høflig
- Mål: nå frem til den bedste løsning i fællesskab
"""

DEBATE_MODERATOR_ASSESSMENT = """\
Du er ordstyrer i en ekspert-panel debat.

Debathistorik:
{history}

Runde: {round_num}

Din opgave:
1. Vurder om deltagerne har nået konsensus om den bedste løsning
2. Identificer resterende uenigheder
3. Formuler én præcis, skarp udfordring til næste runde — det konkrete stridspunkt eller spørgsmål deltagerne SKAL tage direkte stilling til
4. Svar KUN med JSON (ingen markdown, ingen forklaring):

{{"consensus_reached": true/false, "summary": "kort sammenfatning", "remaining_disagreements": ["liste af uenigheder"], "next_challenge": "præcis udfordring til næste runde"}}
"""

DEBATE_MODERATOR_FINAL = """\
Du er ordstyrer i en panel-debat. Baseret på debathistorikken nedenfor, skriv en sammenfatning i præcis dette format — start direkte med "SVAR:", ingen intro:

SVAR: [1-2 sætninger der direkte besvarer debatemnet]

KONSENSUS:
- [Hvad deltagerne var enige om]

UENIGHEDER:
- [Tilbageværende uenigheder, eller "Ingen" hvis fuld konsensus]

Debathistorik:
{history}

Skriv nu sammenfatningen (start med "SVAR:"):"""


def _utf8_clean(s: str) -> str:
    """Remove lone surrogates (U+D800–U+DFFF) that make json.dumps crash, then round-trip through utf-8."""
    s = re.sub(r"[\ud800-\udfff]", "", s)
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _ascii_header_safe(s: str) -> str:
    """HTTP headers must be ASCII — strip non-ASCII characters silently."""
    return s.encode("ascii", errors="ignore").decode("ascii")


async def _debate_call_participant(
    client: httpx.AsyncClient,
    participant: DebateParticipant,
    system_prompt: str,
    history: list[dict],
    topic: str,
    round_num: int = 1,
    challenge: str | None = None,
    other_labels: list[str] | None = None,
) -> str:
    if participant.provider not in PROVIDER_DEFAULTS:
        return f"[Ukendt provider: {participant.provider}]"
    pconf    = PROVIDER_DEFAULTS[participant.provider]
    base_url = (participant.base_url or pconf["base_url"]).rstrip("/")
    model    = participant.model or pconf["default_model"]
    prefix   = pconf.get("auth_prefix", "")
    _key     = _ascii_header_safe(participant.api_key)
    headers  = {
        pconf["auth_header"]: f"{prefix}{_key}" if prefix else _key,
        "Content-Type": "application/json",
    }
    messages = [{"role": "user", "content": _utf8_clean(f"Emne: {topic}\n\nBidrag din ekspertanalyse:")}]
    if history:
        others = ", ".join(other_labels) if other_labels else "de andre deltagere"
        challenge_line = f"\n\nOrdstyrers udfordring til runde {round_num}: {challenge}" if challenge else ""
        followup = (
            f"Runde {round_num}: Du skal nu svare direkte på {others}' argumenter fra forrige runde. "
            f"Hvad er du uenig i? Hvad vil du revidere i din egen position? Vær konkret.{challenge_line}"
        )
        # Begræns historik til ~6000 tegn så kontekstvinduet ikke overflower (moonshot-v1-8k = 8k tokens)
        recent = history[-8:]
        history_text = "\n".join(h["content"] for h in recent)
        if len(history_text) > 6000:
            history_text = "…(ældre bidrag udeladt)…\n" + history_text[-6000:]
        messages = [
            {"role": "user",      "content": _utf8_clean(f"Emne: {topic}")},
            {"role": "assistant", "content": _utf8_clean(history_text)},
            {"role": "user",      "content": _utf8_clean(followup)},
        ]
    try:
        if participant.provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            # Serialisér til bytes selv så vi fanger encoding-fejl præcist
            payload = {
                "model":      model,
                "max_tokens": 600,
                "system":     _utf8_clean(system_prompt),
                "messages":   messages,
            }
            payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            r = await client.post(
                f"{base_url}/messages",
                headers={**headers, "Content-Type": "application/json"},
                content=payload_bytes,
                timeout=60,
            )
            r.raise_for_status()
            body = r.content.decode("utf-8", errors="replace")
            return _utf8_clean(json.loads(body)["content"][0]["text"].strip())
        else:
            full_messages = [{"role": "system", "content": _utf8_clean(system_prompt)}] + messages
            payload = {
                "model":       model,
                "messages":    full_messages,
                "temperature": 0.6,
                "max_tokens":  600,
            }
            payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            r = await client.post(
                f"{base_url}/chat/completions",
                headers={**headers, "Content-Type": "application/json"},
                content=payload_bytes,
                timeout=60,
            )
            r.raise_for_status()
            return _utf8_clean(r.json()["choices"][0]["message"]["content"].strip())
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP {e.response.status_code} fra {participant.provider}/{model}: {e.response.text[:300]}")
        return f"[API fejl HTTP {e.response.status_code}]"
    except Exception as e:
        import traceback
        logger.error(
            f"Fejl i debate_call ({participant.provider}/{model}): {type(e).__name__}: {e}\n"
            + traceback.format_exc()
        )
        return f"[Fejl: {type(e).__name__}]"


async def _call_external_llm(client: httpx.AsyncClient, participant: "DebateParticipant", prompt: str) -> str:
    """Kald ekstern deltager direkte med en prompt — bruges som ordstyrer-fallback."""
    pconf    = PROVIDER_DEFAULTS.get(participant.provider, {})
    base_url = (participant.base_url or pconf.get("base_url", "")).rstrip("/")
    model    = participant.model or pconf.get("default_model", "")
    prefix   = pconf.get("auth_prefix", "")
    _key     = _ascii_header_safe(participant.api_key)
    headers  = {
        pconf["auth_header"]: f"{prefix}{_key}" if prefix else _key,
        "Content-Type": "application/json",
    }
    if participant.provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        safe_prompt = _utf8_clean(prompt)
        payload = {"model": model, "max_tokens": 800, "messages": [{"role": "user", "content": safe_prompt}]}
        r = await client.post(f"{base_url}/messages", headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        body = r.content.decode("utf-8", errors="replace")
        return _utf8_clean(json.loads(body)["content"][0]["text"].strip())
    safe_prompt = _utf8_clean(prompt)
    payload = {"model": model, "messages": [{"role": "user", "content": safe_prompt}], "max_tokens": 800}
    r = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return _utf8_clean(r.json()["choices"][0]["message"]["content"].strip())


async def _debate_moderator_assess(
    client: httpx.AsyncClient,
    history_text: str,
    round_num: int,
    fallback_participants: list,
) -> dict:
    prompt = DEBATE_MODERATOR_ASSESSMENT.format(history=history_text, round_num=round_num)
    try:
        r = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": LLM_MODEL, "stream": False, "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        r.raise_for_status()
        raw = r.json()["message"]["content"].strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        logger.error(f"Ordstyrer fejl (lokal): {type(e).__name__}: {e}")
        logger.warning("Ordstyrer fallback: bruger ekstern deltager")

    for p in fallback_participants:
        try:
            raw = await _call_external_llm(client, p, prompt)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception as e:
            logger.error(f"Ordstyrer fallback fejl ({p.label}): {type(e).__name__}: {e}")
    return {"consensus_reached": False, "summary": "Ordstyrer utilgængelig", "remaining_disagreements": []}


async def _debate_moderator_final(
    client: httpx.AsyncClient,
    history_text: str,
    fallback_participants: list,
) -> str:
    prompt = DEBATE_MODERATOR_FINAL.format(history=history_text)
    try:
        r = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": LLM_MODEL, "stream": False, "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        r.raise_for_status()
        result = _utf8_clean(r.json()["message"]["content"].strip())
        if result:
            return result
        logger.warning("Ordstyrer sammenfatning: tom streng fra lokal model")
    except Exception as e:
        logger.error(f"Ordstyrer sammenfatning fejl (lokal): {type(e).__name__}: {e}")
        logger.warning("Ordstyrer fallback: bruger ekstern deltager til sammenfatning")

    for p in fallback_participants:
        try:
            result = await _call_external_llm(client, p, prompt)
            if result:
                return result
        except Exception as e:
            logger.error(f"Ordstyrer sammenfatning fallback fejl ({p.label}): {type(e).__name__}: {e}")
    return "SVAR: Sammenfatning utilgængelig.\n\nKONSENSUS:\n- Ingen data\n\nUENIGHEDER:\n- Ingen data"


async def _run_debate_stream(req: DebateRequest):
    debate_id = str(uuid.uuid4())
    ctx_line  = f"\nBaggrundsinformation:\n{req.context}" if req.context else ""
    system_prompt = DEBATE_PARTICIPANT_SYSTEM.format(topic=req.topic, ctx_line=ctx_line)
    history: list[dict] = []
    rounds_log: list[dict] = []
    consensus_reached = False
    consensus_round   = None

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield sse("start", {"debate_id": debate_id, "topic": req.topic,
                         "participants": [p.label for p in req.participants]})

    all_labels   = [p.label for p in req.participants]
    next_challenge: str | None = None

    async with httpx.AsyncClient() as client:
        for round_num in range(1, req.max_rounds + 1):
            yield sse("round_start", {"round": round_num, "challenge": next_challenge or ""})
            contributions = []
            challenge_this_round = next_challenge
            next_challenge = None

            for participant in req.participants:
                other_labels = [l for l in all_labels if l != participant.label]
                text = await _debate_call_participant(
                    client, participant, system_prompt, history, req.topic,
                    round_num=round_num,
                    challenge=challenge_this_round,
                    other_labels=other_labels,
                )
                contribution = {"participant": participant.label, "text": text, "round": round_num}
                contributions.append(contribution)
                history.append({"role": "assistant",
                                 "content": f"[{participant.label}]: {text}"})
                yield sse("contribution", contribution)

            history_text = "\n\n".join(h["content"] for h in history)
            assessment   = await _debate_moderator_assess(client, history_text, round_num, req.participants)
            next_challenge = assessment.get("next_challenge") or None

            round_entry = {
                "round":                round_num,
                "contributions":        contributions,
                "moderator_assessment": assessment.get("summary", ""),
                "consensus_reached":    assessment.get("consensus_reached", False),
                "remaining":            assessment.get("remaining_disagreements", []),
                "next_challenge":       next_challenge or "",
            }
            rounds_log.append(round_entry)
            yield sse("round_end", round_entry)

            # Pause for bruger-input hvis aktiveret — ikke efter sidste runde
            consensus_stops_here = req.stop_on_consensus and bool(assessment.get("consensus_reached"))
            is_last_round = (round_num == req.max_rounds) or consensus_stops_here
            if req.user_participation and not is_last_round:
                q: asyncio.Queue = asyncio.Queue(maxsize=1)
                _debate_user_inputs[debate_id] = q
                yield sse("user_input_requested", {"debate_id": debate_id, "round": round_num})
                try:
                    user_text = await asyncio.wait_for(q.get(), timeout=float(req.user_input_timeout))
                    if user_text:
                        history.append({"role": "user", "content": f"[{_OWNER_NAME}]: {user_text}"})
                        yield sse("user_contribution", {
                            "participant": _OWNER_NAME, "text": user_text, "round": round_num
                        })
                except asyncio.TimeoutError:
                    yield sse("user_input_timeout", {
                        "round": round_num,
                        "message": f"Ingen kommentar inden {req.user_input_timeout}s — fortsætter"
                    })
                finally:
                    _debate_user_inputs.pop(debate_id, None)

            if assessment.get("consensus_reached"):
                consensus_reached = True
                consensus_round   = round_num
                if req.stop_on_consensus:
                    break

        # Final sammenfatning
        history_text   = "\n\n".join(h["content"] for h in history)
        final_summary  = _utf8_clean(await _debate_moderator_final(client, history_text, req.participants))

        # Gem i wing hvis ønsket
        saved_to   = None
        save_error = None
        if req.save_to_wing:
            wings_data = load_wings()
            wing_entry = find_wing(wings_data, req.save_to_wing)
            if not wing_entry:
                save_error = f"Wing '{req.save_to_wing}' ikke fundet"
                logger.error(f"Debat gem fejl: {save_error}")
            else:
                scope  = wing_entry.get("scope", "PRIVATE")
                # Strip api_keys fra participants — gemmes kun labels
                clean_rounds = []
                for r_entry in rounds_log:
                    clean_entry = dict(r_entry)
                    clean_entry["contributions"] = [
                        {k: v for k, v in c.items() if k != "api_key"}
                        for c in r_entry.get("contributions", [])
                    ]
                    clean_rounds.append(clean_entry)
                doc    = {
                    "type":              "debate",
                    "topic":             req.topic,
                    "debate_id":         debate_id,
                    "participants":      [p.label for p in req.participants],
                    "rounds":            clean_rounds,
                    "consensus_reached": consensus_reached,
                    "consensus_round":   consensus_round,
                    "final_summary":     final_summary,
                    "timestamp":         datetime.now(timezone.utc).isoformat(),
                }
                text_for_embed = f"Debat: {req.topic}\n\n{final_summary}"
                try:
                    embed_r = await asyncio.to_thread(
                        httpx.post,
                        f"{OLLAMA_URL}/api/embeddings",
                        json={"model": EMBED_MODEL, "prompt": text_for_embed[:8192]},
                        timeout=30,
                    )
                    embed_r.raise_for_status()
                    vector = embed_r.json()["embedding"]
                    point  = {
                        "id":      debate_id,
                        "vector":  vector,
                        "payload": {**doc, "text": text_for_embed, "scope": scope,
                                    "source_file": f"debat_{debate_id[:8]}.json"},
                    }
                    _wing  = req.save_to_wing
                    _scope = scope
                    arb_r = await asyncio.to_thread(
                        httpx.post,
                        f"{ARBITER_URL}/arbiter/write/sync",
                        json={"wing": _wing, "scope": _scope,
                              "operation": "upsert", "points": [point], "source": "debate"},
                        timeout=30,
                    )
                    arb_r.raise_for_status()
                    saved_to = req.save_to_wing
                    logger.info(f"Debat gemt i wing '{saved_to}' (debate_id={debate_id[:8]})")
                except Exception as e:
                    save_error = f"{type(e).__name__}: {e}"
                    logger.error(f"Debat gem fejl: {save_error}")

        final_payload = {
            "debate_id":         debate_id,
            "consensus_reached": consensus_reached,
            "consensus_round":   consensus_round,
            "final_summary":     final_summary,
            "rounds":            len(rounds_log),
            "saved_to":          saved_to,
            "save_error":        save_error,
        }
        try:
            yield sse("final", final_payload)
        except Exception as e:
            logger.error(f"SSE final serialisering fejlede: {type(e).__name__}: {e}")
            safe_summary = final_summary.encode("utf-8", errors="replace").decode("utf-8")
            yield (
                f"event: final\ndata: "
                + json.dumps({**final_payload, "final_summary": safe_summary}, ensure_ascii=True)
                + "\n\n"
            )


@app.post("/debate")
async def debate(req: DebateRequest):
    if not req.participants:
        raise HTTPException(400, "Mindst én deltager er påkrævet")
    if req.max_rounds < 1 or req.max_rounds > 10:
        raise HTTPException(400, "max_rounds skal være 1-10")
    for p in req.participants:
        if p.provider not in PROVIDER_DEFAULTS:
            raise HTTPException(400, f"Ukendt provider '{p.provider}'")
        if not p.api_key.strip():
            raise HTTPException(400, f"api_key mangler for deltager '{p.label}'")

    if req.save_to_wing:
        wings_data = load_wings()
        wing_entry = find_wing(wings_data, req.save_to_wing)
        if not wing_entry:
            raise HTTPException(404, f"Wing '{req.save_to_wing}' ikke fundet")
        scope = wing_entry.get("scope", "PRIVATE")
        if scope == "SECRET" and not req.scope_confirmed:
            return JSONResponse(status_code=403, content={"error": "scope_blocked", "scope": "SECRET"})
        if scope == "PRIVATE" and not req.scope_confirmed:
            return JSONResponse(status_code=403, content={"error": "scope_warning", "scope": "PRIVATE"})

    return StreamingResponse(
        _run_debate_stream(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# === Debate context upload ===

_PIPELINE_DIR = Path("/srv/nous/pipeline")

@app.post("/debate/context-upload")
async def debate_context_upload(file: UploadFile = File(...)):
    """Ekstrahér tekst fra uploadet fil til debat-kontekst. Gemmes ikke permanent."""
    content  = await file.read()
    filename = file.filename or ""
    suffix   = Path(filename).suffix.lower()

    if suffix in (".txt", ".md"):
        return {"text": content.decode("utf-8", errors="replace")[:20000]}

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        if str(_PIPELINE_DIR) not in sys.path:
            sys.path.insert(0, str(_PIPELINE_DIR))
        from ingest import extract_text
        text = extract_text(tmp_path)
        if not text:
            raise HTTPException(422, "Ingen tekst fundet i filen")
        return {"text": text[:20000]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"Tekstudtræk fejlede: {type(e).__name__}: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


class DebateUserInput(BaseModel):
    text: str = ""


@app.post("/debate/{debate_id}/user_input")
async def debate_user_input(debate_id: str, body: DebateUserInput):
    """Send bruger-kommentar til en igangværende debat der afventer input.

    Returnerer 404 hvis debatten ikke er aktiv eller ikke afventer bruger-input.
    """
    q = _debate_user_inputs.get(debate_id)
    if q is None:
        raise HTTPException(404, "Debatten er ikke aktiv eller afventer ikke bruger-input")
    try:
        q.put_nowait(body.text)
    except asyncio.QueueFull:
        raise HTTPException(409, "Input allerede modtaget for denne runde")
    return {"ok": True}


# === Feature defaults ===

FEATURE_DEFAULTS = {
    "panel_debat":      True,
    "swarm":            True,
    "legacy_interview": True,
    "ekstern_api":      True,
    "juridisk":         True,
    "orb":              True,
}

@app.get("/features")
def get_features():
    return FEATURE_DEFAULTS


# === Agent chat endpoint ===

@app.post("/agent/chat")
async def agent_chat(body: AgentChatRequest):
    if not _AGENTS_AVAILABLE:
        raise HTTPException(503, "Agent-system ikke tilgængeligt — tjek at LangGraph er installeret")
    try:
        result = await asyncio.to_thread(
            _run_agent_graph,
            body.prompt,
            body.user or "dan",
        )
    except Exception as e:
        raise HTTPException(503, f"Agent-fejl: {e}")
    return {
        "response":   result.get("response", ""),
        "agent":      result.get("agent_name", ""),
        "routed_to":  result.get("routed_to", ""),
    }


# === Swarm endpoints ===

SWARM_DB      = Path("/mnt/nous-data/swarm_queue.db")
SWARM_OUT_COL = "swarm_outgoing"
SWARM_IN_COL  = "swarm_incoming"
SWARM_PUB_COL = "swarm_public"   # hardkodet destination — klienten kan aldrig ændre dette


def _swarm_db() -> sqlite3.Connection:
    SWARM_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SWARM_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS promotion_queue (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at        TEXT DEFAULT (datetime('now')),
            status            TEXT DEFAULT 'pending',
            original_point_id TEXT NOT NULL,
            original_wing     TEXT NOT NULL,
            original_text     TEXT NOT NULL,
            anonymized_text   TEXT,
            confidence        REAL DEFAULT 0.0,
            reviewed_at       TEXT,
            swarm_point_id    TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public_publications (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            point_id        TEXT NOT NULL,
            original_wing   TEXT NOT NULL,
            published_at    TEXT NOT NULL,
            published_by    TEXT NOT NULL,
            content_hash    TEXT NOT NULL,
            prev_row_hash   TEXT
        )
    """)
    # Hash-chain audit for PRIVATE→SWARM godkendelses-trin (ikke kun SWARM→PUBLIC)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS swarm_approvals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_item_id   INTEGER NOT NULL,
            original_wing   TEXT    NOT NULL,
            anonymized_text TEXT    NOT NULL,
            approved_at     TEXT    NOT NULL,
            content_hash    TEXT    NOT NULL,
            prev_row_hash   TEXT,
            row_hash        TEXT    NOT NULL
        )
    """)
    # Migrér eksisterende promotion_queue der mangler high_sensitivity-kolonnen
    try:
        conn.execute("ALTER TABLE promotion_queue ADD COLUMN high_sensitivity INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    return conn


def _pub_row_hash(point_id: str, content_hash: str, published_at: str,
                  published_by: str, prev_row_hash: str | None) -> str:
    """Hash-chain: inkluderer forrige rækkes hash → tamper-evidens."""
    raw = f"{point_id}:{content_hash}:{published_at}:{published_by}:{prev_row_hash or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _approve_row_hash(queue_item_id: int, original_wing: str, approved_at: str,
                      content_hash: str, prev_row_hash: str | None) -> str:
    """Hash-chain for PRIVATE→SWARM godkendelses-trin."""
    raw = f"{queue_item_id}:{original_wing}:{approved_at}:{content_hash}:{prev_row_hash or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _ensure_swarm_public_collection() -> None:
    """Opret swarm_public Qdrant-collection hvis den ikke eksisterer."""
    try:
        r = httpx.get(f"{QDRANT_URL}/collections/{SWARM_PUB_COL}", timeout=5)
        if r.status_code == 200:
            return
        httpx.put(
            f"{QDRANT_URL}/collections/{SWARM_PUB_COL}",
            json={"vectors": {"size": 768, "distance": "Cosine"}, "on_disk_payload": True},
            timeout=10,
        ).raise_for_status()
        logger.info(f"Oprettet Qdrant-collection '{SWARM_PUB_COL}'")
    except Exception as e:
        logger.warning(f"swarm_public collection init fejl: {e}")


def _qdrant_count(collection: str) -> int:
    try:
        r = httpx.get(f"{QDRANT_URL}/collections/{collection}", timeout=5)
        return r.json().get("result", {}).get("points_count", 0)
    except Exception:
        return -1


@app.get("/swarm/queue")
def get_swarm_queue(status: str = "pending"):
    conn = _swarm_db()
    rows = conn.execute(
        "SELECT * FROM promotion_queue WHERE status = ? ORDER BY created_at DESC",
        (status,),
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        item = dict(r)
        item["high_sensitivity"] = bool(item.get("high_sensitivity", 0))
        items.append(item)
    return {"items": items, "status": status}


@app.post("/swarm/queue/{item_id}/approve")
def approve_swarm_item(item_id: int):
    conn = _swarm_db()
    row = conn.execute(
        "SELECT * FROM promotion_queue WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"Queue-item {item_id} ikke fundet")
    if row["status"] != "pending":
        conn.close()
        raise HTTPException(409, f"Item har status '{row['status']}' — kun pending kan godkendes")

    anon_text = row["anonymized_text"]
    if not anon_text:
        conn.close()
        raise HTTPException(400, "Intet anonymiseret tekst — kan ikke godkendes")

    # Generer embedding
    try:
        embed_r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": anon_text[:8192]},
            timeout=30,
        )
        embed_r.raise_for_status()
        vector = embed_r.json()["embedding"]
    except Exception as e:
        conn.close()
        raise HTTPException(503, f"Embedding fejl: {e}")

    # Skriv til swarm_outgoing via Arbiter
    swarm_point_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    point = {
        "id":      swarm_point_id,
        "vector":  vector,
        "payload": {
            "text":         anon_text,
            "type":         "fact",
            "scope":        "SWARM",
            "source":       "promotion_pipeline",
            "original_wing": row["original_wing"],
            "confidence":   row["confidence"],
            "approved_at":  now,
        },
    }
    try:
        r = httpx.post(
            f"{ARBITER_URL}/arbiter/write/sync",
            json={"wing": "swarm_outgoing", "scope": "SWARM", "operation": "upsert",
                  "points": [point], "source": "api_approve"},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        conn.close()
        raise HTTPException(503, f"Arbiter fejl: {e}")

    # Marker original med swarm_reviewed: true
    try:
        httpx.post(
            f"{QDRANT_URL}/collections/{_find_collection(row['original_wing'])}/points/payload",
            json={"payload": {"swarm_reviewed": True}, "points": [row["original_point_id"]]},
            timeout=10,
        )
    except Exception:
        pass

    # Skriv hashchain-audit for PRIVATE→SWARM godkendelse
    content_hash = hashlib.sha256(anon_text.encode()).hexdigest()
    prev_approval = conn.execute(
        "SELECT queue_item_id, original_wing, approved_at, content_hash, prev_row_hash, row_hash "
        "FROM swarm_approvals ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if prev_approval:
        prev_row_hash = _approve_row_hash(
            prev_approval["queue_item_id"],
            prev_approval["original_wing"],
            prev_approval["approved_at"],
            prev_approval["content_hash"],
            prev_approval["prev_row_hash"],
        )
    else:
        prev_row_hash = None
    row_hash = _approve_row_hash(item_id, row["original_wing"], now, content_hash, prev_row_hash)
    conn.execute(
        "INSERT INTO swarm_approvals "
        "(queue_item_id, original_wing, anonymized_text, approved_at, "
        "content_hash, prev_row_hash, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, row["original_wing"], anon_text, now,
         content_hash, prev_row_hash, row_hash),
    )

    # Opdater queue-status
    conn.execute(
        "UPDATE promotion_queue SET status='approved', reviewed_at=?, swarm_point_id=? WHERE id=?",
        (now, swarm_point_id, item_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "item_id": item_id, "swarm_point_id": swarm_point_id, "row_hash": row_hash}


@app.post("/swarm/queue/{item_id}/reject")
def reject_swarm_item(item_id: int):
    conn = _swarm_db()
    row = conn.execute(
        "SELECT * FROM promotion_queue WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"Queue-item {item_id} ikke fundet")

    now = datetime.now(timezone.utc).isoformat()

    # Marker original med swarm_reviewed: true
    try:
        httpx.post(
            f"{QDRANT_URL}/collections/{_find_collection(row['original_wing'])}/points/payload",
            json={"payload": {"swarm_reviewed": True}, "points": [row["original_point_id"]]},
            timeout=10,
        )
    except Exception:
        pass

    conn.execute(
        "UPDATE promotion_queue SET status='rejected', reviewed_at=? WHERE id=?",
        (now, item_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "item_id": item_id}


@app.get("/swarm/approvals")
def get_swarm_approvals(limit: int = Query(100, le=1000)):
    """Returnerer den tamper-evidente hashchain for alle PRIVATE→SWARM godkendelser."""
    conn = _swarm_db()
    rows = conn.execute(
        "SELECT id, queue_item_id, original_wing, approved_at, content_hash, "
        "prev_row_hash, row_hash FROM swarm_approvals ORDER BY id ASC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return {"approvals": [dict(r) for r in rows], "count": len(rows)}


@app.get("/swarm/status")
def get_swarm_status():
    conn = _swarm_db()
    counts: dict[str, int] = {}
    for s in ("pending", "approved", "rejected", "not_anonymizable"):
        row = conn.execute(
            "SELECT COUNT(*) FROM promotion_queue WHERE status = ?", (s,)
        ).fetchone()
        counts[s] = row[0] if row else 0
    conn.close()
    peers_r = _swarm_agent("GET", "/swarm/peers")
    peer_list = peers_r.get("peers", []) if peers_r else []
    compute_on = _env_get("SWARM_COMPUTE_ENABLED", "false").lower() == "true"
    return {
        "queue":          counts,
        "swarm_outgoing": _qdrant_count(SWARM_OUT_COL),
        "swarm_incoming": _qdrant_count(SWARM_IN_COL),
        "peers":          len(peer_list),
        "phase":          2,
        "compute_enabled": compute_on,
    }


def _find_collection(wing_name: str) -> str:
    data = load_wings()
    w = find_wing(data, wing_name)
    return w["collection"] if w else f"{wing_name}_private"


# ── Swarm Agent proxy helper ──────────────────────────────────────────────────

SWARM_AGENT_URL = os.environ.get("NOUS_SWARM_AGENT_URL", "http://localhost:8020")
NOUS_ENV_FILE   = Path("/srv/nous/.env")


def _swarm_agent(method: str, path: str, **kwargs) -> dict | None:
    try:
        r = httpx.request(method, f"{SWARM_AGENT_URL}{path}", timeout=30, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Swarm agent {method} {path} fejl: {e}")
        return None


def _env_get(key: str, default: str = "") -> str:
    if NOUS_ENV_FILE.exists():
        for line in NOUS_ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line[len(key) + 1:].strip().strip('"').strip("'")
    return os.environ.get(key, default)


def _env_set(key: str, value: str) -> None:
    lines = []
    found = False
    if NOUS_ENV_FILE.exists():
        for line in NOUS_ENV_FILE.read_text().splitlines():
            if line.strip().startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    NOUS_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOUS_ENV_FILE.write_text("\n".join(lines) + "\n")


# ── Swarm Phase 2 request models ──────────────────────────────────────────────

class AddPeerBody(BaseModel):
    tailscale_ip: str
    label: str
    swarm_type: str = "familia"
    port: int = 8020


class ComputeToggleBody(BaseModel):
    enabled: bool


# ── Peer management endpoints ─────────────────────────────────────────────────

@app.get("/swarm/peers")
def get_swarm_peers():
    data = _swarm_agent("GET", "/swarm/peers")
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


@app.get("/swarm/peers/status")
def get_swarm_peers_status():
    """Peer-liste med live load-status (via PeerLoadCache, 30s TTL)."""
    data = _swarm_agent("GET", "/swarm/peers/status")
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


@app.post("/swarm/peers/add")
def add_swarm_peer(body: AddPeerBody):
    data = _swarm_agent("POST", "/swarm/peers/add", json=body.model_dump())
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


@app.delete("/swarm/peers/{node_id}")
def remove_swarm_peer(node_id: str):
    data = _swarm_agent("DELETE", f"/swarm/peers/{node_id}")
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


@app.post("/swarm/sync/trigger")
def trigger_swarm_sync():
    data = _swarm_agent("POST", "/swarm/sync")
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig eller sync fejlede")
    return data


# ── Incoming facts endpoints ──────────────────────────────────────────────────

@app.get("/swarm/incoming")
def get_incoming_facts(limit: int = Query(50, le=200)):
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{SWARM_IN_COL}/points/scroll",
            json={"limit": limit, "with_payload": True, "with_vector": False},
            timeout=15,
        )
        r.raise_for_status()
        points = r.json().get("result", {}).get("points", [])
    except Exception as e:
        raise HTTPException(503, f"Qdrant fejl: {e}")
    return {
        "facts": [
            {
                "id":            str(pt["id"]),
                "text":          pt["payload"].get("text", ""),
                "source_label":  pt["payload"].get("source_label", "Ukendt"),
                "source_node":   pt["payload"].get("source_node", ""),
                "verified":      pt["payload"].get("verified", False),
                "confidence":    pt["payload"].get("confidence", 0),
                "received_at":   pt["payload"].get("received_at", ""),
                "original_wing": pt["payload"].get("original_wing", ""),
                "swarm_type":    pt["payload"].get("swarm_type", ""),
            }
            for pt in points
        ]
    }


@app.post("/swarm/incoming/{point_id}/verify")
def verify_incoming_fact(point_id: str):
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{SWARM_IN_COL}/points/payload",
            json={"payload": {"verified": True}, "points": [point_id]},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(503, f"Qdrant fejl: {e}")
    return {"ok": True, "point_id": point_id, "verified": True}


@app.delete("/swarm/incoming/{point_id}")
def reject_incoming_fact(point_id: str):
    try:
        r = httpx.delete(
            f"{QDRANT_URL}/collections/{SWARM_IN_COL}/points",
            json={"points": [point_id]},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(503, f"Qdrant fejl: {e}")
    return {"ok": True, "point_id": point_id, "deleted": True}


# ── Compute sharing toggle ────────────────────────────────────────────────────

@app.post("/swarm/compute/toggle")
def toggle_compute_sharing(body: ComputeToggleBody):
    val = "true" if body.enabled else "false"
    _env_set("SWARM_COMPUTE_ENABLED", val)
    try:
        subprocess.run(
            ["sudo", "systemctl", "restart", "nous-swarm"],
            timeout=15, check=True, capture_output=True,
        )
        restarted = True
    except Exception as e:
        logger.warning(f"Kunne ikke genstarte nous-swarm: {e}")
        restarted = False
    return {"ok": True, "compute_enabled": body.enabled, "restarted": restarted}


# ── Fase 3: Kin grupper ──────────────────────────────────────────────────────

class CreateGroupRequest(BaseModel):
    name: str
    group_type: str = "familia"
    allowed_wings: list[str] = []


class AddMemberRequest(BaseModel):
    node_id: str
    label: str


@app.get("/swarm/groups")
def get_swarm_groups():
    data = _swarm_agent("GET", "/swarm/groups")
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


@app.post("/swarm/groups")
def create_swarm_group(body: CreateGroupRequest):
    data = _swarm_agent("POST", "/swarm/groups", json={
        "name": body.name,
        "group_type": body.group_type,
        "allowed_wings": body.allowed_wings,
    })
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


@app.post("/swarm/groups/{group_id}/members")
def add_group_member(group_id: str, body: AddMemberRequest):
    data = _swarm_agent("POST", f"/swarm/groups/{group_id}/members", json=body.model_dump())
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


@app.delete("/swarm/groups/{group_id}")
def delete_swarm_group(group_id: str):
    data = _swarm_agent("DELETE", f"/swarm/groups/{group_id}")
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


# ── Fase 3: Credits ───────────────────────────────────────────────────────────

@app.get("/swarm/credits")
def get_swarm_credits():
    data = _swarm_agent("GET", "/swarm/credits")
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


# ── Fase 3: Wing-selektion ────────────────────────────────────────────────────

class WingSwarmConfigRequest(BaseModel):
    wing: str
    familia: bool = False
    global_: bool = False
    work: bool = False

    model_config = {"populate_by_name": True}


@app.get("/config/never-swarm")
def get_never_swarm():
    """Wings der aldrig kan deltage i SWARM — loades fra wings.json."""
    try:
        data = json.loads(WINGS_FILE.read_text())
        never = [w["name"] for w in data.get("wings", [])
                 if w.get("contains_personal_sensitive") or w.get("scope") == "SECRET"]
        return {"never": never}
    except Exception:
        return {"never": []}


@app.get("/swarm/wing-config")
def get_wing_swarm_config():
    data = _swarm_agent("GET", "/swarm/wing-config")
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


@app.post("/swarm/wing-config")
def update_wing_swarm_config(body: WingSwarmConfigRequest):
    data = _swarm_agent("POST", "/swarm/wing-config", json={
        "wing": body.wing,
        "config": {"familia": body.familia, "global": body.global_, "work": body.work},
    })
    if data is None:
        raise HTTPException(503, "Swarm agent ikke tilgængelig")
    return data


# ── SWARM → PUBLIC publishing ─────────────────────────────────────────────────

class PublishRequest(BaseModel):
    confirm: bool


@app.get("/swarm/public/eligible")
def get_public_eligible(limit: int = Query(100, le=500)):
    """Lister punkter i swarm_outgoing (hardkodet kilde — klient kan ikke ændre collection)."""
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{SWARM_OUT_COL}/points/scroll",
            json={"limit": limit, "with_payload": True, "with_vector": False},
            timeout=15,
        )
        r.raise_for_status()
        points = r.json().get("result", {}).get("points", [])
    except Exception as e:
        raise HTTPException(503, f"Qdrant fejl: {e}")

    # Hent allerede publicerede point_id'er for at markere dem
    conn = _swarm_db()
    published_ids = {
        row[0] for row in conn.execute("SELECT point_id FROM public_publications").fetchall()
    }
    conn.close()

    return {
        "facts": [
            {
                "id":            str(pt["id"]),
                "text":          pt["payload"].get("text", ""),
                "original_wing": pt["payload"].get("original_wing", ""),
                "confidence":    pt["payload"].get("confidence", 0),
                "approved_at":   pt["payload"].get("approved_at", ""),
                "already_published": str(pt["id"]) in published_ids,
            }
            for pt in points
        ],
        "source_collection": SWARM_OUT_COL,
    }


@app.post("/swarm/public/{point_id}/publish")
def publish_to_public(point_id: str, body: PublishRequest):
    """Kopiér et punkt fra swarm_outgoing til swarm_public med hash-chain."""
    if not body.confirm:
        raise HTTPException(400, "confirm skal være true")

    _ensure_swarm_public_collection()

    # Hent punkt fra hardkodet kilde (klient kan aldrig sende collection-navn)
    try:
        r = httpx.get(
            f"{QDRANT_URL}/collections/{SWARM_OUT_COL}/points/{point_id}",
            timeout=10,
        )
        if r.status_code == 404:
            raise HTTPException(404, f"Punkt {point_id} ikke fundet i {SWARM_OUT_COL}")
        r.raise_for_status()
        src_point = r.json().get("result")
        if not src_point:
            raise HTTPException(404, "Tomt svar fra Qdrant")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"Qdrant fejl ved hentning: {e}")

    payload   = src_point.get("payload", {})
    text      = payload.get("text", "")
    vector    = src_point.get("vector")

    # Beregn content_hash
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    now          = datetime.now(timezone.utc).isoformat()
    published_by = "dans_cockpit"

    # Hent forrige hash til chain
    conn = _swarm_db()
    prev_row = conn.execute(
        "SELECT id FROM public_publications ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if prev_row:
        prev_hash_row = conn.execute(
            "SELECT point_id, content_hash, published_at, published_by, prev_row_hash "
            "FROM public_publications WHERE id = ?",
            (prev_row[0],),
        ).fetchone()
        prev_hash = _pub_row_hash(
            prev_hash_row[0], prev_hash_row[1],
            prev_hash_row[2], prev_hash_row[3],
            prev_hash_row[4],
        )
    else:
        prev_hash = None

    row_hash = _pub_row_hash(point_id, content_hash, now, published_by, prev_hash)

    # Skriv til public_publications
    conn.execute(
        "INSERT INTO public_publications "
        "(point_id, original_wing, published_at, published_by, content_hash, prev_row_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (point_id, payload.get("original_wing", ""), now, published_by, content_hash, prev_hash),
    )
    conn.commit()
    conn.close()

    # Upsert til swarm_public (hardkodet destination)
    new_payload = {
        **payload,
        "scope":        "PUBLIC",
        "published_at": now,
        "published_by": published_by,
        "row_hash":     row_hash,
    }
    try:
        pub_r = httpx.put(
            f"{QDRANT_URL}/collections/{SWARM_PUB_COL}/points",
            json={"points": [{"id": point_id, "vector": vector, "payload": new_payload}]},
            timeout=15,
        )
        pub_r.raise_for_status()
    except Exception as e:
        raise HTTPException(503, f"Qdrant fejl ved publicering: {e}")

    logger.info(f"Publiceret punkt {point_id} til {SWARM_PUB_COL}, hash={row_hash[:12]}…")
    return {
        "ok":           True,
        "point_id":     point_id,
        "published_at": now,
        "row_hash":     row_hash,
        "destination":  SWARM_PUB_COL,
    }


# === Legacy interview endpoints ===

try:
    import interview_state as _istate
    import legacy_ingest as _lingest
    _LEGACY_AVAILABLE = True
except ImportError as _le:
    logging.warning("Legacy-modul ikke tilgængeligt: %s", _le)
    _LEGACY_AVAILABLE = False


class AnswerRequest(BaseModel):
    question_id:        str
    answer:             str
    parent_question_id: Optional[str] = None  # kun for opfølgningssvar
    question_text:      Optional[str] = None   # kun for opfølgningsspørgsmål ikke i question bank


def _require_legacy():
    if not _LEGACY_AVAILABLE:
        raise HTTPException(503, "Legacy-modul ikke tilgængeligt — tjek /srv/nous/legacy/")


_FOLLOWUP_PROMPT = """\
Dan har svaret på spørgsmålet: "{question}"
Hans svar: "{answer}"

Hvis svaret åbner for noget der er værd at udfolde — en historie, en følelse, en læring — stil ét kort naturligt opfølgningsspørgsmål på dansk. Max 20 ord.
Hvis svaret er fyldestgørende og ikke kalder på uddybning, returner kun: INGEN"""


def _generate_followup_sync(question: str, answer: str) -> str | None:
    """Generér opfølgningsspørgsmål via LLM_MODEL. Returnerer None hvis ikke relevant."""
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model":   LLM_MODEL,
                "stream":  False,
                "messages": [{"role": "user", "content": _FOLLOWUP_PROMPT.format(
                    question=question[:400], answer=answer[:800]
                )}],
                "options": {"temperature": 0.4},
            },
            timeout=30.0,
        )
        r.raise_for_status()
        raw = r.json()["message"]["content"].strip()
        if raw.upper().startswith("INGEN") or len(raw) < 8:
            return None
        # Fjern evt. anførselstegn og trim
        return raw.strip('"').strip("'").strip()
    except Exception as e:
        logging.warning("Legacy follow-up generering fejlede: %s", e)
        return None


@app.get("/legacy/daily-question")
def get_daily_question():
    """Dagens spørgsmål — samme hele dagen, nyt næste dag."""
    _require_legacy()
    q = _istate.get_daily_question()
    return q if q else {"question": None, "message": "Alle spørgsmål er besvaret"}


@app.get("/legacy/questions")
def get_questions(category: Optional[str] = None, answered: Optional[bool] = None):
    """Hent spørgsmål med status — filtrér på kategori og/eller besvaret-flag."""
    _require_legacy()
    questions = _istate.get_all_questions_with_status()
    if category:
        questions = [q for q in questions if q["category"] == category]
    if answered is not None:
        questions = [q for q in questions if q["answered"] == answered]
    return {"questions": questions}


@app.post("/legacy/answer", status_code=201)
async def submit_answer(body: AnswerRequest):
    """
    Gem Dans svar — ordret, ingen ændringer.
    1. Gem i interview_state.db
    2. Ingest i legacy-wing via Memory Arbiter
    3. Generér opfølgningsspørgsmål (max ét, ikke for opfølgningssvar)
    """
    _require_legacy()
    if not body.answer.strip():
        raise HTTPException(400, "Svar må ikke være tomt")

    answer = body.answer.strip()
    is_followup = body.parent_question_id is not None

    # Hent spørgsmålstekst — enten fra bank eller fra request (opfølgning)
    from questions import by_id as _by_id
    q = _by_id(body.question_id)
    if q:
        q_text    = q["question"]
        q_cat     = q["category"]
    elif body.question_text:
        q_text    = body.question_text
        q_cat     = "followup"
    else:
        raise HTTPException(404, f"Spørgsmål '{body.question_id}' ikke fundet")

    saved = _istate.save_answer(
        body.question_id, answer,
        parent_question_id=body.parent_question_id,
        question_text=body.question_text,
    )
    if not saved:
        raise HTTPException(500, "Kunne ikke gemme svar i database")

    ingested = True
    try:
        await asyncio.to_thread(
            _lingest.ingest_answer_to_legacy,
            body.question_id,
            q_text,
            answer,
            q_cat,
            body.parent_question_id,
        )
    except Exception as e:
        logging.error("Legacy ingest fejl: %s", e)
        ingested = False

    # Generér opfølgningsspørgsmål — kun for primære svar (ikke for opfølgninger selv)
    followup_question: str | None = None
    if not is_followup:
        followup_question = await asyncio.to_thread(
            _generate_followup_sync, q_text, answer
        )

    resp: dict = {"status": "saved", "ingested": ingested}
    if not ingested:
        resp["warning"] = "Ingest fejlede — svar er gemt i DB"
    if followup_question:
        resp["followup_question"] = followup_question
        resp["followup_question_id"] = f"followup_{body.question_id}"
    return resp


@app.get("/legacy/progress")
def get_legacy_progress():
    """Statistik: besvaret/total per kategori og samlet."""
    _require_legacy()
    return _istate.get_progress()


# === Kamera & Vision ===

class CameraAnalyzeRequest(BaseModel):
    prompt: str = "Beskriv hvad du ser på billedet. Svar på dansk."


async def _nx_capture() -> tuple[Path, str]:
    """SSH → NX ffmpeg /dev/video0, SCP tilbage til /tmp, returner (lokal_sti, base64_jpg)."""
    job_id = uuid.uuid4().hex[:8]
    remote  = f"/tmp/nous_cam_{job_id}.jpg"
    local   = Path(f"/tmp/nous_cam_{job_id}.jpg")

    cap = await asyncio.to_thread(subprocess.run, [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", NX_HOST,
        f"ffmpeg -y -f v4l2 -video_size 640x480 -i {NX_CAM_DEVICE} -frames:v 1 -q:v 4 {remote} 2>&1",
    ], capture_output=True, text=True)
    if cap.returncode != 0:
        raise HTTPException(502, f"Kamera capture fejl: {(cap.stdout or cap.stderr)[-300:]}")

    scp = await asyncio.to_thread(subprocess.run, [
        "scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        f"{NX_HOST}:{remote}", str(local),
    ], capture_output=True, text=True)
    if scp.returncode != 0:
        raise HTTPException(502, f"SCP fejl: {scp.stderr[-300:]}")

    asyncio.create_task(asyncio.to_thread(
        subprocess.run,
        ["ssh", "-o", "BatchMode=yes", NX_HOST, f"rm -f {remote}"],
        capture_output=True,
    ))

    with open(local, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    return local, b64


@app.post("/camera/capture")
async def camera_capture():
    """Tag et stillbillede fra /dev/video0 på NX via SSH+ffmpeg. Returnerer base64 JPEG."""
    local, b64 = await _nx_capture()
    return {"image": f"data:image/jpeg;base64,{b64}", "path": str(local)}


@app.post("/camera/analyze")
async def camera_analyze(body: CameraAnalyzeRequest):
    """Capture billede + Gemma 4 multimodal analyse via llama.cpp-server på NX:8181."""
    local, b64 = await _nx_capture()
    try:
        await gemma_manager.start()
    except Exception as exc:
        raise HTTPException(503, f"Kunne ikke starte Gemma: {exc}")
    try:
        # llama.cpp mtmd kræver /completion endpoint med [img-N] placeholder i prompt.
        # /v1/chat/completions afviser typed content (supports_typed_content: false).
        prompt = (
            f"<bos><start_of_turn>user\n[img-1]\n{body.prompt}<end_of_turn>\n"
            f"<start_of_turn>model\n"
        )
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{NX_LLAMA_URL}/completion",
                json={
                    "prompt": prompt,
                    "image_data": [{"data": b64, "id": 1}],
                    "max_tokens": 1024,
                    "temperature": 0.3,
                },
            )
        resp.raise_for_status()
        data = resp.json()
        analysis = data["content"]
    except httpx.ConnectError:
        raise HTTPException(503, "llama.cpp-server ikke tilgængelig på NX:8181")
    except Exception as exc:
        raise HTTPException(502, f"Gemma 4 fejl: {exc}")
    finally:
        await gemma_manager.stop()
    return {
        "image":    f"data:image/jpeg;base64,{b64}",
        "analysis": analysis,
        "prompt":   body.prompt,
    }


# === Voice preferences ===

VOICE_PREFS_FILE = Path("/srv/nous/config/voice_prefs.json")
TTS_MODELS_DIR   = Path("/srv/nous/models/tts")
HF_VOICES_URL    = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
_voice_dl_status: dict[str, dict] = {}
_voices_cache: dict | None = None

_DEFAULT_VOICE_PREFS = {
    "Dan":     {"model_key": "da_DK-talesyntese-medium", "model_path": "/srv/nous/models/tts/da.onnx"},
    "Gaia":    {"model_key": "da_DK-talesyntese-medium", "model_path": "/srv/nous/models/tts/da.onnx"},
    "Gabriel": {"model_key": "da_DK-talesyntese-medium", "model_path": "/srv/nous/models/tts/da.onnx"},
}


def _load_voice_prefs() -> dict:
    if not VOICE_PREFS_FILE.exists():
        return dict(_DEFAULT_VOICE_PREFS)
    try:
        return json.loads(VOICE_PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return dict(_DEFAULT_VOICE_PREFS)


def _save_voice_prefs(prefs: dict) -> None:
    VOICE_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    VOICE_PREFS_FILE.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")


class VoicePrefSet(BaseModel):
    user: str        # Dan | Gaia | Gabriel
    model_key: str   # e.g. da_DK-talesyntese-medium
    model_path: str  # absolute path to .onnx on disk


@app.get("/voice/prefs")
def get_voice_prefs():
    return _load_voice_prefs()


@app.post("/voice/pref")
def set_voice_pref(body: VoicePrefSet):
    if body.user not in ("Dan", "Gaia", "Gabriel"):
        raise HTTPException(400, "Ukendt bruger")
    prefs = _load_voice_prefs()
    prefs[body.user] = {"model_key": body.model_key, "model_path": body.model_path}
    _save_voice_prefs(prefs)
    return {"ok": True, "user": body.user, "model_key": body.model_key}


@app.get("/voice/voices")
async def get_piper_voices():
    global _voices_cache
    if _voices_cache:
        return _voices_cache
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(HF_VOICES_URL)
            r.raise_for_status()
        _voices_cache = r.json()
        return _voices_cache
    except Exception as e:
        raise HTTPException(502, f"Kunne ikke hente Piper voice-liste: {e}")


@app.get("/voice/installed")
def get_installed_voices():
    installed = []
    if TTS_MODELS_DIR.exists():
        for f in TTS_MODELS_DIR.glob("*.onnx"):
            # Skip *.onnx.json (they don't end in just .onnx)
            installed.append(f.name)
    return {"installed": installed}


class VoiceDownloadRequest(BaseModel):
    model_key: str
    onnx_path: str   # relative path within HF repo, e.g. da_DK/talesyntese/medium/da_DK-talesyntese-medium.onnx


def _do_voice_download(job_id: str, model_key: str, onnx_path: str) -> None:
    def upd(phase: str, pct: int, msg: str) -> None:
        _voice_dl_status[job_id] = {"phase": phase, "progress": pct, "msg": msg}

    base       = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
    onnx_name  = Path(onnx_path).name
    json_name  = onnx_name + ".json"
    onnx_local = TTS_MODELS_DIR / onnx_name
    json_local = TTS_MODELS_DIR / json_name
    TTS_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # Stream the large ONNX file in 512KB chunks with live progress
        upd("downloading", 2, f"Forbinder til HuggingFace…")
        with httpx.stream("GET", f"{base}/{onnx_path}", timeout=httpx.Timeout(30, read=300), follow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(onnx_local, "wb") as fh:
                for chunk in r.iter_bytes(chunk_size=512 * 1024):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = min(85, int(5 + 80 * downloaded / total))
                        mb_done = downloaded / (1024 * 1024)
                        mb_total = total / (1024 * 1024)
                        upd("downloading", pct, f"{onnx_name} — {mb_done:.1f}/{mb_total:.0f} MB")
                    else:
                        upd("downloading", 40, f"{onnx_name} — {downloaded // (1024*1024)} MB")

        upd("downloading", 90, f"Henter config {json_name}…")
        r2 = httpx.get(f"{base}/{onnx_path}.json", timeout=30, follow_redirects=True)
        r2.raise_for_status()
        json_local.write_bytes(r2.content)

        upd("done", 100, f"{model_key} installeret ✓")
    except Exception as e:
        upd("error", 0, f"Download fejlede: {str(e)[:180]}")
        onnx_local.unlink(missing_ok=True)
        json_local.unlink(missing_ok=True)


@app.post("/voice/download", status_code=202)
async def download_voice(body: VoiceDownloadRequest, background_tasks: BackgroundTasks):
    job_id = uuid.uuid4().hex[:12]
    _voice_dl_status[job_id] = {"phase": "queued", "progress": 0, "msg": f"Downloader {body.model_key}…"}
    background_tasks.add_task(_do_voice_download, job_id, body.model_key, body.onnx_path)
    return {"job_id": job_id, "model_key": body.model_key}


@app.get("/voice/download-status/{job_id}")
def get_voice_download_status(job_id: str):
    return _voice_dl_status.get(job_id, {"phase": "not_found", "progress": 0, "msg": "Ukendt job"})


# === UI preferences (language etc.) ===

UI_PREFS_FILE = Path("/srv/nous/config/ui_prefs.json")

_DEFAULT_FEATURES = {
    "Dan":     {"mic": True, "web_search": True, "external_api": True,  "kids_mode": False, "legacy": True,  "juridisk": True},
    "Gaia":    {"mic": True, "web_search": True, "external_api": False, "kids_mode": True,  "legacy": False, "juridisk": False},
    "Gabriel": {"mic": True, "web_search": True, "external_api": False, "kids_mode": False, "legacy": False, "juridisk": True},
}

_DEFAULT_UI_PREFS = {
    "Dan":     {"lang": "da", "features": _DEFAULT_FEATURES["Dan"]},
    "Gaia":    {"lang": "da", "features": _DEFAULT_FEATURES["Gaia"]},
    "Gabriel": {"lang": "da", "features": _DEFAULT_FEATURES["Gabriel"]},
}


def _load_ui_prefs() -> dict:
    if not UI_PREFS_FILE.exists():
        return dict(_DEFAULT_UI_PREFS)
    try:
        return json.loads(UI_PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return dict(_DEFAULT_UI_PREFS)


def _save_ui_prefs(prefs: dict) -> None:
    UI_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    UI_PREFS_FILE.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")


class UiPrefSet(BaseModel):
    user: str   # Dan | Gaia | Gabriel
    lang: str   # da | en


@app.get("/ui/prefs")
def get_ui_prefs():
    return _load_ui_prefs()


@app.get("/ui/pref/{user}")
def get_ui_pref_user(user: str):
    if user not in ("Dan", "Gaia", "Gabriel"):
        raise HTTPException(400, "Ukendt bruger")
    prefs = _load_ui_prefs()
    return prefs.get(user, {"lang": "da"})


@app.post("/ui/pref")
def set_ui_pref(body: UiPrefSet):
    if body.user not in ("Dan", "Gaia", "Gabriel"):
        raise HTTPException(400, "Ukendt bruger")
    if body.lang not in ("da", "en"):
        raise HTTPException(400, "Ugyldigt sprog — skal være 'da' eller 'en'")
    prefs = _load_ui_prefs()
    prefs.setdefault(body.user, {})["lang"] = body.lang
    _save_ui_prefs(prefs)
    return {"ok": True, "user": body.user, "lang": body.lang}


class FeatureSet(BaseModel):
    user: str
    features: dict  # {mic, web_search, external_api, kids_mode}


_KNOWN_FEATURES = {"mic", "web_search", "external_api", "kids_mode", "legacy", "juridisk"}


@app.get("/ui/features")
def get_all_features():
    prefs = _load_ui_prefs()
    result = {}
    for user in ("Dan", "Gaia", "Gabriel"):
        stored = prefs.get(user, {}).get("features", {})
        result[user] = {**_DEFAULT_FEATURES.get(user, {}), **stored}
    return result


@app.get("/ui/features/{user}")
def get_user_features(user: str):
    if user not in ("Dan", "Gaia", "Gabriel"):
        raise HTTPException(400, "Ukendt bruger")
    prefs = _load_ui_prefs()
    stored = prefs.get(user, {}).get("features", {})
    return {**_DEFAULT_FEATURES.get(user, {}), **stored}


@app.post("/ui/features")
def set_user_features(body: FeatureSet):
    if body.user not in ("Dan", "Gaia", "Gabriel"):
        raise HTTPException(400, "Ukendt bruger")
    unknown = set(body.features.keys()) - _KNOWN_FEATURES
    if unknown:
        raise HTTPException(400, f"Ukendte features: {unknown}")
    prefs = _load_ui_prefs()
    prefs.setdefault(body.user, {}).setdefault("features", {}).update(body.features)
    _save_ui_prefs(prefs)
    return {"ok": True, "user": body.user, "features": prefs[body.user]["features"]}


# === Wake word configuration ===

WAKE_CONFIG_FILE = Path("/srv/nous/config/wake_prefs.json")
WAKE_KEYWORDS_FILE = Path("/srv/nous/models/kws/wake_words.txt")
WAKE_TOKENS_FILE = Path("/srv/nous/models/kws/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01/tokens.txt")

_DEFAULT_WAKE_CONFIG = {
    "Dan":     {"word": "AI",     "tokens": "▁A I"},
    "Gaia":    {"word": "STITCH", "tokens": "▁ST IT CH"},
    "Gabriel": {"word": "CEPTER", "tokens": "▁C E P TER"},
}

# Build tokenizer vocab once at startup
def _build_wake_vocab() -> list[str]:
    if not WAKE_TOKENS_FILE.exists():
        return []
    vocab = []
    for line in WAKE_TOKENS_FILE.read_text().splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] not in ("<blk>", "<sos/eos>", "<unk>"):
            vocab.append(parts[0])
    return sorted(vocab, key=len, reverse=True)

_WAKE_VOCAB: list[str] = _build_wake_vocab()


def _tokenize_wake_word(word: str) -> str | None:
    """Greedy longest-match tokenizer. Returns token string or None if word can't be tokenized."""
    text = "▁" + word.upper().strip()
    result = []
    i = 0
    while i < len(text):
        matched = False
        for tok in _WAKE_VOCAB:
            if text[i:].startswith(tok):
                result.append(tok)
                i += len(tok)
                matched = True
                break
        if not matched:
            return None
    return " ".join(result)


def _load_wake_config() -> dict:
    if not WAKE_CONFIG_FILE.exists():
        return dict(_DEFAULT_WAKE_CONFIG)
    try:
        return json.loads(WAKE_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return dict(_DEFAULT_WAKE_CONFIG)


def _save_wake_config(cfg: dict) -> None:
    WAKE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    WAKE_CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    # Rewrite keywords file
    lines = [v["tokens"] for v in cfg.values() if v.get("tokens")]
    WAKE_KEYWORDS_FILE.write_text("\n".join(lines) + "\n")


class WakeWordEntry(BaseModel):
    user: str   # Dan | Gaia | Gabriel
    word: str   # e.g. NOUS


class WakeConfigSet(BaseModel):
    entries: list[WakeWordEntry]
    restart_service: bool = True


@app.get("/wake/config")
def get_wake_config():
    return _load_wake_config()


@app.get("/wake/tokenize")
def tokenize_wake_word(word: str):
    tokens = _tokenize_wake_word(word)
    return {"word": word.upper(), "tokens": tokens, "ok": tokens is not None}


@app.post("/wake/config")
def set_wake_config(body: WakeConfigSet, background_tasks: BackgroundTasks):
    if not body.entries:
        raise HTTPException(400, "Mindst ét wake word er påkrævet")

    cfg = _load_wake_config()
    errors = []
    for entry in body.entries:
        if entry.user not in ("Dan", "Gaia", "Gabriel"):
            errors.append(f"Ukendt bruger: {entry.user}")
            continue
        word = entry.word.strip().upper()
        if not word:
            errors.append(f"Tomt wake word for {entry.user}")
            continue
        tokens = _tokenize_wake_word(word)
        if tokens is None:
            errors.append(f"'{word}' kan ikke tokeniseres med model-vokabularet")
            continue
        cfg[entry.user] = {"word": word, "tokens": tokens}

    if errors:
        raise HTTPException(400, "; ".join(errors))

    _save_wake_config(cfg)

    if body.restart_service:
        background_tasks.add_task(_restart_voice_service)

    return {"ok": True, "config": cfg, "restarting": body.restart_service}


def _restart_voice_service() -> None:
    try:
        subprocess.run(
            ["sudo", "systemctl", "restart", "nous-voice-assistant.service"],
            timeout=15, check=True, capture_output=True,
        )
        logger.info("nous-voice-assistant.service genstartet")
    except Exception as e:
        logger.warning(f"Kunne ikke genstarte voice-service: {e}")
