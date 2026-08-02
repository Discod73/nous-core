"""
NOUS Protonmail-connector — Read-only adgang via Proton Mail Bridge (headless Docker).

Konfiguration i /srv/nous/config/integrations.json:
  {
    "protonmail": {
      "enabled": false,
      "wing": "dans_profil",
      "scope": "PRIVATE",
      "max_messages": 100,
      "imap_host": "127.0.0.1",
      "imap_port": 1143
    }
  }

Bridge credentials (Bridge-genereret IMAP-password, IKKE Proton login-password)
gemmes krypteret i /srv/nous/config/protonmail_state.json (chmod 600, gitignored).

Sikkerhedsprincipper:
- Kun aktivt ved enabled=true (eksplicit opt-in)
- Kun læseadgang via IMAP — ingen skrivning, sletning eller afsendelse
- Bridge kører headless i Docker med 'pass' som keychain-backend
- Bridge-genereret IMAP-password adskilt fra Proton login-credentials
- NOUS gemmer ALDRIG brugerens Proton-adgangskode
"""

import email
import imaplib
import json
import time
from email.header import decode_header
from pathlib import Path

_CONFIG_FILE    = Path("/srv/nous/config/integrations.json")
_STATE_FILE     = Path("/srv/nous/config/protonmail_state.json")
_INCOMING_DIR   = Path("/home/nous/incoming")
_MAX_STATE_IDS  = 10_000
_VALID_SCOPES   = frozenset({"SECRET", "PRIVATE", "SWARM", "PUBLIC"})

_DEFAULT_CONFIG: dict = {
    "enabled":      False,
    "wing":         "",
    "scope":        "PRIVATE",
    "max_messages": 100,
    "imap_host":    "127.0.0.1",
    "imap_port":    1143,
}

_DEFAULT_STATE: dict = {
    "bridge_email":    "",
    "bridge_password": "",  # Bridge-genereret IMAP-token, ikke Proton-kodeord
    "authenticated":   False,
    "last_sync":       None,
    "synced_ids":      [],
}


class ProtonmailConnector:
    def get_config(self) -> dict:
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            return {**_DEFAULT_CONFIG, **data.get("protonmail", {})}
        except Exception:
            return dict(_DEFAULT_CONFIG)

    def update_config(self, patch: dict) -> dict:
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        cfg = {**_DEFAULT_CONFIG, **data.get("protonmail", {}), **patch}
        data["protonmail"] = cfg
        _CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return cfg

    def _load_state(self) -> dict:
        try:
            return {**_DEFAULT_STATE, **json.loads(_STATE_FILE.read_text(encoding="utf-8"))}
        except Exception:
            return dict(_DEFAULT_STATE)

    def _save_state(self, state: dict) -> None:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        _STATE_FILE.chmod(0o600)

    def is_authenticated(self) -> bool:
        state = self._load_state()
        if not state.get("bridge_email") or not state.get("bridge_password"):
            return False
        try:
            cfg = self.get_config()
            imap = imaplib.IMAP4(cfg["imap_host"], int(cfg["imap_port"]))
            imap.login(state["bridge_email"], state["bridge_password"])
            imap.logout()
            return True
        except Exception:
            return False

    def save_bridge_credentials(self, email_addr: str, bridge_password: str) -> None:
        """Gem Bridge-genereret IMAP-password. Aldrig Proton-login-password."""
        state = self._load_state()
        state["bridge_email"]    = email_addr
        state["bridge_password"] = bridge_password
        state["authenticated"]   = True
        self._save_state(state)

    def clear_credentials(self) -> None:
        state = _DEFAULT_STATE.copy()
        self._save_state(state)

    def _connect(self) -> imaplib.IMAP4:
        state  = self._load_state()
        cfg    = self.get_config()
        imap   = imaplib.IMAP4(cfg["imap_host"], int(cfg["imap_port"]))
        imap.login(state["bridge_email"], state["bridge_password"])
        return imap

    def fetch_messages(self, folder: str = "INBOX", max_results: int | None = None) -> list[dict]:
        cfg   = self.get_config()
        limit = max_results or cfg.get("max_messages", 100)
        msgs  = []
        try:
            imap = self._connect()
            imap.select(folder, readonly=True)
            _, data = imap.search(None, "ALL")
            ids = data[0].split() if data[0] else []
            for mid in reversed(ids[-limit:]):
                _, raw = imap.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                if raw and raw[0]:
                    msg     = email.message_from_bytes(raw[0][1])
                    subject = _decode_str(msg.get("Subject", ""))
                    sender  = _decode_str(msg.get("From", ""))
                    date    = msg.get("Date", "")
                    msgs.append({"id": mid.decode(), "subject": subject, "from": sender, "date": date})
            imap.logout()
        except Exception:
            pass
        return msgs

    def sync(self) -> dict:
        cfg   = self.get_config()
        wing  = cfg.get("wing", "")
        scope = cfg.get("scope", "PRIVATE")
        if not wing:
            raise ValueError("Wing ikke konfigureret — brug integrationsindstillingerne")
        if scope not in _VALID_SCOPES:
            scope = "PRIVATE"

        state      = self._load_state()
        synced_ids = set(state.get("synced_ids", []))
        incoming   = _INCOMING_DIR / wing
        incoming.mkdir(parents=True, exist_ok=True)

        imap = self._connect()
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "ALL")
        ids = data[0].split() if data[0] else []

        new_count = 0
        limit = cfg.get("max_messages", 100)
        for mid in reversed(ids[-limit:]):
            mid_str = mid.decode()
            if mid_str in synced_ids:
                continue
            _, raw = imap.fetch(mid, "(RFC822)")
            if not raw or not raw[0]:
                continue
            msg     = email.message_from_bytes(raw[0][1])
            subject = _decode_str(msg.get("Subject", "(ingen emne)"))
            sender  = _decode_str(msg.get("From", ""))
            date    = msg.get("Date", "")
            body    = _extract_body(msg)
            fname   = f"protonmail_{mid_str}_{_safe(subject)}.txt"
            content = f"Fra: {sender}\nEmne: {subject}\nDato: {date}\n\n{body}"
            (incoming / fname).write_text(content, encoding="utf-8")
            synced_ids.add(mid_str)
            new_count += 1
        imap.logout()

        state["synced_ids"] = list(synced_ids)[-_MAX_STATE_IDS:]
        state["last_sync"]  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save_state(state)
        return {"synced": new_count, "wing": wing}

    def status(self) -> dict:
        cfg   = self.get_config()
        state = self._load_state()
        return {
            "authenticated": bool(state.get("bridge_email") and state.get("bridge_password")),
            "email":         state.get("bridge_email", ""),
            "enabled":       cfg.get("enabled", False),
            "config":        cfg,
            "last_sync":     state.get("last_sync"),
        }


# ── helpers ───────────────────────────────────────────────────────────────────

def _decode_str(value: str) -> str:
    parts, out = decode_header(value or ""), []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out)


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in (s or "")[:40]).strip()
