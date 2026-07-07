#!/usr/bin/env python3
"""
NOUS Ingest — Manuel klassifikation
Brug: python3 nous-ingest-manual.py /sti/til/fil.pdf
"""

import sys
import json
from pathlib import Path

_WINGS_CONFIG = Path("/srv/nous/config/wings.json")

def load_wings() -> list:
    if not _WINGS_CONFIG.exists():
        return []
    data = json.loads(_WINGS_CONFIG.read_text())
    return [w["name"] for w in data.get("wings", [])]

def select_scope():
    print("\n=== DOKUMENTKLASSIFIKATION ===")
    print("Hvem skal have adgang til dette dokument?")
    print("  1. SECRET    — Kun mig (CPR, passwords)")
    print("  2. PRIVATE   — Mine egne enheder (rutiner, journal, private noter)")
    print("  3. SWARM     — Anonymiseret deling med trusted netværk")
    print("  4. PUBLIC    — Offentligt tilgængelig")

    while True:
        choice = input("Vælg (1-4): ").strip()
        scopes = {"1": "SECRET", "2": "PRIVATE", "3": "SWARM", "4": "PUBLIC"}
        if choice in scopes:
            return scopes[choice]

def select_wing(wings: list) -> str:
    print("\n=== WING ===")
    for i, name in enumerate(wings, 1):
        print(f"  {i}. {name}")
    extra = len(wings) + 1
    print(f"  {extra}. Andet")

    choice = input(f"Vælg (1-{extra}): ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(wings):
            return wings[idx]
    except ValueError:
        pass
    if choice == str(extra):
        return input("Ny wing: ").strip().lower().replace(" ", "_")
    return wings[-1] if wings else "nous_projekt"

if __name__ == "__main__":
    filepath = sys.argv[1] if len(sys.argv) > 1 else input("Fil-sti: ")
    wings = load_wings()
    if not wings:
        print("FEJL: config/wings.json ikke fundet eller tom")
        sys.exit(1)
    scope = select_scope()
    wing = select_wing(wings)
    print(f"\nKopierer {filepath} til /home/nous/incoming/{wing}/")
    print(f"Scope: {scope}, Wing: {wing}")
    print("Ingest service processerer automatisk.")
