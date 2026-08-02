#!/usr/bin/env python3
"""
NOUS Protonmail Auth — Fallback-guide til manuel Bridge-login via SSH.

Den anbefalede metode er at bruge NOUS Cockpit-wizarden:
  Indstillinger → Integrationer → Protonmail → FORBIND PROTONMAIL →

Dette script viser manuel SSH-fremgangsmåde hvis wizard-login (pexpect) fejler.
Bridge kører i Docker-containeren 'protonmail-bridge'.

Forudsætninger:
  1. Bridge-containeren er startet (Cockpit wizard trin 3)
  2. Du har adgang til Pi via SSH

Manuel login-procedure:
  1. SSH ind på Pi:
       ssh nous@<pi-ip>

  2. Start Bridge CLI i containeren:
       docker exec -it protonmail-bridge proton-bridge --cli

  3. I Bridge CLI:
       >>> login
       Username: din@proton.me
       Password: ****
       Two factor authentication code: 123456   (kun hvis 2FA aktiveret)
       Logged in as din@proton.me

  4. Hent Bridge IMAP-credentials (IKKE dit Proton-password):
       >>> info
       (noter 'Password:' feltet under IMAP Settings)

  5. Afslut:
       >>> exit

  6. Vend tilbage til Cockpit-wizarden og fortsæt fra trin 5 (IMAP-test).

Token-filer gemmes i /home/nous/.protonmail-bridge/ (Docker-volume, ikke git-tracked).
"""

import sys
from pathlib import Path

_NOUS_ROOT = Path(__file__).resolve().parents[1]
if str(_NOUS_ROOT) not in sys.path:
    sys.path.insert(0, str(_NOUS_ROOT))

_STATE_FILE = Path("/srv/nous/config/protonmail_state.json")
_CONTAINER  = "protonmail-bridge"


def main() -> None:
    print("=== NOUS Protonmail Bridge — Manuel Login-guide ===")
    print()
    print("Brug NOUS Cockpit-wizarden (anbefalet):")
    print("  Indstillinger → Integrationer → Protonmail → FORBIND PROTONMAIL →")
    print()
    print("─" * 60)
    print("MANUEL SSH-PROCEDURE (fallback):")
    print()
    print("  1. SSH ind på Pi:")
    print("       ssh nous@<pi-ip>")
    print()
    print("  2. Start Bridge CLI:")
    print(f"       docker exec -it {_CONTAINER} proton-bridge --cli")
    print()
    print("  3. Log ind:")
    print("       >>> login")
    print("       Username: din@proton.me")
    print("       Password: ****")
    print("       (evt. 2FA-kode hvis aktiveret)")
    print()
    print("  4. Hent Bridge IMAP-password:")
    print("       >>> info")
    print("       (kopiér 'Password:' under IMAP Settings — ikke dit Proton-password)")
    print()
    print("  5. Afslut:")
    print("       >>> exit")
    print()
    print("─" * 60)

    import subprocess
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format={{.State.Running}}", _CONTAINER],
            capture_output=True, text=True
        )
        running = "true" in result.stdout
        print(f"\nContainer '{_CONTAINER}': {'KØR ✓' if running else 'IKKE KØRENDE ✗'}")
    except FileNotFoundError:
        print("\nDocker er ikke installeret eller ikke i PATH")

    if _STATE_FILE.exists():
        import json
        try:
            state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            if state.get("bridge_email"):
                print(f"Gemt konto:  {state['bridge_email']}")
                print(f"Sidst synk:  {state.get('last_sync', 'aldrig')}")
        except Exception:
            pass
    else:
        print("\nStatus: Ingen Bridge-konto gemt endnu")


if __name__ == "__main__":
    main()
