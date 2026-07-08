# NOUS Credits & Acknowledgments

NOUS er udgivet under **GNU Affero General Public License v3.0 (AGPL-3.0)** — se `LICENSE`.

NOUS bygger på open-source komponenter og tager inspiration fra andre projekter.
Dette dokument holder styr på begge dele.

## Direkte komponenter (open-source software vi bruger)

| Komponent | Licens | Brug |
|-----------|--------|------|
| Whisper.cpp | MIT | Speech-to-text på Jetson |
| Piper TTS | MIT | Tekst-til-tale på Pi 5 |
| Ollama | MIT | LLM-runtime på Jetson |
| Qwen2.5 | Apache 2.0 | LLM-model |
| SearXNG | AGPL-3.0 | Privat søgning |
| Qdrant | Apache 2.0 | Vektordatabase |
| FastAPI | MIT | Internet-proxy |

## Inspiration (mønstre vi har lært af)

### jetson-orin-kian (aschweig)
https://github.com/aschweig/jetson-orin-kian
Licens: MIT

Inspiration taget:
- Pipeline-arkitektur: VAD → STT → LLM → TTS → Speaker
- Streaming TTS-mønster (split på tegnsætning, afspil løbende)
- Memory budget-analyse for Jetson Orin Nano 8GB
- Headless-mode anbefaling for at frigøre RAM

Forskelle:
- NOUS er dansk-først, Kian er engelsk
- NOUS har distribueret arkitektur (Pi 5 + Jetson), Kian er enkelt-node
- NOUS har internet-proxy med tool-isolation
- NOUS har scope/wing-baseret hukommelse

