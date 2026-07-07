#!/usr/bin/env python3
"""
Research scraper — BFS deep-crawl via Crawl4AI + Arbiter write
Usage: research_scraper.py <job_id>
Job config: /srv/nous/config/research_jobs.json
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ARBITER_URL   = os.environ.get("NOUS_ARBITER_URL",  "http://localhost:8010")
OLLAMA_URL    = os.environ.get("NOUS_OLLAMA_URL",   "http://localhost:11434")
EMBED_MODEL   = os.environ.get("NOUS_EMBED_MODEL",  "nomic-embed-text")
WINGS_FILE    = Path("/srv/nous/config/wings.json")
RESEARCH_JOBS = Path("/srv/nous/config/research_jobs.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("research_scraper")


def _load_jobs() -> list:
    if not RESEARCH_JOBS.exists():
        return []
    return json.loads(RESEARCH_JOBS.read_text(encoding="utf-8"))


def _find_job(job_id: str) -> dict | None:
    return next((j for j in _load_jobs() if j["id"] == job_id), None)


def _find_wing(name: str) -> dict | None:
    if not WINGS_FILE.exists():
        return None
    data = json.loads(WINGS_FILE.read_text(encoding="utf-8"))
    return next((w for w in data.get("wings", []) if w["name"] == name), None)


def _embed(text: str) -> list[float]:
    r = httpx.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=45,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def _arbiter_write(wing: str, scope: str, text: str, source_url: str) -> str:
    vec       = _embed(text[:2000])
    point_id  = str(uuid.uuid4())
    sha       = hashlib.sha256(text.encode("utf-8")).hexdigest()
    payload   = {
        "wing":      wing,
        "scope":     scope,
        "operation": "upsert",
        "source":    "research_scraper",
        "points": [
            {
                "id":     point_id,
                "vector": vec,
                "payload": {
                    "text":        text[:4000],
                    "source":      source_url,
                    "type":        "research",
                    "sha256":      sha,
                    "ingested_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        ],
    }
    r = httpx.post(f"{ARBITER_URL}/arbiter/write/sync", json=payload, timeout=30)
    r.raise_for_status()
    return point_id


def _chunk_text(text: str, max_chars: int = 1500, overlap: int = 200) -> list[str]:
    chunks = []
    start  = 0
    while start < len(text):
        end   = min(start + max_chars, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start += max_chars - overlap
    return chunks


async def _run_job(job_id: str) -> None:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

    job = _find_job(job_id)
    if not job:
        log.error(f"Job '{job_id}' ikke fundet i {RESEARCH_JOBS}")
        sys.exit(1)

    wing_name       = job["wing"]
    start_url       = job["start_url"]
    max_depth       = job.get("max_depth",       2)
    max_pages       = job.get("max_pages",       30)
    score_threshold = job.get("score_threshold", 0.3)

    wing = _find_wing(wing_name)
    if not wing:
        log.error(f"Wing '{wing_name}' ikke fundet i wings.json")
        sys.exit(1)
    scope = wing["scope"]

    log.info(
        f"START job={job_id!r} url={start_url!r} wing={wing_name!r} "
        f"max_depth={max_depth} max_pages={max_pages} scope={scope}"
    )

    strategy = BFSDeepCrawlStrategy(
        max_depth=max_depth,
        max_pages=max_pages,
        include_external=False,
        score_threshold=score_threshold,
    )
    bc = BrowserConfig(
        browser_type="chromium",
        headless=True,
        extra_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
        ],
    )
    rc = CrawlerRunConfig(
        deep_crawl_strategy=strategy,
        word_count_threshold=10,
        page_timeout=30000,
        wait_until="domcontentloaded",
        stream=True,
    )

    pages_ok      = 0
    pages_err     = 0
    chunks_written = 0

    async with AsyncWebCrawler(config=bc) as crawler:
        async for result in await crawler.arun(start_url, config=rc):
            if not result.success:
                log.warning(f"Side fejlede: {result.url}")
                pages_err += 1
                continue

            md = (result.markdown or "").strip()
            if len(md) < 50:
                log.debug(f"For kort indhold, springes over: {result.url}")
                continue

            pages_ok += 1
            for chunk in _chunk_text(md):
                try:
                    _arbiter_write(wing_name, scope, chunk, result.url)
                    chunks_written += 1
                except Exception as e:
                    log.error(f"Arbiter write fejl ({result.url}): {e}")

    log.info(
        f"FÆRDIG: {pages_ok} sider OK, {pages_err} fejl, "
        f"{chunks_written} chunks → wing='{wing_name}'"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: research_scraper.py <job_id>", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_run_job(sys.argv[1]))
