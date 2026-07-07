#!/bin/bash

INCOMING="/home/nous/incoming"
VENV="/srv/nous/app/.venv/bin/python3"
INGEST="/srv/nous/scripts/ingest.py"
ARCHIVE="/mnt/nous-data/arkiv"

inotifywait -m -r -e close_write,moved_to "$INCOMING" |
while read -r dir event file; do
    filepath="${dir}${file}"
    wing=$(echo "$dir" | sed "s|$INCOMING/||" | cut -d'/' -f1)

    [ -f "$filepath" ] || continue
    [[ "$file" == .* ]] && continue  # skip syncthing temp files

    # Sæt scope baseret på wing — loades fra wings.json
    scope=$(python3 -c "
import json, sys
data = json.loads(open('/srv/nous/config/wings.json').read())
entry = next((w for w in data['wings'] if w['name'] == sys.argv[1]), None)
print(entry['scope'] if entry else 'PRIVATE')
" "$wing" 2>/dev/null || echo "PRIVATE")

    echo "$(date): Ingest $filepath → wing=$wing scope=$scope"
    $VENV $INGEST "$filepath" --wing "$wing" --scope "$scope"
    mkdir -p "$ARCHIVE/$wing"
    mv "$filepath" "$ARCHIVE/$wing/"
done
