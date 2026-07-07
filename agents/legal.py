#!/usr/bin/env python3
"""
NOUS Legal Agent — juridiske spørgsmål, forældreansvarssager, myndighedssager.
Rettigheder styres via config/wings.json (agent-tag: "legal").
SECRET-wings kan aldrig skrives til af denne agent.
"""
import json
from pathlib import Path
from agent_base import NousAgent

_LEGAL_SYSTEM = """\
Du er NOUS legal-agent specialiseret i dansk jura, forældreansvarssager og myndighedssager.
Identificer afgørelser, lovgrundlag og mønstre. Citér præcist fra kilderne.
Hold dig STRENGT til dokumenterne. Opfind aldrig. Svar på dansk."""

_WINGS_FILE = Path("/srv/nous/config/wings.json")

def _secret_wings() -> frozenset[str]:
    """Wings med SECRET scope — aldrig skrivbare."""
    try:
        data = json.loads(_WINGS_FILE.read_text())
        return frozenset(w["name"] for w in data["wings"] if w.get("scope") == "SECRET")
    except Exception:
        return frozenset()

def _wings_for_agent(tag: str, write: bool = False) -> list[str]:
    key = "agents_write" if write else "agents"
    try:
        data = json.loads(_WINGS_FILE.read_text())
        return [w["name"] for w in data.get("wings", []) if tag in w.get(key, [])]
    except Exception:
        return []

_FORBIDDEN_WRITE_WINGS = _secret_wings()


class LegalAgent(NousAgent):
    name = "legal"
    allowed_wings  = _wings_for_agent("legal")
    writable_wings = _wings_for_agent("legal", write=True)
    scope = "PRIVATE"
    # model/timeout sættes ikke — ingen LLM-kald i interaktiv mode (reserveret til night_pipeline)

    def _system_prompt(self) -> str:
        return _LEGAL_SYSTEM

    def write(self, wing: str, points: list[dict]) -> dict:
        if wing in _FORBIDDEN_WRITE_WINGS:
            raise PermissionError(
                f"Legal agent må ALDRIG skrive til '{wing}' — "
                "kun night_pipeline og ingest har skriveadgang hertil"
            )
        if wing not in self.writable_wings:
            raise PermissionError(
                f"Legal agent har ikke skriveadgang til '{wing}'"
            )
        return super().write(wing, points)
