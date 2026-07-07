#!/usr/bin/env bash
# Natjob: ingest lyd/video fra /mnt/nous-data/arkiv/ der ikke allerede er i Qdrant.
# Køres via nous-batch-ingest.timer kl 02:00 dagligt.

set -euo pipefail

ARKIV_DIR="/mnt/nous-data/arkiv"
LOG_FILE="/mnt/nous-data/logs/batch_ingest.log"
QDRANT_URL="http://127.0.0.1:6333"
WINGS_FILE="/srv/nous/config/wings.json"
INGEST_SCRIPT="/srv/nous/scripts/ingest.py"
PYTHON="/srv/nous/pipeline/.venv/bin/python3"

AV_EXTS=("mp3" "wav" "m4a" "ogg" "flac" "mp4" "mkv" "avi" "mov")

mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# Byg regex af extensions
ext_pattern=$(printf '\.%s$\|' "${AV_EXTS[@]}" | sed 's/\\|$//')

log "=== batch_ingest start ==="

if [[ ! -d "$ARKIV_DIR" ]]; then
    log "ARKIV_DIR $ARKIV_DIR findes ikke — afbryder"
    exit 0
fi

# Hent alle source-navne allerede i Qdrant på tværs af alle collections
declare -A already_ingested
while IFS= read -r collection; do
    offset_param=""
    while true; do
        body='{"limit":500,"with_payload":["source"],"with_vector":false'"${offset_param}"'}'
        response=$(curl -sf -X POST "${QDRANT_URL}/collections/${collection}/points/scroll" \
            -H 'Content-Type: application/json' -d "$body" 2>/dev/null || echo '{}')
        while IFS= read -r src; do
            [[ -n "$src" ]] && already_ingested["$src"]=1
        done < <(echo "$response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for pt in d.get('result', {}).get('points', []):
    s = pt.get('payload', {}).get('source', '')
    if s: print(s)
")
        next_offset=$(echo "$response" | python3 -c "
import sys, json
d = json.load(sys.stdin)
o = d.get('result', {}).get('next_page_offset')
print(o if o is not None else '')
" 2>/dev/null)
        [[ -z "$next_offset" ]] && break
        offset_param=",\"offset\":\"${next_offset}\""
    done
done < <(python3 -c "
import json
d = json.load(open('$WINGS_FILE'))
for w in d['wings']: print(w['collection'])
")

log "${#already_ingested[@]} filer allerede ingested"

ingested=0
skipped=0
errors=0

# Bestem wing og scope pr. underfolder i arkiv/
# Forventet struktur: /mnt/nous-data/arkiv/<wing>/<fil>
while IFS= read -r filepath; do
    filename=$(basename "$filepath")
    subdir=$(basename "$(dirname "$filepath")")

    # Opslag af scope fra wings.json
    wing_scope=$(python3 - "$subdir" "$WINGS_FILE" <<'PYEOF'
import sys, json
wing_name, wings_file = sys.argv[1], sys.argv[2]
data = json.load(open(wings_file))
w = next((x for x in data['wings'] if x['name'] == wing_name), None)
print(w['scope'] if w else 'PRIVATE')
PYEOF
)
    wing_name="$subdir"

    if [[ -n "${already_ingested[$filename]+x}" ]]; then
        log "  SPRING OVER (allerede ingested): $filename"
        ((skipped++)) || true
        continue
    fi

    log "  Ingest: $filename → wing=$wing_name scope=$wing_scope"
    if "$PYTHON" "$INGEST_SCRIPT" "$filepath" --wing "$wing_name" --scope "$wing_scope" >> "$LOG_FILE" 2>&1; then
        log "  OK: $filename"
        ((ingested++)) || true
    else
        log "  FEJL: $filename"
        ((errors++)) || true
    fi

done < <(find "$ARKIV_DIR" -type f | grep -iE "$ext_pattern" | sort)

log "=== batch_ingest slut: $ingested ingested, $skipped sprunget over, $errors fejl ==="
