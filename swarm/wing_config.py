"""NOUS Swarm — Wing-selektion per swarm-type."""
import json
import threading
from pathlib import Path

WING_SWARM_CONFIG = Path("/mnt/nous-data/wing_swarm_config.json")
WINGS_FILE        = Path("/srv/nous/config/wings.json")
_lock = threading.Lock()


def _load_wings_json() -> list[dict]:
    try:
        return json.loads(WINGS_FILE.read_text(encoding="utf-8")).get("wings", [])
    except Exception:
        return []

# Hård udelukkelse — wings med contains_personal_sensitive=true eller SECRET scope
# deltager aldrig i SWARM uanset konfiguration. Loades fra wings.json.
def _build_never_swarm() -> frozenset[str]:
    return frozenset(
        w["name"] for w in _load_wings_json()
        if w.get("contains_personal_sensitive", True) or w.get("scope") == "SECRET"
    )

NEVER_SWARM: frozenset[str] = _build_never_swarm()

# WHITELIST — DENY by default.
# En wing kan KUN nå SWARM-promotion hvis den er eksplicit nævnt her.
# Ingen ny wing tilføjes uden en bevidst commit-beslutning.
# Wings med contains_personal_sensitive=true i wings.json kan ALDRIG stå her.
SWARM_ALLOWED_WINGS: frozenset[str] = frozenset({
    "Arbejde",
    "beredskab",
})

def _build_defaults() -> dict[str, dict[str, bool]]:
    """Byg default-konfiguration fra wings.json — alle SWARM-flags False."""
    return {
        w["name"]: {"familia": False, "global": False, "work": False}
        for w in _load_wings_json()
        if w.get("name")
    }

_DEFAULTS: dict[str, dict[str, bool]] = _build_defaults()


def _load() -> dict:
    if WING_SWARM_CONFIG.exists():
        try:
            data = json.loads(WING_SWARM_CONFIG.read_text(encoding="utf-8"))
            # Sikr NEVER_SWARM og SWARM_ALLOWED_WINGS er konsistente
            never = _build_never_swarm()
            for wing in data.get("wings", {}):
                if wing in never or wing not in SWARM_ALLOWED_WINGS:
                    data["wings"][wing] = {"familia": False, "global": False, "work": False}
            return data
        except Exception:
            pass
    return {"wings": dict(_DEFAULTS)}


def _save(data: dict) -> None:
    WING_SWARM_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = WING_SWARM_CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(WING_SWARM_CONFIG)


def _is_personal_sensitive(wing: str) -> bool:
    """Returnerer True hvis wings.json markerer contains_personal_sensitive=true (default True)."""
    try:
        entry = next((w for w in _load_wings_json() if w["name"] == wing), None)
        if entry is None:
            return True  # Ukendt wing — konservativ default
        return entry.get("contains_personal_sensitive", True)
    except Exception:
        return True  # Fejl ved læsning → konservativ default


def get_all_wing_config() -> dict[str, dict[str, bool]]:
    return _load()["wings"]


def get_wings_for_swarm_type(swarm_type: str) -> list[str]:
    """Returner wings der er aktiveret for swarm_type og er på SWARM_ALLOWED_WINGS."""
    wings = _load()["wings"]
    return [
        name for name, cfg in wings.items()
        if cfg.get(swarm_type, False) and name in SWARM_ALLOWED_WINGS
    ]


def set_wing_swarm(wing: str, swarm_type: str, enabled: bool) -> None:
    never = _build_never_swarm()
    if wing in never:
        raise ValueError(f"Wing '{wing}' kan aldrig deltage i SWARM (NEVER_SWARM)")
    if enabled and wing not in SWARM_ALLOWED_WINGS:
        if _is_personal_sensitive(wing):
            raise ValueError(
                f"Wing '{wing}' er markeret contains_personal_sensitive=true i wings.json "
                f"og kan aldrig tilføjes til SWARM_ALLOWED_WINGS"
            )
        raise ValueError(
            f"Wing '{wing}' er ikke på SWARM_ALLOWED_WINGS — tilføj den eksplicit "
            f"til wing_config.py med en bevidst commit-beslutning"
        )
    if swarm_type not in ("familia", "global", "work"):
        raise ValueError(f"Ukendt swarm-type: {swarm_type}")
    with _lock:
        data = _load()
        wings = data.setdefault("wings", {})
        if wing not in wings:
            wings[wing] = {"familia": False, "global": False, "work": False}
        wings[wing][swarm_type] = enabled
        _save(data)


def set_wing_config(wing: str, config: dict[str, bool]) -> None:
    """Sæt fuld konfiguration for én wing på én gang."""
    never = _build_never_swarm()
    if wing in never:
        raise ValueError(f"Wing '{wing}' kan aldrig deltage i SWARM (NEVER_SWARM)")
    enabling_any = any(bool(v) for k, v in config.items() if k in ("familia", "global", "work"))
    if enabling_any and wing not in SWARM_ALLOWED_WINGS:
        if _is_personal_sensitive(wing):
            raise ValueError(
                f"Wing '{wing}' er markeret contains_personal_sensitive=true og kan aldrig aktiveres for SWARM"
            )
        raise ValueError(
            f"Wing '{wing}' er ikke på SWARM_ALLOWED_WINGS"
        )
    with _lock:
        data = _load()
        wings = data.setdefault("wings", {})
        current = wings.get(wing, {"familia": False, "global": False, "work": False})
        current.update({k: bool(v) for k, v in config.items() if k in ("familia", "global", "work")})
        wings[wing] = current
        _save(data)
