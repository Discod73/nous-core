"""
NOUS Gmail Connector — Read-only adgang til Gmail via Google API.

Konfiguration i /srv/nous/config/integrations.json:
  {
    "gmail": {
      "enabled": false,
      "wing": "dans_profil",
      "scope": "PRIVATE",
      "label_filter": "",       <- Gmail label-ID (tom = INBOX)
      "search_query": "",       <- Gmail søgning (tom = alt)
      "max_messages": 100,      <- Max emails pr. sync
      "credentials_file": "/srv/nous/config/gmail_credentials.json",
      "token_file": "/srv/nous/config/gmail_token.json",
      "state_file": "/srv/nous/config/gmail_state.json"
    }
  }

Sikkerhedsprincipper:
- Kun aktivt ved enabled=true (eksplicit opt-in)
- Kun gmail.readonly scope — ingen skrive- eller sletteoperationer mod Gmail
- Alle ingest-handlinger logges via audit_log
- Token og credentials gemmes kun lokalt på Pi
- State-fil tracker processerede message-IDs (dedup, max 10.000)
"""

import base64
import html as html_lib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_CONFIG_FILE = Path("/srv/nous/config/integrations.json")
_INCOMING_DIR = Path("/home/nous/incoming")
_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_VALID_SCOPES = frozenset({"SECRET", "PRIVATE", "SWARM", "PUBLIC"})
_MAX_STATE_IDS = 10_000

_DEFAULT_GMAIL_CONFIG: dict = {
    "enabled": False,
    "wing": "dans_profil",
    "scope": "PRIVATE",
    "label_filter": "",
    "search_query": "",
    "max_messages": 100,
    "credentials_file": "/srv/nous/config/gmail_credentials.json",
    "token_file": "/srv/nous/config/gmail_token.json",
    "state_file": "/srv/nous/config/gmail_state.json",
}

_lock = threading.Lock()


def _load_config() -> dict:
    try:
        raw = _CONFIG_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        gmail = data.get("gmail", {})
        defaults = _DEFAULT_GMAIL_CONFIG.copy()
        defaults.update(gmail)
        data["gmail"] = defaults
        return data
    except FileNotFoundError:
        return {"gmail": _DEFAULT_GMAIL_CONFIG.copy()}
    except Exception:
        return {"gmail": _DEFAULT_GMAIL_CONFIG.copy()}


def _save_config(data: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_CONFIG_FILE)


def _strip_html(text: str) -> str:
    text = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_body(payload: dict) -> str:
    """Rekursiv udtræk af tekst fra Gmail message payload."""
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")

    if mime == "text/html" and body_data:
        raw_html = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        return _strip_html(raw_html)

    if mime.startswith("multipart/"):
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                result = _extract_body(part)
                if result.strip():
                    return result
        for part in parts:
            if part.get("mimeType") == "text/html":
                result = _extract_body(part)
                if result.strip():
                    return result
        for part in parts:
            result = _extract_body(part)
            if result.strip():
                return result

    return ""


def _get_header(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _safe_filename(msg_id: str, subject: str, date_str: str) -> str:
    safe = re.sub(r"[^\w\-æøåÆØÅ ]", "", subject)[:50].strip().replace(" ", "_")
    date_part = date_str[:10] if len(date_str) >= 10 else "ukendt"
    return f"gmail_{date_part}_{msg_id[:8]}_{safe or 'ingen_emne'}.txt"


class GmailConnector:
    """Read-only connector til Gmail via Google API."""

    def get_config(self) -> dict:
        return _load_config().get("gmail", _DEFAULT_GMAIL_CONFIG.copy())

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
            gmail = data.setdefault("gmail", _DEFAULT_GMAIL_CONFIG.copy())
            gmail.update(patch)
            _save_config(data)
            return gmail.copy()

    def is_authenticated(self) -> bool:
        cfg = self.get_config()
        token_file = Path(cfg.get("token_file") or _DEFAULT_GMAIL_CONFIG["token_file"])
        if not token_file.exists():
            return False
        try:
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)
            # Har refresh_token → kan forny; expired uden refresh_token → ikke brugbar
            return creds is not None and (not creds.expired or bool(creds.refresh_token))
        except Exception:
            return False

    def _get_credentials(self):
        cfg = self.get_config()
        token_file = Path(cfg.get("token_file") or _DEFAULT_GMAIL_CONFIG["token_file"])
        if not token_file.exists():
            raise FileNotFoundError(
                f"Gmail token ikke fundet: {token_file}. "
                "Kør 'python3 /srv/nous/scripts/gmail_auth.py' for at autentificere."
            )
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _build_service(self):
        from googleapiclient.discovery import build
        creds = self._get_credentials()
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _load_state(self) -> set:
        cfg = self.get_config()
        state_file = Path(cfg.get("state_file") or _DEFAULT_GMAIL_CONFIG["state_file"])
        if not state_file.exists():
            return set()
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return set(data.get("processed_ids", []))
        except Exception:
            return set()

    def _save_state(self, processed_ids: set) -> None:
        cfg = self.get_config()
        state_file = Path(cfg.get("state_file") or _DEFAULT_GMAIL_CONFIG["state_file"])
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

    def _list_new_message_ids(self, service, cfg: dict, processed: set) -> list[str]:
        """Hent liste af nye (ikke-processerede) message-IDs fra Gmail."""
        limit = cfg.get("max_messages", 100)

        query_parts = []
        if cfg.get("label_filter"):
            query_parts.append(f"label:{cfg['label_filter']}")
        if cfg.get("search_query"):
            query_parts.append(cfg["search_query"])
        q = " ".join(query_parts) or None

        list_kwargs: dict = {
            "userId": "me",
            "maxResults": min(limit * 3, 500),
        }
        if q:
            list_kwargs["q"] = q
        if not cfg.get("label_filter"):
            list_kwargs["labelIds"] = ["INBOX"]

        result = service.users().messages().list(**list_kwargs).execute()
        all_msgs = result.get("messages", [])
        return [m["id"] for m in all_msgs if m["id"] not in processed][:limit]

    def fetch_messages(self, max_results: int | None = None) -> list[dict]:
        """
        Preview: hent metadata for nye (ikke-processerede) beskeder.
        Returnerer liste af {id, subject, from, date} — ingen body-hentning.
        """
        cfg = self.get_config()
        if max_results:
            cfg = dict(cfg)
            cfg["max_messages"] = max_results

        service = self._build_service()
        processed = self._load_state()
        new_ids = self._list_new_message_ids(service, cfg, processed)

        messages = []
        for msg_id in new_ids:
            meta = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = meta.get("payload", {}).get("headers", [])
            messages.append(
                {
                    "id": msg_id,
                    "subject": _get_header(headers, "Subject") or "(ingen emne)",
                    "from": _get_header(headers, "From"),
                    "date": _get_header(headers, "Date"),
                }
            )
        return messages

    def sync(self) -> dict:
        """
        Hent nye beskeder og skriv dem som .txt til /home/nous/incoming/<wing>/.
        Den eksisterende ingest-pipeline håndterer embedding og Qdrant-upsert.
        """
        cfg = self.get_config()
        wing = cfg.get("wing", "").strip()
        if not wing:
            raise ValueError("Gmail wing er ikke konfigureret")
        qdrant_scope = cfg.get("scope", "PRIVATE")

        service = self._build_service()
        processed = self._load_state()
        new_ids = self._list_new_message_ids(service, cfg, processed)

        dest_dir = _INCOMING_DIR / wing
        dest_dir.mkdir(parents=True, exist_ok=True)

        written = 0
        skipped = 0
        errors = 0

        for msg_id in new_ids:
            try:
                full = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )
                payload = full.get("payload", {})
                headers = payload.get("headers", [])

                subject = _get_header(headers, "Subject") or "(ingen emne)"
                sender = _get_header(headers, "From")
                date_str = _get_header(headers, "Date")

                body = _extract_body(payload).strip()
                if not body:
                    skipped += 1
                    processed.add(msg_id)
                    continue

                content = (
                    f"Fra: {sender}\n"
                    f"Dato: {date_str}\n"
                    f"Emne: {subject}\n"
                    f"Gmail-ID: {msg_id}\n"
                    f"Hentet: {datetime.now(timezone.utc).isoformat()}\n"
                    f"\n"
                    f"{body}\n"
                )

                dest = dest_dir / _safe_filename(msg_id, subject, date_str)
                dest.write_text(content, encoding="utf-8")
                processed.add(msg_id)
                written += 1

            except Exception:
                errors += 1
                # Ikke tilføjet til processed — forsøges igen ved næste sync

        self._save_state(processed)
        _audit_write(
            wing,
            qdrant_scope,
            f"Gmail sync: {written} nye, {skipped} tomme, {errors} fejl",
        )

        return {
            "synced": written,
            "skipped_empty": skipped,
            "errors": errors,
            "wing": wing,
            "scope": qdrant_scope,
            "total_found": len(new_ids),
        }

    def status(self) -> dict:
        cfg = self.get_config()
        state_file = Path(cfg.get("state_file") or _DEFAULT_GMAIL_CONFIG["state_file"])
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
            "enabled": self.is_enabled(),
            "authenticated": self.is_authenticated(),
            "processed_message_count": processed_count,
            "last_sync": last_sync,
            "config": cfg,
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
