#!/usr/bin/env python3
"""
NOUS Gmail Auth — Engangs-OAuth-autorisering for Gmail (gmail.readonly).

Forudsætninger:
  1. Opret et Google Cloud-projekt og aktiver Gmail API
     https://console.cloud.google.com/
  2. Opret OAuth 2.0-klientoplysninger (type: Desktop-app)
     APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
  3. Download JSON og gem som:
     /srv/nous/config/gmail_credentials.json

Scriptet starter en lokal webserver på port 8085.
Besøg URL'en i en browser — brug SSH-tunnel ved behov:
  ssh -L 8085:localhost:8085 nous@<pi-ip>

Token gemmes i /srv/nous/config/gmail_token.json (chmod 600).
"""

import sys
from pathlib import Path

_NOUS_ROOT = Path(__file__).resolve().parents[1]
if str(_NOUS_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUS_ROOT))

from google_auth_oauthlib.flow import InstalledAppFlow

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_CREDENTIALS_FILE = Path("/srv/nous/config/gmail_credentials.json")
_TOKEN_FILE = Path("/srv/nous/config/gmail_token.json")
_PORT = 8085


def main() -> None:
    if not _CREDENTIALS_FILE.exists():
        print(f"FEJL: credentials-fil ikke fundet: {_CREDENTIALS_FILE}")
        print()
        print("Trin-for-trin:")
        print("  1. Gå til https://console.cloud.google.com/")
        print("  2. Opret eller vælg et projekt")
        print("  3. Aktiver 'Gmail API' under APIs & Services → Library")
        print("  4. Gå til APIs & Services → Credentials")
        print("  5. Create Credentials → OAuth 2.0 Client ID → Desktop app")
        print("  6. Download JSON og gem som:")
        print(f"     {_CREDENTIALS_FILE}")
        sys.exit(1)

    print("=== NOUS Gmail Auth ===")
    print()
    print(f"Starter lokal OAuth-server på port {_PORT}...")
    print()
    print("Kører du via SSH? Åbn en tunnel i en separat terminal:")
    print(f"  ssh -L {_PORT}:localhost:{_PORT} nous@<pi-ip>")
    print()
    print("Besøg URL'en herunder i din browser og godkend adgang.")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_FILE), _SCOPES)
    creds = flow.run_local_server(
        port=_PORT,
        prompt="consent",
        access_type="offline",
        open_browser=False,
    )

    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    _TOKEN_FILE.chmod(0o600)

    print()
    print(f"Token gemt: {_TOKEN_FILE}")
    print("Gmail-integration er nu autentificeret.")
    print()
    print("Næste trin:")
    print("  1. Aktiver i NOUS Cockpit: Integrationer → Gmail → aktivér")
    print("  2. Eller kør manuel sync:")
    print("     python3 /srv/nous/scripts/gmail_sync.py")


if __name__ == "__main__":
    main()
