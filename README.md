# NOUS

*From Greek νοῦς , mind, intellect, awareness.*

NOUS is a self-hosted personal AI assistant that runs entirely on your own hardware. It manages your household's everyday needs, analyzes documents, preserves family knowledge across generations, and can share anonymized facts with trusted nodes in a federated network. No cloud. No subscription. Your data never leaves your home.

> **v1.0, note:** This is the first public release. The core system is running in production on the developer's hardware. `install.sh` has not yet been tested on a completely fresh machine, so you may encounter edge cases. Issues and PRs are welcome.

---

## Hardware requirements

**Minimum , single machine:**

| | |
|-|-|
| Architecture | 64-bit Linux (aarch64 or x86-64) |
| RAM | 8 GB (limits concurrent model loading) |
| Storage | 64 GB SSD |
| Inference | Ollama with a 7B model (`qwen2.5:7b`) |

**Recommended , two machines:**

| Machine | Role |
|---------|------|
| Raspberry Pi 5 16GB (or similar SBC) | API · RAG · ingest · web UI · proxy |
| Jetson Orin NX 16GB (or CUDA-capable GPU machine) | LLM inference · Whisper STT |

The two-node split keeps the inference node air-gapped from the internet. All network access from the inference side routes through `nous-proxy` on the primary host. Response times are significantly faster because the primary's RAM stays free for vector search while the inference node handles model calls.

The installer detects the platform automatically and configures accordingly.

---

## Getting started

```bash
git clone <repo-url> /srv/nous
cd /srv/nous
sudo bash install.sh
```

The installer walks you through feature selection and configuration interactively. It handles everything: system packages, Python environment, Docker infrastructure (Qdrant, SearXNG), Ollama model pulls, systemd services, and the initial `.env` and `wings.json`.

### What `install.sh` does

1. **Detects platform** , Raspberry Pi 5 or Jetson Orin NX. Falls back to generic aarch64.
2. **Feature selector** , interactive yes/no menu. Only selected modules are installed.
3. **Asks for configuration** , Ollama URL, data directory, LAN subnet, optional Whisper URL. Writes `.env`.
4. **Installs dependencies** , apt packages, Docker, Python venv, pip packages.
5. **Creates data directories** , under `/mnt/nous-data/` by default.
6. **Starts Docker infrastructure** , Qdrant and SearXNG via `docker compose up -d`.
7. **Sets up Qdrant collections** from `wings.json`.
8. **Installs and enables systemd services** , API, Arbiter, night pipeline, optional Swarm and camera.
9. **Prints a summary** with service status and the web UI URL.

Run `sudo bash install.sh` again to reconfigure or add features , it is idempotent.

---

## Architecture

```
┌─────────────────────────────────┐     ┌────────────────────────────────┐
│  Primary host (Pi 5 or similar) │     │  Inference host (Jetson / GPU) │
│                                 │     │                                │
│  nous-api        :8000          │────▶│  Ollama (LLM)       :11434     │
│  nous-arbiter    :8010          │     │  faster-whisper STT :8080      │
│  nous-swarm      :8020          │     │                                │
│  nous-proxy      :8090          │     │  No default route ,            │
│  Qdrant          :6333          │     │  internet via nous-proxy only  │
│  SearXNG         :8080 (local)  │     └────────────────────────────────┘
│  Web UI          (static HTML)  │
│  Ingest pipeline (watchdog)     │
└─────────────────────────────────┘
```

The primary host handles all I/O, business logic, vector search and the web interface. The inference host handles only LLM calls and STT , it has no route to the internet and no access to the vector database. Communication between hosts is plain HTTP on the LAN.

**Core services (always installed):**

| Service | Port | Description |
|---------|------|-------------|
| `nous-api` | 8000 | Main FastAPI backend. Chat, ingest, analysis, scraper, swarm endpoints. |
| `nous-arbiter` | 8010 | Memory Arbiter. Single writer to Qdrant. Enforces scope rules. All writes must pass through here. |
| `nous-swarm` | 8020 | P2P agent. Anonymizes and exchanges facts with peer nodes. |
| `nous-proxy` | 8090 | Internet proxy for the inference host. Exposes weather, web search and URL fetch. |
| Qdrant | 6333 | Vector database. Collections are named `<wing>_<scope>`. |
| SearXNG | 8080 (local) | Private web search engine. Used by the proxy. |

**Night pipeline** runs at 02:30 via systemd timer. Generates summaries, extracts structured facts, runs cross-document pattern analysis, and feeds results back into the knowledge base.

---

## Features

### Household
Everyday assistant for general questions, document queries, and household knowledge. Uses the `qwen2.5:7b` model pre-warmed in RAM for fast responses.

### Legal
Document analysis engine specialized for legal material: statutes, rulings, administrative decisions. Runs with `qwen3:14b` as a night job. Detects inconsistencies and patterns across documents over time.

### Legacy
A structured interview system for preserving your voice and values. Daily questions, ad hoc entries, automatic follow-up questions. Answers are stored permanently with scope `PRIVATE` and are never shared via Swarm.

### Swarm

> **Note:** Swarm phase 1 (local promotion pipeline and approval UI) is implemented and tested. Phase 2 and 3 (P2P node-to-node exchange) are designed and built but have not been tested in a live multi-node setup. Treat them as experimental.

Anonymized fact sharing between NOUS nodes. Before any data leaves the node, a local model strips all PII (names, dates, locations, identifiers). Three trust levels:

- **Kin** , invite-only group, PSK-encrypted. For family members or close contacts with their own NOUS.
- **Collective** , open, fully anonymized. Incoming facts require manual approval in the cockpit.
- **Work** , closed group for organizational use.

### Model Manager
Select which Ollama model handles day and night workloads independently. Tune temperature, context window, and GPU layer count per role. Hot-swap models without restarting services.

### Panel debate
Submit a topic to multiple AI models simultaneously. A local `qwen2.5:7b` instance acts as moderator. Participants can include any combination of local Ollama models and external APIs (Claude, GPT-4o, Gemini, DeepSeek). Upload a background document all participants share. Results saved to any wing.

---

## Web interface

Three modes, single HTML file:

- **Cockpit** , system overview, wing management, document upload, scraper jobs, swarm panel, analysis workbench.
- **Assistant** , clean chat UI. Per-user profiles with optional PIN. Simplified for family members.
- **Analysis** , legal workbench with wing and file selection, external AI toggle, stored results and night job scheduling.

Feature toggles per user. Admin profile has full access; secondary profiles are limited to Assistant mode by default.

---

## Privacy and scope enforcement

Documents are stored in **wings** , named Qdrant collections. Each wing has a **scope**:

| Scope | Access | Swarm | Anonymization |
|-------|--------|-------|---------------|
| `SECRET` | Admin only | Never | None |
| `PRIVATE` | Admin only | Never | None |
| `SWARM` | All users | Yes | Full PII strip before export |
| `PUBLIC` | All users | No | CPR, phone, address, email stripped |

Scope is enforced in code at the Memory Arbiter layer. `SECRET` data cannot reach `SWARM` , this is not a UI toggle, it is a hard constraint in the write path.

---

## Known limitations

**Voice is not production-ready.**
`faster-whisper` installs via pip, but on Jetson hardware CUDA support requires running it inside a Docker container. The Docker-based Whisper container is not yet integrated into `install.sh` or the service configuration. Voice features work in development setups with an external Whisper endpoint.

**Camera is not implemented.**
The installer creates the `nous-kamera.service` unit and installs camera system packages, but the ingestion script (`kamera.py`) is a placeholder. Camera-to-AI image input is not functional in this release.

**Danish-language bias.**
System prompts, TTS voices and some STT tuning are optimized for Danish. Other languages work but have not been tested systematically.

**Single-node Qdrant.**
No replication or clustering. If Qdrant's data directory is corrupted, use `pipeline/fix_wing.py <wing>` to rebuild the collection from scratch.

---

## Roadmap

- **Voice via Docker** , containerized `faster-whisper` with CUDA on Jetson, integrated into `install.sh` and the voice service.
- **Multimodal / camera** , real-time image ingestion from a connected camera into the RAG pipeline.
- **Swarm phase 2 and 3** , cross-node fact synthesis, distributed night jobs, credit-weighted queries.
- **llama.cpp migration** , replace Ollama on the inference host with `llama.cpp` for lower memory overhead and better quantization control.
- **BGE-m3 embeddings** , replace `nomic-embed-text` with `bge-m3` for improved multilingual retrieval quality.

---

## Project structure

```
api/          FastAPI backend (port 8000)
arbiter/      Memory Arbiter , access control and scope enforcement (port 8010)
agents/       Multi-agent system: supervisor, household, legal, legacy
swarm/        P2P knowledge sharing with anonymization and credits (port 8020)
legacy/       Interview system and question bank
pipeline/     Ingest pipeline, privacy guard, task router, night jobs
scripts/      CLI tools, smoke test, backup
config/       wings.json, scraper_jobs.json  (gitignored , use *.example.json)
web/          Single-file cockpit UI (index.html)
models/       TTS and embedding model files (gitignored)
proxy/        Lightweight internet proxy for the air-gapped inference host
docs/         Architecture notes
```

---

## License

**AGPL-3.0** , free for personal and non-profit use.

Commercial use (integration into a product or service you sell) requires a separate license.

When using or distributing this source, retain this license and make any modifications available under the same terms.
