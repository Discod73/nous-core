#!/usr/bin/env python3
import os, subprocess, tempfile, requests

JETSON_IP = os.environ.get("NOUS_JETSON_IP", "localhost")
WHISPER_URL = f"http://{JETSON_IP}:8080/inference"
OLLAMA_URL = f"http://{JETSON_IP}:11434/api/generate"
PROXY_URL = "http://localhost:8090"
OLLAMA_MODEL = "gemma4:e2b"
PIPER_DIR = "/srv/nous/models/tts"
RECORD_DEV = "plughw:2,0"
VENV_PY = "/srv/nous/pipeline/.venv/bin/python3"

def record(path, seconds=5):
    subprocess.run(["arecord","-D",RECORD_DEV,"-d",str(seconds),"-f","S16_LE","-r","16000","-c","1",path], capture_output=True)
    print(" optaget", flush=True)

def stt(wav_path):
    with open(wav_path,"rb") as f:
        r = requests.post(WHISPER_URL, files={"file":("audio.wav",f,"audio/wav")}, data={"language":"auto"}, timeout=15)
    return r.json().get("text","").strip()

def enrich(prompt):
    try:
        lower = prompt.lower()
        ctx = []
        if any(w in lower for w in ["klokken","tid","uret"]):
            t = requests.get(f"{PROXY_URL}/time", timeout=5).json()
            ctx.append(f"Klokken er {t['human_da']}.")
        if any(w in lower for w in ["vejr","regn","temperatur","grad"]):
            w = requests.get(f"{PROXY_URL}/weather?location=Aarhus", timeout=10).json()
            ctx.append(f"Vejr: {w['temperature_c']}°C, vind {w['wind_kmh']} km/t.")
        if any(w in lower for w in ["hvem er","hvad er","nyheder"]):
            s = requests.get(f"{PROXY_URL}/search?q={requests.utils.quote(prompt)}&n=2", timeout=15).json()
            if s.get("results"):
                ctx.append("Søg: " + " | ".join([f"{r['title']}: {r['content'][:100]}" for r in s["results"]]))
        if ctx:
            return f"Bruger: '{prompt}'. Kontekst: {' '.join(ctx)} Svar på dansk."
    except: pass
    return f"Svar på dansk: {prompt}"

def llm(prompt):
    r = requests.post(OLLAMA_URL, json={"model":OLLAMA_MODEL,"prompt":enrich(prompt),"stream":False}, timeout=60)
    return r.json().get("response","").strip()

def tts(text):
    raw,out = "/tmp/nous_raw.wav","/tmp/nous_speak.wav"
    env = os.environ.copy(); env["ORT_DISABLE_GPU_PROBE"] = "1"
    proc = subprocess.Popen([VENV_PY,"-m","piper","--model",f"{PIPER_DIR}/da.onnx","--config",f"{PIPER_DIR}/da.onnx.json","--output_file",raw], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    proc.stdin.write(text.encode()); proc.stdin.close(); proc.wait()
    subprocess.run(["sox",raw,"-r","48000","-c","2",out], capture_output=True)
    subprocess.run(["aplay",out], capture_output=True)

print("="*50); print(" NOUS Voice Loop v3 — Med Proxy"); print(" Sig noget -> vent -> hør svar"); print(" Ctrl+C for at stoppe"); print("="*50)
with tempfile.TemporaryDirectory() as tmp:
    wav = os.path.join(tmp,"input.wav")
    while True:
        try:
            print("\n [optager]",end="",flush=True); record(wav)
            user = stt(wav)
            if not user: print("  [ingen tekst]"); continue
            print(f"  Dig: {user}")
            print("  [tænker]",end="",flush=True); reply = llm(user); print(" ok")
            print(f"  NOUS: {reply}")
            print("  [svarer]",end="",flush=True); tts(reply); print(" ok")
        except KeyboardInterrupt: print("\n  Farvel!"); break
