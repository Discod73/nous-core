#!/bin/bash

INCOMING="/home/nous/incoming"
VENV="/srv/nous/app/.venv/bin/python3"
INGEST="/srv/nous/scripts/ingest.py"
ARCHIVE="/mnt/nous-data/arkiv"

WINGS=($(python3 -c "import json; d=json.loads(open('/srv/nous/config/wings.json').read()); print(' '.join(w['name'] for w in d['wings']))"))


for wing in "${WINGS[@]}"; do
    dir="$INCOMING/$wing"
    for f in "$dir"/*; do
        [ -f "$f" ] || continue
        echo "Ingest: $f → $wing"
        $VENV $INGEST "$f" --wing "$wing" --scope private
        mkdir -p "$ARCHIVE/$wing"
        mv "$f" "$ARCHIVE/$wing/"
    done
done
