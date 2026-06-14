"""
Scope validation for Memory Arbiter.
Rules are static — SECRET can never go to SWARM, hardcoded.
"""
import json
import os
from pathlib import Path

WINGS_FILE  = Path(os.environ.get("NOUS_WINGS_FILE", "/srv/nous/config/wings.json"))
VALID_SCOPES = frozenset({"SECRET", "PRIVATE", "SWARM", "PUBLIC"})

# Hardcoded — not configurable
_SECRET_FORBIDDEN_TARGETS = frozenset({"SWARM", "PUBLIC"})


class ScopeError(Exception):
    pass


def validate(wing_name: str, scope: str) -> str:
    """Validate intent and return the Qdrant collection name."""
    if scope not in VALID_SCOPES:
        raise ScopeError(f"Ugyldigt scope '{scope}' — tilladt: {', '.join(sorted(VALID_SCOPES))}")

    data = json.loads(WINGS_FILE.read_text(encoding="utf-8"))
    wing = next((w for w in data.get("wings", []) if w["name"] == wing_name), None)
    if wing is None:
        raise ScopeError(f"Wing '{wing_name}' ikke fundet i wings.json")

    wing_scope = wing["scope"]
    if scope != wing_scope:
        raise ScopeError(
            f"Scope-mismatch: wing '{wing_name}' er {wing_scope}, intent angiver {scope}"
        )

    # SECRET-data forlader aldrig enheden
    if wing_scope == "SECRET" and scope in _SECRET_FORBIDDEN_TARGETS:
        raise ScopeError("SECRET-data må aldrig skrives til SWARM eller PUBLIC")

    return wing["collection"]
