#!/bin/bash

INCOMING="/home/nous/incoming"
VENV="/srv/nous/app/.venv/bin/python3"
INGEST="/srv/nous/scripts/ingest.py"
ARCHIVE="/mnt/nous-data/arkiv"

# Sæt scope baseret på wing
case "$wing" in
    boernesag) scope="SECRET" ;;
    *) scope="PRIVATE" ;;
esac

$VENV $INGEST "$filepath" --wing "$wing" --scope "$scope"

inotifywait -m -r -e close_write,moved_to "$INCOMING" |
while read -r dir event file; do
    filepath="${dir}${file}"
    wing=$(echo "$dir" | sed "s|$INCOMING/||" | cut -d'/' -f1)
    
    [ -f "$filepath" ] || continue
    [[ "$file" == .* ]] && continue  # skip syncthing temp files
    
    echo "$(date): Ingest $filepath → wing=$wing"
    $VENV $INGEST "$filepath" --wing "$wing" --scope PRIVATE
    mkdir -p "$ARCHIVE/$wing"
    mv "$filepath" "$ARCHIVE/$wing/"
done
