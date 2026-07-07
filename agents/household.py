#!/usr/bin/env python3
"""
NOUS Household Agent — rutiner, madplan, kalender, hjem, familie, hverdagsopgaver.
Rettigheder styres via config/wings.json (agent-tag: "household").
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agent_base import NousAgent, OLLAMA_URL, EMBED_MODEL, LLM_7B, AGENT_TIMEOUT_FAST

_WINGS_FILE = Path("/srv/nous/config/wings.json")

def _wings_for_agent(tag: str, write: bool = False) -> list[str]:
    key = "agents_write" if write else "agents"
    try:
        data = json.loads(_WINGS_FILE.read_text())
        return [w["name"] for w in data.get("wings", []) if tag in w.get(key, [])]
    except Exception:
        return []

_HOUSEHOLD_SYSTEM = """\
Du er NOUS household-agent. Du hjælper Dan med rutiner, madplan, kalender, hjem, familie og hverdagsopgaver.
Brug vidensbasen som primær kilde. Svar kort og præcist på dansk.
Find aldrig på fakta — sig 'Det ved jeg ikke' hvis du mangler information."""


class HouseholdAgent(NousAgent):
    name = "household"
    allowed_wings = _wings_for_agent("household")
    scope = "PRIVATE"
    model = LLM_7B
    timeout = AGENT_TIMEOUT_FAST

    def _system_prompt(self) -> str:
        return _HOUSEHOLD_SYSTEM

    def save_fact(self, text: str) -> dict:
        """Gem ny husstandsoplysning via Arbiter."""
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
                "source":    "household_agent",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
        write_wings = _wings_for_agent("household", write=True)
        return self.write(write_wings[0] if write_wings else "", [point])
