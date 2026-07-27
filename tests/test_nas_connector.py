#!/usr/bin/env python3
"""
Unit tests for integrations/nas_connector.py

Kør: python3 /srv/nous/tests/test_nas_connector.py

Bruger kun temp-directories og mocks — ingen live NAS, Ollama, Qdrant eller Qdrant.
"""
import json
import sys
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

# Tilføj projekt-rod til path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Patch audit_log før import så vi ikke rammer det ægte auditlog
_mock_audit = MagicMock()
sys.modules.setdefault("audit_log", MagicMock(log_event=_mock_audit))

from integrations.nas_connector import NasConnector, _DEFAULT_CONFIG

PASS = "OK"
FAIL = "FEJL"
errors: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS}: {label}")
    else:
        msg = label + (f" — {detail}" if detail else "")
        print(f"  {FAIL}: {msg}")
        errors.append(msg)


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_connector(tmp_dir: Path, config: dict | None = None) -> NasConnector:
    """Opret en NasConnector der bruger tmp_dir som config-dir."""
    cfg_file = tmp_dir / "integrations.json"
    if config is not None:
        cfg_file.write_text(json.dumps(config), encoding="utf-8")
    connector = NasConnector()
    # Patch CONFIG_FILE til temp-stien
    import integrations.nas_connector as mod
    mod._CONFIG_FILE = cfg_file
    return connector


# ── Test 1: Standardkonfiguration ─────────────────────────────────────────────
print("\n[1] Standardkonfiguration")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    import integrations.nas_connector as _mod
    orig_config = _mod._CONFIG_FILE
    _mod._CONFIG_FILE = tmp_path / "integrations.json"  # ingen fil endnu

    conn = NasConnector()
    cfg = conn.get_config()

    check("enabled er False som default", cfg["enabled"] is False)
    check("mount_path er tom string", cfg["mount_path"] == "")
    check("scope er PRIVATE", cfg["scope"] == "PRIVATE")
    check("is_enabled() returnerer False", conn.is_enabled() is False)

    _mod._CONFIG_FILE = orig_config


# ── Test 2: Config-opdatering ──────────────────────────────────────────────────
print("\n[2] Config-opdatering")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    _mod._CONFIG_FILE = tmp_path / "integrations.json"

    conn = NasConnector()
    conn.update_config({"mount_path": "/mnt/nas", "wing": "arkiv", "scope": "PRIVATE"})
    cfg = conn.get_config()

    check("mount_path gemt korrekt", cfg["mount_path"] == "/mnt/nas")
    check("wing gemt korrekt", cfg["wing"] == "arkiv")
    check("scope gemt korrekt", cfg["scope"] == "PRIVATE")
    check("enabled forbliver False", cfg["enabled"] is False)

    conn.update_config({"enabled": True})
    check("enabled kan sættes til True", conn.get_config()["enabled"] is True)
    check("is_enabled() returnerer True", conn.is_enabled() is True)

    _mod._CONFIG_FILE = orig_config


# ── Test 3: Ugyldig scope afvises ──────────────────────────────────────────────
print("\n[3] Ugyldig scope afvises")

with tempfile.TemporaryDirectory() as tmp:
    _mod._CONFIG_FILE = Path(tmp) / "integrations.json"
    conn = NasConnector()
    try:
        conn.update_config({"scope": "UGYLDIG"})
        check("ValueError kastet for ugyldig scope", False, "ingen exception")
    except ValueError as e:
        check("ValueError kastet for ugyldig scope", True)
        check("Fejlbesked nævner scope-værdien", "UGYLDIG" in str(e))
    _mod._CONFIG_FILE = orig_config


# ── Test 4: Ikke-absolut mount_path afvises ────────────────────────────────────
print("\n[4] Relativ mount_path afvises")

with tempfile.TemporaryDirectory() as tmp:
    _mod._CONFIG_FILE = Path(tmp) / "integrations.json"
    conn = NasConnector()
    try:
        conn.update_config({"mount_path": "relativ/sti"})
        check("ValueError kastet for relativ mount_path", False, "ingen exception")
    except ValueError as e:
        check("ValueError kastet for relativ mount_path", True)
    _mod._CONFIG_FILE = orig_config


# ── Test 5: list_directory kræver enabled=True ────────────────────────────────
print("\n[5] list_directory afvises når integration er deaktiveret")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    _mod._CONFIG_FILE = tmp_path / "integrations.json"
    conn = NasConnector()
    # enabled=False — simulér API-lag check (connector kaster ikke selv, men mount_path er tom)
    check("is_enabled() er False", conn.is_enabled() is False)
    try:
        conn.list_directory("")
    except (ValueError, FileNotFoundError) as e:
        check("list_directory fejler med tom mount_path", True)
    _mod._CONFIG_FILE = orig_config


# ── Test 6: list_directory med temp-mappe ──────────────────────────────────────
print("\n[6] list_directory returnerer korrekte entries")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    _mod._CONFIG_FILE = tmp_path / "integrations.json"

    # Opret en fake NAS i en undermappe
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    (nas_root / "dokument.pdf").write_bytes(b"%PDF-1.4 dummy")
    (nas_root / "noter.txt").write_text("hej verden", encoding="utf-8")
    (nas_root / "foto.jpg").write_bytes(b"\xff\xd8\xff dummy")
    (nas_root / "undermappe").mkdir()

    conn = NasConnector()
    conn.update_config({"enabled": True, "mount_path": str(nas_root), "wing": "arkiv", "scope": "PRIVATE"})

    entries = conn.list_directory("")
    names = {e["name"] for e in entries}

    check("dokument.pdf er i listing", "dokument.pdf" in names)
    check("noter.txt er i listing", "noter.txt" in names)
    check("undermappe er i listing", "undermappe" in names)

    pdf_entry = next(e for e in entries if e["name"] == "dokument.pdf")
    check("dokument.pdf er importable", pdf_entry["importable"] is True)
    check("dokument.pdf type er 'file'", pdf_entry["type"] == "file")

    jpg_entry = next(e for e in entries if e["name"] == "foto.jpg")
    check("foto.jpg er IKKE importable", jpg_entry["importable"] is False)

    dir_entry = next(e for e in entries if e["name"] == "undermappe")
    check("undermappe type er 'dir'", dir_entry["type"] == "dir")
    check("undermappe size er None", dir_entry["size"] is None)

    _mod._CONFIG_FILE = orig_config


# ── Test 7: Directory traversal blokeres ──────────────────────────────────────
print("\n[7] Directory traversal blokeres")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    _mod._CONFIG_FILE = tmp_path / "integrations.json"

    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    secret_dir = tmp_path / "hemmelig"
    secret_dir.mkdir()
    (secret_dir / "hemmelig.txt").write_text("slet ikke se her", encoding="utf-8")

    conn = NasConnector()
    conn.update_config({"enabled": True, "mount_path": str(nas_root), "wing": "test", "scope": "PRIVATE"})

    # Forsøg på ../hemmelig — traversal
    try:
        conn.list_directory("../hemmelig")
        check("Traversal-forsøg blokeret", False, "ingen exception — KRITISK!")
    except (ValueError, FileNotFoundError):
        check("Traversal-forsøg blokeret korrekt", True)

    # Forsøg med absolut sti inden for rel_path
    try:
        conn._resolve_safe("../../etc/passwd")
        check("Dyb traversal blokeret", False, "ingen exception — KRITISK!")
    except (ValueError, FileNotFoundError):
        check("Dyb traversal blokeret korrekt", True)

    _mod._CONFIG_FILE = orig_config


# ── Test 8: stage_for_import kopierer til incoming ────────────────────────────
print("\n[8] stage_for_import kopierer fil til incoming-mappe")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    _mod._CONFIG_FILE = tmp_path / "integrations.json"

    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    test_file = nas_root / "rapport.txt"
    test_file.write_text("Testindhold\n", encoding="utf-8")

    incoming = tmp_path / "incoming"
    _mod._INCOMING_DIR = incoming

    conn = NasConnector()
    conn.update_config({
        "enabled": True,
        "mount_path": str(nas_root),
        "wing": "arkiv",
        "scope": "PRIVATE",
    })

    result = conn.stage_for_import("rapport.txt")

    check("Returværdi indeholder wing", result.get("wing") == "arkiv")
    check("Returværdi indeholder scope", result.get("scope") == "PRIVATE")
    check("Returværdi indeholder filename", result.get("filename") == "rapport.txt")
    dest = Path(result["dest"])
    check("Fil er kopieret til incoming", dest.exists())
    check("Kopieret indhold er korrekt", dest.read_text() == "Testindhold\n")

    _mod._CONFIG_FILE = orig_config
    _mod._INCOMING_DIR = Path("/home/nous/incoming")


# ── Test 9: Ikke-importerbar filtype afvises ──────────────────────────────────
print("\n[9] Ikke-importerbar filtype afvises ved import")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    _mod._CONFIG_FILE = tmp_path / "integrations.json"

    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    (nas_root / "billede.jpg").write_bytes(b"\xff\xd8\xff")

    conn = NasConnector()
    conn.update_config({"enabled": True, "mount_path": str(nas_root), "wing": "test", "scope": "PRIVATE"})

    try:
        conn.stage_for_import("billede.jpg")
        check("ValueError for ikke-importerbar filtype", False, "ingen exception")
    except ValueError as e:
        check("ValueError for ikke-importerbar filtype", True)
        check("Fejlbesked nævner filtypen", ".jpg" in str(e))

    _mod._CONFIG_FILE = orig_config


# ── Test 10: Wing-override ved import ────────────────────────────────────────
print("\n[10] Wing-override ved import")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    _mod._CONFIG_FILE = tmp_path / "integrations.json"

    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    (nas_root / "fil.txt").write_text("x", encoding="utf-8")

    incoming = tmp_path / "incoming"
    _mod._INCOMING_DIR = incoming

    conn = NasConnector()
    conn.update_config({"enabled": True, "mount_path": str(nas_root), "wing": "default_wing", "scope": "PRIVATE"})

    result = conn.stage_for_import("fil.txt", wing="override_wing", scope="SWARM")
    check("Wing er overskrevet korrekt", result["wing"] == "override_wing")
    check("Scope er overskrevet korrekt", result["scope"] == "SWARM")

    _mod._CONFIG_FILE = orig_config
    _mod._INCOMING_DIR = Path("/home/nous/incoming")


# ── Test 11: Manglende wing afvises ved import ────────────────────────────────
print("\n[11] Manglende wing afvises ved import")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    _mod._CONFIG_FILE = tmp_path / "integrations.json"

    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    (nas_root / "fil.txt").write_text("x", encoding="utf-8")

    conn = NasConnector()
    conn.update_config({"enabled": True, "mount_path": str(nas_root), "wing": "", "scope": "PRIVATE"})

    try:
        conn.stage_for_import("fil.txt")
        check("ValueError for manglende wing", False, "ingen exception")
    except ValueError as e:
        check("ValueError for manglende wing", True)

    _mod._CONFIG_FILE = orig_config


# ── Test 12: Subfolder browsing ───────────────────────────────────────────────
print("\n[12] Subfolder browsing")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    _mod._CONFIG_FILE = tmp_path / "integrations.json"

    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    sub = nas_root / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("hej", encoding="utf-8")

    conn = NasConnector()
    conn.update_config({"enabled": True, "mount_path": str(nas_root), "wing": "t", "scope": "PRIVATE"})

    top = conn.list_directory("")
    sub_entry = next((e for e in top if e["name"] == "sub"), None)
    check("Subfolder ses i rod-listing", sub_entry is not None)

    sub_entries = conn.list_directory("sub")
    check("Fil i subfolder ses ved subfolder-browse", any(e["name"] == "nested.txt" for e in sub_entries))

    nested = next(e for e in sub_entries if e["name"] == "nested.txt")
    check("rel_path er korrekt for nested fil", nested["rel_path"] == "sub/nested.txt")

    _mod._CONFIG_FILE = orig_config


# ── Resultat ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 50)
if errors:
    print(f"FEJL: {len(errors)} test(s) fejlede:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("Alle tests bestod.")
