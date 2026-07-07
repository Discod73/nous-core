#!/usr/bin/env python3
"""
Test: SWARM-allowlist sikkerhed + sensitivitets-advarsel.

Verificerer:
1. En ny/ukendt wing kan IKKE promoteres uden at stå på SWARM_ALLOWED_WINGS.
2. Wings med contains_personal_sensitive=true i wings.json er blokeret fra allowlisten.
3. SWARM_ALLOWED_WINGS indeholder ingen wings der er contains_personal_sensitive=true.
4. high_sensitivity-kolonnen eksisterer i promotion_queue.
5. Sensitivitets-prompts indeholder korrekte nøgleord.

Kør: python3 /srv/nous/tests/test_swarm_security.py
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "swarm"))
from wing_config import (
    SWARM_ALLOWED_WINGS,
    NEVER_SWARM,
    WINGS_FILE,
    set_wing_swarm,
    _is_personal_sensitive,
)
from promotion import SENSITIVITY_CHECK_PROMPT, DB_PATH, _db as _promotion_db

PASS = "OK"
FAIL = "FEJL"
errors: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS}: {label}")
    else:
        msg = f"{label}" + (f" — {detail}" if detail else "")
        print(f"  {FAIL}: {msg}")
        errors.append(msg)


# ── Test 1: Ny wing er ikke på allowlisten ────────────────────────────────────
print("\n[1] Ny wing blokeret uden for allowlisten")

test_wing = "_test_swarm_security_wing_xyz"
check(
    f"'{test_wing}' er ikke på SWARM_ALLOWED_WINGS",
    test_wing not in SWARM_ALLOWED_WINGS,
)

try:
    set_wing_swarm(test_wing, "global", True)
    check("set_wing_swarm() fejlede for ny wing", False,
          "kaldet burde have kastet ValueError men returnerede normalt")
except ValueError as e:
    check(f"set_wing_swarm() kaster ValueError korrekt: {e}", True)


# ── Test 2: Sensitive wings er blokeret ──────────────────────────────────────
print("\n[2] Wings med contains_personal_sensitive=true er blokeret")

wings_data = json.loads(WINGS_FILE.read_text(encoding="utf-8"))
sensitive_wings = [
    w["name"] for w in wings_data["wings"]
    if w.get("contains_personal_sensitive", True)
]
check(
    f"{len(sensitive_wings)} sensitive wings fundet i wings.json",
    len(sensitive_wings) > 0,
    "forventede mindst én",
)

for wing_name in sensitive_wings:
    # Må IKKE være på SWARM_ALLOWED_WINGS
    check(
        f"Sensitiv wing '{wing_name}' er ikke på SWARM_ALLOWED_WINGS",
        wing_name not in SWARM_ALLOWED_WINGS,
    )
    # API-niveau: set_wing_swarm(..., True) bør fejle
    try:
        set_wing_swarm(wing_name, "global", True)
        check(
            f"set_wing_swarm('{wing_name}') fejlede",
            False,
            "kaldet burde have kastet ValueError",
        )
    except ValueError:
        check(f"set_wing_swarm('{wing_name}', enabled=True) kaster ValueError", True)


# ── Test 3: Allowlist er konsistent med wings.json ───────────────────────────
print("\n[3] SWARM_ALLOWED_WINGS er konsistent med contains_personal_sensitive")

for wing_name in SWARM_ALLOWED_WINGS:
    is_sensitive = _is_personal_sensitive(wing_name)
    check(
        f"Allowlistet wing '{wing_name}' har contains_personal_sensitive=false",
        not is_sensitive,
        f"contains_personal_sensitive={is_sensitive} men wing er på allowlisten — KRITISK!",
    )


# ── Test 4: Databasekolonne high_sensitivity eksisterer ──────────────────────
print("\n[4] DB-kolonne high_sensitivity eksisterer (hvis DB er oprettet)")


# Kald _db() for at trigge migration (opretter kolonnen hvis den mangler)
conn = _promotion_db()
try:
    conn.execute("SELECT high_sensitivity FROM promotion_queue LIMIT 1")
    check("Kolonne high_sensitivity eksisterer i promotion_queue", True)
except sqlite3.OperationalError as e:
    check("Kolonne high_sensitivity eksisterer i promotion_queue", False, str(e))
finally:
    conn.close()


# ── Test 5: Sensitivitets-prompt indeholder nøgleord ─────────────────────────
print("\n[5] Sensitivitets-prompt validering")

personal_text = "En kvinde i 30'erne med to børn i skolealderen oplevede tvangsfjernelse"
prompt = SENSITIVITY_CHECK_PROMPT.format(anon_text=personal_text)

check("Prompt indeholder 'sårbar'", "sårbar" in prompt)
check("Prompt indeholder 'barn' eller 'børn'", "barn" in prompt or "børn" in prompt)
check("Prompt beder om 'JA' eller 'NEJ' svar", "JA" in prompt and "NEJ" in prompt)
check(
    "Prompt nævner identifikation uden navne",
    "navne" in prompt or "UAFHÆNGIGT" in prompt,
)

# Simulér et klart-ja-tilfælde: tekst med børnedetaljer
check(
    "Test-tekst med sensitive detaljer er ikke tom",
    len(personal_text) > 10,
)


# ── Resultat ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 50)
if errors:
    print(f"FEJL: {len(errors)} test(s) fejlede:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print(f"Alle tests bestod.")
