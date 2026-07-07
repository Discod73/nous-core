#!/usr/bin/env python3
"""
NOUS Privacy Guard
==================
PII-detection og konfigurerbar anonymisering med key-map database.

Princip:
  - Rå data bevares i PRIVATE/SECRET wings (ingen anonymisering ved lokalt arbejde)
  - Anonymisering sker FØRST ved promotion til SWARM/PUBLIC
  - Key-map gemmer token→original mapping så Legal Engine kan resolve tilbage
  - Bruger kan til/fravælge hvilke PII-typer der anonymiseres per promotion

Eksempel (offentlig person):
  Malue Montclairre Bruun → navn bevares i PUBLIC (PERSON=False i DEFAULT_RULES)
  CPR/adresser/telefon → anonymiseres i PUBLIC

Brug:
    from privacy_guard import anonymize, detect_pii, DEFAULT_RULES, store_mapping, get_original
"""
import os
import re
import sqlite3
import hashlib
from typing import Dict, List, Optional, Tuple
from datetime import datetime

# ---------------------------------------------------------------------------
# PII Patterns — dansk + generel
# ---------------------------------------------------------------------------
PII_PATTERNS = {
    "DK_CPR":     (r"\b\d{6}[-\s]?\d{4}\b",                              "CPR-nummer"),
    "DK_PHONE":   (r"\b(?:\+45\s?)?(?:\d{2}\s?){3}\d{2}\b",              "Telefonnummer"),
    "DK_ADDRESS": (r"\b[A-ZÆØÅ][a-zæøå]+(?:vej|gade|plads|allé|boulevard|stræde)\s+\d+[A-Z]?", "Adresse"),
    "EMAIL":      (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "Email"),
    "PERSON":     (r"\b[A-ZÆØÅ][a-zæøå]+\s+[A-ZÆØÅ][a-zæøå]+(?:\s+[A-ZÆØÅ][a-zæøå]+)?\b", "Navn"),
}

# ---------------------------------------------------------------------------
# DEFAULT_RULES: Anonymiseringsregler per scope
# 
# Princip: Kun ved promotion til offentlige wings sker anonymisering.
# SECRET/PRIVATE: Alt bevares (lokalt arbejde, intet maskeres)
# SWARM:         Alt anonymiseres (følsomt, men delt inden for cirklen)
# PUBLIC:        CPR/tlf/adresse/email anonymiseres. Navne BEVARES.
#                Offentlige personer som Malue Montclairre Bruun skal
#                kunne identificeres i PUBLIC wings.
# ---------------------------------------------------------------------------
DEFAULT_RULES = {
    "SECRET":  {"DK_CPR": False, "DK_PHONE": False, "DK_ADDRESS": False, "EMAIL": False, "PERSON": False},
    "PRIVATE": {"DK_CPR": False, "DK_PHONE": False, "DK_ADDRESS": False, "EMAIL": False, "PERSON": False},
    "SWARM":   {"DK_CPR": True,  "DK_PHONE": True,  "DK_ADDRESS": True,  "EMAIL": True,  "PERSON": True},
    "PUBLIC":  {"DK_CPR": True,  "DK_PHONE": True,  "DK_ADDRESS": True,  "EMAIL": True,  "PERSON": False},
}

# ---------------------------------------------------------------------------
# Key-map Database
# ---------------------------------------------------------------------------
KEYMAP_DB = "/mnt/nous-data/keymap.db"


def init_keymap(db_path: str = KEYMAP_DB):
    """Initialiserer keymap SQLite database."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keymap (
            token       TEXT PRIMARY KEY,
            original    TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            scope       TEXT NOT NULL,
            doc_id      TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_keymap_doc ON keymap(doc_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_keymap_scope ON keymap(scope)")
    conn.commit()
    conn.close()


def store_mapping(token: str, original: str, entity_type: str, scope: str, doc_id: str, db_path: str = KEYMAP_DB):
    """Gemmer token→original mapping."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO keymap (token, original, entity_type, scope, doc_id) VALUES (?,?,?,?,?)",
        (token, original, entity_type, scope, doc_id)
    )
    conn.commit()
    conn.close()


def get_original(token: str, db_path: str = KEYMAP_DB) -> Optional[Tuple[str, str]]:
    """Henter original tekst og entity type for et token."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT original, entity_type FROM keymap WHERE token=?", (token,)
    ).fetchone()
    conn.close()
    return row if row else None


def resolve_text(anonymized_text: str, db_path: str = KEYMAP_DB) -> str:
    """Erstatter alle tokens i teksten med deres originale værdier."""
    # Find alle tokens: <ENTITYTYPE_HASH_INDEX>
    token_pattern = r"<([A-Z_]+)_([0-9a-f]+)_\d+>"
    
    def replace_token(match):
        token = match.group(0)
        result = get_original(token, db_path)
        if result:
            return result[0]  # original tekst
        return token  # bevar token hvis ikke fundet
    
    return re.sub(token_pattern, replace_token, anonymized_text)


def get_mappings_for_doc(doc_id: str, db_path: str = KEYMAP_DB) -> List[Dict]:
    """Henter alle mappings for et dokument."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT token, original, entity_type, scope, created_at FROM keymap WHERE doc_id=?",
        (doc_id,)
    ).fetchall()
    conn.close()
    return [
        {"token": r[0], "original": r[1], "entity_type": r[2], "scope": r[3], "created_at": r[4]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# PII Detection
# ---------------------------------------------------------------------------
def detect_pii(text: str) -> List[Dict]:
    """Finder PII i tekst med regex patterns."""
    found = []
    seen_spans = set()
    
    for entity_type, (pattern, label) in PII_PATTERNS.items():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            span = match.span()
            if span not in seen_spans:
                seen_spans.add(span)
                found.append({
                    "type": entity_type,
                    "label": label,
                    "value": match.group(),
                    "start": span[0],
                    "end": span[1],
                })
    
    found.sort(key=lambda x: x["start"])
    return found


def has_pii(text: str) -> bool:
    """Hurtig check om tekst indeholder PII."""
    return len(detect_pii(text)) > 0


# ---------------------------------------------------------------------------
# Anonymisering
# ---------------------------------------------------------------------------
def generate_token(entity_type: str, value: str, index: int) -> str:
    """Genererer et deterministisk token."""
    short_hash = hashlib.md5(value.encode()).hexdigest()[:4]
    return f"<{entity_type}_{short_hash}_{index}>"


def anonymize(text: str, scope: str = "PUBLIC", custom_rules: Optional[Dict[str, bool]] = None,
              doc_id: str = "", db_path: str = KEYMAP_DB) -> Tuple[str, List[Dict]]:
    """
    Anonymiserer tekst baseret på scope-regler.
    
    Args:
        text: Original tekst
        scope: Target scope (SECRET/PRIVATE/SWARM/PUBLIC)
        custom_rules: Optional overstyring af DEFAULT_RULES
        doc_id: Dokument ID til keymap
        db_path: Sti til keymap database
    
    Returns:
        (anonymiseret_tekst, liste_af_mappings)
    
    Eksempel:
        >>> text = "Malue Montclairre Bruun, 010203-1234, mal@email.dk"
        >>> result, maps = anonymize(text, scope="PUBLIC")
        >>> # CPR og email anonymiseres, navn bevares
    """
    init_keymap(db_path)
    
    # Bestem regler
    rules = custom_rules if custom_rules is not None else DEFAULT_RULES.get(scope, DEFAULT_RULES["PUBLIC"])
    
    pii_list = detect_pii(text)
    if not pii_list:
        return text, []
    
    # Byg replacements
    replacements = []
    mappings = []
    
    for i, pii in enumerate(pii_list):
        entity_type = pii["type"]
        value = pii["value"]
        
        if not rules.get(entity_type, False):
            continue
        
        token = generate_token(entity_type, value, i)
        replacements.append((value, token, entity_type))
        mappings.append({
            "token": token,
            "original": value,
            "type": entity_type,
            "label": pii["label"],
        })
    
    # Udfør replacements
    result = text
    for value, token, entity_type in replacements:
        result = result.replace(value, token, 1)
        store_mapping(token, value, entity_type, scope, doc_id, db_path)
    
    return result, mappings


def anonymize_with_preview(text: str, scope: str = "PUBLIC") -> Tuple[str, List[Dict], List[Dict]]:
    """
    Anonymiserer og returnerer også hvad der BEVARES (ikke-anonymiseret).
    
    Returns:
        (anonymiseret_tekst, anonymiseret_liste, bevaret_liste)
    """
    rules = DEFAULT_RULES.get(scope, DEFAULT_RULES["PUBLIC"])
    pii_list = detect_pii(text)
    
    anonymized_list = []
    preserved_list = []
    
    for pii in pii_list:
        if rules.get(pii["type"], False):
            anonymized_list.append(pii)
        else:
            preserved_list.append(pii)
    
    result, mappings = anonymize(text, scope=scope)
    return result, mappings, preserved_list


# ---------------------------------------------------------------------------
# Validation / Audit
# ---------------------------------------------------------------------------
def audit_document(text: str, scope: str = "PUBLIC") -> Dict:
    """Returnerer en audit-rapport for et dokument."""
    pii_list = detect_pii(text)
    rules = DEFAULT_RULES.get(scope, DEFAULT_RULES["PUBLIC"])
    
    by_type = {}
    for pii in pii_list:
        by_type.setdefault(pii["type"], {"count": 0, "label": pii["label"], "examples": [], "will_mask": rules.get(pii["type"], False)})
        by_type[pii["type"]]["count"] += 1
        if len(by_type[pii["type"]]["examples"]) < 3:
            by_type[pii["type"]]["examples"].append(pii["value"])
    
    return {
        "scope": scope,
        "total_pii": len(pii_list),
        "by_type": by_type,
        "will_be_masked": sum(1 for p in pii_list if rules.get(p["type"], False)),
        "will_be_preserved": sum(1 for p in pii_list if not rules.get(p["type"], False)),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import sys
    
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nBrug:")
        print("  python3 privacy_guard.py detect  <tekst>     # Vis PII")
        print("  python3 privacy_guard.py mask    <tekst> <scope>  # Anonymiser")
        print("  python3 privacy_guard.py resolve <tekst>          # Resolve tokens")
        print("  python3 privacy_guard.py audit   <tekst> <scope>  # Audit rapport")
        sys.exit(0)
    
    cmd = sys.argv[1]
    
    if cmd == "detect":
        text = sys.argv[2]
        found = detect_pii(text)
        for f in found:
            print(f"  [{f['type']}] {f['label']}: {f['value']}")
        if not found:
            print("  Ingen PII fundet")
    
    elif cmd == "mask":
        text = sys.argv[2]
        scope = sys.argv[3].upper() if len(sys.argv) > 3 else "PUBLIC"
        result, mappings = anonymize(text, scope=scope)
        print(f"Original: {text}")
        print(f"Resultat: {result}")
        if mappings:
            print(f"\nMappings ({len(mappings)}):")
            for m in mappings:
                print(f"  {m['token']} → {m['original']} ({m['label']})")
    
    elif cmd == "resolve":
        text = sys.argv[2]
        result = resolve_text(text)
        print(f"Tokens resolved: {result}")
    
    elif cmd == "audit":
        text = sys.argv[2]
        scope = sys.argv[3].upper() if len(sys.argv) > 3 else "PUBLIC"
        report = audit_document(text, scope)
        print(f"\n  AUDIT: {scope}")
        print(f"  Total PII: {report['total_pii']}")
        print(f"  Maskeres: {report['will_be_masked']}")
        print(f"  Bevares:  {report['will_be_preserved']}")
        for etype, info in report['by_type'].items():
            action = "→ MASKERES" if info['will_mask'] else "→ bevares"
            print(f"\n  [{etype}] {info['label']} ({info['count']}) {action}")
            for ex in info['examples']:
                print(f"      • {ex}")


if __name__ == "__main__":
    main()
