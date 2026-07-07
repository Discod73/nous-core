#!/usr/bin/env python3
"""
NOUS Ingest — PDF/DOCX/TXT → Qdrant via REST API
Brug: python3 ingest.py /sti/til/fil.pdf --wing <wing-navn> --scope SECRET
"""
import argparse, sys, uuid, json
from pathlib import Path
import httpx

import os as _os
QDRANT_URL = _os.environ.get("NOUS_QDRANT_URL", "http://127.0.0.1:6333")
OLLAMA_URL = _os.environ.get("NOUS_OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = "nomic-embed-text"
CHUNK_SIZE = 200
CHUNK_OVERLAP = 30

_WINGS_FILE = Path("/srv/nous/config/wings.json")

def _load_wing_map() -> dict:
    try:
        data = json.loads(_WINGS_FILE.read_text())
        return {w["name"]: {w["scope"]: w["collection"], "PRIVATE": w["collection"]}
                for w in data.get("wings", []) if w.get("name") and w.get("collection")}
    except Exception:
        return {}

WING_MAP = _load_wing_map()

def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pypdf
        reader = pypdf.PdfReader(str(path))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    elif suffix == ".docx":
        import docx
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    elif suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".csv":
        import csv, io
        raw = path.read_text(encoding="utf-8", errors="replace")
        reader = csv.reader(io.StringIO(raw))
        rows = list(reader)
        if not rows:
            return ""
        headers = rows[0]
        lines = ["\t".join(headers)]
        for row in rows[1:]:
            pairs = ", ".join(f"{h}: {v}" for h, v in zip(headers, row))
            lines.append(pairs)
        return "\n".join(lines)
    elif suffix == ".rtf":
        from striprtf.striprtf import rtf_to_text
        raw = path.read_text(encoding="utf-8", errors="replace")
        return rtf_to_text(raw)
    elif suffix in (".jpg", ".jpeg", ".png", ".tiff", ".bmp"):
        import pytesseract
        from PIL import Image, ImageEnhance, ImageFilter
        img = Image.open(str(path)).convert("RGB")
        w, h = img.size
        if w < 1800:
            img = img.resize((w * 2, h * 2), Image.LANCZOS)
        img = img.convert("L")
        img = ImageEnhance.Contrast(img).enhance(2.0)
        img = img.filter(ImageFilter.SHARPEN)
        config = "--oem 1 --psm 3 -c preserve_interword_spaces=1"
        return pytesseract.image_to_string(img, lang="dan+eng", config=config)
    elif suffix in (".mp3", ".wav", ".m4a", ".ogg", ".flac"):
        return _whisper_transcribe(path)
    elif suffix in (".mp4", ".mkv", ".avi", ".mov"):
        return _video_transcribe(path)
    else:
        print(f"  Springer over: ikke understøttet filtype {suffix}", file=sys.stderr)
        return ""


WHISPER_URL = _os.environ.get("NOUS_WHISPER_URL", "http://localhost:8080/inference")


def _whisper_transcribe(audio_path: Path) -> str:
    print(f"  [Whisper] Transskriberer {audio_path.name}…")
    with audio_path.open("rb") as fh:
        r = httpx.post(
            WHISPER_URL,
            data={"language": "da", "response_format": "text"},
            files={"file": (audio_path.name, fh)},
            timeout=300,
        )
    r.raise_for_status()
    return r.text.strip()


def _video_transcribe(video_path: Path) -> str:
    import tempfile, subprocess
    print(f"  [ffmpeg] Udtrækker audio fra {video_path.name}…")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path),
             "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", str(tmp_path)],
            capture_output=True, timeout=600,
        )
        if result.returncode != 0:
            print(f"  ✗ ffmpeg fejl: {result.stderr.decode()[:300]}", file=sys.stderr)
            return ""
        return _whisper_transcribe(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

def chunk_text(text: str) -> list:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunk = " ".join(words[i:i+CHUNK_SIZE])
        if chunk.strip():
            chunks.append(chunk)
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks

def embed(text: str) -> list:
    r = httpx.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()["embedding"]

def upsert_point(collection: str, point_id: str, vector: list, payload: dict) -> bool:
    r = httpx.put(
        f"{QDRANT_URL}/collections/{collection}/points",
        content=json.dumps({"points": [{
            "id": point_id,
            "vector": vector,
            "payload": payload,
        }]}),
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    if r.status_code == 200:
        return True
    print(f"  ✗ Fejl {r.status_code}: {r.text}")
    return False

def ingest(filepath: str, wing: str, scope: str):
    path = Path(filepath)
    if not path.exists():
        sys.exit(f"Fil ikke fundet: {filepath}")

    collection = WING_MAP.get(wing, {}).get(scope, f"{wing}_{scope.lower()}")
    print(f"Læser {path.name} → {collection}...")

    text = extract_text(path)
    chunks = chunk_text(text)
    print(f"{len(chunks)} chunks")

    ok = 0
    for i, chunk in enumerate(chunks):
        print(f"  Chunk {i+1}/{len(chunks)}: embedder...", end="\r")
        vector = embed(chunk)
        payload = {
            "text": chunk,
            "source": path.name,
            "scope": scope,
            "wing": wing,
        }
        if upsert_point(collection, str(uuid.uuid4()), vector, payload):
            ok += 1

    print()
    print(f"✓ {ok}/{len(chunks)} chunks indsat i '{collection}'")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("file")
    p.add_argument("--wing", default="")  # ingen choices-restriktion, fallback i WING_MAP.get() haandterer nye wings
    p.add_argument("--scope", choices=["SECRET", "PRIVATE", "SWARM", "PUBLIC"], default="SECRET")
    args = p.parse_args()
    ingest(args.file, args.wing, args.scope)
