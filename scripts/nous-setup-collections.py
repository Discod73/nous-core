#!/usr/bin/env python3
"""
NOUS Setup Collections
----------------------
Opretter alle Qdrant collections der mangler.
Kør efter Qdrant genstart eller ved nye wing-installationer.

Brug:
    python3 nous-setup-collections.py
"""
import os
import json
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
VECTOR_DIM = 768  # nomic-embed-text

_WINGS_CONFIG = Path("/srv/nous/config/wings.json")

def load_collections() -> list:
    if not _WINGS_CONFIG.exists():
        print(f"FEJL: {_WINGS_CONFIG} ikke fundet")
        return []
    data = json.loads(_WINGS_CONFIG.read_text())
    return [w["collection"] for w in data.get("wings", [])]


def main():
    print("=" * 60)
    print("  NOUS: Opretter Qdrant collections")
    print("=" * 60 + "\n")
    
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    
    for name in load_collections():
        try:
            client.get_collection(name)
            print(f"  ✓ {name:30} findes allerede")
        except Exception:
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(
                    size=VECTOR_DIM,
                    distance=Distance.COSINE,
                    on_disk=True,
                ),
            )
            print(f"  → {name:30} OPPRETTET")
    
    print("\n" + "=" * 60)
    print("  Alle collections klar")
    print("=" * 60)


if __name__ == "__main__":
    main()
