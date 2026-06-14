#!/bin/bash
set -euo pipefail

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/mnt/nous-data/backups"
GPG_RECIPIENT="${NOUS_GPG_KEY_ID:?Sæt NOUS_GPG_KEY_ID i .env}"
mkdir -p "$BACKUP_DIR"

# Qdrant: tar storage-mappen direkte (bind-mount fra Docker)
tar czf "${BACKUP_DIR}/qdrant-${DATE}.tar.gz" -C /mnt/nous-data/qdrant storage

# Kuzu backup
cp -r /mnt/nous-data/kuzu.db "${BACKUP_DIR}/kuzu-${DATE}.db"

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
