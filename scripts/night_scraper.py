#!/usr/bin/env python3
"""
NOUS Night Scraper — søger og ingestor nye juridiske dokumenter automatisk.
Læser jobs fra /srv/nous/config/scraper_jobs.json.
Kører søndage kl 01:00 via nous-scraper.timer.

Venv: /srv/nous/app/.venv
"""
import hashlib
import os
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

# ── Endpoints ─────────────────────────────────────────────────────────────────
QDRANT_URL  = "http://localhost:6333"
OLLAMA_URL  = os.environ.get("NOUS_OLLAMA_URL", "http://localhost:11434")
PROXY_SEARCH = "http://localhost:8090/search"
PROXY_FETCH  = "http://localhost:8090/fetch"
EMBED_MODEL  = "nomic-embed-text"
VECTOR_DIM   = 768

JOBS_FILE   = Path("/srv/nous/config/scraper_jobs.json")
WINGS_FILE  = Path("/srv/nous/config/wings.json")
LOG_FILE    = Path("/mnt/nous-data/logs/scraper.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

MIN_CONTENT_CHARS = 500
MAX_CONTENT_CHARS = 40_000
CHUNK_SIZE        = 200
CHUNK_OVERLAP     = 30
RESULTS_PER_QUERY = 5

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("night_scraper")

DANISH_PATTERNS = re.compile(
    r"\b(og|er|det|at|en|den|til|i|af|på|med|han|hun|de|vi|ikke|for|fra|som)\b",
    re.IGNORECASE,
)


def is_danish(text: str) -> bool:
    sample = text[:2000]
    hits   = len(DANISH_PATTERNS.findall(sample))
    words  = len(sample.split())
    return words > 20 and hits / max(words, 1) > 0.06


def load_wings() -> dict:
    return json.loads(WINGS_FILE.read_text(encoding="utf-8"))


def get_collection(wing_name: str) -> str | None:
    for w in load_wings().get("wings", []):
        if w["name"] == wing_name:
            return w["collection"]
    return None


def get_scope(wing_name: str) -> str:
    for w in load_wings().get("wings", []):
        if w["name"] == wing_name:
            return w["scope"]
    return "PRIVATE"


def url_already_ingested(collection: str, url: str) -> bool:
    try:
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/scroll",
            json={
                "filter": {"must": [{"key": "source_url", "match": {"value": url}}]},
                "limit": 1,
                "with_payload": False,
                "with_vector": False,
            },
            timeout=10.0,
        )
        return bool(r.json().get("result", {}).get("points"))
    except Exception:
        return False


def embed(text: str) -> list:
    r = httpx.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text[:8192]},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def chunk_text(text: str) -> list[str]:
    words  = text.split()
    chunks = []
    i      = 0
    while i < len(words):
        chunk = " ".join(words[i : i + CHUNK_SIZE])
        if chunk.strip():
            chunks.append(chunk)
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def ingest_content(
    collection: str,
    scope: str,
    wing_name: str,
    url: str,
    title: str,
    content: str,
) -> int:
    source_file = title[:120] or urlparse(url).path.split("/")[-1] or "scraped"
    chunks      = chunk_text(content)
    now         = datetime.now(timezone.utc).isoformat()
    content_hash = hashlib.md5(content.encode()).hexdigest()[:16]
    ingested    = 0

    points = []
    for i, chunk in enumerate(chunks):
        point_id = str(uuid.uuid5(
            uuid.NAMESPACE_DNS, f"{collection}:{url}:chunk:{i}"
        ))
        try:
            vec = embed(chunk)
        except Exception as e:
            log.warning(f"  Embed fejl chunk {i}: {e}")
            continue
        points.append({
            "id":     point_id,
            "vector": vec,
            "payload": {
                "source_file":   source_file,
                "source_url":    url,
                "chunk_index":   i,
                "scope":         scope,
                "wing":          wing_name,
                "text":          chunk,
                "timestamp":     now,
                "content_hash":  content_hash,
            },
        })
        ingested += 1

    if points:
        r = httpx.put(
            f"{QDRANT_URL}/collections/{collection}/points",
            json={"points": points},
            timeout=30.0,
        )
        r.raise_for_status()

    return ingested


def run_job(job: dict) -> dict:
    job_id    = job["id"]
    wing_name = job["wing"]
    queries   = job.get("queries", [])
    seed_urls = job.get("seed_urls", [])
    danish_only = job.get("danish_only", True)

    log.info(f"\n── Job: {job_id} → wing={wing_name} ({len(queries)} queries, {len(seed_urls)} seed-URLs) ──")

    collection = get_collection(wing_name)
    if not collection:
        log.error(f"  Wing {wing_name!r} ikke fundet i wings.json")
        return {"job_id": job_id, "new_chunks": 0, "new_docs": 0, "error": "wing not found"}

    scope = get_scope(wing_name)
    new_docs   = 0
    new_chunks = 0
    seen_urls: set[str] = set()

    # Direkte URL-fetch (seed_urls) uden søgning
    for url in seed_urls:
        url = url.strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if url_already_ingested(collection, url):
            log.debug(f"  Skip seed-URL (allerede ingestet): {url}")
            continue
        try:
            fr = httpx.get(PROXY_FETCH, params={"url": url}, timeout=20.0)
            fr.raise_for_status()
            content = fr.json().get("text", "").strip()
        except Exception as e:
            log.warning(f"  Fetch fejlede seed-URL {url}: {e}")
            continue
        if len(content) < MIN_CONTENT_CHARS:
            log.debug(f"  For kort ({len(content)} tegn): {url}")
            continue
        if danish_only and not is_danish(content):
            log.debug(f"  Ikke dansk: {url}")
            continue
        title = urlparse(url).path.split("/")[-1] or url
        content = content[:MAX_CONTENT_CHARS]
        log.info(f"  Ingesterer seed-URL: {title!r} ({len(content)} tegn)")
        try:
            n = ingest_content(collection, scope, wing_name, url, title, content)
            new_chunks += n
            new_docs   += 1
            log.info(f"    → {n} chunks ingestet")
        except Exception as e:
            log.error(f"  Ingest fejl seed-URL: {e}")

    for query in queries:
        log.info(f"  Søger: {query!r}")
        try:
            r = httpx.get(
                PROXY_SEARCH,
                params={"q": query, "n": RESULTS_PER_QUERY},
                timeout=15.0,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:
            log.warning(f"  Søgning fejlede: {e}")
            continue

        for res in results:
            url   = res.get("url", "").strip()
            title = res.get("title", "")

            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            if url_already_ingested(collection, url):
                log.debug(f"  Skip (allerede ingestet): {url}")
                continue

            # Fetch indhold
            try:
                fr = httpx.get(
                    PROXY_FETCH,
                    params={"url": url},
                    timeout=20.0,
                )
                fr.raise_for_status()
                content = fr.json().get("text", "").strip()
            except Exception as e:
                log.warning(f"  Fetch fejlede {url}: {e}")
                continue

            if len(content) < MIN_CONTENT_CHARS:
                log.debug(f"  For kort ({len(content)} tegn): {url}")
                continue

            if danish_only and not is_danish(content):
                log.debug(f"  Ikke dansk: {url}")
                continue

            content = content[:MAX_CONTENT_CHARS]
            log.info(f"  Ingesterer: {title!r} ({len(content)} tegn)")

            try:
                n = ingest_content(collection, scope, wing_name, url, title, content)
                new_chunks += n
                new_docs   += 1
                log.info(f"    → {n} chunks ingestet")
            except Exception as e:
                log.error(f"  Ingest fejl: {e}")

    return {"job_id": job_id, "new_docs": new_docs, "new_chunks": new_chunks}


def main() -> None:
    start = datetime.now()
    log.info("=" * 60)
    log.info(f"NOUS Night Scraper starter — {start.strftime('%Y-%m-%d %H:%M')}")

    if not JOBS_FILE.exists():
        log.error(f"Ingen jobs-fil fundet: {JOBS_FILE}")
        sys.exit(1)

    jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    active_jobs = [j for j in jobs if j.get("active", True)]
    log.info(f"{len(active_jobs)} aktive jobs")

    total_docs   = 0
    total_chunks = 0

    for job in active_jobs:
        result       = run_job(job)
        total_docs   += result.get("new_docs", 0)
        total_chunks += result.get("new_chunks", 0)

    elapsed = (datetime.now() - start).total_seconds()
    log.info(f"\n{'=' * 60}")
    log.info(f"Færdig — {total_docs} nye dokumenter, {total_chunks} nye chunks")
    log.info(f"Samlet tid: {elapsed:.0f}s")
    log.info("=" * 60)


if __name__ == "__main__":
    # Kan kaldes med specifikt job-id: python night_scraper.py jura_alienation
    if len(sys.argv) > 1:
        job_id    = sys.argv[1]
        jobs      = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        job_match = next((j for j in jobs if j["id"] == job_id), None)
        if not job_match:
            log.error(f"Job {job_id!r} ikke fundet")
            sys.exit(1)
        run_job(job_match)
    else:
        main()
