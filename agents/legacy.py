#!/usr/bin/env python3
"""
NOUS Legacy Agent — taler i ejerens stemme til børnene.
Læseadgang: dans_profil. Skriveadgang: dans_profil.
Returnerer altid kilder til menneskelig verifikation.
"""
import os
import uuid
from datetime import datetime, timezone

import httpx

from agent_base import NousAgent, OLLAMA_URL, EMBED_MODEL, LLM_7B, AGENT_TIMEOUT_FAST

_OWNER_NAME = os.environ.get("NOUS_OWNER_NAME", "Dan")

_LEGACY_SYSTEM = f"""\
Du er NOUS, og du taler på vegne af {_OWNER_NAME} til hans børn.

Du har adgang til verificerede facts og erindringer om {_OWNER_NAME}.
TAL I {_OWNER_NAME.upper()}S STEMME: direkte, varm, jordnær, uden floskler.

ABSOLUT REGEL: Opfind ALDRIG minder, citater eller holdninger.
Hvis du ikke har belæg i kilderne: sig præcist "Det ved jeg ikke om Far."
Baser ALT udelukkende på verificerede facts fra vidensbasen."""

_LEGACY_TYPES = frozenset({"summary", "fact", "direct_memory"})


class LegacyAgent(NousAgent):
    name = "legacy"
    allowed_wings = ["dans_profil"]
    scope = "PRIVATE"
    model = LLM_7B
    timeout = AGENT_TIMEOUT_FAST

    def _system_prompt(self) -> str:
        return _LEGACY_SYSTEM

    def read(self, query: str, wing: str = "dans_profil", limit: int = 10, threshold: float = 0.50) -> list[dict]:
        """Søger kun summaries og facts — filtrerer chunks fra."""
        hits = super().read(query, wing, limit=limit * 2, threshold=threshold)
        filtered = [h for h in hits if h["payload"].get("type") in _LEGACY_TYPES]
        return filtered[:limit]

    def think_with_sources(self, query: str) -> tuple[str, list[dict]]:
        """Returnerer (svar, kildeliste) til menneskelig verifikation."""
        hits: list[dict] = []
        try:
            hits = self.read(query)
        except Exception:
            pass
        context = "\n\n---\n\n".join(
            h["payload"].get("text", "")[:600] for h in hits
        )
        response = self.think(context, query)
        return response, hits

    def save_verified_fact(self, text: str) -> dict:
        """Gem verificeret fact i dans_profil via Arbiter."""
        embed_r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30,
        )
        embed_r.raise_for_status()
        vector = embed_r.json()["embedding"]
        point = {
            "id":     str(uuid.uuid4()),
            "vector": vector,
            "payload": {
                "text":      text,
                "type":      "fact",
                "scope":     "PRIVATE",
                "source":    "legacy_agent_verified",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
        return self.write("dans_profil", [point])
