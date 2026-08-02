"""
NOUS Google Connector — Read-only adgang til Gmail, Kalender og Drive via Google APIs.

Én OAuth-forbindelse, tre tjenester:
  - Gmail (gmail.readonly)
  - Google Kalender (calendar.readonly)
  - Google Drive + Docs (drive.readonly)

Konfiguration i /srv/nous/config/integrations.json:
  {
    "google": {
      "gmail_enabled": false,
      "gmail_wing": "dans_profil",
      "gmail_scope": "PRIVATE",
      "gmail_max_messages": 100,
      "calendar_enabled": false,
      "calendar_wing": "dans_profil",
      "calendar_scope": "PRIVATE",
      "calendar_days_ahead": 30,
      "drive_enabled": false,
      "drive_wing": "dans_profil",
      "drive_scope": "PRIVATE",
      "drive_folder_id": "",
      "email_address": "",
      "credentials_file": "/srv/nous/config/gmail_credentials.json",
      "token_file": "/srv/nous/config/google_token.json",
      "state_file": "/srv/nous/config/google_state.json"
    }
  }

Google Docs hentes via Drive-eksport (text/plain) — intet separat Docs API-kald.
"""

import base64
import html as _html_lib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_CONFIG_FILE   = Path("/srv/nous/config/integrations.json")
_INCOMING_DIR  = Path("/home/nous/incoming")
_VALID_SCOPES  = frozenset({"SECRET", "PRIVATE", "SWARM", "PUBLIC"})
_MAX_STATE_IDS = 10_000

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# MIME types exportable from Google Workspace → text/plain
_GAPPS_TEXT_EXPORT = {
    "application/vnd.google-apps.document":     "text/plain",
    "application/vnd.google-apps.spreadsheet":  "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
# Direct-download MIME types accepted by ingest pipeline
_DIRECT_DOWNLOAD = {"text/plain", "text/html", "text/csv", "text/markdown", "application/pdf"}

_DEFAULT: dict = {
    "gmail_enabled":    False,
    "gmail_wing":       "dans_profil",
    "gmail_scope":      "PRIVATE",
    "gmail_max_messages": 100,
    "calendar_enabled": False,
    "calendar_wing":    "dans_profil",
    "calendar_scope":   "PRIVATE",
    "calendar_days_ahead": 30,
    "drive_enabled":    False,
    "drive_wing":       "dans_profil",
    "drive_scope":      "PRIVATE",
    "drive_folder_id":  "",
    "email_address":    "",
    "credentials_file": "/srv/nous/config/gmail_credentials.json",
    "token_file":       "/srv/nous/config/google_token.json",
    "state_file":       "/srv/nous/config/google_state.json",
}

_lock = threading.Lock()


# ── config helpers ────────────────────────────────────────────────────────────

def _load_all() -> dict:
    try:
        return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_all(data: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_CONFIG_FILE)


def _strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html_lib.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _safe_name(prefix: str, uid: str, label: str, date_str: str) -> str:
    safe  = re.sub(r"[^\w\-æøåÆØÅ ]", "", label)[:50].strip().replace(" ", "_")
    dpart = date_str[:10] if len(date_str) >= 10 else "ukendt"
    return f"{prefix}_{dpart}_{uid[:8]}_{safe or 'ingen_titel'}.txt"


# ── main connector ────────────────────────────────────────────────────────────

class GoogleConnector:
    """Unified read-only connector til Gmail, Kalender og Drive."""

    # ── config ────────────────────────────────────────────────────────────────

    def get_config(self) -> dict:
        data = _load_all()
        cfg  = _DEFAULT.copy()
        cfg.update(data.get("google", {}))
        return cfg

    def update_config(self, patch: dict) -> dict:
        for svc in ("gmail", "calendar", "drive"):
            scope_key = f"{svc}_scope"
            if scope_key in patch and patch[scope_key] not in _VALID_SCOPES:
                raise ValueError(f"Ugyldig scope: {patch[scope_key]!r}")
        for svc in ("gmail",):
            key = f"{svc}_max_messages"
            if key in patch:
                v = int(patch[key])
                if not (1 <= v <= 1000):
                    raise ValueError(f"{key} skal være 1–1000")
                patch[key] = v
        if "calendar_days_ahead" in patch:
            v = int(patch["calendar_days_ahead"])
            if not (1 <= v <= 365):
                raise ValueError("calendar_days_ahead skal være 1–365")
            patch["calendar_days_ahead"] = v
        with _lock:
            data = _load_all()
            cfg  = data.setdefault("google", _DEFAULT.copy())
            cfg.update(patch)
            _save_all(data)
            return cfg.copy()

    def is_any_enabled(self) -> bool:
        cfg = self.get_config()
        return any(cfg.get(f"{s}_enabled") for s in ("gmail", "calendar", "drive"))

    # ── auth ──────────────────────────────────────────────────────────────────

    def _creds_path(self) -> Path:
        return Path(self.get_config().get("credentials_file") or _DEFAULT["credentials_file"])

    def _token_path(self) -> Path:
        return Path(self.get_config().get("token_file") or _DEFAULT["token_file"])

    def is_authenticated(self) -> bool:
        tp = self._token_path()
        cp = self._creds_path()
        if not tp.exists() or not cp.exists():
            return False
        try:
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(str(tp), _SCOPES)
            return creds is not None and (not creds.expired or bool(creds.refresh_token))
        except Exception:
            return False

    def _get_credentials(self):
        tp = self._token_path()
        if not tp.exists():
            raise FileNotFoundError(
                "Google-token ikke fundet. Brug Cockpit-wizarden til at forbinde."
            )
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(tp), _SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            tp.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _build(self, service: str, version: str):
        from googleapiclient.discovery import build
        creds = self._get_credentials()
        return build(service, version, credentials=creds, cache_discovery=False)

    # ── state ─────────────────────────────────────────────────────────────────

    def _state_path(self) -> Path:
        return Path(self.get_config().get("state_file") or _DEFAULT["state_file"])

    def _load_state(self) -> dict:
        sp = self._state_path()
        if not sp.exists():
            return {}
        try:
            return json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        sp = self._state_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        for key in ("gmail_ids", "calendar_ids", "drive_ids"):
            if key in state:
                state[key] = list(state[key])[-_MAX_STATE_IDS:]
        state["updated"] = datetime.now(timezone.utc).isoformat()
        tmp = sp.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(sp)

    # ── Gmail ─────────────────────────────────────────────────────────────────

    def _gmail_get_header(self, headers: list, name: str) -> str:
        name_lower = name.lower()
        for h in headers:
            if h.get("name", "").lower() == name_lower:
                return h.get("value", "")
        return ""

    def _gmail_extract_body(self, payload: dict) -> str:
        mime = payload.get("mimeType", "")
        data = payload.get("body", {}).get("data", "")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        if mime == "text/html" and data:
            raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            return _strip_html(raw)
        if mime.startswith("multipart/"):
            parts = payload.get("parts", [])
            for part in parts:
                if part.get("mimeType") == "text/plain":
                    r = self._gmail_extract_body(part)
                    if r.strip(): return r
            for part in parts:
                if part.get("mimeType") == "text/html":
                    r = self._gmail_extract_body(part)
                    if r.strip(): return r
            for part in parts:
                r = self._gmail_extract_body(part)
                if r.strip(): return r
        return ""

    def gmail_fetch_messages(self, max_results: int | None = None) -> list[dict]:
        """Preview: metadata for nye (ikke-processerede) Gmail-beskeder."""
        cfg   = self.get_config()
        limit = max_results or cfg.get("gmail_max_messages", 100)
        svc   = self._build("gmail", "v1")
        state = self._load_state()
        processed = set(state.get("gmail_ids", []))

        result = svc.users().messages().list(
            userId="me", labelIds=["INBOX"],
            maxResults=min(limit * 3, 500),
        ).execute()
        messages = []
        for m in result.get("messages", []):
            if m["id"] not in processed and len(messages) < limit:
                meta = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                hdrs = meta.get("payload", {}).get("headers", [])
                messages.append({
                    "id":      m["id"],
                    "subject": self._gmail_get_header(hdrs, "Subject") or "(ingen emne)",
                    "from":    self._gmail_get_header(hdrs, "From"),
                    "date":    self._gmail_get_header(hdrs, "Date"),
                })
        return messages

    def gmail_sync(self) -> dict:
        """Hent nye Gmail-beskeder og skriv til ingest-pipeline."""
        cfg   = self.get_config()
        wing  = cfg.get("gmail_wing", "").strip()
        if not wing:
            raise ValueError("Gmail wing er ikke konfigureret")
        limit       = cfg.get("gmail_max_messages", 100)
        qdrant_scope = cfg.get("gmail_scope", "PRIVATE")

        svc   = self._build("gmail", "v1")
        state = self._load_state()
        processed = set(state.get("gmail_ids", []))

        result = svc.users().messages().list(
            userId="me", labelIds=["INBOX"], maxResults=min(limit * 3, 500),
        ).execute()

        dest_dir = _INCOMING_DIR / wing
        dest_dir.mkdir(parents=True, exist_ok=True)
        written = skipped = errors = 0

        for m in result.get("messages", []):
            if written >= limit: break
            if m["id"] in processed: continue
            try:
                full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
                payload = full.get("payload", {})
                hdrs    = payload.get("headers", [])
                subject  = self._gmail_get_header(hdrs, "Subject") or "(ingen emne)"
                sender   = self._gmail_get_header(hdrs, "From")
                date_str = self._gmail_get_header(hdrs, "Date")
                body     = self._gmail_extract_body(payload).strip()
                if not body:
                    skipped += 1; processed.add(m["id"]); continue
                content = (
                    f"Fra: {sender}\nDato: {date_str}\nEmne: {subject}\n"
                    f"Gmail-ID: {m['id']}\nHentet: {datetime.now(timezone.utc).isoformat()}\n\n{body}\n"
                )
                fname = _safe_name("gmail", m["id"], subject, date_str)
                (dest_dir / fname).write_text(content, encoding="utf-8")
                processed.add(m["id"]); written += 1
            except Exception:
                errors += 1

        state["gmail_ids"] = list(processed)
        self._save_state(state)
        _audit_write(wing, qdrant_scope, f"Google/Gmail sync: {written} nye, {skipped} tomme, {errors} fejl")
        return {"synced": written, "skipped_empty": skipped, "errors": errors, "wing": wing, "scope": qdrant_scope}

    # ── Kalender ──────────────────────────────────────────────────────────────

    def calendar_list_events(self, days_ahead: int = 30) -> list[dict]:
        """Preview: kommende kalender-hændelser."""
        svc  = self._build("calendar", "v3")
        now  = datetime.now(timezone.utc)
        end  = now + timedelta(days=days_ahead)
        result = svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        ).execute()
        events = []
        for e in result.get("items", []):
            start = e.get("start", {})
            events.append({
                "id":      e.get("id", ""),
                "title":   e.get("summary", "(ingen titel)"),
                "start":   start.get("dateTime") or start.get("date", ""),
                "location": e.get("location", ""),
            })
        return events

    def calendar_sync(self) -> dict:
        """Hent kalender-hændelser og skriv til ingest-pipeline."""
        cfg  = self.get_config()
        wing = cfg.get("calendar_wing", "").strip()
        if not wing:
            raise ValueError("Kalender wing er ikke konfigureret")
        qdrant_scope = cfg.get("calendar_scope", "PRIVATE")
        days_ahead   = cfg.get("calendar_days_ahead", 30)

        svc  = self._build("calendar", "v3")
        now  = datetime.now(timezone.utc)
        end  = now + timedelta(days=days_ahead)
        result = svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=500,
        ).execute()

        state = self._load_state()
        processed = set(state.get("calendar_ids", []))

        dest_dir = _INCOMING_DIR / wing
        dest_dir.mkdir(parents=True, exist_ok=True)
        written = skipped = 0

        for e in result.get("items", []):
            eid   = e.get("id", "")
            title = e.get("summary", "(ingen titel)")
            start = e.get("start", {})
            start_str = start.get("dateTime") or start.get("date", "")
            end_e = e.get("end", {})
            end_str   = end_e.get("dateTime") or end_e.get("date", "")
            desc  = (e.get("description") or "").strip()
            loc   = e.get("location", "")
            updated = e.get("updated", "")

            # Dedup: skip if seen and not modified
            dedup_key = f"{eid}:{updated}"
            if dedup_key in processed:
                skipped += 1; continue

            body_parts = [
                f"Titel: {title}",
                f"Start: {start_str}",
                f"Slut: {end_str}",
            ]
            if loc:
                body_parts.append(f"Sted: {loc}")
            if desc:
                body_parts.append(f"\nBeskrivelse:\n{desc}")
            body_parts += [
                f"\nGoogle-Event-ID: {eid}",
                f"Hentet: {datetime.now(timezone.utc).isoformat()}",
            ]
            content = "\n".join(body_parts) + "\n"
            fname = _safe_name("kalender", eid, title, start_str)
            (dest_dir / fname).write_text(content, encoding="utf-8")
            processed.add(dedup_key); written += 1

        state["calendar_ids"] = list(processed)
        self._save_state(state)
        _audit_write(wing, qdrant_scope, f"Google/Kalender sync: {written} nye, {skipped} uændrede")
        return {"synced": written, "skipped_unchanged": skipped, "wing": wing, "scope": qdrant_scope}

    # ── Drive + Docs ──────────────────────────────────────────────────────────

    def drive_list_files(self, folder_id: str = "", query: str = "") -> list[dict]:
        """List filer i Drive (eller undermappe)."""
        svc = self._build("drive", "v3")
        q_parts = ["trashed = false"]
        if folder_id:
            q_parts.append(f"'{folder_id}' in parents")
        if query:
            q_parts.append(f"name contains '{query}'")
        result = svc.files().list(
            q=" and ".join(q_parts),
            fields="files(id,name,mimeType,size,modifiedTime,webViewLink)",
            pageSize=200,
        ).execute()
        files = []
        for f in result.get("files", []):
            mime = f.get("mimeType", "")
            exportable = mime in _GAPPS_TEXT_EXPORT
            downloadable = any(mime.startswith(m) for m in _DIRECT_DOWNLOAD)
            files.append({
                "id":           f.get("id", ""),
                "name":         f.get("name", ""),
                "mimeType":     mime,
                "size":         f.get("size"),
                "modifiedTime": f.get("modifiedTime", ""),
                "webViewLink":  f.get("webViewLink", ""),
                "importable":   exportable or downloadable,
            })
        return files

    def drive_sync(self) -> dict:
        """Hent Drive-filer og skriv tekst-indhold til ingest-pipeline."""
        cfg    = self.get_config()
        wing   = cfg.get("drive_wing", "").strip()
        if not wing:
            raise ValueError("Drive wing er ikke konfigureret")
        qdrant_scope = cfg.get("drive_scope", "PRIVATE")
        folder_id    = cfg.get("drive_folder_id", "").strip()

        svc = self._build("drive", "v3")
        q_parts = ["trashed = false"]
        if folder_id:
            q_parts.append(f"'{folder_id}' in parents")

        result = svc.files().list(
            q=" and ".join(q_parts),
            fields="files(id,name,mimeType,modifiedTime)",
            pageSize=500,
        ).execute()

        state = self._load_state()
        processed = set(state.get("drive_ids", []))

        dest_dir = _INCOMING_DIR / wing
        dest_dir.mkdir(parents=True, exist_ok=True)
        written = skipped = errors = 0

        for f in result.get("files", []):
            fid      = f.get("id", "")
            fname    = f.get("name", "ukendt")
            mime     = f.get("mimeType", "")
            modified = f.get("modifiedTime", "")
            dedup    = f"{fid}:{modified}"

            if dedup in processed:
                skipped += 1; continue

            try:
                if mime in _GAPPS_TEXT_EXPORT:
                    # Eksportér Google Workspace-filer som tekst
                    export_mime = _GAPPS_TEXT_EXPORT[mime]
                    content_bytes = svc.files().export(
                        fileId=fid, mimeType=export_mime
                    ).execute()
                    text = content_bytes.decode("utf-8", errors="replace") if isinstance(content_bytes, bytes) else str(content_bytes)
                    ext  = ".csv" if export_mime == "text/csv" else ".txt"
                    out_name = re.sub(r"[^\w\-æøåÆØÅ .]", "_", fname)[:80] + ext
                elif any(mime.startswith(m) for m in _DIRECT_DOWNLOAD):
                    from googleapiclient.http import MediaIoBaseDownload
                    import io
                    request = svc.files().get_media(fileId=fid)
                    buf = io.BytesIO()
                    downloader = MediaIoBaseDownload(buf, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                    raw = buf.getvalue()
                    if mime == "application/pdf":
                        # PDF direkte til ingest-pipeline
                        out_name = re.sub(r"[^\w\-æøåÆØÅ .]", "_", fname)[:80]
                        if not out_name.lower().endswith(".pdf"):
                            out_name += ".pdf"
                        (dest_dir / out_name).write_bytes(raw)
                        processed.add(dedup); written += 1; continue
                    text = raw.decode("utf-8", errors="replace")
                    out_name = re.sub(r"[^\w\-æøåÆØÅ .]", "_", fname)[:80]
                    if not out_name.endswith(".txt"):
                        out_name += ".txt"
                else:
                    skipped += 1; processed.add(dedup); continue

                # Tilføj metadata-header
                final = (
                    f"Filnavn: {fname}\n"
                    f"Drive-ID: {fid}\n"
                    f"Ændret: {modified}\n"
                    f"Hentet: {datetime.now(timezone.utc).isoformat()}\n\n"
                    + text
                )
                (dest_dir / out_name).write_text(final, encoding="utf-8")
                processed.add(dedup); written += 1
            except Exception:
                errors += 1

        state["drive_ids"] = list(processed)
        self._save_state(state)
        _audit_write(wing, qdrant_scope, f"Google/Drive sync: {written} nye, {skipped} uændrede, {errors} fejl")
        return {"synced": written, "skipped_unchanged": skipped, "errors": errors, "wing": wing, "scope": qdrant_scope}

    # ── status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        cfg   = self.get_config()
        state = self._load_state()
        return {
            "authenticated": self.is_authenticated(),
            "email":         cfg.get("email_address", ""),
            "gmail_enabled":    cfg.get("gmail_enabled", False),
            "calendar_enabled": cfg.get("calendar_enabled", False),
            "drive_enabled":    cfg.get("drive_enabled", False),
            "gmail_synced":     len(state.get("gmail_ids", [])),
            "calendar_synced":  len(state.get("calendar_ids", [])),
            "drive_synced":     len(state.get("drive_ids", [])),
            "last_sync":        state.get("updated"),
            "config":           cfg,
        }


# ── audit ─────────────────────────────────────────────────────────────────────

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
