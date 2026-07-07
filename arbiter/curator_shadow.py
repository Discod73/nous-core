"""
Curator v1 Shadow-mode for Memory Arbiter.

Observerer og logger kun — foreslår ALDRIG, skriver ALDRIG, blokerer ALDRIG.
Perioden er 6 uger fra 2026-07-03 til ca. 2026-08-14.

Curator v1 = TF-IDF + LogisticRegression (sklearn Pipeline, pkl-fil).
Klasser loades fra wings.json. SECRET-scope wings er ekskluderet fra træning;
alle forudsigelser for SECRET-punkter vil altid være "dangerous" per scope-hierarkiet.
"""
import json
import logging
import pickle
import warnings
from pathlib import Path

log = logging.getLogger("arbiter.curator")

CURATOR_PKL = Path("/mnt/nous-data/gaia_baseline_curatorv1.pkl")
WINGS_FILE  = Path("/srv/nous/config/wings.json")

# Scope-hierarki: højere tal = bredere deling
SCOPE_BREADTH: dict[str, int] = {
    "SECRET": 0,
    "PRIVATE": 1,
    "SWARM": 2,
    "PUBLIC": 3,
}

# ── Asymmetrisk fejltærskel — HARDKODET, ikke konfigurerbar ──────────────────
# None = ingen tærskel defineret for denne retning.
# Farlig retning: modellen foreslår bredere deling end korrekt.
# Sikker retning: modellen foreslår snævrere deling end korrekt.
THRESHOLDS: dict[str, dict[str, float | None]] = {
    "SECRET":  {"dangerous": 0.00,  "safe": None},
    "PRIVATE": {"dangerous": 0.01,  "safe": 0.03},
    "SWARM":   {"dangerous": 0.01,  "safe": 0.03},
    "PUBLIC":  {"dangerous": None,  "safe": 0.05},
}

_model      = None
_wing_scope: dict[str, str] = {}


def load_curator() -> bool:
    """Indlæs Curator v1 pkl ved opstart. Returnerer True ved succes."""
    global _model, _wing_scope
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")   # undertrykker sklearn-versionsadvarsler
            with open(CURATOR_PKL, "rb") as f:
                _model = pickle.load(f)
        _wing_scope = _build_wing_scope()
        log.info(
            "Curator v1 indlæst — klasser: %s  shadow-logging aktiv",
            list(str(c) for c in _model.classes_),
        )
        return True
    except Exception as e:
        log.warning("Curator v1 indlæsning fejlede: %s — shadow-logging deaktiveret", e)
        return False


def _build_wing_scope() -> dict[str, str]:
    try:
        data = json.loads(WINGS_FILE.read_text(encoding="utf-8"))
        return {w["name"]: w["scope"] for w in data.get("wings", [])}
    except Exception as e:
        log.warning("Kunne ikke indlæse wing→scope mapping: %s", e)
        return {}


def predict(text: str) -> tuple[str, str, float]:
    """Returnér (predicted_wing, predicted_scope, confidence).

    predicted_scope = 'UNKNOWN' hvis wing ikke er i wings.json.
    Returnerer ('', '', 0.0) hvis model ikke er indlæst eller tekst er tom.
    """
    if _model is None or not text or not text.strip():
        return "", "", 0.0
    try:
        proba = _model.predict_proba([text[:2000]])[0]
        idx   = int(proba.argmax())
        wing  = str(_model.classes_[idx])
        conf  = float(round(float(proba[idx]), 4))
        scope = _wing_scope.get(wing, "UNKNOWN")
        return wing, scope, conf
    except Exception as e:
        log.debug("Curator predict fejl: %s", e)
        return "", "", 0.0


def error_direction(actual_scope: str, predicted_scope: str) -> str:
    """Klassificér fejlretning for en forudsigelse.

    Returværdier:
      'correct'      — korrekt scope (wing kan stadig være forkert)
      'dangerous'    — forudsiger bredere deling end korrekt
      'safe'         — forudsiger snævrere deling end korrekt
      'scope_unknown'— forudsagt wing er ikke i wings.json
    """
    if predicted_scope == "UNKNOWN":
        return "scope_unknown"
    ab = SCOPE_BREADTH.get(actual_scope, -1)
    pb = SCOPE_BREADTH.get(predicted_scope, -1)
    if ab < 0 or pb < 0:
        return "scope_unknown"
    if pb > ab:
        return "dangerous"
    if pb < ab:
        return "safe"
    return "correct"
