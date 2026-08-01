"""
NOUS Hotmail/Outlook Connector — Read-only adgang til Hotmail/Outlook via Microsoft Graph API.

Konfiguration i /srv/nous/config/integrations.json:
  {
    "hotmail": {
      "enabled": false,
      "wing": "dans_profil",
      "scope": "PRIVATE",
      "max_messages": 100,
      "credentials_file": "/srv/nous/config/hotmail_credentials.json",
      "token_file": "/srv/nous/config/hotmail_token.json",
      "state_file": "/srv/nous/config/hotmail_state.json"
    }
  }

Sikkerhedsprincipper:
- Kun aktivt ved enabled=true (eksplicit opt-in)
- Kun Mail.Read scope — ingen skrivning, sletning eller afsendelse
- Alle ingest-handlinger logges via audit_log
- Token og credentials gemmes kun lokalt på Pi (chmod 600)
- State-fil tracker processerede message-IDs (dedup, max 10.000)
"""

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_CONFIG_FILE   = Path("/srv/nous/config/integrations.json")
_INCOMING_DIR  = Path("/home/nous/incoming")
_GRAPH_BASE    = "https://graph.microsoft.com/v1.0"
_AUTHORITY     = "https://login.microsoftonline.com/consumers"
_SCOPES        = ["Mail.Read", "User.Read", "offline_access"]
_VALID_SCOPES  = frozenset({"SECRET", "PRIVATE", "SWARM", "PUBLIC"})
_MAX_STATE_IDS = 10_000

_DEFAULT_CONFIG: dict = {
    "enabled":          False,
    "wing":             "dans_profil",
    "scope":            "PRIVATE",
    "max_messages":     100,
    "credentials_file": "/srv/nous/config/hotmail_credentials.json",
    "token_file":       "/srv/nous/config/hotmail_token.json",
    "state_file":       "/srv/nous/config/hotmail_state.json",
}

_lock = threading.Lock()


def _load_config() -> dict:
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        cfg = _DEFAULT_CONFIG.copy()
        cfg.update(data.get("hotmail", {}))
        data["hotmail"] = cfg
        return data
    except FileNotFoundError:
        return {"hotmail": _DEFAULT_CONFIG.copy()}
    except Exception:
        return {"hotmail": _DEFAULT_CONFIG.copy()}


def _save_config(data: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_CONFIG_FILE)


def _safe_filename(msg_id: str, subject: str, date_str: str) -> str:
    safe = re.sub(r"[^\w\-æøåÆØÅ ]", "", subject)[:50].strip().replace(" ", "_")
    date_part = date_str[:10] if len(date_str) >= 10 else "ukendt"
    return f"hotmail_{date_part}_{msg_id[:8]}_{safe or 'ingen_emne'}.txt"


class HotmailConnector:
    """Read-only connector til Hotmail/Outlook via Microsoft Graph API."""

    def get_config(self) -> dict:
        return _load_config().get("hotmail", _DEFAULT_CONFIG.copy())

    def is_enabled(self) -> bool:
        return bool(self.get_config().get("enabled", False))

    def update_config(self, patch: dict) -> dict:
        if "scope" in patch and patch["scope"] not in _VALID_SCOPES:
            raise ValueError(f"Ugyldig scope: {patch['scope']!r}")
        if "max_messages" in patch:
            v = int(patch["max_messages"])
            if not (1 <= v <= 1000):
                raise ValueError("max_messages skal være 1–1000")
            patch["max_messages"] = v
        with _lock:
            data = _load_config()
            cfg = data.setdefault("hotmail", _DEFAULT_CONFIG.copy())
            cfg.update(patch)
            _save_config(data)
            return cfg.copy()

    def _creds_path(self) -> Path:
        return Path(self.get_config().get("credentials_file") or _DEFAULT_CONFIG["credentials_file"])

    def _token_path(self) -> Path:
        return Path(self.get_config().get("token_file") or _DEFAULT_CONFIG["token_file"])

    def _load_msal_app(self):
        """Opret MSAL ConfidentialClientApplication med token-cache."""
        import msal
        creds_path = self._creds_path()
        if not creds_path.exists():
            raise FileNotFoundError(f"Hotmail credentials ikke fundet: {creds_path}")
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        cache = msal.SerializableTokenCache()
        token_path = self._token_path()
        if token_path.exists():
            cache.deserialize(token_path.read_text(encoding="utf-8"))
        app = msal.ConfidentialClientApplication(
            creds["client_id"],
            client_credential=creds["client_secret"],
            authority=_AUTHORITY,
            token_cache=cache,
        )
        return app, cache

    def _save_cache(self, cache) -> None:
        token_path = self._token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(cache.serialize(), encoding="utf-8")
        token_path.chmod(0o600)

    def is_authenticated(self) -> bool:
        token_path = self._token_path()
        creds_path = self._creds_path()
        if not token_path.exists() or not creds_path.exists():
            return False
        try:
            app, cache = self._load_msal_app()
            accounts = app.get_accounts()
            if not accounts:
                return False
            result = app.acquire_token_silent(_SCOPES, account=accounts[0])
            return result is not None and "access_token" in result
        except Exception:
            return False

    def _get_access_token(self) -> str:
        app, cache = self._load_msal_app()
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(_SCOPES, account=accounts[0])
            if result and "access_token" in result:
                if cache.has_state_changed:
                    self._save_cache(cache)
                return result["access_token"]
        raise PermissionError(
            "Hotmail token ikke gyldigt — brug Cockpit wizard til at forbinde igen"
        )

    def _graph_get(self, token: str, path: str, params: dict | None = None) -> dict:
        import urllib.request, urllib.parse
        url = f"{_GRAPH_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def fetch_messages(self, max_results: int | None = None) -> list[dict]:
        """
        Preview: metadata for nye (ikke-processerede) beskeder.
        Returnerer liste af {id, subject, from, date}.
        """
        cfg = self.get_config()
        limit = max_results or cfg.get("max_messages", 100)
        token = self._get_access_token()
        processed = self._load_state()
        data = self._graph_get(token, "/me/messages", {
            "$select": "id,subject,from,receivedDateTime",
            "$top":    min(limit * 3, 500),
            "$filter": "isDraft eq false",
        })
        messages = []
        for msg in data.get("value", []):
            if msg["id"] not in processed and len(messages) < limit:
                messages.append({
                    "id":      msg["id"],
                    "subject": msg.get("subject") or "(ingen emne)",
                    "from":    (msg.get("from") or {}).get("emailAddress", {}).get("address", ""),
                    "date":    msg.get("receivedDateTime", ""),
                })
        return messages

    def _load_state(self) -> set:
        cfg = self.get_config()
        state_file = Path(cfg.get("state_file") or _DEFAULT_CONFIG["state_file"])
        if not state_file.exists():
            return set()
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return set(data.get("processed_ids", []))
        except Exception:
            return set()

    def _save_state(self, processed_ids: set) -> None:
        cfg = self.get_config()
        state_file = Path(cfg.get("state_file") or _DEFAULT_CONFIG["state_file"])
        ids_list = list(processed_ids)[-_MAX_STATE_IDS:]
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"processed_ids": ids_list, "updated": datetime.now(timezone.utc).isoformat()},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(state_file)

    def sync(self) -> dict:
        """
        Hent nye beskeder og skriv dem som .txt til /home/nous/incoming/<wing>/.
        Den eksisterende ingest-pipeline håndterer embedding og Qdrant-upsert.
        """
        cfg = self.get_config()
        wing = cfg.get("wing", "").strip()
        if not wing:
            raise ValueError("Hotmail wing er ikke konfigureret")
        qdrant_scope = cfg.get("scope", "PRIVATE")
        limit = cfg.get("max_messages", 100)

        token = self._get_access_token()
        processed = self._load_state()

        data = self._graph_get(token, "/me/messages", {
            "$select": "id,subject,from,receivedDateTime,body",
            "$top":    min(limit * 3, 500),
            "$filter": "isDraft eq false",
        })

        dest_dir = _INCOMING_DIR / wing
        dest_dir.mkdir(parents=True, exist_ok=True)

        written = 0
        skipped = 0
        errors  = 0

        for msg in data.get("value", []):
            if written >= limit:
                break
            msg_id = msg.get("id", "")
            if msg_id in processed:
                continue
            try:
                subject  = msg.get("subject") or "(ingen emne)"
                sender   = (msg.get("from") or {}).get("emailAddress", {}).get("address", "")
                date_str = msg.get("receivedDateTime", "")
                body_obj = msg.get("body") or {}
                body_text = body_obj.get("content", "").strip()

                # Strip basic HTML if content is HTML
                if body_obj.get("contentType") == "html":
                    body_text = re.sub(r"<[^>]+>", " ", body_text)
                    body_text = re.sub(r"\s{2,}", " ", body_text).strip()

                if not body_text:
                    skipped += 1
                    processed.add(msg_id)
                    continue

                content = (
                    f"Fra: {sender}\n"
                    f"Dato: {date_str}\n"
                    f"Emne: {subject}\n"
                    f"Hotmail-ID: {msg_id}\n"
                    f"Hentet: {datetime.now(timezone.utc).isoformat()}\n"
                    f"\n"
                    f"{body_text}\n"
                )
                dest = dest_dir / _safe_filename(msg_id, subject, date_str)
                dest.write_text(content, encoding="utf-8")
                processed.add(msg_id)
                written += 1
            except Exception:
                errors += 1

        self._save_state(processed)
        _audit_write(
            wing,
            qdrant_scope,
            f"Hotmail sync: {written} nye, {skipped} tomme, {errors} fejl",
        )
        return {
            "synced":        written,
            "skipped_empty": skipped,
            "errors":        errors,
            "wing":          wing,
            "scope":         qdrant_scope,
            "total_found":   len(data.get("value", [])),
        }

    def status(self) -> dict:
        cfg = self.get_config()
        state_file = Path(cfg.get("state_file") or _DEFAULT_CONFIG["state_file"])
        processed_count = 0
        last_sync = None
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                processed_count = len(data.get("processed_ids", []))
                last_sync = data.get("updated")
            except Exception:
                pass
        return {
            "enabled":                 self.is_enabled(),
            "authenticated":           self.is_authenticated(),
            "processed_message_count": processed_count,
            "last_sync":               last_sync,
            "config":                  cfg,
        }


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
