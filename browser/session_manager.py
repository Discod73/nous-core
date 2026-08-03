"""
Browser-agent: session manager.

Handles:
- Session lifecycle (create, expire, destroy)
- Optional encrypted persistence of Playwright storage_state

Security model:
  By default sessions are EPHEMERAL (in-memory only).
  Encrypted persistence is opt-in via BROWSER_SESSION_KEY env var.
  Key is AES-128 via Fernet (symmetric, HMAC-verified).
  The key must be a 32-byte URL-safe base64 string (Fernet format).
  Generate one with: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

  Why Fernet and not raw AES?
  Fernet = AES-128-CBC + PKCS7 + HMAC-SHA256 authenticated.
  This prevents both decryption-without-key AND tampering without key.
  Storage_state contains session cookies; a tampered file could inject
  attacker-controlled cookies — auth integrity matters here.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

_SESSION_TIMEOUT_S = int(os.environ.get("BROWSER_SESSION_TIMEOUT", "1800"))  # 30 min default
_SESSION_DIR       = Path(os.environ.get("BROWSER_SESSION_DIR", "/tmp/nous_browser_sessions"))
_SESSION_KEY_RAW   = os.environ.get("BROWSER_SESSION_KEY", "")

_fernet = None
if _SESSION_KEY_RAW:
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_SESSION_KEY_RAW.encode())
    except Exception as e:
        import sys
        print(f"[session_manager] WARNING: BROWSER_SESSION_KEY invalid ({e}) — falling back to ephemeral sessions", file=sys.stderr)


class Session:
    __slots__ = ("session_id", "created_at", "last_activity", "storage_state", "active")

    def __init__(self) -> None:
        self.session_id    = str(uuid.uuid4())
        self.created_at    = time.time()
        self.last_activity = time.time()
        self.storage_state: dict | None = None
        self.active        = True

    def touch(self) -> None:
        self.last_activity = time.time()

    def is_expired(self) -> bool:
        return time.time() - self.last_activity > _SESSION_TIMEOUT_S

    def age_seconds(self) -> float:
        return time.time() - self.last_activity

    def to_status(self) -> dict:
        return {
            "session_id":    self.session_id,
            "active":        self.active,
            "created_at":    self.created_at,
            "last_activity": self.last_activity,
            "idle_seconds":  round(self.age_seconds(), 1),
            "timeout_in":    max(0, round(_SESSION_TIMEOUT_S - self.age_seconds(), 1)),
            "persistent":    _fernet is not None,
        }


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        self._expire_old()
        s = Session()
        self._sessions[s.session_id] = s
        return s

    def get(self, session_id: str) -> Session | None:
        self._expire_old()
        s = self._sessions.get(session_id)
        if s is None or not s.active:
            return None
        s.touch()
        return s

    def destroy(self, session_id: str) -> bool:
        s = self._sessions.pop(session_id, None)
        if s:
            s.active = False
            _delete_session_file(session_id)
            return True
        return False

    def list_active(self) -> list[dict]:
        self._expire_old()
        return [s.to_status() for s in self._sessions.values() if s.active]

    def _expire_old(self) -> None:
        expired = [sid for sid, s in self._sessions.items() if s.is_expired()]
        for sid in expired:
            s = self._sessions.pop(sid)
            s.active = False
            _delete_session_file(sid)

    # ── Persistent storage_state (opt-in, encrypted) ──────────────────────────

    def save_storage_state(self, session_id: str, state: dict) -> None:
        """Persist Playwright storage_state (cookies, localStorage) encrypted."""
        if _fernet is None:
            return  # ephemeral: never write to disk
        _SESSION_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        raw   = json.dumps(state).encode()
        token = _fernet.encrypt(raw)
        path  = _session_path(session_id)
        path.write_bytes(token)
        path.chmod(0o600)

    def load_storage_state(self, session_id: str) -> dict | None:
        """Load encrypted storage_state from disk."""
        if _fernet is None:
            return None
        path = _session_path(session_id)
        if not path.exists():
            return None
        try:
            token = path.read_bytes()
            raw   = _fernet.decrypt(token)
            return json.loads(raw)
        except Exception:
            return None


def _session_path(session_id: str) -> Path:
    # sanitize: session_id is UUID4 so alphanumeric + hyphens only
    safe = "".join(c for c in session_id if c.isalnum() or c == "-")
    return _SESSION_DIR / f"{safe}.enc"


def _delete_session_file(session_id: str) -> None:
    try:
        _session_path(session_id).unlink(missing_ok=True)
    except Exception:
        pass


# Module-level singleton used by browser_server.py
manager = SessionManager()
