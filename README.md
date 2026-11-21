# NOUS — Locally-hosted Personal AI Assistant

NOUS is a privacy-first, locally hosted personal AI system for document retrieval, voice interaction, and assisted workflows. Local content is processed on the owner's hardware by default. Optional external AI providers and connected services may transmit data when explicitly enabled; review the applicable access scopes before using them.

> **Project status (November 2026):** This repository preserves the NOUS implementation and its verified improvements. Work on Gaia is separate and is intended to supersede NOUS in the future; NOUS has **not** been declared decommissioned here. The GitHub repository does not necessarily mirror every local experiment or deployed configuration.

## What it does

- **Document RAG and ingestion** — ingest PDFs, Word and text files into topic-based collections (wings), then query them using natural language.
- **Resilient ingestion** — retry transient ingest failures; quarantine documents without extractable text instead of silently dropping them. Permanent errors and operator recovery still require monitoring.
- **Voice interface** — wake-word detection, Whisper speech recognition, local LLM and Piper speech synthesis.
- **Web cockpit** — wings, analysis tools, scraper jobs and optional integrations.
- **Scope controls** — SECRET / PRIVATE / SWARM / PUBLIC classifications with access and export rules.
- **Optional external models** — support for multiple providers and custom endpoints, including a Kimi/Moonshot compatibility update for `kimi-k2.6`.
- **Multi-user support** — admin and family profiles with configurable access.
- **Compute lending** — optional swarm delegation with local fallback and scope restrictions.
- **GBrain** — graph-based inference over a Kuzu knowledge base.
- **Swarm shadow observer** — read-only telemetry; the documented 24-hour isolated observer test reported zero boundary violations.
- **Browser interaction (opt-in)** — isolated browser agent, audit trail and a user-confirmation gate for actions detected as potentially consequential. Detection heuristics must not be treated as a guarantee that every irreversible action will be caught.

## Deployment hardware

NOUS supports a multi-device LAN configuration, or a single-machine setup with appropriately configured inference endpoints. Documented development hardware includes:

| Role | Example hardware |
|---|---|
| API, retrieval, ingest, web cockpit | Raspberry Pi 5 16 GB |
| Local LLM and speech inference | Jetson Orin NX 16 GB |
| Swarm / peer compute | Jetson Orin Nano 8 GB |

The current services, host assignments and model availability depend on the actual deployment. Do not treat this table as a live inventory.

To use one machine, configure `NOUS_OLLAMA_URL` and `NOUS_WHISPER_URL` for the appropriate local endpoints.

## Architecture and models

NOUS separates task handling, document retrieval, local inference and optional external services. Model names in previous development notes were configuration examples rather than guaranteed built-in downloads. External-provider availability and exact model IDs can change; consult the current backend configuration before relying on a particular provider.

The November 2026 merged fix updated the Kimi/Moonshot debate integration to `kimi-k2.6` with model-specific request settings. It does not establish that all external-provider paths have been retested against live APIs.

## Quick start

### 1. Infrastructure

```bash
cd /srv/nous
docker compose up -d
```

The Docker configuration includes the project's retrieval/search infrastructure; check `docker-compose.yml` and local environment settings before exposing any service.

### 2. Python environment

```bash
python3 -m venv pipeline/.venv
source pipeline/.venv/bin/activate
pip install -r pipeline/requirements.txt
```

### 3. Configuration

```bash
cp .env.example .env
cp config/wings.example.json config/wings.json
```

Edit the new files for your own network addresses, inference endpoints, document collections and access rules. Do not commit runtime secrets, credentials, private keys, tokens or personal documents.

### 4. API and web cockpit

```bash
cd /srv/nous/api
uvicorn main:app --host 0.0.0.0 --port 8000
```

Serve or open `web/index.html` as appropriate for the deployment; reverse-proxy configuration is environment-specific.

### 5. Optional system service

```bash
sudo cp api/nous-api.service /etc/systemd/system/
sudo systemctl enable --now nous-api.service
```

Verify paths, permissions and dependencies in the service file before installing it.

## Wings, scopes and ingestion

| Scope | Typical access | Export handling |
|---|---|---|
| SECRET | Admin only | Not automatically anonymised; subject to strict outbound restrictions |
| PRIVATE | Admin only | Outbound use requires explicit scope confirmation |
| SWARM | Shared users | PII masking applies to shared/exported content |
| PUBLIC | Broad access | Defined personal identifiers are masked on export |

These are project-level rules; audit any external connector or deployment-specific changes before assuming protection is complete.

Drop suitable files in `incoming/<wing-name>/` for the configured watcher to process. The updated ingest pipeline retries temporary failures and quarantines files without usable text; monitor its operational logs and quarantine state.

## Optional integrations

The repository includes integration code and setup flows for:

- **Google services:** Gmail, Calendar, Drive and Docs-related workflows via the Google connector.
- **Outlook/Hotmail:** Microsoft Graph OAuth setup and mail access.
- **Proton Mail:** Bridge-based integration (requires a working Proton Mail Bridge environment).
- **NAS:** opt-in, read-oriented mounted-file browsing and controlled import.
- **Browser agent:** opt-in Playwright/Docker execution with session, isolation and confirmation-gate mechanisms.

Availability depends on credentials, upstream providers, local services and opt-in configuration. Presence of a connector in the repository is not proof that it is enabled or live-tested on every deployment.

## Voice setup

Requires a compatible microphone, a Whisper-based ASR endpoint and Piper TTS model. Optional ReSpeaker/XVF3800 integration depends on the hardware and software installed on the host.

```bash
mkdir -p models/tts
# Place the required Danish Piper model and its JSON configuration in models/tts/
bash scripts/voice_test.sh
```

See `CLAUDE.md` and deployment-specific configuration for additional architecture notes.

## Project structure

```text
api/          FastAPI backend and integration endpoints
browser/      Opt-in isolated browser agent
config/       Local configuration (sensitive runtime files should be gitignored)
docs/         Architecture notes and project documentation
integrations/ Connectors for external services and mounted NAS
models/       Local model assets (not distributed in Git)
pipeline/     Document ingestion, retrieval and safeguards
proxy/        Proxy components for LAN-isolated inference
scripts/      Operations, tests and CLI helpers
web/          Web cockpit
```

## Verified changes and limitations

### November 2026 — merged updates (PR #1)

- **Kimi/Moonshot:** migrated debate requests to `kimi-k2.6` and model-compatible settings. The project report records a live check on 2026-09-06; this PR's offline checks passed 4/4.
- **Ingest:** retry transient errors, quarantine documents with no extractable text and remove dependency on sandbox telemetry. Ingest tests passed 14/14.
- **External chat/debate scope checks:** 20/20 offline checks passed.

**Not rerun for PR #1:** `tests/test_swarm_security.py` (it would modify live wing settings/database); the Kimi path through `external_chat` was not separately tested offline. These limitations remain open, not PASS.

### August 2026 — repository features

- Browser integration, isolated runtime and interactive user-confirmation flow (opt-in).
- Connector and cockpit setup flows for Google services, Outlook/Hotmail, Proton Mail and mounted NAS.

### v0.2.0 — prior documented release

- Compute lending with scope guard and contribution accounting.
- GBrain graph processing with Kuzu.
- Swarm shadow observer with a documented 24-hour read-only trial.

### v0.1.0 — initial release

- Retrieval, voice, cockpit, access scopes and initial swarm features.

## Security and operational notes

- This is a **public repository**. Never push private keys, `.env` files, tokens, database contents, model weights or local logs containing personal information.
- Confirmation gates and scope enforcement are safeguards, not blanket security guarantees. Test every connected provider and deployment boundary independently.
- Backups, runtime model files and host-specific configuration are not reproduced by GitHub alone.

## License

AGPL-3.0 — see `LICENSE`.
