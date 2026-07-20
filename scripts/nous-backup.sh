#!/bin/bash
set -euo pipefail

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/mnt/nous-data/backups"
QDRANT_SNAP_DIR="/mnt/nous-data/qdrant/snapshots"

# Obligatorisk: GPG-modtager skal være sat — fail-closed
GPG_RECIPIENT="${NOUS_BACKUP_GPG_RECIPIENT:?Sæt NOUS_BACKUP_GPG_RECIPIENT (GPG fingerprint) i service environment}"

mkdir -p "$BACKUP_DIR"

# ── Obligatorisk: Qdrant full snapshot ────────────────────────────
# Qdrant ignorerer 'location'-parametret og skriver altid til sit eget snapshots-dir.
# Vi kalder API'et, parser det returnerede navn og kopierer filen derfra.
SNAP_RESPONSE=$(curl -sf -X POST "http://localhost:6333/snapshots" \
  -H "Content-Type: application/json" \
  -d '{}')

SNAP_NAME=$(echo "$SNAP_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])")

if [ -z "$SNAP_NAME" ]; then
  logger "NOUS backup ERROR: Qdrant snapshot API returnerede intet navn"
  exit 1
fi

SNAP_SRC="${QDRANT_SNAP_DIR}/${SNAP_NAME}"

# Vent op til 10s på at filen dukker op (async skrivning)
for i in $(seq 1 10); do
  [ -f "$SNAP_SRC" ] && break
  sleep 1
done

if [ ! -f "$SNAP_SRC" ]; then
  logger "NOUS backup ERROR: Qdrant snapshot-fil ikke fundet: $SNAP_SRC"
  exit 1
fi

cp "$SNAP_SRC" "${BACKUP_DIR}/qdrant-${DATE}.snapshot"
rm -f "$SNAP_SRC"   # ryd op i Qdrant's eget snapshots-dir
logger "NOUS backup: Qdrant snapshot kopieret (${SNAP_NAME})"

# ── Valgfri: Kuzu database ────────────────────────────────────────
# Accepterer både /mnt/nous-data/kuzu (mappe) og kuzu.db (fil/mappe med extension)
KUZU_SRC=""
for candidate in /mnt/nous-data/kuzu /mnt/nous-data/kuzu.db; do
  if [ -e "$candidate" ]; then
    KUZU_SRC="$candidate"
    break
  fi
done

if [ -n "$KUZU_SRC" ]; then
  cp -r "$KUZU_SRC" "${BACKUP_DIR}/kuzu-${DATE}.db"
  logger "NOUS backup: kuzu inkluderet fra $KUZU_SRC"
else
  logger "NOUS backup: Kuzu not present — skipped"
fi

# ── Krypter alle filer fra dette backup-job ───────────────────────
shopt -s nullglob
files=("${BACKUP_DIR}"/*-"${DATE}".*)
shopt -u nullglob

if [ ${#files[@]} -eq 0 ]; then
  logger "NOUS backup ERROR: ingen filer at kryptere"
  exit 1
fi

for f in "${files[@]}"; do
  [ -f "$f" ] || continue
  gpg --batch --yes --encrypt --recipient "$GPG_RECIPIENT" \
      --output "${f}.gpg" "$f" && rm "$f"
done

# ── Rens gamle (>30 dage) ─────────────────────────────────────────
find "$BACKUP_DIR" -name "*.gpg" -mtime +30 -delete

logger "NOUS backup: $DATE — OK"
