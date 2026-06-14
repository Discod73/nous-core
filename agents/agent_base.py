#!/usr/bin/env python3
"""
NOUS Agent base-klasse.
reads går direkte til Qdrant. writes KUN via Memory Arbiter (port 8010).
"""
import json
import os
from pathlib import Path

import httpx

QDRANT_URL  = os.environ.get("NOUS_QDRANT_URL",  "http://localhost:6333")
ARBITER_URL = os.environ.get("NOUS_ARBITER_URL", "http://localhost:8010")
OLLAMA_URL  = os.environ.get("NOUS_OLLAMA_URL",  "http://localhost:11434")
EMBED_MODEL = os.environ.get("NOUS_EMBED_MODEL", "nomic-embed-text")
LLM_7B      = os.environ.get("NOUS_LLM_7B",      "qwen2.5:7b")
LLM_14B     = os.environ.get("NOUS_LLM_14B",     "qwen3:14b")

AGENT_TIMEOUT_FAST = 120   # qwen2.5:7b — supervisor, household
AGENT_TIMEOUT_SLOW = 300   # qwen3:14b — legal

_WINGS_FILE = Path(os.environ.get("NOUS_WINGS_FILE", "/srv/nous/config/wings.json"))


def _load_wings() -> list[dict]:
    return json.loads(_WINGS_FILE.read_text(encoding="utf-8")).get("wings", [])


class NousAgent:
    name: str = ""
    allowed_wings: list[str] = []
    scope: str = "PRIVATE"
    model: str = LLM_14B
    timeout: int = AGENT_TIMEOUT_FAST

    def _wing_collection(self, wing: str) -> str:
        entry = next((w for w in _load_wings() if w["name"] == wing), None)
        if entry is None:
            raise ValueError(f"Wing '{wing}' ikke fundet i wings.json")
        return entry["collection"]

    def _wing_scope(self, wing: str) -> str:
        entry = next((w for w in _load_wings() if w["name"] == wing), None)
        if entry is None:
            raise ValueError(f"Wing '{wing}' ikke fundet")
        return entry["scope"]

    def read(self, query: str, wing: str, limit: int = 8, threshold: float = 0.45) -> list[dict]:
        """Direkte Qdrant-søgning — reads går IKKE via Arbiter."""
        if wing not in self.allowed_wings:
            raise PermissionError(f"Agent '{self.name}' har ingen læseadgang til wing '{wing}'")

        embed_r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": query[:8192]},
            timeout=30,
        )
        embed_r.raise_for_status()
        vector = embed_r.json()["embedding"]

        collection = self._wing_collection(wing)
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={"vector": vector, "limit": limit, "with_payload": True},
            timeout=15,
        )
        return [h for h in r.json().get("result", []) if h["score"] > threshold]

    def write(self, wing: str, points: list[dict]) -> dict:
        """Skriver KUN via Memory Arbiter — direkte Qdrant-kald er forbudt."""
        wing_scope = self._wing_scope(wing)
        r = httpx.post(
            f"{ARBITER_URL}/arbiter/write/sync",
            json={
                "wing":      wing,
                "scope":     wing_scope,
                "operation": "upsert",
                "points":    points,
                "source":    f"agent_{self.name}",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def think(self, context: str, query: str) -> str:
        """Kalder Ollama via NX — bruger agentens konfigurerede model og timeout."""
        system = self._system_prompt()
        if context:
            system += f"\n\nKontekst fra NOUS vidensbase:\n{context}"
        r = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model":   self.model,
                "stream":  False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": query},
                ],
                "options": {"temperature": 0.3},
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["message"]["content"]

    def _system_prompt(self) -> str:
        return f"Du er NOUS {self.name}-agent. Svar kort og præcist på dansk."
