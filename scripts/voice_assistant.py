#!/usr/bin/env python3
"""
NOUS voice_assistant.py
Wake-word watchdog. Lukker stream HELT under subprocess for at frigive ALSA-device.
"""
import logging
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from openwakeword import Model

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger("nous-wake")

SAMPLE_RATE = 16000
CHUNK_SIZE = 1280
WAKE_WORD = "hey_jarvis"
THRESHOLD = 0.5
COOLDOWN_SEC = 2.0
SESSION_TIMEOUT = 60
DEVICE_RELEASE_WAIT = 0.8  # ALSA skal have tid til reelt at frigive

VOICE_CHAT_SCRIPT = Path("/srv/nous/scripts/voice_chat.py")
VENV_PYTHON = Path("/srv/nous/pipeline/.venv/bin/python3")
TTS_MODEL = Path("/srv/nous/models/tts/da.onnx")


def find_input_device():
    """Match XVF3800 case-insensitivt. Falder tilbage til hw:2,0 hvis kendt."""
    for i, d in enumerate(sd.query_devices()):
        name_lc = d['name'].lower()
        if d['max_input_channels'] > 0 and ('xvf3800' in name_lc or 'respeaker' in name_lc):
            log.info(f"Input device: {d['name']} (idx {i}, {d['max_input_channels']} ch)")
            return i
    log.warning("XVF3800 ikke fundet, bruger system default input")
    return None


def play_ack():
    cmd = (
        f"echo 'Ja?' | piper --model {TTS_MODEL} --output_raw 2>/dev/null | "
        f"sox -t raw -r 22050 -e signed-integer -b 16 -c 1 - "
        f"-r 48000 -c 2 -t alsa default 2>/dev/null"
    )
    subprocess.run(["bash", "-c", cmd], check=False)


def open_stream(device, callback):
    """Opret og start en ny InputStream."""
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype='float32',
        blocksize=CHUNK_SIZE,
        device=device,
        callback=callback,
    )
    stream.start()
    return stream


def main():
    log.info("Initialiserer openWakeWord...")
    oww = Model()

    test_pred = oww.predict(np.zeros(CHUNK_SIZE, dtype=np.int16))
    available = list(test_pred.keys())
    log.info(f"Tilgængelige wake-words: {available}")

    if WAKE_WORD not in available:
        log.error(f"'{WAKE_WORD}' ikke i loaded models")
        return

    log.info(f"Lytter efter '{WAKE_WORD}' (threshold {THRESHOLD})")
    device = find_input_device()
    if device is None:
        for attempt in range(12):
            log.info(f"Ingen lydenhed — venter 5s og prøver igen ({attempt + 1}/12)...")
            time.sleep(5)
            device = find_input_device()
            if device is not None:
                break
    if device is None:
        log.error("XVF3800 ikke fundet efter 60s. Afslutter — systemd genstarter.")
        sys.exit(1)

    audio_buffer = []
    last_audio_ts = time.time()
    AUDIO_WATCHDOG_SEC = 45

    def audio_callback(indata, frames, time_info, status):
        nonlocal last_audio_ts
        if status:
            log.debug(f"Audio status: {status}")
        chunk = (indata[:, 0] * 32767).astype(np.int16)
        audio_buffer.append(chunk)
        last_audio_ts = time.time()

    stream = open_stream(device, audio_callback)
    log.info("Stream startet. Vent på wake-word...")

    try:
        while True:
            if time.time() - last_audio_ts > AUDIO_WATCHDOG_SEC:
                log.warning(f"Ingen audio i {AUDIO_WATCHDOG_SEC}s — stream død. Afslutter til systemd-genstart.")
                sys.exit(1)

            if not audio_buffer:
                time.sleep(0.02)
                continue

            chunk = audio_buffer.pop(0)
            if len(chunk) != CHUNK_SIZE:
                continue

            score = oww.predict(chunk).get(WAKE_WORD, 0.0)

            if score > THRESHOLD:
                log.info(f"WAKE: {WAKE_WORD} score={score:.3f}")

                # KRITISK: close (ikke kun stop) så ALSA frigiver devicet
                stream.stop()
                stream.close()
                del stream
                oww.reset()
                audio_buffer.clear()
                time.sleep(DEVICE_RELEASE_WAIT)

                play_ack()
                log.info("Spawner voice_chat session...")
                try:
                    rc = subprocess.run(
                        [str(VENV_PYTHON), str(VOICE_CHAT_SCRIPT)],
                        timeout=SESSION_TIMEOUT,
                    ).returncode
                    log.info(f"Session done (rc={rc})")
                except subprocess.TimeoutExpired:
                    log.warning("Session timeout - dræbt")

                time.sleep(COOLDOWN_SEC)
                audio_buffer.clear()

                # Genåbn fresh stream
                stream = open_stream(device, audio_callback)
                log.info("Lytter igen...")

    except KeyboardInterrupt:
        log.info("Afslutter på Ctrl-C")
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
