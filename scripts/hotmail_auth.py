#!/usr/bin/env python3
"""
NOUS Hotmail Auth — Engangs-OAuth-autorisering for Hotmail/Outlook (Mail.Read).

Den anbefalede metode er at bruge NOUS Cockpit-wizarden:
  Indstillinger → Integrationer → Hotmail / Outlook → FORBIND HOTMAIL →

Dette script er en fallback til fejlfinding og kan ikke bruges til
den fulde OAuth-flow (der kræver browser-redirect via NOUS-server).

Forudsætninger:
  1. Opret en app-registrering i Azure Portal
     https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade
  2. Tilføj Mail.Read og User.Read under Microsoft Graph (delegated permissions)
  3. Opret en client secret under Certificates & secrets
  4. Gem Application (client) ID og client secret som:
     /srv/nous/config/hotmail_credentials.json
     Format: {"client_id": "...", "client_secret": "..."}

Token gemmes i /srv/nous/config/hotmail_token.json (chmod 600).
"""

import sys
from pathlib import Path

_NOUS_ROOT = Path(__file__).resolve().parents[1]
if str(_NOUS_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUS_ROOT))

_CREDS_FILE = Path("/srv/nous/config/hotmail_credentials.json")
_TOKEN_FILE = Path("/srv/nous/config/hotmail_token.json")


def main() -> None:
    print("=== NOUS Hotmail Auth ===")
    print()

    if not _CREDS_FILE.exists():
        print(f"FEJL: Credentials-fil ikke fundet: {_CREDS_FILE}")
        print()
        print("Brug NOUS Cockpit-wizarden (anbefalet):")
        print("  Indstillinger → Integrationer → Hotmail / Outlook → FORBIND HOTMAIL →")
        print()
        print("Eller opret filen manuelt:")
        print(f"  {_CREDS_FILE}")
        print('  Indhold: {"client_id": "...", "client_secret": "..."}')
        sys.exit(1)

    import json
    creds = json.loads(_CREDS_FILE.read_text(encoding="utf-8"))
    client_id = creds.get("client_id", "")

    if not client_id or not creds.get("client_secret"):
        print("FEJL: Credentials-fil mangler client_id eller client_secret")
        sys.exit(1)

    print(f"Client ID: {client_id[:8]}…")
    print()
    print("OAuth-flow kræver browser-redirect — brug Cockpit-wizarden:")
    print("  http://<pi-ip>/ → Indstillinger → Integrationer → Hotmail / Outlook")
    print()

    if _TOKEN_FILE.exists():
        try:
            from integrations.hotmail_connector import HotmailConnector
            connector = HotmailConnector()
            if connector.is_authenticated():
                print(f"Status: Autentificeret ✓")
                print(f"Token:  {_TOKEN_FILE}")
            else:
                print("Status: Token er udløbet eller ugyldigt")
                print("        Brug Cockpit-wizarden til at genautentificere")
        except Exception as e:
            print(f"Status: Kunne ikke verificere ({e})")
    else:
        print("Status: Ikke autentificeret endnu")
        print("        Brug Cockpit-wizarden til at sætte det op")


if __name__ == "__main__":
    main()
