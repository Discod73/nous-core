#!/usr/bin/env python3
import asyncio, json, re, subprocess, tempfile, wave
from pathlib import Path
import numpy as np
import sounddevice as sd
import torch
import httpx
from silero_vad import load_silero_vad

import os as _os
JETSON = _os.environ.get("NOUS_NX_HOST", "localhost")
WHISPER_URL = _os.environ.get("NOUS_WHISPER_URL", f"http://{JETSON}:8080/inference")
WHISPER_PROMPT = "Dette er en samtale på dansk."
LLM_URL = _os.environ.get("NOUS_NX_LLAMA_URL", f"http://{JETSON}:8081/v1/chat/completions")
PROXY_URL = "http://localhost:8090"
SAMPLE_RATE = 16000
PIPER_MODEL = "/srv/nous/models/tts/da.onnx"
MIN_FRAGMENT_LEN = 60
SPLIT_REGEX = re.compile(r'([.!?]+\s+)')
VAD_SAMPLES = 512
SILENCE_HANG_FRAMES = 25
MAX_RECORDING_SEC = 15

TOOLS = [
    {"type": "function", "function": {"name": "get_time", "description": "Aktuel dato/tid i Danmark", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "get_weather", "description": "Vejr for dansk by", "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}}},
    {"type": "function", "function": {"name": "search_web", "description": "Søger nyheder og aktuelle info", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]

SYSTEM = """Du er NOUS, en dansk personlig AI-assistent. Brugeren hedder Dan.

Input kommer fra talegenkendelse og kan have fejl. Hvis en sætning ikke giver mening på dansk, så tolk den fonetisk:
- "Si'" eller "Si" → "Sig"
- Engelske ord ("job", "shot", "see") → ofte fejl for danske ord ("sjovt", "sjov", "sig")
- "Hver" → ofte "Hvad er"
- Eksempel: "Si\' noget job" → forstå som "sig noget sjovt"

Hvis input giver fin mening som det står, så tag det bogstaveligt - du må ikke "korrigere" rigtige ytringer. Ved reel tvivl: spørg kort.

REGLER FOR TOOL-BRUG:
- For TID/DATO i Danmark: brug get_time
- For VEJR: brug get_weather
- For NYHEDER, AKTUELLE BEGIVENHEDER, FAKTA om personer/steder/ting/film/musik/historie/videnskab: brug search_web
- Hvis du er i tvivl om noget faktuelt, brug ALTID search_web før du svarer

EFTER TOOL-KALD:
- Læs resultatet og giv et kort, naturligt dansk svar
- Find aldrig på detaljer der ikke står i resultatet

GENERELT:
- Svar ALTID på dansk, aldrig svensk eller norsk
- Hold svar korte - 2-3 sætninger normalt
- Svaret læses højt af en stemme, så undgå punktopstillinger og lange forklaringer
- Hvis brugeren stiller et generelt vidensspørgsmål du ikke er 100% sikker på, brug search_web"""


def find_respeaker():
    for i, d in enumerate(sd.query_devices()):
        if "respeaker" in d['name'].lower() or "xvf3800" in d['name'].lower():
            return i
    return None


def record_with_vad(output_wav):
    vad_model = load_silero_vad()
    device_idx = find_respeaker()
    print("🎤 Lytter... (start med at tale)", flush=True)
    audio_buffer = []
    # Pre-roll: keep last ~500ms of audio in circular buffer for context before speech
    PREROLL_FRAMES = 16  # ~512ms at 32ms/frame
    preroll = []
    silence_counter = 0
    speech_started = False
    max_frames = int(MAX_RECORDING_SEC * SAMPLE_RATE / VAD_SAMPLES)
    frame_count = 0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32', device=device_idx, blocksize=VAD_SAMPLES) as stream:
        while frame_count < max_frames:
            frame, _ = stream.read(VAD_SAMPLES)
            t = torch.from_numpy(frame.flatten())
            prob = vad_model(t, SAMPLE_RATE).item()
            if prob > 0.4:  # Lavere threshold for at fange begyndelsen
                if not speech_started:
                    print("🗣️  Tale detekteret", flush=True)
                    speech_started = True
                    # Inkludér pre-roll så vi får starten af sætningen
                    audio_buffer.extend(preroll)
                silence_counter = 0
                audio_buffer.append(frame.flatten())
            elif speech_started:
                silence_counter += 1
                audio_buffer.append(frame.flatten())
                if silence_counter >= SILENCE_HANG_FRAMES:
                    print("🤫 Stilhed - stopper", flush=True)
                    break
            else:
                # Hold løbende pre-roll buffer
                preroll.append(frame.flatten())
                if len(preroll) > PREROLL_FRAMES:
                    preroll.pop(0)
            frame_count += 1
    if not speech_started or len(audio_buffer) < 10:
        return False
    audio = np.concatenate(audio_buffer)
    audio_int16 = (audio * 32767).astype(np.int16)
    with wave.open(output_wav, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_int16.tobytes())
    return True


def transcribe(wav_path):
    with open(wav_path, "rb") as f:
        r = httpx.post(
            WHISPER_URL,
            files={"file": f},
            data={
                "language": "da",
                "response_format": "json",
                "prompt": WHISPER_PROMPT,
                "temperature": "0.0",
            },
            timeout=30.0,
        )
    return r.json().get("text", "").strip()


def call_tool(name, args):
    try:
        if name == "get_time":
            r = httpx.get(f"{PROXY_URL}/time", timeout=5.0)
        elif name == "get_weather":
            r = httpx.get(f"{PROXY_URL}/weather", params={"location": args["location"]}, timeout=10.0)
        elif name == "search_web":
            r = httpx.get(f"{PROXY_URL}/search", params={"q": args["query"], "n": 3}, timeout=15.0)
        else:
            return json.dumps({"error": f"unknown: {name}"})
        return json.dumps(r.json(), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def clean_for_tts(text):
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'#+\s*', '', text)
    text = re.sub(r'[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]', '', text)
    text = re.sub(r'https?://[^\s\)]+', '', text)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


async def synth_one(fragment):
    """Synthesize one fragment to wav, return path."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    proc = await asyncio.create_subprocess_exec("piper", "--model", PIPER_MODEL, "--output_file", wav_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.communicate(fragment.encode())
    return wav_path


async def synth_and_play(queue):
    """Pre-synthesize next fragment while current is playing."""
    next_wav_task = None
    while True:
        fragment = await queue.get()
        if fragment is None:
            if next_wav_task:
                wav_path = await next_wav_task
                play = await asyncio.create_subprocess_exec("play", "-q", wav_path, "rate", "48000", "channels", "2", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await play.wait()
                Path(wav_path).unlink(missing_ok=True)
            break
        if next_wav_task is None:
            next_wav_task = asyncio.create_task(synth_one(fragment))
            continue
        wav_path = await next_wav_task
        next_wav_task = asyncio.create_task(synth_one(fragment))
        play = await asyncio.create_subprocess_exec("play", "-q", wav_path, "rate", "48000", "channels", "2", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await play.wait()
        Path(wav_path).unlink(missing_ok=True)


async def stream_llm_response(messages, queue, max_iter=5):
    """Non-streaming probe for tool_calls, derefter streaming til TTS-queue."""
    full_text = ""
    for _ in range(max_iter):
        # Probe: synkront kald for at opdage tool_calls (Qwen3 streamer ikke tool_calls)
        r = httpx.post(
            LLM_URL,
            json={"model": "qwen3", "messages": messages, "tools": TOOLS, "temperature": 0.6},
            timeout=60.0,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("choices"):
            await queue.put("Beklager, jeg fik intet svar.")
            await queue.put(None)
            return "Beklager, jeg fik intet svar."
        msg = data["choices"][0]["message"]
        tcs = msg.get("tool_calls")
        if tcs:
            messages.append(msg)
            for tc in tcs:
                try:
                    fn = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except (json.JSONDecodeError, KeyError):
                    continue
                print(f"  🔧 {fn}({args})", flush=True)
                result = call_tool(fn, args)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            continue
        # Ingen tool_calls: stream det endelige svar
        buffer = ""
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", LLM_URL,
                json={"model": "qwen3", "messages": messages, "temperature": 0.6, "stream": True},
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if not delta:
                            continue
                        buffer += delta
                        full_text += delta
                        # Udpak komplette sætninger til queue løbende
                        while len(buffer) >= MIN_FRAGMENT_LEN:
                            m = SPLIT_REGEX.search(buffer)
                            if m and m.end() >= MIN_FRAGMENT_LEN:
                                frag = clean_for_tts(buffer[:m.end()].strip())
                                buffer = buffer[m.end():]
                                if frag:
                                    await queue.put(frag)
                            else:
                                break
                    except (json.JSONDecodeError, KeyError):
                        continue
        # Rest af buffer efter stream
        if buffer.strip():
            await queue.put(clean_for_tts(buffer.strip()))
        await queue.put(None)
        return full_text
    # Max iterationer nået uden tekst-svar
    error_msg = "Beklager, jeg kunne ikke svare."
    await queue.put(error_msg)
    await queue.put(None)
    return error_msg


def chat_with_tools(user_message, max_iter=5):
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_message}]
    for _ in range(max_iter):
        r = httpx.post(LLM_URL, json={"model": "qwen3", "messages": messages, "tools": TOOLS, "temperature": 0.6}, timeout=60.0)
        r.raise_for_status()
        data = r.json()
        if not data.get("choices"):
            return "Beklager, jeg fik intet svar."
        msg = data["choices"][0]["message"]
        tcs = msg.get("tool_calls")
        if tcs:
            messages.append(msg)
            for tc in tcs:
                try:
                    fn = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except (json.JSONDecodeError, KeyError):
                    continue
                print(f"  🔧 {fn}({args})", flush=True)
                result = call_tool(fn, args)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            continue
        return msg.get("content", "")
    return "Beklager, jeg kunne ikke svare."


async def main():
    workdir = Path("/tmp/nous_voice")
    workdir.mkdir(exist_ok=True)
    wav_input = str(workdir / "input.wav")
    if not record_with_vad(wav_input):
        print("❌ Ingen tale detekteret")
        return
    print("🧠 Whisper...", flush=True)
    transcript = transcribe(wav_input)
    print(f"📝 Du sagde: '{transcript}'", flush=True)
    if not transcript or len(transcript.strip()) < 3:
        print("❌ For kort transkription")
        return
    print("💭 NOUS tænker...", flush=True)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": transcript}]
    queue = asyncio.Queue()
    player = asyncio.create_task(synth_and_play(queue))
    try:
        full_text = await stream_llm_response(messages, queue)
        print(f"🤖 {full_text}", flush=True)
    except Exception as e:
        print(f"❌ Fejl: {e}", flush=True)
        player.cancel()
    finally:
        await queue.put(None)
        try:
            await player
        except (asyncio.CancelledError, Exception):
            pass
    print("✅ Færdig", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
