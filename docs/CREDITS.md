# NOUS Credits & Acknowledgments

NOUS is released under the **GNU Affero General Public License v3.0 (AGPL-3.0)** — see `LICENSE`.

NOUS builds on open-source components and draws inspiration from other projects.
This document tracks both.

## Direct components (open-source software we use)

| Component | Licence | Use |
|-----------|---------|-----|
| Whisper.cpp | MIT | Speech-to-text on Jetson |
| Piper TTS | MIT | Text-to-speech on Pi 5 |
| Ollama | MIT | LLM runtime on Jetson |
| Qwen2.5 | Apache 2.0 | LLM model |
| SearXNG | AGPL-3.0 | Private search |
| Qdrant | Apache 2.0 | Vector database |
| FastAPI | MIT | API framework |

## Inspiration (patterns we learned from)

### jetson-orin-kian (aschweig)
https://github.com/aschweig/jetson-orin-kian
Licence: MIT

Patterns taken from:
- Pipeline architecture: VAD → STT → LLM → TTS → Speaker
- Streaming TTS pattern (split on punctuation, play progressively)
- Memory budget analysis for Jetson Orin Nano 8GB
- Headless-mode recommendation to free up RAM

Differences:
- NOUS targets multilingual use; Kian is English-only
- NOUS has a distributed architecture (Pi 5 + Jetson); Kian is single-node
- NOUS has an internet proxy with tool isolation
- NOUS has scope/wing-based memory
