#!/bin/bash
# End-to-end voice pipeline test
# Optag → Whisper (Jetson) → Ollama (Jetson) → Piper (Pi 5) → Afspil

set -e
WORK=/tmp/voice_test
mkdir -p $WORK
cd $WORK

JETSON="${NOUS_JETSON_IP:-localhost}"

echo "🎤 Optager 5 sekunder... (tal nu)"
arecord -D plughw:2,0 -f S16_LE -r 16000 -c 1 -d 5 $WORK/input.wav 2>/dev/null
echo "✅ Optaget"

echo "🧠 Sender til Whisper..."
TRANSCRIPT=$(curl -s -X POST http://$JETSON:8080/inference \
  -F file=@$WORK/input.wav \
  -F language=da \
  -F response_format=json | python3 -c "import sys,json;print(json.load(sys.stdin)['text'].strip())")
echo "📝 Du sagde: '$TRANSCRIPT'"

if [ -z "$TRANSCRIPT" ]; then
    echo "❌ Tom transkription - afbryder"
    exit 1
fi

echo "💭 Spørger NOUS..."
PROMPT='Du er NOUS, en dansk personlig assistent. Svar ALTID kort på flydende dansk. Aldrig norsk.

Bruger: '"$TRANSCRIPT"'
NOUS:'

RESPONSE=$(curl -s http://$JETSON:11434/api/generate -d "$(python3 -c "
import json
print(json.dumps({'model':'nous-da','prompt':'''$PROMPT''','stream':False}))
")" | python3 -c "import sys,json;print(json.load(sys.stdin)['response'].strip())")

echo "🤖 NOUS svarer: '$RESPONSE'"

echo "🔊 Genererer tale..."
source /srv/nous/pipeline/.venv/bin/activate
echo "$RESPONSE" | piper --model /srv/nous/models/tts/da.onnx --output_file $WORK/output.wav

echo "🎵 Afspiller..."
play $WORK/output.wav rate 48000 channels 2 2>/dev/null

echo "✅ Færdig"
