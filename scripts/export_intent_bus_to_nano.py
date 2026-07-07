#!/usr/bin/env python3
"""
Eksporter intent_bus.db til Nano — filtrerer SECRET-scope fra.

Køres MANUELT fra Pi 5 (push, ikke pull):
    python3 /srv/nous/scripts/export_intent_bus_to_nano.py <NANO_IP>

Kræver: ssh-nøgle-adgang fra Pi 5 til nous@<NANO_IP>
"""
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_DB   = Path("/mnt/nous-data/intent_bus.db")
NANO_USER   = "nous"
NANO_DEST   = "/srv/nous-test/data/intent_bus.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    status      TEXT NOT NULL DEFAULT 'pending',
    wing        TEXT NOT NULL,
    scope       TEXT NOT NULL,
    operation   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    source      TEXT NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON intents(status);
CREATE INDEX IF NOT EXISTS idx_wing   ON intents(wing);
"""


def main() -> None:
    if len(sys.argv) != 2:
        print("Brug: python3 export_intent_bus_to_nano.py <NANO_IP>")
        sys.exit(1)

    nano_ip = sys.argv[1].strip()

    if not SOURCE_DB.exists():
        print(f"FEJL: Kilde-database ikke fundet: {SOURCE_DB}")
        sys.exit(1)

    # Tæl rækker i kilden
    src = sqlite3.connect(SOURCE_DB)
    src.row_factory = sqlite3.Row
    total_rows   = src.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
    secret_rows  = src.execute("SELECT COUNT(*) FROM intents WHERE scope = 'SECRET'").fetchone()[0]
    included_rows = total_rows - secret_rows

    print(f"Kilde: {SOURCE_DB}")
    print(f"  Totalt antal rækker : {total_rows}")
    print(f"  SECRET-rækker (ekskl.): {secret_rows}")
    print(f"  Rækker inkluderet   : {included_rows}")
    print()

    if included_rows == 0:
        print("ADVARSEL: Ingen rækker at eksportere efter filtrering. Afbryder.")
        src.close()
        sys.exit(0)

    # Opret filtreret kopi i en midlertidig fil
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tmp_path = Path(tf.name)

    try:
        dst = sqlite3.connect(tmp_path)
        for stmt in SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                dst.execute(stmt)
        dst.commit()

        rows = src.execute(
            "SELECT id, created_at, status, wing, scope, operation, payload, source, error "
            "FROM intents WHERE scope != 'SECRET' ORDER BY id"
        ).fetchall()

        dst.executemany(
            "INSERT INTO intents (id, created_at, status, wing, scope, operation, payload, source, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [tuple(r) for r in rows],
        )
        dst.commit()
        dst.close()
        src.close()

        # Verificér tælling i eksport-filen
        verify = sqlite3.connect(tmp_path)
        exported = verify.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
        secret_check = verify.execute("SELECT COUNT(*) FROM intents WHERE scope = 'SECRET'").fetchone()[0]
        verify.close()

        print(f"Eksport-fil: {tmp_path}")
        print(f"  Rækker i eksport-fil : {exported}")
        print(f"  SECRET-rækker i fil  : {secret_check}  ← skal være 0")
        print()

        if secret_check != 0:
            print("FEJL: SECRET-rækker fundet i eksport-fil. Afbryder — sender ingenting.")
            sys.exit(1)

        if exported != included_rows:
            print(f"FEJL: Forventet {included_rows} rækker, fik {exported}. Afbryder.")
            sys.exit(1)

        # Opret destinationsmappe på Nano hvis den ikke findes
        print(f"Opretter /srv/nous-test/data/ på {nano_ip} (hvis nødvendigt)...")
        subprocess.run(
            ["ssh", f"{NANO_USER}@{nano_ip}", "mkdir -p /srv/nous-test/data"],
            check=True,
        )

        # Push til Nano via scp
        dest = f"{NANO_USER}@{nano_ip}:{NANO_DEST}"
        print(f"Sender {tmp_path} → {dest} ...")
        subprocess.run(["scp", str(tmp_path), dest], check=True)

        # Bekræft modtaget filstørrelse
        result = subprocess.run(
            ["ssh", f"{NANO_USER}@{nano_ip}", f"wc -c < {NANO_DEST}"],
            capture_output=True, text=True, check=True,
        )
        nano_bytes = int(result.stdout.strip())
        local_bytes = tmp_path.stat().st_size

        print()
        print(f"Overført filstørrelse: {local_bytes} bytes (lokal) / {nano_bytes} bytes (Nano)")
        if nano_bytes != local_bytes:
            print("ADVARSEL: Filstørrelser matcher ikke — tjek overførslen manuelt.")
        else:
            print("Størrelser matcher. Eksport gennemført.")

        print()
        print("Opsummering:")
        print(f"  {included_rows} rækker inkluderet  ({', '.join(sorted(set(r[3] for r in rows)))} wings)")
        print(f"  {secret_rows} rækker udeladt (SECRET scope — røres aldrig)")

    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
