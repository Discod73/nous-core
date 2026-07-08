# NOUS — Locally-hosted Personal AI Assistant

NOUS is a privacy-first, locally-hosted personal AI system. All data stays on your own hardware — nothing leaves your network unless you explicitly choose to use an external AI provider via the built-in external API feature.

## What it does

- **RAG over your documents** — ingest PDFs, Word files and text, query them in natural language
- **Voice interface** — wake-word detection → Whisper STT → local LLM → Piper TTS
- **Web cockpit** — browser UI with wings (topic-based document collections), analysis mode, and scraper jobs
- **Scope system** — documents tagged SECRET / PRIVATE / SWARM / PUBLIC with enforced access controls
- **Optional external AI** — send queries to Anthropic, OpenAI, Groq or a custom endpoint; key stays in browser session only
- **Multi-user** — admin user plus family profiles with configurable wing access

## Hardware

The system is designed for a two-device LAN setup, but can run on a single machine:

| Device | Role | Tested on |
|---|---|---|
| Primary host (Pi 5 recommended) | API, RAG, ingest, web UI, proxy | Raspberry Pi 5 16GB |
| Inference host (optional) | LLM + STT | Jetson Orin Nano 8GB |

Single-machine: point `NOUS_OLLAMA_URL` and `NOUS_WHISPER_URL` at `localhost`.

## Multi-agent arkitektur

NOUS bruger specialiserede agenter til forskellige domæner (husstand, juridisk, legacy).
Hver agent er designet til at køre sin egen model, men på begrænset hardware
deles modeller intelligent:

- **Daglig brug** (supervisor, husstand, børn): qwen2.5:7b — holdes permanent
  i hukommelsen for øjeblikkelig respons
- **Nat-analyse** (juridisk, inkonsistens): qwen3:14b — loader on-demand

Når hardware med større unified memory (32GB+) bliver tilgængeligt,
eller domænespecifikke modeller frigives som GGUF, kan hver agent
konfigureres med sin egen specialiserede model uden arkitekturændringer.

## Quick start

### 1. Infrastructure (Docker)

```bash
cd /srv/nous
docker compose up -d        # starts Qdrant + SearXNG
```

### 2. Python environment

```bash
python3 -m venv pipeline/.venv
source pipeline/.venv/bin/activate
pip install -r pipeline/requirements.txt   # fastapi uvicorn httpx qdrant-client ...
```

### 3. Configuration

```bash
cp .env.example .env
# Edit .env — set NOUS_OWNER_NAME and network addresses

cp config/wings.example.json config/wings.json
# Edit wings.json — define your document collections
```

### 4. Start the API

```bash
cd /srv/nous/api
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `web/index.html` in a browser (or serve it via nginx — see `docs/CREDITS.md`).

### 5. Systemd service (optional)

```bash
sudo cp api/nous-api.service /etc/systemd/system/
sudo systemctl enable --now nous-api.service
```

## Wings and scopes

Wings are named document collections. Each wing has a **scope**:

| Scope | Who can see it | PII anonymization on export |
|---|---|---|
| SECRET | Admin only | None |
| PRIVATE | Admin only | None |
| SWARM | All users | All PII masked |
| PUBLIC | Everyone | CPR, phone, address, email masked |

Non-admin users can be blocked from specific wings via `blockedWings` in `web/index.html` → `USERS`.

Drop files into `incoming/<wing-name>/` for automatic ingest (picked up within ~10 seconds by the file watcher).

## Voice setup

Requires: any USB microphone, Piper TTS model, Whisper running on inference host.

> **Optional hardware:** ReSpeaker/XVF3800 users may install their vendor control tools separately — NOUS auto-detects the device by name. See your hardware vendor's documentation.

```bash
# Download Danish Piper TTS model
mkdir -p models/tts
# Place da.onnx and da.onnx.json in models/tts/

# Test voice pipeline
bash scripts/voice_test.sh
```

See `CLAUDE.md` for full architecture details.

## External AI

The web cockpit includes an optional external AI panel (🌐) in Analyse and Assistant modes. When enabled, queries are sent to Anthropic / OpenAI / Groq / custom endpoint after scope confirmation. The API key is never stored — it lives only in the browser session.

PRIVATE wings require one confirmation click. SECRET wings require typing `JEG FORSTÅR RISIKOEN` before sending.

## Project structure

```
api/          FastAPI backend
config/       wings.json, scraper_jobs.json (gitignored — use *.example.json)
docs/         Architecture notes, credits
models/       TTS + embedding models (gitignored)
pipeline/     Ingest pipeline, privacy guard, task router
proxy/        Lightweight internet proxy for LAN-isolated inference host
scripts/      CLI tools: voice_chat.py, nous_chat.py, promote.py, ...
web/          Single-file cockpit UI (index.html)
```

## License

AGPL-3.0 — see `LICENSE`.
