#!/usr/bin/env python3
"""
NOUS streaming voice pipeline
Inspireret af jetson-orin-kian (MIT) - se /srv/nous/docs/CREDITS.md

Mic → Whisper (Jetson) → Ollama stream (Jetson) → Piper stream (Pi 5) → Speaker
"""
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
import os
import httpx

JETSON = os.environ.get("NOUS_NX_HOST", "localhost")
WHISPER_URL = os.environ.get("NOUS_WHISPER_URL", f"http://{JETSON}:8080/inference")
OLLAMA_URL = os.environ.get("NOUS_OLLAMA_URL", f"http://{JETSON}:11434/api/generate")
MODEL = "nous-da"
PIPER_MODEL = "/srv/nous/models/tts/da.onnx"
RECORD_DEVICE = "plughw:2,0"  # ReSpeaker
MIN_FRAGMENT_LEN = 25
SPLIT_REGEX = re.compile(r'([.!?]+\s+|[,;]\s+)')


def record(seconds: int, output: str) -> None:
    subprocess.run(
        ["arecord", "-D", RECORD_DEVICE, "-f", "S16_LE",
         "-r", "16000", "-c", "1", "-d", str(seconds), output],
        check=True, stderr=subprocess.DEVNULL
    )


def transcribe(wav_path: str) -> str:
    with open(wav_path, "rb") as f:
        r = httpx.post(
            WHISPER_URL,
            files={"file": f},
            data={"language": "da", "response_format": "json"},
            timeout=30.0,
        )
    return r.json().get("text", "").strip()


async def synth_and_play(queue: asyncio.Queue) -> None:
    """Background task: take fragments from queue, synthesize via Piper, play."""
    while True:
        fragment = await queue.get()
        if fragment is None:
            break
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
        proc = await asyncio.create_subprocess_exec(
            "piper", "--model", PIPER_MODEL, "--output_file", wav_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(fragment.encode())
        # Play with sox - 48kHz stereo for ReSpeaker
        play = await asyncio.create_subprocess_exec(
            "play", "-q", wav_path, "rate", "48000", "channels", "2",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await play.wait()
        Path(wav_path).unlink(missing_ok=True)


async def stream_llm(prompt: str, queue: asyncio.Queue) -> str:
    """Stream tokens from Ollama, push fragments to TTS queue."""
    buffer = ""
    full_response = ""
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", OLLAMA_URL, json={
            "model": MODEL,
            "prompt": prompt,
            "stream": True,
        }) as response:
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("response", "")
                buffer += token
                full_response += token

                # Try to split on punctuation
                while len(buffer) >= MIN_FRAGMENT_LEN:
                    match = SPLIT_REGEX.search(buffer)
                    if match and match.end() >= MIN_FRAGMENT_LEN:
                        fragment = buffer[:match.end()].strip()
                        buffer = buffer[match.end():]
                        if fragment:
                            await queue.put(fragment)
                    else:
                        break

                if chunk.get("done"):
                    if buffer.strip():
                        await queue.put(buffer.strip())
                    break
    return full_response


async def main():
    workdir = Path("/tmp/nous_voice")
    workdir.mkdir(exist_ok=True)
    wav_input = str(workdir / "input.wav")

    print("🎤 Optager 5 sekunder...", flush=True)
    record(5, wav_input)

    print("🧠 Whisper...", flush=True)
    transcript = transcribe(wav_input)
    print(f"📝 Du sagde: '{transcript}'", flush=True)

    if not transcript:
        print("❌ Tom transkription")
        return

    prompt = f"""Du er NOUS, en dansk personlig assistent. Svar kort på flydende dansk.

Bruger: {transcript}
NOUS:"""

    print("💭 NOUS svarer (streaming)...", flush=True)
    queue: asyncio.Queue = asyncio.Queue()
    player = asyncio.create_task(synth_and_play(queue))

    response = await stream_llm(prompt, queue)
    await queue.put(None)  # signal done
    await player

    print(f"\n🤖 Komplet svar: '{response}'")
    print("✅ Færdig")


if __name__ == "__main__":
    asyncio.run(main())
