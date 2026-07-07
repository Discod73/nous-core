# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ABSOLUT SIKKERHEDSREGEL — MÅ ALDRIG BRYDES

Før enhver `git add`, `git commit` eller `git push`:

1. Kør `git diff --cached --name-only` og gennemgå ALLE filer
2. STOP og afvis hvis nogen fil matcher:
   - `*Overleveringsdokument*`, `*SESSION*`, `*NOUS_v[0-9]*`, `*rapport*`
   - `*voice_prefs*`, `*ui_prefs*`, `*wake_prefs*`, `*research_jobs*`
   - `*.docx`, `*SECRET*`, `*credentials*`, `*secrets*`
   - `config/` (hele mappen er forbudt)
3. Kør grep på diff og STOP hvis noget matcher:
   - `boernesag`, `fbf`, `Isabella`, `Helle`, `CPR`, `personnummer`
   - `192\.168`, `100\.65` (IP-adresser)
   - `password`, `api_key`, `token`, `secret_key`
4. Bed Dan om eksplicit godkendelse af komplet filliste inden push
5. Ingen `--force` push uden Dans eksplicitte ordre

Hardkodede værdier er forbudt i al kode der pushes:
- Ingen IP-adresser (brug miljøvariabler)
- Ingen passwords, tokens, nøgler
- Ingen personnavne fra sensitive wings
- Ingen systemstier i README eller dokumentation

Hook fejler = STOP. Ingen undtagelser.

---

## Project overview

NOUS is a Danish-language, locally-hosted personal AI assistant. It runs across two machines on a private LAN with the Jetson air-gapped from the internet.

| Device | Env var | Role |
|---|---|---|
| Pi 5 (controller) | — | Wake-word detection, VAD, TTS, ingest, proxy, RAG |
| Jetson Orin NX 16GB | `NOUS_NX_HOST` | LLM inference (Ollama), STT (Whisper) |

Network addresses are configured via environment variables — see `.env.example`. Never hardcode IPs.

## Python environment

The main venv used by all scripts and the systemd service is `/srv/nous/pipeline/.venv`. This must be first in PATH when running under systemd (critical — without it, `piper` and `sox` are not found).

```bash
source /srv/nous/pipeline/.venv/bin/activate
```

`/srv/nous/app/.venv` is a separate venv used only by Kuzu-related code.

## Key commands

```bash
# System health check
bash /srv/nous/scripts/smoke-test.sh

# Live service logs
journalctl -u nous-voice-assistant.service -f

# Restart voice assistant service
sudo systemctl restart nous-voice-assistant.service

# Manual one-shot voice session (bypasses wake-word)
source /srv/nous/pipeline/.venv/bin/activate
python3 /srv/nous/scripts/voice_chat.py

# Text chat (no voice)
python3 /srv/nous/scripts/nous_chat.py 'dit spørgsmål'

# Start the internet proxy manually (normally a service)
cd /srv/nous/proxy && uvicorn nous_proxy:app --host 0.0.0.0 --port 8090

# Backup persistent data
bash /srv/nous/scripts/nous-backup.sh

# Set up Qdrant collections (run after Qdrant restart or new wing install)
python3 /srv/nous/scripts/nous-setup-collections.py

# Ingest a document interactively
python3 /srv/nous/scripts/nous-ingest-manual.py /sti/til/fil.pdf

# Promote document chunks between wings with PII review
python3 /srv/nous/scripts/promote.py <source_wing> <target_scope> [doc_id]

# PII detection/anonymization CLI
python3 /srv/nous/pipeline/privacy_guard.py detect "tekst"
python3 /srv/nous/pipeline/privacy_guard.py mask "tekst" PUBLIC
python3 /srv/nous/pipeline/privacy_guard.py audit "tekst" SWARM

# Repair a corrupted Qdrant collection
python3 /srv/nous/pipeline/fix_wing.py <wing_name>

# Test audio pipeline (TTS + playback) without full voice session
bash /srv/nous/scripts/voice_test.sh

# Docker infrastructure (Qdrant + SearXNG)
cd /srv/nous && docker compose up -d
cd /srv/nous && docker compose restart qdrant
cd /srv/nous && docker compose logs -f qdrant

# Check Ollama/Whisper on Jetson
curl $NOUS_WHISPER_URL/../health
curl $NOUS_NX_LLAMA_URL/../health
```

## Architecture

### Voice pipeline (end-to-end)

```
ReSpeaker XVF3800 (hw:2,0 on Pi 5)
  → voice_assistant.py: openWakeWord ("hey_jarvis", threshold 0.5)
  → stream.close() + 0.8s ALSA release + Piper "Ja?" ACK
  → voice_chat.py subprocess:
      Silero VAD (threshold 0.4, 16-frame pre-roll)
      → Whisper STT ($NOUS_WHISPER_URL, language=da, temperature=0.0)
      → Qwen3 LLM ($NOUS_NX_LLAMA_URL, /v1/chat/completions, tools)
      → nous-proxy tool execution (Pi:8090)
      → clean_for_tts() strips markdown/emoji/URLs
      → Piper TTS streaming (da.onnx) via asyncio pipeline
      → sox + ALSA 48kHz stereo → speaker
  → 2.0s cooldown → reopen stream → listen again
```

The subprocess model (`voice_assistant.py` spawning `voice_chat.py`) ensures full ALSA device release between sessions.

### Services on Pi 5

| Service | File | Port |
|---|---|---|
| `nous-voice-assistant.service` | `/etc/systemd/system/nous-voice-assistant.service` | — |
| nous-proxy | `/srv/nous/proxy/nous_proxy.py` | 8090 |
| Qdrant | Docker (`docker-compose.yml`) | 6333, 6334 |
| SearXNG | Docker (`docker-compose.yml`) | 8080 (localhost) |

Proxy endpoints: `/health`, `/time`, `/weather?location=`, `/search?q=`, `/fetch?url=`

### LLM tool-calling

`nous_chat.py` and `voice_chat.py` both implement the same three tools against nous-proxy:
- `get_time` — current Danish datetime
- `get_weather(location)` — via Open-Meteo (no API key)
- `search_web(query)` — via SearXNG

The LLM uses the OpenAI-compatible `/v1/chat/completions` endpoint on the Jetson with `model: "qwen3"`.

### Task Router (circuit breaker)

`pipeline/task_router.py` exports a global `router` instance. It tracks Jetson availability with a circuit breaker: 3 failures → OPEN (60s timeout) → HALF_OPEN → CLOSED after 2 successes. Import it as `from task_router import router`.

### Data storage and privacy model

All persistent data lives under `/mnt/nous-data/`. Qdrant vectors are at `/mnt/nous-data/qdrant/`.

Documents are stored in **wings** (topic-based Qdrant collections) with a **scope** (access level):

| Scope | Anonymization |
|---|---|
| SECRET | None — only on Pi |
| PRIVATE | None — only on owner devices |
| SWARM | All PII (CPR, phone, address, email, names) |
| PUBLIC | CPR, phone, address, email — names preserved |

Wing → collection mapping is defined in `config/wings.json` (gitignored, never commit). Load dynamically — never hardcode wing names in code.

**Promotion** (moving data to a more public scope) requires explicit user confirmation and runs through `promote.py`, which shows a PII preview before writing. The PII token→original mapping is stored in `/mnt/nous-data/keymap.db` (SQLite).

The ingest pipeline watches `/home/nous/incoming/` and routes files to wings based on subdirectory name. Drop files into `/home/nous/incoming/<wing>/` for automatic ingest.

### Vector embeddings

`nomic-embed-text` via Ollama on the Jetson. Vector dimension: 768. Chunk size: 512 words, 50-word overlap.

## Kuzu graph database

`/srv/nous/app/.venv` is a separate Python environment for Kuzu 0.11.3 (claim-tracking graph DB). Database files live at `/mnt/nous-data/kuzu`. It is not yet integrated into the main voice pipeline — activate it only when working on graph-related features. The main pipeline venv (`pipeline/.venv`) has no Kuzu dependency.

## Coding conventions

All user-facing strings, system prompts, comments, and log messages are written in **Danish**. This applies to LLM `SYSTEM` prompt text, TTS output, CLI tool docstrings, and inline comments. English is acceptable in code identifiers and technical error messages that will never reach the user.

## Known issues (from v1.9 status doc)

- **DK_PHONE and DK_ADDRESS regex** in `privacy_guard.py` have known false-positive/miss issues (P1)
- Whisper-medium Danish WER 12-18%; fuzzy-match SYSTEM prompt in `voice_chat.py` compensates for common STT errors
- Streaming token-by-token TTS during LLM generation is not yet implemented (P0)
