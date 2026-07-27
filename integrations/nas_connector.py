"""
NOUS NAS Connector — Mount-baseret read-only adgang til netværkslager.

Konfiguration i /srv/nous/config/integrations.json:
  {
    "nas": {
      "enabled": false,        <- ALDRIG true som default
      "mount_path": "",        <- Absolut sti til mount-punkt (SMB/CIFS eller NFS)
      "wing": "",              <- Standard mål-wing ved import
      "scope": "PRIVATE"       <- SECRET | PRIVATE | SWARM | PUBLIC
    }
  }

Sikkerhedsprincipper:
- Kun aktivt ved enabled=true (eksplicit opt-in af brugeren)
- Kun læs (list + read) — ingen skrive- eller sletteoperationer mod NAS
- Alle sti-kald valideres mod mount_path (ingen directory traversal)
- Alle READ-handlinger logges via audit_log
- Import sker via NOUS' eksisterende ingest-pipeline (/home/nous/incoming/)
"""

import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path

_CONFIG_FILE = Path("/srv/nous/config/integrations.json")
_INCOMING_DIR = Path("/home/nous/incoming")

_INGESTABLE_SUFFIXES = frozenset({".pdf", ".docx", ".doc", ".txt"})

_VALID_SCOPES = frozenset({"SECRET", "PRIVATE", "SWARM", "PUBLIC"})

_DEFAULT_CONFIG: dict = {
    "nas": {
        "enabled": False,
        "mount_path": "",
        "wing": "",
        "scope": "PRIVATE",
    }
}

_lock = threading.Lock()


def _load_config() -> dict:
    try:
        raw = _CONFIG_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        nas = data.get("nas", {})
        defaults = _DEFAULT_CONFIG["nas"].copy()
        defaults.update(nas)
        data["nas"] = defaults
        return data
    except FileNotFoundError:
        return {k: v.copy() if isinstance(v, dict) else v
                for k, v in _DEFAULT_CONFIG.items()}
    except Exception:
        return {k: v.copy() if isinstance(v, dict) else v
                for k, v in _DEFAULT_CONFIG.items()}


def _save_config(data: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_CONFIG_FILE)


def _detect_mount_type(mount_path: Path) -> str:
    """Autodetect mount type via /proc/mounts. Returns 'cifs', 'nfs', or 'unknown'."""
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8")
        path_str = str(mount_path.resolve())
        for line in mounts.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == path_str:
                fs = parts[2].lower()
                if "cifs" in fs or "smb" in fs:
                    return "cifs"
                if "nfs" in fs:
                    return "nfs"
                return fs
    except Exception:
        pass
    return "unknown"


class NasConnector:
    """Read-only connector til et OS-monteret netværkslager."""

    def get_config(self) -> dict:
        return _load_config().get("nas", _DEFAULT_CONFIG["nas"].copy())

    def is_enabled(self) -> bool:
        return bool(self.get_config().get("enabled", False))

    def update_config(self, patch: dict) -> dict:
        """Opdater NAS-konfiguration. Returnerer den opdaterede NAS-blok."""
        if "scope" in patch and patch["scope"] not in _VALID_SCOPES:
            raise ValueError(f"Ugyldig scope: {patch['scope']!r}")
        if "mount_path" in patch:
            mp = patch["mount_path"].strip()
            if mp and not Path(mp).is_absolute():
                raise ValueError("mount_path skal være en absolut sti")
            patch["mount_path"] = mp
        with _lock:
            data = _load_config()
            nas = data.setdefault("nas", _DEFAULT_CONFIG["nas"].copy())
            nas.update(patch)
            _save_config(data)
            return nas.copy()

    def _resolve_safe(self, rel_path: str) -> Path:
        """
        Resolver rel_path inden for mount_path og sikrer ingen directory traversal.
        Kaster ValueError ved traversal, FileNotFoundError ved manglende mount_path.
        """
        cfg = self.get_config()
        mount_path_str = cfg.get("mount_path", "").strip()
        if not mount_path_str:
            raise ValueError("NAS mount_path er ikke konfigureret")
        mount = Path(mount_path_str).resolve()
        if not mount.exists():
            raise FileNotFoundError(f"NAS mount-punkt '{mount}' eksisterer ikke")

        # Rens relativ sti: fjern absolut præfix og normaliser
        clean = rel_path.lstrip("/").replace("..", "")
        target = (mount / clean).resolve()

        if not str(target).startswith(str(mount)):
            raise ValueError("Adgang uden for NAS mount-punkt er ikke tilladt")
        return target

    def list_directory(self, rel_path: str = "") -> list[dict]:
        """
        List filer og mapper på NAS-stien.
        Auditlogges som READ (scope fra config).
        """
        target = self._resolve_safe(rel_path)
        if not target.is_dir():
            raise FileNotFoundError(f"Sti '{rel_path}' er ikke en mappe")

        cfg = self.get_config()
        wing = cfg.get("wing") or "nas"
        scope = cfg.get("scope") or "PRIVATE"

        _audit_read(wing, scope, f"NAS browse: {rel_path or '/'}")

        entries = []
        try:
            for item in sorted(target.iterdir()):
                stat = item.stat()
                entries.append({
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size": stat.st_size if item.is_file() else None,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "importable": item.is_file() and item.suffix.lower() in _INGESTABLE_SUFFIXES,
                    "rel_path": (rel_path.rstrip("/") + "/" + item.name).lstrip("/"),
                })
        except PermissionError as e:
            raise PermissionError(f"Ingen adgang til NAS-sti '{rel_path}': {e}")

        return entries

    def stage_for_import(
        self,
        rel_path: str,
        wing: str | None = None,
        scope: str | None = None,
    ) -> dict:
        """
        Kopiér én fil fra NAS til NOUS' ingest-mappe (/home/nous/incoming/<wing>/).
        Den eksisterende ingest-pipeline håndterer embedding og upsert til Qdrant.

        Auditlogges som WRITE.
        Returnerer info-dict med target-sti og filinfo.
        """
        target = self._resolve_safe(rel_path)
        if not target.is_file():
            raise FileNotFoundError(f"'{rel_path}' er ikke en fil på NAS")
        if target.suffix.lower() not in _INGESTABLE_SUFFIXES:
            raise ValueError(
                f"Filtype '{target.suffix}' understøttes ikke. "
                f"Understøttede: {', '.join(sorted(_INGESTABLE_SUFFIXES))}"
            )

        cfg = self.get_config()
        effective_wing = (wing or cfg.get("wing") or "").strip()
        if not effective_wing:
            raise ValueError("Wing er ikke angivet og ikke konfigureret i NAS-indstillinger")
        effective_scope = (scope or cfg.get("scope") or "PRIVATE").strip()
        if effective_scope not in _VALID_SCOPES:
            raise ValueError(f"Ugyldig scope: {effective_scope!r}")

        dest_dir = _INCOMING_DIR / effective_wing
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / target.name

        shutil.copy2(str(target), str(dest))

        _audit_write(effective_wing, effective_scope,
                     f"NAS import: {rel_path} → {effective_wing}/{target.name}")

        return {
            "source": rel_path,
            "dest": str(dest),
            "wing": effective_wing,
            "scope": effective_scope,
            "filename": target.name,
            "size": target.stat().st_size,
        }

    def mount_info(self) -> dict:
        """Returnér info om mount-typen (CIFS/NFS/unknown) og om stien er tilgængelig."""
        cfg = self.get_config()
        mp_str = cfg.get("mount_path", "").strip()
        if not mp_str:
            return {"available": False, "mount_type": "unknown", "mount_path": ""}
        mp = Path(mp_str)
        available = mp.exists() and mp.is_dir()
        mount_type = _detect_mount_type(mp) if available else "unknown"
        return {
            "available": available,
            "mount_type": mount_type,
            "mount_path": mp_str,
        }


def _audit_read(wing: str, scope: str, summary: str) -> None:
    try:
        import sys
        _NOUS = Path(__file__).resolve().parent.parent
        if str(_NOUS) not in sys.path:
            sys.path.insert(0, str(_NOUS))
        from audit_log import log_event
        log_event("READ", wing, scope, None, summary)
    except Exception:
        pass


def _audit_write(wing: str, scope: str, summary: str) -> None:
    try:
        import sys
        _NOUS = Path(__file__).resolve().parent.parent
        if str(_NOUS) not in sys.path:
            sys.path.insert(0, str(_NOUS))
        from audit_log import log_event
        log_event("WRITE", wing, scope, None, summary)
    except Exception:
        pass
