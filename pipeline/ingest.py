#!/usr/bin/env python3
"""
NOUS Ingest Pipeline — Session 2
Overvåger /home/nous/incoming/, extract tekst, embed, upsert til Qdrant Wings
"""

import os
import sys
import json
import time
import uuid
import shutil
import hashlib
import requests
import importlib.metadata as _md
from pathlib import Path
from datetime import datetime


from task_router import router

# === Ingest telemetry — LOCAL, stdlib-only, bounded ==========================
# D5 (2026-09-07): the previous tap `sys.path`-injected the DO_NOT_INTEGRATE
# `rna_phase1a` sandbox and wrote an unrotated JSONL — that unbounded file was
# the source of defect C (6.76 GB). Production now has ZERO dependency on the
# sandbox. `_tel()` appends one JSON line to a local file next to this module
# and hard-rotates at _TEL_MAX_BYTES (keeping only current + .1 → a fixed
# ceiling). Fail-open: any error is swallowed, ingest continues. The rna
# sandbox keeps its own separate copy of a telemetry writer for its own
# research — that is sandbox-local and not our concern.
_TEL_FILE = Path(__file__).with_name("ingest_events.jsonl")
_TEL_MAX_BYTES = 10 * 1024 * 1024


def _tel(event_type, source="ingest", *, documents_processed=0,
         write_latency_ms=None, extra=None, **_ignored) -> bool:
    try:
        rec = {
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "event_type": event_type,
            "documents_processed": documents_processed,
            "write_latency_ms": write_latency_ms,
        }
        if extra:
            rec["extra"] = extra
        try:
            if _TEL_FILE.exists() and _TEL_FILE.stat().st_size >= _TEL_MAX_BYTES:
                bak = _TEL_FILE.with_suffix(_TEL_FILE.suffix + ".1")
                try:
                    bak.unlink()
                except OSError:
                    pass
                _TEL_FILE.rename(bak)          # fixed 2-file ceiling
        except OSError:
            pass
        with open(_TEL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False

# === CONFIG ===
WATCH_DIR = Path("/home/nous/incoming")
QDRANT_URL = "http://localhost:6333"
# Bruger Task Router i stedet
EMBED_MODEL = "nomic-embed-text"
CHUNK_SIZE = 512
CHUNK_OVERLAP = 50

_WINGS_CONFIG = Path("/srv/nous/config/wings.json")

def _load_wing_map() -> dict:
    if not _WINGS_CONFIG.exists():
        log(f"ADVARSEL: {_WINGS_CONFIG} ikke fundet — ingen wings konfigureret")
        return {}
    data = json.loads(_WINGS_CONFIG.read_text())
    mapping = {}
    for w in data.get("wings", []):
        name  = w["name"]
        scope = w.get("scope", "PRIVATE")
        mapping[name] = (name, scope)
        for alias in w.get("aliases", []):
            mapping[alias] = (name, scope)
    return mapping

WING_MAP = _load_wing_map()

def log(msg):
    ts = datetime.now().isoformat()
    print(f"[{ts}] {msg}", flush=True)

def extract_text(filepath):
    """Extract text fra PDF, DOCX, TXT"""
    path = Path(filepath)
    
    if path.suffix.lower() == ".pdf":
        try:
            import fitz  # pymupdf
            doc = fitz.open(path)
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            return text
        except Exception as e:
            log(f"PDF extract fejl: {e}")
            return None
            
    elif path.suffix.lower() in (".docx", ".doc"):
        try:
            from docx import Document
            doc = Document(path)
            text = "\n".join(p.text for p in doc.paragraphs)
            return text
        except Exception as e:
            log(f"DOCX extract fejl: {e}")
            return None
            
    elif path.suffix.lower() == ".txt":
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            log(f"TXT read fejl: {e}")
            return None
    
    else:
        log(f"Ukendt filtype: {path.suffix}")
        return None

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split tekst i overlappende chunks"""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + size])
        chunks.append(chunk)
        i += size - overlap
    return chunks

def embed_text(text):
    """Send til Jetson Ollama for embedding"""
    try:
        r = requests.post(router.ollama_url + "/api/embeddings", json={
            "model": EMBED_MODEL,
            "prompt": text[:8192]  # nomic-embed-text max context
        }, timeout=30)
        r.raise_for_status()
        return r.json()["embedding"]
    except Exception as e:
        log(f"Embedding fejl: {e}")
        return None

def get_wing(filepath):
    """Bestem wing ud fra mappestruktur under /home/nous/incoming/"""
    path = Path(filepath)
    
    # Kun tjek under incoming/
    try:
        rel = path.relative_to("/home/nous/incoming/")
        parts = rel.parts
    except ValueError:
        parts = path.parts
    
    for part in parts:
        lower = part.lower()
        if lower in WING_MAP:
            return WING_MAP[lower]
    
    # Default
    log(f"Ingen wing-match for {filepath}, bruger nous_projekt/PRIVATE")
    return ("nous_projekt", "PRIVATE")

def upsert_to_qdrant(wing, scope, points):
    """Upsert points til Qdrant collection"""
    collection = f"{wing}_{scope.lower()}"
    url = f"{QDRANT_URL}/collections/{collection}/points"
    try:
        r = requests.put(url, json={"points": points}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"Qdrant upsert fejl: {e}")
        return False

def process_file(filepath):
    """Hovedflow: extract → chunk → embed → upsert.

    Returns "ok" | "reject" (DETERMINISTIC — never embeddable via this path,
    e.g. no extractable text) | "retry" (TRANSIENT — Qdrant/Ollama down;
    the work is still required and must not be permanently dropped).
    """
    filepath = Path(filepath)
    log(f"=== Processing: {filepath.name} ===")

    _tel("ingest_start", source="ingest", extra={"file": filepath.name})

    # Bestem wing
    wing, scope = get_wing(filepath)
    log(f"  Wing: {wing}, Scope: {scope}")

    # Extract tekst
    text = extract_text(filepath)
    if not text or len(text.strip()) == 0:
        if text is None:
            _LAST_REJECT["reason"] = "no_extractable_text"
            _LAST_REJECT["evidence"] = f"extract_text returned None for {filepath.suffix} (parser failed / unsupported)"
        else:
            _LAST_REJECT["reason"] = "no_extractable_text"
            _LAST_REJECT["evidence"] = f"extract_text returned {len(text)} chars ({len(text.strip())} non-whitespace)"
        log(f"  Ingen brugbar tekst — deterministisk afvisning: {_LAST_REJECT['evidence']}")
        _tel("error", source="ingest", extra={"reason": "no_text", "file": filepath.name})
        return "reject"

    log(f"  Tekst længde: {len(text)} chars")

    # Chunk
    chunks = chunk_text(text)
    log(f"  Chunks: {len(chunks)}")

    # Embed og upsert batch
    points = []
    for i, chunk in enumerate(chunks):
        vector = embed_text(chunk)
        if vector is None:
            log(f"  Chunk {i}: embedding fejlede, skipper")
            _tel("error", source="ingest",
                 extra={"reason": "embed_failed", "chunk": i, "file": filepath.name})
            continue

        point = {
            "id": str(uuid.uuid4()),
            "vector": vector,
            "payload": {
                "source_file": filepath.name,
                "chunk_index": i,
                "scope": scope,
                "text": chunk,
                "timestamp": datetime.now().isoformat(),
                "content_hash": hashlib.sha256(chunk.encode()).hexdigest()[:16]
            }
        }
        points.append(point)
        log(f"  Chunk {i}: embedded ({len(vector)} dim)")

    # Upsert til Qdrant
    if points:
        t0 = time.time()
        ok = upsert_to_qdrant(wing, scope, points)
        write_lat = round((time.time() - t0) * 1000, 1)
        if ok:
            log(f"  UPSERTET: {len(points)} points til {wing}")
            _tel("ingest_batch", source="ingest",
                 documents_processed=len(points),
                 write_latency_ms=write_lat,
                 extra={"wing": wing, "scope": scope, "file": filepath.name})
            return "ok"
        else:
            log(f"  FEJL: Kunne ikke upserte til {wing} — transient, prøver igen")
            _tel("error", source="ingest",
                 documents_processed=len(points),
                 write_latency_ms=write_lat,
                 extra={"reason": "qdrant_upsert_failed", "wing": wing})
            return "retry"
    else:
        log("  Ingen points at upserte — embedding utilgængelig, transient")
        _tel("error", source="ingest",
             extra={"reason": "no_points", "file": filepath.name})
        return "retry"

# --- retry / quarantine HOW (longevity/audit closure D3, 2026-09-06/07) -------
# Dan: a fixed attempt-cap is REJECTED.
#   TRANSIENT  ("retry": Qdrant/Ollama/timeout) → exponential backoff, stays in
#     the scan forever, retried at a bounded RATE (30 s → … → 1 h cap). NEVER
#     dropped, NEVER moved.
#   DETERMINISTIC ("reject": no extractable text) → moved to incoming/_rejected/
#     — QUARANTINE, ikke gravplads. It is NOT constantly rescanned, BUT it is
#     automatically re-evaluated whenever a RELEVANT capability CHANGES
#     (extractor version, a new parser, OCR becomes available, the failure
#     condition otherwise disappears). A manual operator requeue (move the file
#     back by hand) still works and is documented — but it is the fallback, not
#     the only way back.
_REJECTED_DIRNAME = "_rejected"
_MANIFEST_NAME = "MANIFEST.jsonl"
_BACKOFF_START_S = 30.0
_BACKOFF_MAX_S = 3600.0
_CAP_RECHECK_S = 300.0        # recompute the capability fingerprint at most this often

_LAST_REJECT = {"reason": "no_extractable_text", "evidence": ""}

# which fingerprint dimensions matter for a given reject reason. A
# "no_extractable_text" reject only cares about text extraction — NOT the
# embedding model. Unknown reasons fall back to "everything" (conservative).
_REJECT_RELEVANCE = {
    "no_extractable_text": ("extractor", "parsers", "ocr"),
}
_ALL_DIMS = ("extractor", "parsers", "ocr", "embedding_model")


def _pkg_version(*names) -> str | None:
    for n in names:
        try:
            return _md.version(n)
        except Exception:
            pass
    return None


def _ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return False
    return shutil.which("tesseract") is not None


def capability_fingerprint() -> dict:
    """The CURRENT text-extraction/embedding capability state, computed the
    same cheap way every time. A quarantined file is re-evaluated only when a
    dimension of this that is RELEVANT to its reject reason has changed."""
    parsers = {}
    if _pkg_version("pymupdf", "PyMuPDF", "fitz"):
        parsers["pdf"] = _pkg_version("pymupdf", "PyMuPDF", "fitz")
    if _pkg_version("python-docx", "docx"):
        parsers["docx"] = _pkg_version("python-docx", "docx")
    parsers["txt"] = "builtin"
    return {
        "extractor": {
            "pymupdf": _pkg_version("pymupdf", "PyMuPDF", "fitz"),
            "python_docx": _pkg_version("python-docx", "docx"),
        },
        "parsers": parsers,
        "ocr": _ocr_available(),
        "embedding_model": EMBED_MODEL,
    }


def _relevantly_changed(reason: str, old_fp: dict, new_fp: dict) -> bool:
    dims = _REJECT_RELEVANCE.get(reason, _ALL_DIMS)
    return any((old_fp or {}).get(d) != (new_fp or {}).get(d) for d in dims)


def _content_hash(p: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _manifest_path() -> Path:
    return WATCH_DIR / _REJECTED_DIRNAME / _MANIFEST_NAME


def _manifest_append(entry: dict) -> None:
    try:
        mp = _manifest_path()
        mp.parent.mkdir(parents=True, exist_ok=True)
        with open(mp, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_quarantine(state) -> None:
    """Rebuild state['quarantine'] from the append-only manifest on startup.
    quarantine: {moved_to_path: entry}. An entry is live iff its most recent
    manifest event is 'quarantine' or 'requarantine' (a 'return' clears it)."""
    q = {}
    mp = _manifest_path()
    if not mp.exists():
        state["quarantine"] = q
        return
    try:
        for line in mp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            ev = e.get("event", "quarantine")
            mt = (e.get("provenance") or {}).get("moved_to") or e.get("moved_to")
            if not mt:
                continue
            if ev in ("quarantine", "requarantine"):
                q[mt] = e
            elif ev == "return":
                q.pop(mt, None)
    except (OSError, json.JSONDecodeError):
        pass
    state["quarantine"] = q


def _reject_file(filepath: Path, state, reason: str, evidence: str,
                 prior_evaluations: int = 0) -> Path:
    """QUARANTINE a deterministically-unprocessable file. Moves it aside, writes
    a rich manifest entry (reason / timestamp / capability fingerprint /
    provenance / attempt state), tracks it in state['quarantine']. Never
    deletes. Auto-re-evaluated on a relevant capability change (see
    reevaluate_quarantine)."""
    rej_dir = WATCH_DIR / _REJECTED_DIRNAME
    rej_dir.mkdir(parents=True, exist_ok=True)
    dest = rej_dir / filepath.name
    n = 1
    while dest.exists():
        dest = rej_dir / f"{filepath.stem}.{n}{filepath.suffix}"
        n += 1
    try:
        wing, scope = get_wing(filepath)
    except Exception:
        wing, scope = None, None
    provenance = {
        "original_path": str(filepath),
        "moved_to": str(dest),
        "source_file": filepath.name,
        "wing": wing, "scope": scope, "subcategory": None,   # HEAD get_wing is 2-tuple
        "content_hash": _content_hash(filepath),
    }
    try:
        filepath.rename(dest)
    except OSError:
        dest = filepath
        provenance["moved_to"] = str(dest)

    now_iso = datetime.now().isoformat()
    evals = prior_evaluations + 1
    entry = {
        "event": "requarantine" if prior_evaluations else "quarantine",
        "reason": reason,
        "rejected_at": now_iso,
        "capability_fingerprint": capability_fingerprint(),
        "provenance": provenance,
        "attempt_state": {
            "evaluations": evals,
            "last_attempt_at": now_iso,
            "evidence": evidence,
        },
        "requeue_note": ("auto-returns to incoming/ when a relevant capability "
                         "changes (extractor/parser/OCR); a manual move back "
                         "into incoming/<wing>/ also works as a fallback"),
    }
    _manifest_append(entry)
    state.setdefault("quarantine", {})[str(dest)] = entry
    _tel("error", source="ingest",
         extra={"reason": "quarantine", "file": filepath.name,
                "moved_to": str(dest), "detail": evidence,
                "evaluations": evals})
    return dest


def reevaluate_quarantine(state) -> int:
    """Once per scan cycle: for each quarantined file, if a capability dimension
    RELEVANT to its reject reason has changed since it was quarantined, move it
    back into the queue (exactly once — the return event clears it from
    quarantine; if it fails again it re-quarantines against the NEW fingerprint
    and will not re-trigger until the NEXT change). Returns #files returned."""
    cur = state["cap_fp"]
    returned = 0
    for mt, entry in list(state.get("quarantine", {}).items()):
        old_fp = entry.get("capability_fingerprint") or {}
        reason = entry.get("reason", "")
        if not _relevantly_changed(reason, old_fp, cur):
            continue
        prov = entry.get("provenance") or {}
        src = Path(mt)
        dst = Path(prov.get("original_path") or "")
        if src.exists() and str(dst) and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
            except OSError:
                continue
        _manifest_append({
            "event": "return",
            "reason": reason,
            "returned_at": datetime.now().isoformat(),
            "trigger": "capability_change",
            "provenance": {"moved_to": mt, "original_path": str(dst),
                           "source_file": prov.get("source_file")},
            "old_capability_fingerprint": old_fp,
            "new_capability_fingerprint": cur,
            "attempt_state": entry.get("attempt_state", {}),
        })
        state["quarantine"].pop(mt, None)
        # remember the prior attempt count so a re-quarantine increments it,
        # and let the file be processed again
        state.setdefault("returned", {})[str(dst)] = \
            entry.get("attempt_state", {}).get("evaluations", 1)
        state["done"].discard(str(dst))
        state["retry_at"].pop(str(dst), None)
        state["backoff"].pop(str(dst), None)
        returned += 1
    return returned


def scan_existing():
    """Scan eksisterende filer i incoming"""
    log("=== Initial scan ===")
    processed = 0
    for ext in ("*.pdf", "*.docx", "*.doc", "*.txt"):
        for filepath in WATCH_DIR.rglob(ext):
            if filepath.is_file() and _REJECTED_DIRNAME not in filepath.parts:
                log(f"Fandt: {filepath}")
                # TODO: Track processed files for incremental
                # For nu: processer alle
                if process_file(filepath) == "ok":
                    processed += 1
    log(f"=== Scan færdig: {processed} filer processeret ===")

def main():
    log("=== NOUS Ingest Pipeline startet ===")
    log(f"Watcher: {WATCH_DIR}")
    log(f"Qdrant: {QDRANT_URL}")
    log(f"Ollama: {router.ollama_url}/api/embeddings")
    
    # Sikr at incoming mappe findes
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    
    # Scan eksisterende
    scan_existing()
    
    # TODO: watchdog for nye filer (inotify/watchdog)
    # For nu: loop med sleep
    log("=== Loop startet, tjekker hvert 10. sekund ===")
    state = new_state()
    load_quarantine(state)
    while True:
        scan_once(state)
        time.sleep(10)


def new_state() -> dict:
    return {"done": set(), "retry_at": {}, "backoff": {}, "returned": {},
            "quarantine": {},
            "cap_fp": capability_fingerprint(), "cap_checked_at": time.monotonic()}


def scan_once(state):
    """One pass over incoming/. `state` carries between passes:
      done         — "ok" / quarantined paths, not rescanned
      retry_at     — path -> monotonic time before which a transient retry is skipped
      backoff      — path -> current backoff seconds (doubles per failure, capped)
      quarantine   — moved_to path -> manifest entry (rich: reason / fingerprint /
                     provenance / attempt_state)
      returned     — original_path -> prior evaluations count (for a re-quarantine)
      cap_fp       — cached capability fingerprint, refreshed on a bounded cadence
    A transient failure is NEVER dropped (bounded retry rate). A deterministic
    failure is QUARANTINED and auto-re-evaluated only on a relevant capability
    CHANGE — not a constant rescan.
    """
    state.setdefault("done", set()); state.setdefault("retry_at", {})
    state.setdefault("backoff", {}); state.setdefault("returned", {})
    state.setdefault("quarantine", {})
    if "cap_fp" not in state:
        state["cap_fp"] = capability_fingerprint()
        state["cap_checked_at"] = time.monotonic()
    done, retry_at, backoff = state["done"], state["retry_at"], state["backoff"]
    now = time.monotonic()

    # 1. refresh the capability fingerprint on a bounded cadence
    if now - state.get("cap_checked_at", 0.0) >= _CAP_RECHECK_S:
        state["cap_fp"] = capability_fingerprint()
        state["cap_checked_at"] = now

    # 2. auto-return any quarantined file whose relevant capability changed
    n_ret = reevaluate_quarantine(state)
    if n_ret:
        log(f"  {n_ret} fil(er) auto-returneret fra quarantine (capability ændret)")

    # 3. the scan
    for ext in ("*.pdf", "*.docx", "*.doc", "*.txt"):
        for filepath in WATCH_DIR.rglob(ext):
            key = str(filepath)
            if _REJECTED_DIRNAME in filepath.parts:
                continue
            if not filepath.is_file() or key in done:
                continue
            if key in retry_at and now < retry_at[key]:   # transient backoff — still queued
                continue

            outcome = process_file(filepath)
            if outcome == "ok":
                done.add(key); retry_at.pop(key, None); backoff.pop(key, None)
                state.get("returned", {}).pop(key, None)
            elif outcome == "reject":
                prior = state.get("returned", {}).pop(key, 0)
                _reject_file(filepath, state,
                             reason=_LAST_REJECT["reason"],
                             evidence=_LAST_REJECT["evidence"],
                             prior_evaluations=prior)
                done.add(key); retry_at.pop(key, None); backoff.pop(key, None)
            else:  # "retry" — transient; keep queued, back off, NEVER give up
                b = backoff.get(key, _BACKOFF_START_S)
                retry_at[key] = time.monotonic() + b
                backoff[key] = min(b * 2.0, _BACKOFF_MAX_S)

if __name__ == "__main__":
    main()
