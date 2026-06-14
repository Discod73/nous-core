# NOUS , your family's private AI, running entirely on hardware you own

NOUS started as a simple question: why should my family's documents, conversations, and personal data pass through someone else's servers to get the benefits of AI?

It began as a single Raspberry Pi experiment. Today it's a two-node system , a Pi 5 handling documents, search, and the web interface, and a Jetson handling AI inference, air-gapped with zero internet access. Your data never leaves your home. No subscriptions, no API costs, no usage limits, no one reading along.

---

## What it does now

- Ingests and analyzes your documents , drop a PDF, image, or audio file and it's searchable via semantic search
- Organizes knowledge in "wings" with strict access levels , family data stays private, sensitive data stays secret, period
- Runs a nightly analysis pipeline that summarizes, extracts facts, and finds inconsistencies across your documents while you sleep
- Multi-agent architecture: a household assistant for everyday questions, a legal engine for document-heavy casework
- Panel debate: let multiple AI models argue a question and give you the consensus
- Full model control: choose which models run day and night, tune them from the UI
- A guide built into the interface so non-technical family members can actually use it

---

## How it's built

NOUS is a two-node system by design. The primary node (Raspberry Pi 5) runs the web interface, document ingestion, vector search (Qdrant), and the Memory Arbiter , a single-writer gatekeeper that enforces access levels on every piece of data. The inference node (Jetson Orin NX) runs the language models and is air-gapped: it can only talk to the primary node, nothing else. Not even the internet.

Data is classified in four tiers , SECRET, PRIVATE, SWARM, PUBLIC , and the architecture enforces that classified data physically cannot leave the system. Promotion of knowledge to shareable tiers requires explicit human approval through the UI.

Everything is orchestrated through LangGraph multi-agents, with a supervisor routing questions to the right specialist. A night pipeline runs heavier models when the system is idle, so daytime stays fast.

Runs on a single 8 GB machine too , the two-node split is recommended, not required.

---

## Under the hood

**Memory Arbiter** , the heart of the system. Single writer to the vector database, scope enforcer, audit trail. Every write goes through it; nothing bypasses it. Reads are direct for speed.

**Intent Bus** , SQLite WAL-based message passing between components, no message broker needed.

**Wings** , isolated knowledge domains, each with its own collection and access tier.

**Night pipeline** , domain-aware summaries, fact extraction, cross-document analysis and inconsistency detection, all on the heavier night model.

**Swarm Agent** , phase 1: local promotion pipeline with anonymization and human approval. Phase 2+3 (P2P sharing between trusted nodes) is built and awaiting pilot.

**Two-tier retrieval** , summary embeddings first, then chunks, so search stays accurate even with large document bases.

**Zero-trust networking** , Tailscale ACL controls who, nftables protects the inference node. Trust the person, not the machine.

---

## Standing on the shoulders of

Built with llama.cpp, Ollama, Qdrant, LangGraph, faster-whisper, SearXNG, and Crawl4AI. Memory architecture draws inspiration from MemoryOS (BAI-LAB, EMNLP 2025) and the broader research on hierarchical agent memory. The night-pipeline pattern and single-writer arbiter are NOUS originals born from running multi-agent systems on constrained hardware , sometimes 16 GB forces better architecture.

---

## Full transparency on how this was built

I'm not a programmer. I'm a hobbyist with a background in cooking, IT support, gardening, and truck driving. NOUS exists because I had a clear idea of what I wanted, and AI did the heavy lifting on implementation.

The architecture, the decisions, the doctrine , local-first, evidence-bound, single-writer memory, four-tier classification , that's mine, refined through months of arguing with multiple AI models until the design held up. The code itself is largely written by Claude (chat for architecture and planning, Claude Code for implementation), with ChatGPT, DeepSeek and Kimi as sparring partners along the way.

I mention this for two reasons: honesty, and because it means you can do this too. If you can describe what you want precisely enough and push back when the AI overcomplicates things, you can build systems like this. The bottleneck isn't coding anymore , it's knowing what you want and why.

---

*An AI system, thought out by a human, built by AI.*

*That sentence would have been science fiction three years ago. Now it's just how I spent my evenings.*
