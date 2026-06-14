#!/usr/bin/env python3
"""
NOUS voice_assistant.py
Wake-word watchdog. Lukker stream HELT under subprocess for at frigive ALSA-device.
"""

import logging
import subprocess
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
THRESHOLD = 0.6
COOLDOWN_SEC = 3.5
SESSION_TIMEOUT = 60
DEVICE_RELEASE_WAIT = 0.8

VOICE_CHAT_SCRIPT = Path("/srv/nous/scripts/voice_chat.py")
VENV_PYTHON = Path("/srv/nous/pipeline/.venv/bin/python3")
TTS_MODEL = Path("/srv/nous/models/tts/da.onnx")


def find_input_device():
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
    audio_buffer = []
    cooldown_until = 0.0

    def audio_callback(indata, frames, time_info, status):
        if status:
            log.debug(f"Audio status: {status}")
        chunk = (indata[:, 0] * 32767).astype(np.int16)
        audio_buffer.append(chunk)

    stream = open_stream(device, audio_callback)
    log.info("Stream startet. Vent på wake-word...")

    try:
        while True:
            if not audio_buffer:
                time.sleep(0.02)
                continue

            chunk = audio_buffer.pop(0)
            if len(chunk) != CHUNK_SIZE:
                continue

            if time.time() < cooldown_until:
                continue
            score = oww.predict(chunk).get(WAKE_WORD, 0.0)

            if score > THRESHOLD:
                log.info(f"WAKE: {WAKE_WORD} score={score:.3f}")

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
                    log.warning("Session timeout – dræbt")

                time.sleep(COOLDOWN_SEC)
                audio_buffer.clear()

                stream = open_stream(device, audio_callback)

                drain_end = time.time() + 2.5
                while time.time() < drain_end:
                    if audio_buffer:
                        audio_buffer.pop(0)
                    else:
                        time.sleep(0.01)
                audio_buffer.clear()
                oww.reset()

                cooldown_until = time.time() + COOLDOWN_SEC
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
