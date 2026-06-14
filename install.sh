#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# NOUS — Interaktiv installer
# Platform: Raspberry Pi 5 (aarch64) · Jetson Orin NX (JetPack 6.2)
# Brug:  sudo bash install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -Eeuo pipefail
IFS=$'\n\t'

# ── Farver ────────────────────────────────────────────────────────────────────
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'
B='\033[1;34m'; C='\033[0;36m'; W='\033[1;37m'; N='\033[0m'

hdr()  { echo -e "\n${B}══╡${W} $* ${B}╞══${N}"; }
ok()   { echo -e "  ${G}✓${N}  $*"; }
info() { echo -e "  ${C}·${N}  $*"; }
warn() { echo -e "  ${Y}!${N}  $*"; }
die()  { echo -e "\n${R}FEJL:${N} $*\n" >&2; exit 1; }
ask()  {                          # ask "spørgsmål" [default Y/N]
  local prompt="$1" default="${2:-N}"
  local yn="[y/N]"; [[ "$default" == "Y" ]] && yn="[Y/n]"
  echo -en "  ${W}?${N}  ${prompt} ${yn} "
  read -r ans
  ans="${ans:-$default}"
  [[ "${ans,,}" == "y" ]]
}

# ── Platform-detektion ────────────────────────────────────────────────────────
ARCH=$(uname -m)
[[ "$ARCH" == "aarch64" ]] || die "Kræver aarch64 (ARM64). Fandt: $ARCH"

IS_JETSON=false
IS_PI=false
if [[ -f /etc/nv_tegra_release ]] || grep -qi "tegra\|jetson" /proc/device-tree/compatible 2>/dev/null; then
  IS_JETSON=true
elif grep -qi "Raspberry Pi\|BCM2712" /proc/cpuinfo 2>/dev/null || \
     grep -qi "rpi\|raspberry" /proc/device-tree/model 2>/dev/null; then
  IS_PI=true
fi

if $IS_JETSON; then
  PLATFORM="Jetson Orin NX (JetPack)"
elif $IS_PI; then
  PLATFORM="Raspberry Pi 5"
else
  PLATFORM="Ukendt aarch64"
  warn "Platform ikke genkendt — fortsætter som generisk aarch64."
fi

# ── Root-tjek ─────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Installer skal køres som root:  sudo bash install.sh"

# ── Repo-lokation ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOUS_DIR="/srv/nous"

# Hvis vi IKKE befinder os i en eksisterende NOUS-installation …
if [[ ! -f "$NOUS_DIR/api/main.py" ]]; then
  # Tjek om install.sh selv er inde i repo'et
  if [[ -f "$SCRIPT_DIR/api/main.py" ]]; then
    NOUS_DIR="$SCRIPT_DIR"
  else
    echo
    echo -e "${W}NOUS-filerne er ikke fundet i $NOUS_DIR.${N}"
    echo -en "  ${W}?${N}  GitHub URL til NOUS-repo [https://github.com/din-bruger/nous.git]: "
    read -r REPO_URL
    REPO_URL="${REPO_URL:-https://github.com/din-bruger/nous.git}"
    info "Kloner $REPO_URL → $NOUS_DIR …"
    git clone --depth 1 "$REPO_URL" "$NOUS_DIR" || die "Kunne ikke klone repo."
  fi
fi
ok "NOUS-filer: $NOUS_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# VELKOMST
# ─────────────────────────────────────────────────────────────────────────────
clear
echo -e "${B}"
cat <<'EOF'
  ███╗   ██╗ ██████╗ ██╗   ██╗███████╗
  ████╗  ██║██╔═══██╗██║   ██║██╔════╝
  ██╔██╗ ██║██║   ██║██║   ██║███████╗
  ██║╚██╗██║██║   ██║██║   ██║╚════██║
  ██║ ╚████║╚██████╔╝╚██████╔╝███████║
  ╚═╝  ╚═══╝ ╚═════╝  ╚═════╝ ╚══════╝
EOF
echo -e "${N}  Personlig AI-assistent — Installer v1.0"
echo -e "  Platform: ${C}${PLATFORM}${N}\n"

# ─────────────────────────────────────────────────────────────────────────────
# FEATURE-VALG
# ─────────────────────────────────────────────────────────────────────────────
hdr "FEATURE-VALG"
echo -e "  Vælg hvilke moduler der skal installeres.\n"
echo -e "  ${W}Core${N} installeres altid: API, Arbiter, Qdrant, SearXNG, night pipeline."
echo

ask "Household — hverdagsassistent (madplan, rutiner, kalender)"  N && FEAT_HOUSEHOLD=true  || FEAT_HOUSEHOLD=false
ask "Legal     — juridisk assistent (dokumenter, love, sager)"    N && FEAT_LEGAL=true      || FEAT_LEGAL=false
ask "Legacy    — legat-funktion (interview-svar til fremtiden)"   N && FEAT_LEGACY=true     || FEAT_LEGACY=false
ask "Swarm     — P2P videndeling med andre NOUS-noder"            N && FEAT_SWARM=true      || FEAT_SWARM=false
ask "Voice     — stemmestøtte med faster-whisper (lokal STT)"     N && FEAT_VOICE=true      || FEAT_VOICE=false
ask "Kamera    — kameraintegration (billede-input til AI)"        N && FEAT_KAMERA=true     || FEAT_KAMERA=false

echo
info "Valgte features:"
$FEAT_HOUSEHOLD && info "  + Household" || true
$FEAT_LEGAL     && info "  + Legal"     || true
$FEAT_LEGACY    && info "  + Legacy"    || true
$FEAT_SWARM     && info "  + Swarm"     || true
$FEAT_VOICE     && info "  + Voice"     || true
$FEAT_KAMERA    && info "  + Kamera"    || true
ask "Bekræft installation?" Y || die "Installationen annulleret."

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURATIONSPARAMETRE
# ─────────────────────────────────────────────────────────────────────────────
hdr "KONFIGURATION"

prompt_val() {   # prompt_val "Prompt" "default" → sætter REPLY
  echo -en "  ${W}?${N}  $1 [${C}$2${N}]: "
  read -r REPLY
  REPLY="${REPLY:-$2}"
}

prompt_val "Dit navn (vises i Legacy-mode svar)"   "Dit Navn";    CFG_OWNER_NAME="$REPLY"
prompt_val "Ollama URL (LLM-inferens)"              "http://localhost:11434"; CFG_OLLAMA_URL="$REPLY"
prompt_val "Qdrant URL"                             "http://localhost:6333"; CFG_QDRANT_URL="$REPLY"
prompt_val "Dag-model (hurtig, altid i RAM)"        "qwen2.5:7b";   CFG_LLM_DAY="$REPLY"
prompt_val "Nat-model (stor, on-demand)"            "qwen3:14b";    CFG_LLM_NIGHT="$REPLY"
prompt_val "Embed-model"                            "nomic-embed-text"; CFG_EMBED="$REPLY"

if $FEAT_VOICE; then
  prompt_val "Whisper URL (ekstern — lad stå tom for lokal faster-whisper)" ""; CFG_WHISPER_URL="$REPLY"
  prompt_val "ALSA-lydenhed til mikrofon"            "default"; CFG_AUDIO_DEV="$REPLY"
fi

# ─────────────────────────────────────────────────────────────────────────────
# FORUDSÆTNINGER
# ─────────────────────────────────────────────────────────────────────────────
hdr "FORUDSÆTNINGER"

apt_pkgs=(
  curl wget git jq sqlite3
  python3 python3-venv python3-pip python3-dev
  build-essential libffi-dev libssl-dev
  ffmpeg sox alsa-utils
  nftables
)

$IS_PI    && apt_pkgs+=(libraspberrypi-bin)
$FEAT_KAMERA && $IS_PI    && apt_pkgs+=(python3-picamera2 libcamera-apps)
$FEAT_KAMERA && $IS_JETSON && apt_pkgs+=(v4l-utils)
$FEAT_VOICE  && apt_pkgs+=(portaudio19-dev libasound2-dev)

info "Opdaterer pakkeliste …"
apt-get update -qq

info "Installerer systempakker …"
apt-get install -y --no-install-recommends "${apt_pkgs[@]}" 2>/dev/null | \
  grep -E "^(Setting up|Unpacking)" | sed 's/^/  /' || true
ok "Systempakker installeret."

# Docker
if ! command -v docker &>/dev/null; then
  info "Installerer Docker …"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
  usermod -aG docker nous 2>/dev/null || true
  ok "Docker installeret."
else
  ok "Docker allerede installeret."
fi

# docker compose (plugin)
if ! docker compose version &>/dev/null 2>&1; then
  info "Installerer docker compose plugin …"
  apt-get install -y --no-install-recommends docker-compose-plugin 2>/dev/null | grep "^Setting up" | sed 's/^/  /' || true
fi
ok "Docker Compose: $(docker compose version --short 2>/dev/null || echo 'tilgængelig')"

# ─────────────────────────────────────────────────────────────────────────────
# SYSTEMBRUGER
# ─────────────────────────────────────────────────────────────────────────────
hdr "SYSTEMBRUGER"

if ! id nous &>/dev/null; then
  useradd --system --shell /bin/bash --home /home/nous --create-home nous
  ok "Bruger 'nous' oprettet."
else
  ok "Bruger 'nous' eksisterer allerede."
fi

usermod -aG audio nous 2>/dev/null || true
usermod -aG video nous 2>/dev/null || true
usermod -aG docker nous 2>/dev/null || true
$FEAT_KAMERA && $IS_PI && usermod -aG dialout,plugdev nous 2>/dev/null || true

# Sudoers til nft og systemctl (bruges af nx-install.sh og swarm-firewall.sh)
SUDOERS_FILE=/etc/sudoers.d/nous
if [[ ! -f "$SUDOERS_FILE" ]]; then
  cat > "$SUDOERS_FILE" <<'EOF'
nous ALL=(ALL) NOPASSWD: /usr/sbin/nft, /bin/systemctl
EOF
  chmod 440 "$SUDOERS_FILE"
  ok "Sudoers: nous → nft + systemctl (NOPASSWD)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# MAPPE-STRUKTUR
# ─────────────────────────────────────────────────────────────────────────────
hdr "MAPPE-STRUKTUR"

NOUS_DATA="/mnt/nous-data"

dirs=(
  "$NOUS_DIR"
  "$NOUS_DATA"
  "$NOUS_DATA/qdrant/storage"
  "$NOUS_DATA/qdrant/snapshots"
  "$NOUS_DATA/logs"
  "$NOUS_DATA/arkiv"
  "$NOUS_DATA/searxng"
  "/home/nous/incoming"
)

for d in "${dirs[@]}"; do
  mkdir -p "$d"
  chown nous:nous "$d"
done

# Wing incoming-mapper (fra wings.example.json)
python3 - <<PYEOF 2>/dev/null || true
import json, os, pathlib
wings_file = "$NOUS_DIR/config/wings.example.json"
try:
    wings = json.loads(pathlib.Path(wings_file).read_text())["wings"]
    for w in wings:
        d = pathlib.Path("/home/nous/incoming") / w["name"]
        d.mkdir(exist_ok=True)
        os.chown(d, $(id -u nous), $(id -g nous))
        print(f"  created {d}")
except Exception as e:
    print(f"  (advarsel: {e})")
PYEOF

# Repos / filer ejes af nous
chown -R nous:nous "$NOUS_DIR" 2>/dev/null || true
ok "Mappe-struktur oprettet under $NOUS_DATA"

# ─────────────────────────────────────────────────────────────────────────────
# PYTHON VENVS
# ─────────────────────────────────────────────────────────────────────────────
hdr "PYTHON VENVS"

PYTHON=python3
# Foretrækker python3.13 hvis tilgængeligt
command -v python3.13 &>/dev/null && PYTHON=python3.13

pip_install() {  # pip_install <venv-dir> <pkg1> [pkg2 ...]
  local venv="$1"; shift
  sudo -u nous "$venv/bin/pip" install --quiet --upgrade pip 2>/dev/null || true
  sudo -u nous "$venv/bin/pip" install --quiet "$@"
}

# ── pipeline/.venv ─ bruges af nous-api + night pipeline + ingest ──────────
PIPELINE_VENV="$NOUS_DIR/pipeline/.venv"
if [[ ! -d "$PIPELINE_VENV" ]]; then
  info "Opretter pipeline venv …"
  sudo -u nous "$PYTHON" -m venv "$PIPELINE_VENV"
fi

info "Installerer core-pakker i pipeline venv …"
pip_install "$PIPELINE_VENV" \
  "fastapi>=0.115.0" \
  "uvicorn[standard]>=0.30.0" \
  "httpx>=0.27.0" \
  "qdrant-client>=1.11.0" \
  "watchdog>=6.0.0" \
  "pydantic>=2.0.0" \
  "python-multipart" \
  "langchain-core>=0.2.0" \
  "langgraph>=0.2.0" \
  "requests" \
  "aiosqlite>=0.20.0"

if $FEAT_VOICE; then
  info "Installerer voice-pakker (pipeline venv) …"
  pip_install "$PIPELINE_VENV" \
    "openwakeword>=0.4.0" \
    "silero-vad>=5.0.0" \
    "sounddevice>=0.5.0" \
    "numpy" \
    "faster-whisper>=1.0.0"
fi

ok "pipeline venv klar."

# ── arbiter/.venv ─────────────────────────────────────────────────────────────
ARBITER_VENV="$NOUS_DIR/arbiter/.venv"
if [[ ! -d "$ARBITER_VENV" ]]; then
  info "Opretter arbiter venv …"
  sudo -u nous "$PYTHON" -m venv "$ARBITER_VENV"
fi
info "Installerer arbiter-pakker …"
if [[ -f "$NOUS_DIR/arbiter/requirements.txt" ]]; then
  sudo -u nous "$ARBITER_VENV/bin/pip" install --quiet --upgrade pip 2>/dev/null
  sudo -u nous "$ARBITER_VENV/bin/pip" install --quiet -r "$NOUS_DIR/arbiter/requirements.txt" 2>/dev/null
else
  pip_install "$ARBITER_VENV" \
    "fastapi>=0.115.0" "uvicorn[standard]>=0.30.0" \
    "httpx>=0.27.0" "aiosqlite>=0.20.0"
fi
ok "arbiter venv klar."

# ── app/.venv ─ night pipeline (kuzu + playwright) ────────────────────────────
APP_VENV="$NOUS_DIR/app/.venv"
if [[ ! -d "$APP_VENV" ]]; then
  info "Opretter app (night pipeline) venv …"
  sudo -u nous "$PYTHON" -m venv "$APP_VENV"
fi
info "Installerer night-pipeline pakker (app venv) …"
pip_install "$APP_VENV" "httpx>=0.27.0" "kuzu>=0.6.0"
# Playwright: kun hvis scraper-feature ønskes (tung afhængighed)
if $FEAT_LEGAL || $FEAT_HOUSEHOLD; then
  pip_install "$APP_VENV" "playwright>=1.40.0" || \
    warn "Playwright-installation fejlede — scraper vil ikke virke. Kør manuelt: ${APP_VENV}/bin/playwright install chromium"
fi
ok "app venv klar."

# ── swarm/.venv ───────────────────────────────────────────────────────────────
if $FEAT_SWARM; then
  SWARM_VENV="$NOUS_DIR/swarm/.venv"
  if [[ ! -d "$SWARM_VENV" ]]; then
    info "Opretter swarm venv …"
    sudo -u nous "$PYTHON" -m venv "$SWARM_VENV"
  fi
  info "Installerer swarm-pakker …"
  pip_install "$SWARM_VENV" \
    "fastapi>=0.115.0" "uvicorn[standard]>=0.30.0" \
    "httpx>=0.27.0" "aiosqlite>=0.20.0" "datasketch>=1.5.0"
  ok "swarm venv klar."
fi

# Rettigheder på alle venvs
chown -R nous:nous "$NOUS_DIR" 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURATIONSFILER
# ─────────────────────────────────────────────────────────────────────────────
hdr "KONFIGURATIONSFILER"

# ── .env ─────────────────────────────────────────────────────────────────────
ENV_FILE="$NOUS_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  WHISPER_LINE=""
  $FEAT_VOICE && WHISPER_LINE="NOUS_WHISPER_URL=${CFG_WHISPER_URL}"
  cat > "$ENV_FILE" <<EOF
# NOUS — Miljøvariable (genereret af install.sh)
NOUS_OWNER_NAME=${CFG_OWNER_NAME}
NOUS_OLLAMA_URL=${CFG_OLLAMA_URL}
NOUS_QDRANT_URL=${CFG_QDRANT_URL}
NOUS_LLM_MODEL=${CFG_LLM_DAY}
NOUS_LLM_14B=${CFG_LLM_NIGHT}
NOUS_EMBED_MODEL=${CFG_EMBED}
NOUS_INCOMING_DIR=/home/nous/incoming
NOUS_LLM_7B=${CFG_LLM_DAY}
${WHISPER_LINE}
EOF
  chown nous:nous "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok ".env oprettet."
else
  ok ".env eksisterer allerede — springer over."
fi

# ── config/wings.json ─────────────────────────────────────────────────────────
WINGS_FILE="$NOUS_DIR/config/wings.json"
if [[ ! -f "$WINGS_FILE" ]]; then
  if [[ -f "$NOUS_DIR/config/wings.example.json" ]]; then
    cp "$NOUS_DIR/config/wings.example.json" "$WINGS_FILE"
    chown nous:nous "$WINGS_FILE"
    ok "wings.json oprettet fra example."
  else
    cat > "$WINGS_FILE" <<'EOF'
{
  "wings": [
    {"name": "mit_arkiv", "scope": "PRIVATE", "collection": "mit_arkiv_private", "subcategories": []}
  ]
}
EOF
    chown nous:nous "$WINGS_FILE"
    ok "wings.json oprettet med minimal config."
  fi
else
  ok "wings.json eksisterer allerede."
fi

# ── /mnt/nous-data/model_roles.json ─────────────────────────────────────────
MODEL_ROLES="$NOUS_DATA/model_roles.json"
if [[ ! -f "$MODEL_ROLES" ]]; then
  cat > "$MODEL_ROLES" <<EOF
{
  "day": "${CFG_LLM_DAY}",
  "night": "${CFG_LLM_NIGHT}",
  "day_params": {"temperature": 0.7, "num_ctx": 8192, "num_gpu": 99},
  "night_params": {"temperature": 0.7, "num_ctx": 8192, "num_gpu": 99}
}
EOF
  chown nous:nous "$MODEL_ROLES"
  ok "model_roles.json oprettet."
else
  ok "model_roles.json eksisterer allerede."
fi

# ── /mnt/nous-data/external_keys.json ───────────────────────────────────────
EXT_KEYS="$NOUS_DATA/external_keys.json"
if [[ ! -f "$EXT_KEYS" ]]; then
  cat > "$EXT_KEYS" <<'EOF'
{
  "anthropic": "",
  "groq": "",
  "openai": "",
  "custom": ""
}
EOF
  chown nous:nous "$EXT_KEYS"
  chmod 600 "$EXT_KEYS"
  ok "external_keys.json oprettet (tom — udfyld manuelt)."
else
  ok "external_keys.json eksisterer allerede."
fi

# ── SQLite-databaser ──────────────────────────────────────────────────────────
for db in "intent_bus.db" "swarm_queue.db" "keymap.db"; do
  DBPATH="$NOUS_DATA/$db"
  if [[ ! -f "$DBPATH" ]]; then
    sqlite3 "$DBPATH" "PRAGMA journal_mode=WAL;" 2>/dev/null || touch "$DBPATH"
    chown nous:nous "$DBPATH"
    ok "$db oprettet."
  else
    ok "$db eksisterer allerede."
  fi
done

# ── /mnt/nous-data/swarm_peers.json ─────────────────────────────────────────
if $FEAT_SWARM && [[ ! -f "$NOUS_DATA/swarm_peers.json" ]]; then
  echo '[]' > "$NOUS_DATA/swarm_peers.json"
  chown nous:nous "$NOUS_DATA/swarm_peers.json"
  ok "swarm_peers.json oprettet (tom)."
fi

# ─────────────────────────────────────────────────────────────────────────────
# SYSTEMD SERVICES
# ─────────────────────────────────────────────────────────────────────────────
hdr "SYSTEMD SERVICES"

install_service() {   # install_service <service-name> <source-path>
  local name="$1" src="$2"
  if [[ -f "$src" ]]; then
    cp "$src" "/etc/systemd/system/$name"
    ok "Service installeret: $name"
  else
    warn "Service-fil ikke fundet: $src — springer over."
  fi
}

install_timer() {     # install_timer <timer-name> <source-path>
  local name="$1" src="$2"
  if [[ -f "$src" ]]; then
    cp "$src" "/etc/systemd/system/$name"
    ok "Timer installeret: $name"
  else
    warn "Timer-fil ikke fundet: $src — springer over."
  fi
}

# ── NOUS Qdrant proxy service (sikrer at Qdrant er klar) ────────────────────
cat > /etc/systemd/system/nous-qdrant.service <<'EOF'
[Unit]
Description=NOUS Qdrant (docker container proxy)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/srv/nous
ExecStart=/usr/bin/docker compose up -d qdrant
ExecStop=/usr/bin/docker compose stop qdrant
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
ok "nous-qdrant.service oprettet."

# ── Core: API + Arbiter ───────────────────────────────────────────────────────
install_service nous-api.service     "$NOUS_DIR/api/nous-api.service"
install_service nous-arbiter.service "$NOUS_DIR/arbiter/nous-arbiter.service"

# ── Night pipeline + scraper ──────────────────────────────────────────────────
install_service nous-night-pipeline.service "$NOUS_DIR/scripts/nous-night-pipeline.service"
install_service nous-scraper.service        "$NOUS_DIR/scripts/nous-scraper.service"
install_timer   nous-night-pipeline.timer   "$NOUS_DIR/scripts/nous-night-pipeline.timer"
install_timer   nous-scraper.timer          "$NOUS_DIR/scripts/nous-scraper.timer"

# ── Swarm ─────────────────────────────────────────────────────────────────────
if $FEAT_SWARM; then
  install_service nous-swarm.service      "$NOUS_DIR/swarm/nous-swarm.service"
  install_service nous-swarm-sync.service "$NOUS_DIR/swarm/nous-swarm-sync.service"
  install_timer   nous-swarm-sync.timer   "$NOUS_DIR/swarm/nous-swarm-sync.timer"
fi

# ── Voice ─────────────────────────────────────────────────────────────────────
if $FEAT_VOICE; then
  VOICE_SERVICE=/etc/systemd/system/nous-voice.service
  # Brug eksisterende service-fil hvis den findes, ellers opret den
  if [[ -f "$NOUS_DIR/scripts/nous-voice.service" ]]; then
    install_service nous-voice.service "$NOUS_DIR/scripts/nous-voice.service"
  else
    WHISPER_ENV=""
    [[ -n "${CFG_WHISPER_URL:-}" ]] && WHISPER_ENV="Environment=NOUS_WHISPER_URL=${CFG_WHISPER_URL}"
    cat > "$VOICE_SERVICE" <<EOF
[Unit]
Description=NOUS Voice Assistant (wake-word + STT + TTS)
After=network.target nous-api.service
Wants=nous-api.service

[Service]
Type=simple
User=nous
WorkingDirectory=$NOUS_DIR/scripts
Environment=PATH=$NOUS_DIR/pipeline/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=HOME=/home/nous
Environment=NOUS_AUDIO_DEVICE=${CFG_AUDIO_DEV:-default}
EnvironmentFile=-$NOUS_DIR/.env
${WHISPER_ENV}
ExecStart=$NOUS_DIR/pipeline/.venv/bin/python3 $NOUS_DIR/scripts/voice_assistant.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    ok "nous-voice.service genereret."
  fi

  # Batch ingest (lyd/video-arkiv)
  install_service nous-batch-ingest.service "$NOUS_DIR/scripts/nous-batch-ingest.service"
  install_timer   nous-batch-ingest.timer   "$NOUS_DIR/scripts/nous-batch-ingest.timer"
fi

# ── Kamera ────────────────────────────────────────────────────────────────────
if $FEAT_KAMERA; then
  if [[ -f "$NOUS_DIR/scripts/nous-kamera.service" ]]; then
    install_service nous-kamera.service "$NOUS_DIR/scripts/nous-kamera.service"
  else
    cat > /etc/systemd/system/nous-kamera.service <<EOF
[Unit]
Description=NOUS Kamera-service
After=network.target nous-api.service
Wants=nous-api.service

[Service]
Type=simple
User=nous
WorkingDirectory=$NOUS_DIR/scripts
Environment=PATH=$NOUS_DIR/pipeline/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=HOME=/home/nous
EnvironmentFile=-$NOUS_DIR/.env
ExecStart=$NOUS_DIR/pipeline/.venv/bin/python3 $NOUS_DIR/scripts/kamera.py
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    warn "nous-kamera.service genereret (kamera.py mangler endnu — tilpas scriptet)."
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# DOCKER COMPOSE — Qdrant + SearXNG
# ─────────────────────────────────────────────────────────────────────────────
hdr "DOCKER SERVICES"

if [[ ! -f "$NOUS_DIR/docker-compose.yml" ]]; then
  cat > "$NOUS_DIR/docker-compose.yml" <<'EOF'
services:
  qdrant:
    image: qdrant/qdrant:v1.16.2
    container_name: nous-qdrant
    restart: unless-stopped
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - /mnt/nous-data/qdrant/storage:/qdrant/storage
      - /mnt/nous-data/qdrant/snapshots:/qdrant/snapshots
    environment:
      - RUN_MODE=production
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
    healthcheck:
      test: ["CMD-SHELL", "timeout 3 bash -c '(echo > /dev/tcp/127.0.0.1/6333) 2>/dev/null' || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s

  searxng:
    image: docker.io/searxng/searxng:latest
    container_name: nous-searxng
    restart: unless-stopped
    network_mode: host
    volumes:
      - /mnt/nous-data/searxng:/etc/searxng
    environment:
      - SEARXNG_BASE_URL=http://localhost:8080/
      - SEARXNG_PORT=8080
      - SEARXNG_BIND_ADDRESS=127.0.0.1
EOF
  ok "docker-compose.yml oprettet."
fi

info "Starter Qdrant + SearXNG via Docker Compose …"
cd "$NOUS_DIR"
docker compose pull --quiet 2>/dev/null || warn "Docker pull fejlede — tjek internetforbindelsen."
docker compose up -d 2>/dev/null && ok "Docker containers startet." || warn "Docker Compose fejlede — kør manuelt: cd $NOUS_DIR && docker compose up -d"
cd /

# ─────────────────────────────────────────────────────────────────────────────
# AKTIVER SERVICES
# ─────────────────────────────────────────────────────────────────────────────
hdr "AKTIVERING AF SERVICES"

systemctl daemon-reload

enable_and_start() {
  local svc="$1"
  if [[ -f "/etc/systemd/system/$svc" ]]; then
    systemctl enable --now "$svc" 2>/dev/null && ok "$svc aktiveret og startet." || \
      warn "$svc kunne ikke startes — tjek: journalctl -u $svc"
  fi
}

enable_and_start nous-qdrant.service
# Vent på Qdrant er klar
info "Venter på Qdrant (maks. 30s) …"
for i in $(seq 1 30); do
  curl -sf http://localhost:6333/healthz >/dev/null 2>&1 && break
  sleep 1
done
curl -sf http://localhost:6333/healthz >/dev/null 2>&1 && ok "Qdrant klar." || warn "Qdrant svarer ikke endnu — fortsætter."

enable_and_start nous-arbiter.service
enable_and_start nous-api.service

if $FEAT_SWARM; then
  enable_and_start nous-swarm.service
  systemctl enable nous-swarm-sync.timer 2>/dev/null && ok "nous-swarm-sync.timer aktiveret."
fi

if $FEAT_VOICE; then
  enable_and_start nous-voice.service
  systemctl enable nous-batch-ingest.timer 2>/dev/null && ok "nous-batch-ingest.timer aktiveret."
fi

if $FEAT_KAMERA; then
  if [[ -f /etc/systemd/system/nous-kamera.service ]]; then
    systemctl enable nous-kamera.service 2>/dev/null && info "nous-kamera.service aktiveret (startes ikke automatisk — mangler kamera.py)."
  fi
fi

# Night pipeline og scraper timers (kører altid)
systemctl enable nous-night-pipeline.timer nous-scraper.timer 2>/dev/null && ok "Night pipeline + scraper timers aktiveret."

# ─────────────────────────────────────────────────────────────────────────────
# QDRANT COLLECTIONS SETUP
# ─────────────────────────────────────────────────────────────────────────────
hdr "QDRANT COLLECTIONS"

if [[ -f "$NOUS_DIR/scripts/nous-setup-collections.py" ]]; then
  info "Opretter Qdrant collections …"
  # Vent lidt på at API er klar
  for i in $(seq 1 15); do
    curl -sf http://localhost:8000/status >/dev/null 2>&1 && break
    sleep 2
  done
  sudo -u nous "$NOUS_DIR/pipeline/.venv/bin/python3" \
    "$NOUS_DIR/scripts/nous-setup-collections.py" 2>/dev/null && \
    ok "Qdrant collections oprettet." || \
    warn "Collections setup fejlede — kør manuelt: python3 $NOUS_DIR/scripts/nous-setup-collections.py"
else
  warn "nous-setup-collections.py ikke fundet — spring over."
fi

# ─────────────────────────────────────────────────────────────────────────────
# .GITIGNORE
# ─────────────────────────────────────────────────────────────────────────────
hdr ".GITIGNORE"

GITIGNORE="$NOUS_DIR/.gitignore"
touch "$GITIGNORE"

add_to_gitignore() {
  grep -qxF "$1" "$GITIGNORE" || echo "$1" >> "$GITIGNORE"
}

# Sikkerhedskritiske filer
add_to_gitignore ".env"
add_to_gitignore ".env.local"
add_to_gitignore "config/wings.json"
add_to_gitignore "config/scraper_jobs.json"

# Data-filer der ALDRIG må committes
add_to_gitignore "/mnt/"
add_to_gitignore "mnt/"
add_to_gitignore "*.db"
add_to_gitignore "*.db-shm"
add_to_gitignore "*.db-wal"

# Venvs og cache
add_to_gitignore ".venv/"
add_to_gitignore "pipeline/.venv/"
add_to_gitignore "arbiter/.venv/"
add_to_gitignore "app/.venv/"
add_to_gitignore "swarm/.venv/"
add_to_gitignore "__pycache__/"
add_to_gitignore "*.pyc"

# Modeller
add_to_gitignore "models/"

ok ".gitignore opdateret (external_keys.json og model_roles.json er aldrig i NOUS_DIR)."

# ─────────────────────────────────────────────────────────────────────────────
# KAMERA-SPECIFIK OPSÆTNING
# ─────────────────────────────────────────────────────────────────────────────
if $FEAT_KAMERA; then
  hdr "KAMERA OPSÆTNING"
  if $IS_PI; then
    if command -v raspi-config &>/dev/null; then
      info "Aktiverer kamera via raspi-config …"
      raspi-config nonint do_camera 0 2>/dev/null && ok "Pi kamera aktiveret." || \
        warn "raspi-config kamera-aktivering fejlede — aktiver manuelt i raspi-config."
    fi
    ok "Raspberry Pi kamera konfigureret (picamera2 + libcamera)."
    info "Test kamera med:  libcamera-hello -t 5000"
  elif $IS_JETSON; then
    ok "Jetson kamera: brug V4L2 (/dev/video0) eller tegra-camera via nvarguscamerasrc."
    info "Test kamera med:  v4l2-ctl --list-devices"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# VOICE-SPECIFIK: PIPER TTS MODEL
# ─────────────────────────────────────────────────────────────────────────────
if $FEAT_VOICE; then
  hdr "VOICE / TTS MODEL"
  PIPER_MODEL_DIR="$NOUS_DIR/models/tts"
  mkdir -p "$PIPER_MODEL_DIR"
  chown -R nous:nous "$NOUS_DIR/models"

  if [[ ! -f "$PIPER_MODEL_DIR/da.onnx" ]]; then
    info "Downloader dansk Piper TTS-model …"
    PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/da/da_DK/talesyntese/medium"
    curl -fsSL --retry 3 "$PIPER_BASE/da_DK-talesyntese-medium.onnx" \
      -o "$PIPER_MODEL_DIR/da.onnx" 2>/dev/null && \
    curl -fsSL --retry 3 "$PIPER_BASE/da_DK-talesyntese-medium.onnx.json" \
      -o "$PIPER_MODEL_DIR/da.onnx.json" 2>/dev/null && \
    chown nous:nous "$PIPER_MODEL_DIR/da.onnx" "$PIPER_MODEL_DIR/da.onnx.json" && \
    ok "Piper dansk TTS-model downloadet." || \
    warn "Piper model-download fejlede. Download manuelt fra:
       https://huggingface.co/rhasspy/piper-voices/tree/main/da/da_DK
       Gem som: $PIPER_MODEL_DIR/da.onnx + $PIPER_MODEL_DIR/da.onnx.json"
  else
    ok "Piper TTS-model eksisterer allerede."
  fi

  # Installer piper binary hvis det mangler
  if ! command -v piper &>/dev/null && [[ ! -x "$NOUS_DIR/pipeline/.venv/bin/piper" ]]; then
    info "Downloader piper-tts binary …"
    PIPER_URL="https://github.com/rhasspy/piper/releases/latest/download/piper_linux_aarch64.tar.gz"
    TMP_PIPER=$(mktemp -d)
    curl -fsSL --retry 3 "$PIPER_URL" -o "$TMP_PIPER/piper.tar.gz" 2>/dev/null && \
    tar -xzf "$TMP_PIPER/piper.tar.gz" -C "$TMP_PIPER" 2>/dev/null && \
    install -m 755 "$TMP_PIPER/piper/piper" "$NOUS_DIR/pipeline/.venv/bin/piper" 2>/dev/null && \
    ok "piper binary installeret." || \
    warn "piper binary download fejlede. Installer manuelt fra:
       https://github.com/rhasspy/piper/releases"
    rm -rf "$TMP_PIPER"
  else
    ok "piper binary er tilgængeligt."
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CHECK
# ─────────────────────────────────────────────────────────────────────────────
hdr "OLLAMA CHECK"

OLLAMA_HOST=$(echo "$CFG_OLLAMA_URL" | sed 's|http://||;s|https://||;s|:.*||')
if [[ "$OLLAMA_HOST" == "localhost" ]] || [[ "$OLLAMA_HOST" == "127.0.0.1" ]]; then
  if ! command -v ollama &>/dev/null; then
    if ask "Ollama er ikke installeret. Installer nu?"; then
      info "Installerer Ollama …"
      curl -fsSL https://ollama.com/install.sh | sh && ok "Ollama installeret." || \
        warn "Ollama install fejlede — installer manuelt: https://ollama.com/download"
    fi
  else
    ok "Ollama: $(ollama --version 2>/dev/null || echo 'tilgængeligt')"
  fi

  # Pull modeller
  if command -v ollama &>/dev/null; then
    if ask "Pull dag-model '${CFG_LLM_DAY}' nu? (kan tage lang tid)"; then
      info "Puller ${CFG_LLM_DAY} …"
      sudo -u nous ollama pull "$CFG_LLM_DAY" 2>/dev/null && ok "${CFG_LLM_DAY} klar." || \
        warn "Pull fejlede — kør manuelt: ollama pull ${CFG_LLM_DAY}"
    fi
    if ask "Pull embed-model '${CFG_EMBED}' nu?"; then
      sudo -u nous ollama pull "$CFG_EMBED" 2>/dev/null && ok "${CFG_EMBED} klar." || \
        warn "Pull fejlede — kør manuelt: ollama pull ${CFG_EMBED}"
    fi
  fi
else
  ok "Ollama er konfigureret på ekstern host ($CFG_OLLAMA_URL) — springer lokal install over."
fi

# ─────────────────────────────────────────────────────────────────────────────
# SMOKE-TEST
# ─────────────────────────────────────────────────────────────────────────────
hdr "SMOKE-TEST"

sleep 3   # giv services tid til at starte

TESTS_OK=0; TESTS_FAIL=0
smoke() {    # smoke "beskrivelse" <kommando>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    ok "$desc"
    TESTS_OK=$((TESTS_OK+1))
  else
    warn "$desc — FEJLET"
    TESTS_FAIL=$((TESTS_FAIL+1))
  fi
}

smoke "Qdrant svarer"          curl -sf http://localhost:6333/healthz
smoke "nous-api svarer"        curl -sf http://localhost:8000/status
smoke "nous-arbiter kører"     systemctl is-active --quiet nous-arbiter
smoke "model_roles.json"       test -f "$NOUS_DATA/model_roles.json"
smoke "external_keys.json"     test -f "$NOUS_DATA/external_keys.json"
smoke "intent_bus.db"          test -f "$NOUS_DATA/intent_bus.db"
smoke ".env"                   test -f "$NOUS_DIR/.env"
smoke "wings.json"             test -f "$NOUS_DIR/config/wings.json"
smoke "/home/nous/incoming"    test -d /home/nous/incoming

$FEAT_SWARM  && smoke "nous-swarm kører" systemctl is-active --quiet nous-swarm
$FEAT_VOICE  && smoke "nous-voice.service" test -f /etc/systemd/system/nous-voice.service

# ─────────────────────────────────────────────────────────────────────────────
# FÆRDIG
# ─────────────────────────────────────────────────────────────────────────────
hdr "INSTALLATION FÆRDIG"
echo
echo -e "  ${G}Tests OK:${N}   ${TESTS_OK}"
[[ $TESTS_FAIL -gt 0 ]] && echo -e "  ${R}Tests FEJL:${N} ${TESTS_FAIL}" || true
echo
echo -e "  ${W}Næste skridt:${N}"
echo -e "  1. Udfyld API-nøgler:  ${C}nano $NOUS_DATA/external_keys.json${N}"
echo -e "  2. Tilpas wings:       ${C}nano $NOUS_DIR/config/wings.json${N}"
echo -e "  3. Cockpit UI:         ${C}http://$(hostname -I | awk '{print $1}'):8000${N}"
echo

$FEAT_VOICE && echo -e "  ${W}Voice:${N} Test med  ${C}sudo -u nous journalctl -fu nous-voice${N}"
$FEAT_SWARM && echo -e "  ${W}Swarm:${N} Tilføj peers i  ${C}$NOUS_DATA/swarm_peers.json${N}"
$FEAT_KAMERA && $IS_PI && echo -e "  ${W}Kamera:${N} Implementér $NOUS_DIR/scripts/kamera.py og start nous-kamera.service"

echo
echo -e "  ${B}Logfiler:${N}"
echo -e "  · API:            journalctl -u nous-api -f"
echo -e "  · Nat pipeline:   $NOUS_DATA/logs/night_pipeline.log"
echo -e "  · Scraper:        $NOUS_DATA/logs/scraper.log"
echo
echo -e "  ${G}God fornøjelse med NOUS!${N}"
echo
