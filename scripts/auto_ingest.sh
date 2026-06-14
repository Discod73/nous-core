#!/bin/bash

INCOMING="/home/nous/incoming"
VENV="/srv/nous/app/.venv/bin/python3"
INGEST="/srv/nous/scripts/ingest.py"
ARCHIVE="/mnt/nous-data/arkiv"

WINGS=(boernesag jura familie fbf nous_projekt dans_profil fbf_data)

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
