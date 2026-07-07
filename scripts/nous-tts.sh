#!/bin/bash
# NOUS TTS — Dansktalende assistent
# Brug: ./nous-tts.sh "Din tekst her"

MODEL_DIR="/srv/nous/models/tts"
TMP_WAV="/tmp/nous_raw.wav"
OUT_WAV="/tmp/nous_speak.wav"

if [ -z "$1" ]; then
    echo "Brug: $0 'Din tekst her'"
    exit 1
fi

cd "$MODEL_DIR"
echo "$1" | python3 -m piper --model da.onnx --config da.onnx.json --output_file "$TMP_WAV"
sox "$TMP_WAV" -r 48000 -c 2 "$OUT_WAV"
aplay "$OUT_WAV"
