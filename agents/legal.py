#!/usr/bin/env python3
"""
NOUS Legal Agent — juridiske spørgsmål, forældreansvarssager, myndighedssager.
Læseadgang: jura, boernesag. Skriveadgang: jura ONLY.
ALDRIG skriv til boernesag — kun night_pipeline og ingest må skrive der.
"""
from agent_base import NousAgent

_LEGAL_SYSTEM = """\
Du er NOUS legal-agent specialiseret i dansk jura, forældreansvarssager og myndighedssager.
Identificer afgørelser, lovgrundlag og mønstre. Citér præcist fra kilderne.
Hold dig STRENGT til dokumenterne. Opfind aldrig. Svar på dansk."""

_FORBIDDEN_WRITE_WINGS = frozenset({"boernesag"})


class LegalAgent(NousAgent):
    name = "legal"
    allowed_wings = ["jura", "boernesag"]   # læseadgang
    writable_wings = ["jura"]               # skriveadgang — boernesag aldrig
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
