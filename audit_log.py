"""
NOUS Audit Log — append-only SHA256 hash-chain log for SECRET/PRIVATE scope access.

Rules (hardcoded):
- Only SECRET and PRIVATE scope events are logged.
- No DELETE capability, ever.
- Rotation at 100 MB: current file renamed with UTC timestamp, new file started.
  All rotated files are kept permanently.
- Thread-safe via threading.Lock (fast append, negligible contention).
- Caller exceptions are swallowed — audit failure must never crash a request.
"""
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

AUDIT_DIR  = Path("/mnt/nous-data/audit")
AUDIT_FILE = AUDIT_DIR / "secret_access.log"
MAX_BYTES  = 100 * 1024 * 1024  # 100 MB

LOGGED_SCOPES = frozenset({"SECRET", "PRIVATE"})

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_last_hash() -> str:
    """Return entry_hash of the last JSON line in the current log, or '' if none."""
    if not AUDIT_FILE.exists():
        return ""
    try:
        with AUDIT_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return ""
            chunk = min(size, 4096)
            f.seek(-chunk, 2)
            tail = f.read().decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        if not lines:
            return ""
        return json.loads(lines[-1]).get("entry_hash", "")
    except Exception:
        return ""


def _rotate_if_needed() -> None:
    """Rename current log to a timestamped file if it has reached 100 MB."""
    if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size >= MAX_BYTES:
        ts = _now_iso().replace(":", "").replace("+", "Z").replace("-", "")
        AUDIT_FILE.rename(AUDIT_DIR / f"secret_access_{ts}.log")


def log_event(
    event_type: str,   # READ | WRITE | QUERY | SCOPE_VIOLATION
    wing:       str,
    scope:      str,
    user:       str | None,
    summary:    str,
) -> None:
    """Append a hash-chained audit entry. No-op for SWARM/PUBLIC scope."""
    if scope not in LOGGED_SCOPES:
        return
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            _rotate_if_needed()
            prev = _read_last_hash()
            entry: dict = {
                "timestamp":  _now_iso(),
                "event_type": event_type,
                "wing":       wing,
                "scope":      scope,
                "user":       (user or "unknown")[:64],
                "summary":    summary[:100],
                "prev_hash":  prev,
                "entry_hash": "",
            }
            # Hash all fields except entry_hash itself (which is not yet set)
            hashable = json.dumps(
                {k: v for k, v in entry.items() if k != "entry_hash"},
                ensure_ascii=False,
                sort_keys=True,
            )
            entry["entry_hash"] = hashlib.sha256(hashable.encode()).hexdigest()
            with AUDIT_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def verify_chain(path: Path = AUDIT_FILE) -> dict:
    """Read the log and verify every prev_hash pointer. Returns a report dict."""
    if not path.exists():
        return {"ok": True, "entries": 0, "errors": []}

    entries = []
    errors  = []
    prev    = ""

    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: JSON parse error — {exc}")
                continue

            # Recompute entry_hash
            hashable = json.dumps(
                {k: v for k, v in e.items() if k != "entry_hash"},
                ensure_ascii=False,
                sort_keys=True,
            )
            expected = hashlib.sha256(hashable.encode()).hexdigest()
            if e.get("entry_hash") != expected:
                errors.append(f"line {lineno}: entry_hash mismatch (entry may be tampered)")

            # Verify prev_hash pointer
            if e.get("prev_hash") != prev:
                errors.append(
                    f"line {lineno}: prev_hash mismatch — "
                    f"expected {prev!r}, got {e.get('prev_hash')!r}"
                )

            prev = e.get("entry_hash", "")
            entries.append(e)

    return {
        "ok":      len(errors) == 0,
        "entries": len(entries),
        "errors":  errors,
    }
