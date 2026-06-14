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
import hashlib
import requests
from pathlib import Path
from datetime import datetime


from task_router import router

# === CONFIG ===
WATCH_DIR = Path("/home/nous/incoming")
QDRANT_URL = "http://localhost:6333"
# Bruger Task Router i stedet
# # Bruger Task Router i stedet

EMBED_MODEL = "nomic-embed-text"
CHUNK_SIZE = 512
CHUNK_OVERLAP = 50

# Wing routing fra mappestruktur
WING_MAP = {
    "secret":     ("boernesag", "SECRET"),
    "boernesag":  ("boernesag", "SECRET"),
    "fbf_data":     ("fbf_data", "PRIVATE"),
    "jura":       ("jura", "PRIVATE"),
    "dans_profil":  ("dans_profil", "PRIVATE"),
    "familie":    ("familie", "PRIVATE"),
    "nous_projekt": ("nous_projekt", "PRIVATE"),
}

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
    """Hovedflow: extract → chunk → embed → upsert"""
    filepath = Path(filepath)
    log(f"=== Processing: {filepath.name} ===")
    
    # Bestem wing
    wing, scope = get_wing(filepath)
    log(f"  Wing: {wing}, Scope: {scope}")
    
    # Extract tekst
    text = extract_text(filepath)
    if not text or len(text.strip()) == 0:
        log("  Ingen tekst extracted, skipper")
        return False
    
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
        if upsert_to_qdrant(wing, scope, points):
            log(f"  UPSERTET: {len(points)} points til {wing}")
            return True
        else:
            log(f"  FEJL: Kunne ikke upserte til {wing}")
            return False
    else:
        log("  Ingen points at upserte")
        return False

def scan_existing():
    """Scan eksisterende filer i incoming"""
    log("=== Initial scan ===")
    processed = 0
    for ext in ("*.pdf", "*.docx", "*.doc", "*.txt"):
        for filepath in WATCH_DIR.rglob(ext):
            if filepath.is_file():
                log(f"Fandt: {filepath}")
                # TODO: Track processed files for incremental
                # For nu: processer alle
                if process_file(filepath):
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
    processed_files = set()
    
    while True:
        for ext in ("*.pdf", "*.docx", "*.doc", "*.txt"):
            for filepath in WATCH_DIR.rglob(ext):
                if filepath.is_file() and str(filepath) not in processed_files:
                    if process_file(filepath):
                        processed_files.add(str(filepath))
        time.sleep(10)

if __name__ == "__main__":
    main()
