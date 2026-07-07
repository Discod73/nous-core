#!/usr/bin/env python3
"""
Trin 1 — Eksportér eval-data fra Pi5 til Nano.

Kør:  python3 /srv/nous/scripts/export_eval_data.py
Output: /mnt/nous-data/eval_data.json  (beholdes på Pi5)
        <NOUS_NANO_USER>@<NOUS_NANO_HOST>:<NOUS_NANO_EVAL_DEST>  (SCP til Nano)

Ekskluderer SECRET scope. Balancerer til maks. 400 pr. klasse.
"""
from __future__ import annotations
import json
import os
import random
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# ── Syntetisk supplement til wings med for lidt rigtig data ──────────────────
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "home/nous/lora_poc"))
try:
    from dataset import ALL_EXAMPLES as SYNTHETIC
    _HAS_SYNTHETIC = True
except ImportError:
    SYNTHETIC = []
    _HAS_SYNTHETIC = False

SOURCE_DB   = Path("/mnt/nous-data/intent_bus.db")
OUT_PATH    = Path("/mnt/nous-data/eval_data.json")
NANO_USER   = os.environ.get("NOUS_NANO_USER", "nous")
NANO_IP     = os.environ.get("NOUS_NANO_HOST", "CHANGE_ME")
NANO_DEST   = os.environ.get("NOUS_NANO_EVAL_DEST", "/home/nous/lora_v4/eval_data.json")
SSH_KEY     = Path(os.environ.get("NOUS_SSH_KEY_PATH", str(Path.home() / ".ssh" / "id_ed25519")))

SEED             = 42
CAP_PER_CLASS    = 400    # maks. rigtige eksempler pr. klasse
MIN_SYNTH_FLOOR  = 25     # klasser med < dette suppleres med syntetisk data
MIN_TEST_NEEDED  = 6      # mål-antal testeksempler pr. klasse


def extract_from_db() -> list[dict]:
    """Udtræk (text, wing) fra intent_bus.db (kun op=upsert, ikke SECRET)."""
    conn = sqlite3.connect(SOURCE_DB)
    rows = conn.execute(
        "SELECT wing, payload FROM intents "
        "WHERE scope != 'SECRET' AND operation = 'upsert'"
    ).fetchall()
    conn.close()

    records: list[dict] = []
    for wing, payload_str in rows:
        try:
            payload = json.loads(payload_str)
            for point in payload.get("points", []):
                text = point.get("payload", {}).get("text", "")
                if text and len(text.strip()) > 20:
                    records.append({"text": text.strip(), "label": wing})
        except (json.JSONDecodeError, AttributeError):
            continue
    return records


def train_test_split(
    records: list[dict],
    seed: int,
    min_test: int,
) -> tuple[list[dict], list[dict]]:
    """Stratificeret split: mindst `min_test` pr. klasse i test."""
    rng = random.Random(seed)
    by_class: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_class[r["label"]].append(r)

    train_all: list[dict] = []
    test_all:  list[dict] = []

    for label, examples in sorted(by_class.items()):
        rng.shuffle(examples)
        n_test = max(min_test, int(len(examples) * 0.20))
        n_test = min(n_test, len(examples) - min_test)  # lad mindst min_test gå til train
        n_test = max(n_test, 1)
        test_all.extend(examples[:n_test])
        train_all.extend(examples[n_test:])

    rng.shuffle(train_all)
    rng.shuffle(test_all)
    return train_all, test_all


def main() -> None:
    if not SOURCE_DB.exists():
        print(f"FEJL: {SOURCE_DB} ikke fundet")
        sys.exit(1)

    print(f"Indlæser {SOURCE_DB} ...")
    records = extract_from_db()

    # Tæl pr. klasse
    from collections import Counter
    raw_counts = Counter(r["label"] for r in records)
    print("\nRå data pr. klasse:")
    for label, n in sorted(raw_counts.items()):
        print(f"  {label:<20}: {n:5d}")
    print(f"  {'TOTAL':<20}: {len(records):5d}")

    # Balancér: cap pr. klasse
    rng = random.Random(SEED)
    by_class: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_class[r["label"]].append(r)

    balanced: list[dict] = []
    for label, examples in sorted(by_class.items()):
        rng.shuffle(examples)
        balanced.extend(examples[:CAP_PER_CLASS])

    # Supplér kleine klasser med syntetisk data
    counts_after = Counter(r["label"] for r in balanced)
    if _HAS_SYNTHETIC:
        synth_by_class: dict[str, list[dict]] = defaultdict(list)
        for ex in SYNTHETIC:
            synth_by_class[ex["label"]].append({"text": ex["text"], "label": ex["label"]})

        for label, n in sorted(counts_after.items()):
            if n < MIN_SYNTH_FLOOR and label in synth_by_class:
                needed = MIN_SYNTH_FLOOR - n
                synth = synth_by_class[label][:needed]
                balanced.extend(synth)
                print(f"  Syntetisk supplement til {label}: +{len(synth)}")

    # Endelig fordeling
    final_counts = Counter(r["label"] for r in balanced)
    print("\nEfter balancering:")
    for label, n in sorted(final_counts.items()):
        print(f"  {label:<20}: {n:5d}")
    print(f"  {'TOTAL':<20}: {len(balanced):5d}")

    # Stratificeret split
    train, test = train_test_split(balanced, seed=SEED, min_test=MIN_TEST_NEEDED)

    test_counts = Counter(r["label"] for r in test)
    print("\nTestsæt pr. klasse:")
    for label, n in sorted(test_counts.items()):
        flag = " ⚠ under 5" if n < 5 else ""
        print(f"  {label:<20}: {n:3d}{flag}")

    # Gem lokalt på Pi5
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seed": SEED,
        "cap_per_class": CAP_PER_CLASS,
        "train": train,
        "test": test,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nGemt lokalt: {OUT_PATH}  ({len(train)} train / {len(test)} test)")

    # Opret mappe på Nano og SCP
    print(f"\nOpret /home/nous/lora_v4/ på Nano ({NANO_IP}) ...")
    subprocess.run(
        ["ssh", "-i", str(SSH_KEY), "-o", "BatchMode=yes",
         f"{NANO_USER}@{NANO_IP}", "mkdir -p /home/nous/lora_v4"],
        check=True,
    )
    dest = f"{NANO_USER}@{NANO_IP}:{NANO_DEST}"
    print(f"SCP → {dest} ...")
    subprocess.run(
        ["scp", "-i", str(SSH_KEY), str(OUT_PATH), dest],
        check=True,
    )

    # Verificér
    result = subprocess.run(
        ["ssh", "-i", str(SSH_KEY), "-o", "BatchMode=yes",
         f"{NANO_USER}@{NANO_IP}",
         f"python3 -c \"import json; d=json.load(open('{NANO_DEST}')); "
         f"print('OK train:', len(d['train']), 'test:', len(d['test']))\""],
        capture_output=True, text=True, check=True,
    )
    print(f"Nano bekræfter: {result.stdout.strip()}")
    print("\nTrin 1 fuldført.")


if __name__ == "__main__":
    main()
