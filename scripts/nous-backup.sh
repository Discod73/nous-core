#!/bin/bash
set -euo pipefail

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/mnt/nous-data/backups"
GPG_RECIPIENT="${NOUS_BACKUP_GPG_RECIPIENT:?Sæt NOUS_BACKUP_GPG_RECIPIENT (GPG fingerprint) i service environment}"
mkdir -p "$BACKUP_DIR"

# Qdrant snapshot
curl -s -X POST "http://localhost:6333/snapshots" \
  -H "Content-Type: application/json" \
  -d "{\"location\": \"${BACKUP_DIR}/qdrant-${DATE}.snapshot\"}"

# Kuzu backup
cp -r /mnt/nous-data/kuzu "${BACKUP_DIR}/kuzu-${DATE}.db"

# Krypter
for f in "${BACKUP_DIR}"/*-"${DATE}".*; do
  [ -f "$f" ] || continue
  gpg --batch --yes --encrypt --recipient "$GPG_RECIPIENT" \
      --output "${f}.gpg" "$f"
  rm "$f"
done

# Rens gamle (>30 dage)
find "$BACKUP_DIR" -name "*.gpg" -mtime +30 -delete

logger "NOUS backup: $DATE"
