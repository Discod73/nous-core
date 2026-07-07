#!/usr/bin/env bash
# nous-reset.sh — nulstiller NOUS til clean state
# Bevar: external_keys.json
# Slet: alle Qdrant collections, intent_bus.db, swarm_queue.db, analyzed_*.json
# Reset: model_roles.json til defaults

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Kør som root: sudo $0"
    exit 1
fi

# ── Obligatorisk interaktiv bekræftelse ──────────────────────────────────────
# Dette script sletter ALLE Qdrant collections og er uigenkaldeligt.
# Det MÅ IKKE køres af scripts, CI/CD, eller automatiserede processer.
if [[ ! -t 0 ]]; then
    echo "FEJL: nous-reset.sh kræver en interaktiv terminal (stdin er ikke en tty)."
    echo "Dette forhindrer utilsigtet kørsel fra scripts eller automatisering."
    exit 1
fi

echo ""
echo -e "\033[0;31m╔══════════════════════════════════════════════════════════════╗\033[0m"
echo -e "\033[0;31m║  ADVARSEL: DETTE SLETTER AL DATA I QDRANT PERMANENT          ║\033[0m"
echo -e "\033[0;31m║  Alle collections, alle dokumenter, alle embeddings forsvinder ║\033[0m"
echo -e "\033[0;31m╚══════════════════════════════════════════════════════════════╝\033[0m"
echo ""
echo "  Seneste backup: $(ls -t /mnt/nous-data/backups/*.tar.gz 2>/dev/null | head -1)"
echo ""
read -rp "Skriv 'SLET ALT' for at bekræfte (eller Enter for at afbryde): " CONFIRM
if [[ "$CONFIRM" != "SLET ALT" ]]; then
    echo "Afbrudt — ingen data slettet."
    exit 0
fi
echo ""

QDRANT_URL="http://localhost:6333"
DATA_DIR="/mnt/nous-data"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
step() { echo -e "\n${YELLOW}>>>${NC} $*"; }

# ── 1. Stop services ──────────────────────────────────────────────────────────
step "Stopper NOUS services..."

SERVICES=(
    nous-night-pipeline.timer
    nous-night-pipeline.service
    nous-voice-assistant.service
    nous-swarm.service
    nous-arbiter.service
    nous-api.service
)

for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        systemctl stop "$svc" && ok "Stoppet: $svc"
    else
        warn "Var ikke aktiv: $svc"
    fi
done

sleep 2

# ── 2. Slet alle Qdrant collections ──────────────────────────────────────────
step "Sletter Qdrant collections..."

collections=$(curl -s "${QDRANT_URL}/collections" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); [print(c['name']) for c in d['result']['collections']]" \
    2>/dev/null) || fail "Kunne ikke nå Qdrant på ${QDRANT_URL}"

if [[ -z "$collections" ]]; then
    warn "Ingen collections at slette"
else
    while IFS= read -r col; do
        status=$(curl -s -o /dev/null -w "%{http_code}" \
            -X DELETE "${QDRANT_URL}/collections/${col}")
        if [[ "$status" == "200" ]]; then
            ok "Slettet collection: $col"
        else
            warn "Kunne ikke slette $col (HTTP $status)"
        fi
    done <<< "$collections"
fi

# ── 3. Reset databaser og JSON-filer ─────────────────────────────────────────
step "Nulstiller databaser og JSON-filer..."

# intent_bus.db
rm -f "${DATA_DIR}/intent_bus.db"
python3 -c "import sqlite3; sqlite3.connect('${DATA_DIR}/intent_bus.db').close()"
ok "Genskabt: intent_bus.db"

# swarm_queue.db
rm -f "${DATA_DIR}/swarm_queue.db"
python3 -c "import sqlite3; sqlite3.connect('${DATA_DIR}/swarm_queue.db').close()"
ok "Genskabt: swarm_queue.db"

# analyzed_media.json
echo '{}' > "${DATA_DIR}/analyzed_media.json"
ok "Nulstillet: analyzed_media.json"

# analyzed_audio.json
echo '{}' > "${DATA_DIR}/analyzed_audio.json"
ok "Nulstillet: analyzed_audio.json"

# ── 4. Reset model_roles.json ─────────────────────────────────────────────────
step "Nulstiller model_roles.json til defaults..."

cat > "${DATA_DIR}/model_roles.json" << 'EOF'
{
  "day": "qwen2.5:7b",
  "night": "qwen3:14b",
  "day_params": {},
  "night_params": {}
}
EOF
ok "Nulstillet: model_roles.json"

# ── 5. Verificer at external_keys.json er urørt ───────────────────────────────
if [[ -f "${DATA_DIR}/external_keys.json" ]]; then
    ok "Bevaret: external_keys.json (urørt)"
fi

# ── 6. Genstart services ──────────────────────────────────────────────────────
step "Genstarter NOUS services..."

START_SERVICES=(
    nous-api.service
    nous-arbiter.service
    nous-swarm.service
    nous-voice-assistant.service
    nous-night-pipeline.timer
)

for svc in "${START_SERVICES[@]}"; do
    if systemctl start "$svc" 2>/dev/null; then
        ok "Startet: $svc"
    else
        warn "Kunne ikke starte: $svc"
    fi
done

sleep 3

# ── 7. Statuscheck ────────────────────────────────────────────────────────────
step "Verificerer services..."

CHECK_SERVICES=(
    nous-api.service
    nous-arbiter.service
    nous-swarm.service
    nous-voice-assistant.service
    nous-night-pipeline.timer
)

all_ok=true
for svc in "${CHECK_SERVICES[@]}"; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        ok "Aktiv: $svc"
    else
        state=$(systemctl is-active "$svc" 2>/dev/null || echo "ukendt")
        warn "Ikke aktiv ($state): $svc"
        all_ok=false
    fi
done

echo ""
if $all_ok; then
    echo -e "${GREEN}=== NOUS reset fuldført — alle services kører ===${NC}"
else
    echo -e "${YELLOW}=== NOUS reset fuldført — tjek services ovenfor ===${NC}"
fi
