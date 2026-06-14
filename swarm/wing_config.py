"""NOUS Swarm — Wing-selektion per swarm-type."""
import json
import threading
from pathlib import Path

WING_SWARM_CONFIG = Path("/mnt/nous-data/wing_swarm_config.json")
_lock = threading.Lock()

# Disse wings deltager aldrig i SWARM uanset konfiguration
NEVER_SWARM: frozenset[str] = frozenset({"boernesag", "fbf", "dans_profil"})

_DEFAULTS: dict[str, dict[str, bool]] = {
    "boernesag":   {"familia": False, "global": False, "work": False},
    "fbf":         {"familia": False, "global": False, "work": False},
    "dans_profil": {"familia": False, "global": False, "work": False},
    "jura":        {"familia": True,  "global": True,  "work": False},
    "familie":     {"familia": True,  "global": False, "work": False},
    "Arbejde":     {"familia": False, "global": True,  "work": True},
    "okonomi":     {"familia": False, "global": True,  "work": False},
    "beredskab":   {"familia": False, "global": True,  "work": False},
    "nous_projekt":{"familia": False, "global": True,  "work": False},
}


def _load() -> dict:
    if WING_SWARM_CONFIG.exists():
        try:
            data = json.loads(WING_SWARM_CONFIG.read_text(encoding="utf-8"))
            # Sikr altid at NEVER_SWARM er slået fra
            for wing in NEVER_SWARM:
                if wing in data.get("wings", {}):
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


def get_all_wing_config() -> dict[str, dict[str, bool]]:
    return _load()["wings"]


def get_wings_for_swarm_type(swarm_type: str) -> list[str]:
    """Returner wings der er aktiveret for swarm_type ('familia'|'global'|'work')."""
    wings = _load()["wings"]
    return [name for name, cfg in wings.items() if cfg.get(swarm_type, False)]


def set_wing_swarm(wing: str, swarm_type: str, enabled: bool) -> None:
    if wing in NEVER_SWARM:
        raise ValueError(f"Wing '{wing}' kan aldrig deltage i SWARM")
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
    if wing in NEVER_SWARM:
        raise ValueError(f"Wing '{wing}' kan aldrig deltage i SWARM")
    with _lock:
        data = _load()
        wings = data.setdefault("wings", {})
        current = wings.get(wing, {"familia": False, "global": False, "work": False})
        current.update({k: bool(v) for k, v in config.items() if k in ("familia", "global", "work")})
        wings[wing] = current
        _save(data)
