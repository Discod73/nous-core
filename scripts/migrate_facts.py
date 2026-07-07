"""
migrate_facts.py — slet alle Qdrant-facts med text > 200 tegn (old-style v1 facts).
Kør én gang manuelt efter udrulning af fact_extractor v2.

Brug:
  python3 migrate_facts.py            # Kør migrering
  python3 migrate_facts.py --dry-run  # Vis hvad der ville blive slettet
"""
import json
import sys
from pathlib import Path

import httpx

QDRANT_URL   = "http://localhost:6333"
WINGS_FILE   = Path("/srv/nous/config/wings.json")
MAX_TEXT_LEN = 200
DRY_RUN      = "--dry-run" in sys.argv


def load_wings() -> list[dict]:
    return json.loads(WINGS_FILE.read_text(encoding="utf-8")).get("wings", [])


def find_old_facts(collection: str) -> list:
    """Returnerer ID-liste på facts med text > MAX_TEXT_LEN tegn og ingen fact_schema_version."""
    to_delete = []
    offset = None

    while True:
        body: dict = {
            "limit": 256,
            "with_payload": ["text", "fact_schema_version"],
            "with_vector": False,
            "filter": {"must": [{"key": "type", "match": {"value": "fact"}}]},
        }
        if offset:
            body["offset"] = offset

        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/scroll",
            json=body, timeout=20.0,
        )
        r.raise_for_status()
        result = r.json().get("result", {})

        for pt in result.get("points", []):
            payload = pt.get("payload", {})
            # Bevar v2-facts (har fact_schema_version=2)
            if payload.get("fact_schema_version") == 2:
                continue
            text = payload.get("text", "")
            if len(text) > MAX_TEXT_LEN:
                to_delete.append(pt["id"])

        offset = result.get("next_page_offset")
        if not offset:
            break

    return to_delete


def delete_batch(collection: str, ids: list) -> int:
    deleted = 0
    for i in range(0, len(ids), 100):
        batch = ids[i : i + 100]
        r = httpx.post(
            f"{QDRANT_URL}/collections/{collection}/points/delete",
            json={"points": batch},
            timeout=20.0,
        )
        r.raise_for_status()
        deleted += len(batch)
    return deleted


def main() -> None:
    wings = load_wings()
    total_found = total_deleted = 0

    print(f"Migrér v1-facts (text > {MAX_TEXT_LEN} tegn){'  [DRY RUN]' if DRY_RUN else ''}")
    print("─" * 55)

    for w in wings:
        name       = w["name"]
        collection = w["collection"]
        print(f"  {name} ({collection})... ", end="", flush=True)

        try:
            ids = find_old_facts(collection)
            total_found += len(ids)

            if not ids:
                print("ingen")
                continue

            if DRY_RUN:
                print(f"{len(ids)} at slette")
            else:
                n = delete_batch(collection, ids)
                total_deleted += n
                print(f"{n}/{len(ids)} slettet")

        except Exception as e:
            print(f"FEJL: {e}")

    print("─" * 55)
    if DRY_RUN:
        print(f"Total: {total_found} v1-facts ville blive slettet")
    else:
        print(f"Total: {total_deleted}/{total_found} v1-facts slettet")


if __name__ == "__main__":
    main()
