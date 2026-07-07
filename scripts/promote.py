#!/usr/bin/env python3
"""
NOUS Promotion Tool v2
----------------------
Flytter dokumenter mellem wings med PII-preview, flueben-godkendelse
og eksplicit offentliggørelses-godkendelse.

Brug:
    python3 promote_v2.py <source_wing> <target_scope> [doc_id]
"""
import sys
import os
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import json
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

_WINGS_CONFIG = Path("/srv/nous/config/wings.json")

def _load_wings() -> tuple[dict, dict]:
    wing_coll: dict = {}
    scope_coll: dict = {}
    if not _WINGS_CONFIG.exists():
        return {}, {}
    data = json.loads(_WINGS_CONFIG.read_text())
    for w in data.get("wings", []):
        wing_coll[w["name"]] = w["collection"]
        scope = w.get("scope", "PRIVATE")
        if scope not in scope_coll:
            scope_coll[scope] = w["collection"]
    return wing_coll, scope_coll

WING_COLLECTIONS, SCOPE_COLLECTIONS = _load_wings()

PII_PATTERNS = {
    "DK_CPR":     (r"\b\d{6}[-\s]?\d{4}\b",                              "CPR-nummer"),
    "DK_PHONE":   (r"\b(?:\+45\s?)?(?:\d{2}\s?){3}\d{2}\b",              "Telefonnummer"),
    "DK_ADDRESS": (r"\b[A-ZÆØÅ][a-zæøå]+(?:vej|gade|plads|allé|boulevard|stræde)\s+\d+[A-Z]?", "Adresse"),
    "EMAIL":      (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "Email"),
    "PERSON":     (r"\b[A-ZÆØÅ][a-zæøå]+\s+[A-ZÆØÅ][a-zæøå]+(?:\s+[A-ZÆØÅ][a-zæøå]+)?\b", "Navn"),
}

DEFAULT_RULES = {
    "SECRET":  {"DK_CPR": False, "DK_PHONE": False, "DK_ADDRESS": False, "EMAIL": False, "PERSON": False},
    "PRIVATE": {"DK_CPR": False, "DK_PHONE": False, "DK_ADDRESS": False, "EMAIL": False, "PERSON": False},
    "SWARM":   {"DK_CPR": True,  "DK_PHONE": True,  "DK_ADDRESS": True,  "EMAIL": True,  "PERSON": True},
    "PUBLIC":  {"DK_CPR": True,  "DK_PHONE": True,  "DK_ADDRESS": True,  "EMAIL": True,  "PERSON": False},
}

KEYMAP_DB = "/mnt/nous-data/keymap.db"


def init_keymap():
    conn = sqlite3.connect(KEYMAP_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS keymap (
        token TEXT PRIMARY KEY, original TEXT NOT NULL, entity_type TEXT NOT NULL,
        scope TEXT NOT NULL, doc_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_keymap_doc ON keymap(doc_id)")
    conn.commit()
    conn.close()

def store_mapping(token, original, entity_type, scope, doc_id):
    conn = sqlite3.connect(KEYMAP_DB)
    conn.execute("INSERT OR REPLACE INTO keymap VALUES (?,?,?,?,?,?)",
        (token, original, entity_type, scope, doc_id, datetime.now(datetime.timezone.utc).isoformat()))
    conn.commit()
    conn.close()


def detect_pii(text: str) -> List[Dict]:
    found = []
    seen = set()
    for etype, (pat, label) in PII_PATTERNS.items():
        for m in re.finditer(pat, text, re.IGNORECASE):
            s = m.span()
            if s not in seen:
                seen.add(s)
                found.append({"type": etype, "label": label, "value": m.group(), "start": s[0], "end": s[1]})
    found.sort(key=lambda x: x["start"])
    return found


def generate_token(etype, value, idx):
    h = hex(hash(value) & 0xFFFF)[2:].zfill(4)
    return f"<{etype}_{h}_{idx}>"


def get_client():
    return QdrantClient(host=os.environ.get("QDRANT_HOST","localhost"), port=int(os.environ.get("QDRANT_PORT","6333")))


def ensure_collection(client, name):
    try:
        client.get_collection(name)
    except Exception:
        client.create_collection(name, vectors_config=VectorParams(size=768, distance=Distance.COSINE, on_disk=True))
        print(f"  → Collection '{name}' oprettet")


def get_points(client, collection, doc_id=None):
    pts, offset = [], None
    while True:
        res = client.scroll(collection_name=collection, limit=100, offset=offset, with_payload=True, with_vectors=True)
        for p in res[0]:
            if doc_id is None or p.payload.get("doc_id") == doc_id:
                pts.append({"id": p.id, "vector": p.vector, "payload": p.payload})
        offset = res[1]
        if offset is None: break
    return pts


def checkbox_menu(pii_list: List[Dict], rules: Dict[str, bool], scope: str) -> Dict[str, bool]:
    """Flueben-menu: bruger vælger y/N per entity INSTANCE (ikke bare per type)."""
    print("\n" + "="*70)
    print(f"  FLUEBEN-GODKENDELSE  →  Target: {scope}")
    print("="*70)
    print("\n  Vælg hvad der skal anonymiseres (y = masker, n = bevar):\n")
    
    custom = {}
    by_type = {}
    for p in pii_list:
        by_type.setdefault(p["type"], []).append(p)
    
    for etype, items in by_type.items():
        default = "y" if rules.get(etype) else "n"
        label = items[0]["label"]
        print(f"\n  --- {label} ({len(items)} fundet) ---")
        for item in items:
            print(f"    '{item['value']}' [{default}] ", end="")
            val = input().strip().lower()
            if not val:
                val = default
            key = f"{etype}:{item['value']}"
            custom[key] = val in ("y", "j", "ja")
    
    # Konvertér tilbage til per-type regler (True hvis NOGET af den type skal maskeres)
    result = {}
    for etype in rules:
        items = by_type.get(etype, [])
        result[etype] = any(custom.get(f"{etype}:{i['value']}", rules[etype]) for i in items)
    
    return result


def final_preview(text: str, rules: Dict[str, bool]) -> str:
    """Viser hvordan teksten ser ud efter anonymisering UDEN at gemme."""
    pii_list = detect_pii(text)
    if not pii_list:
        return text
    
    result = text
    idx_map = {}
    idx = 0
    for p in pii_list:
        key = f"{p['type']}:{p['value']}"
        if not rules.get(p["type"], False):
            continue
        if key not in idx_map:
            idx_map[key] = idx
            idx += 1
        token = generate_token(p["type"], p["value"], idx_map[key])
        result = result.replace(p["value"], token, 1)
    return result


def promote(source_wing, target_scope, doc_id=None):
    print(f"\n{'='*70}")
    print(f"  NOUS PROMOTION: {source_wing} → {target_scope}")
    print(f"{'='*70}\n")
    
    source_coll = WING_COLLECTIONS.get(source_wing)
    target_coll = SCOPE_COLLECTIONS.get(target_scope)
    if not source_coll or not target_coll:
        print("FEJL: Ukendt wing eller scope")
        sys.exit(1)
    
    init_keymap()
    client = get_client()
    ensure_collection(client, target_coll)
    
    print(f"  → Henter fra '{source_coll}' ...")
    points = get_points(client, source_coll, doc_id)
    print(f"  ✓ {len(points)} punkter fundet\n")
    
    if not points:
        print("  Ingen data at promovere.")
        return
    
    # Find et punkt med tekst til preview
    rules = dict(DEFAULT_RULES.get(target_scope, DEFAULT_RULES["PUBLIC"]))
    
    # --- STEP 1: Vis standard-regler og spørg ---
    print("-"*70)
    print("  STANDARD-REGLER for dette scope:")
    print("-"*70)
    for etype, mask in rules.items():
        print(f"    {PII_PATTERNS[etype][1]:12} {'→ MASKERES' if mask else '→ BEVARES'}")
    print("\n  Muligheder:")
    print("    [1] Brug standard (ovenstående)")
    print("    [2] Flueben-godkend per værdi")
    print("    [3] Annuller")
    choice = input("\n  Vælg [1/2/3]: ").strip()
    
    if choice == "3":
        print("\n  Annulleret.")
        return
    elif choice == "2":
        # Tag første punkt med tekst som repræsentativt
        preview_text = next((p["payload"].get("text","") for p in points if p["payload"].get("text","").strip()), "")
        if preview_text:
            pii = detect_pii(preview_text)
            if pii:
                rules = checkbox_menu(pii, rules, target_scope)
            else:
                print("  Ingen PII fundet - bruger standard regler")
        else:
            print("  Ingen tekst at vise - bruger standard regler")
    
    # --- STEP 2: Final preview + eksplicit godkendelse ---
    preview_text = next((p["payload"].get("text","") for p in points if p["payload"].get("text","").strip()), "")
    if preview_text:
        preview_result = final_preview(preview_text, rules)
        print("\n" + "="*70)
        print("  PREVIEW AF ANONYMISERING")
        print("="*70)
        print(f"\n  Original:\n    {preview_text[:200]}{'...' if len(preview_text)>200 else ''}")
        print(f"\n  Efter anonymisering:\n    {preview_result[:200]}{'...' if len(preview_result)>200 else ''}")
    
    print("\n" + "!"*70)
    print("  GODKEND OFFENTLIGGØRELSE")
    print("!"*70)
    print(f"\n  Dette vil kopiere {len(points)} punkt(er) fra")
    print(f"  '{source_wing}' til '{target_scope}' med valgte anonymisering.")
    print(f"\n  Key-map gemmes i: {KEYMAP_DB}")
    print(f"  Du kan resolve tokens tilbage via Legal Engine.")
    print("\n  [G] Godkend og offentliggør")
    print("  [A] Annuller")
    
    godkend = input("\n  Vælg [G/A]: ").strip().lower()
    if godkend not in ("g", "godkend"):
        print("\n  Annulleret - intet er kopieret.")
        return
    
    # --- STEP 3: Udfør promotion ---
    print(f"\n  → Promoverer {len(points)} punkter ...")
    promoted = 0
    for point in points:
        text = point["payload"].get("text", "")
        pid = str(point["id"])
        if text:
            new_text, mappings = anonymize_and_store(text, rules, target_scope, pid)
        else:
            new_text, mappings = text, []
        
        new_payload = dict(point["payload"])
        new_payload["text"] = new_text
        new_payload["promoted_from"] = source_wing
        new_payload["promoted_at"] = datetime.now(datetime.timezone.utc).isoformat()
        new_payload["pii_mappings"] = mappings
        new_payload["anonymization_applied"] = {k:v for k,v in rules.items() if v}
        
        client.upsert(collection_name=target_coll, points=[
            PointStruct(id=point["id"], vector=point["vector"], payload=new_payload)
        ])
        promoted += 1
    
    print(f"\n{'='*70}")
    print(f"  ✅ PROMOTION FULDFØRT")
    print(f"{'='*70}")
    print(f"  {promoted} punkter kopieret til '{target_coll}'")
    print(f"  Key-map: {KEYMAP_DB}")
    print(f"{'='*70}\n")


def anonymize_and_store(text, rules, scope, doc_id):
    pii = detect_pii(text)
    if not pii:
        return text, []
    result = text
    mappings = []
    for i, p in enumerate(pii):
        if not rules.get(p["type"], False):
            continue
        token = generate_token(p["type"], p["value"], i)
        result = result.replace(p["value"], token, 1)
        store_mapping(token, p["value"], p["type"], scope, doc_id)
        mappings.append({"token": token, "original": p["value"], "type": p["type"], "label": p["label"]})
    return result, mappings


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Brug: python3 {sys.argv[0]} <source_wing> <target_scope> [doc_id]")
        print(f"Wings: {', '.join(WING_COLLECTIONS.keys())}")
        print(f"Scopes: {', '.join(SCOPE_COLLECTIONS.keys())}")
        sys.exit(1)
    promote(sys.argv[1], sys.argv[2].upper(), sys.argv[3] if len(sys.argv) > 3 else None)
