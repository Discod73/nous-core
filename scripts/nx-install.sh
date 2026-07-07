#!/usr/bin/env bash
# nx-install.sh — åbn NX's air-gap midlertidigt, kør install-kommando, luk igen
# Brug: nx-install.sh '<shell-kommando til NX>'
# Kræver: nous@NX har NOPASSWD sudo til nft og systemctl nftables
#
# NX air-gap virker ved at input-chain dropper alt undtagen Pi5-IP.
# Output er allerede accept, men returnpakker droppes uden ct-rule.
# Flush åbner fuldstændigt — /etc/nftables.conf genindsættes bagefter.

set -euo pipefail

NX="${NOUS_NX_HOST:?Sæt NOUS_NX_HOST (f.eks. nous@192.168.x.x)}"
NFT_CONF="/etc/nftables.conf"
INSTALL_CMD="${1:-}"

if [[ -z "$INSTALL_CMD" ]]; then
    echo "Fejl: angiv en install-kommando som argument"
    echo "  Eksempel: $0 'apt-get install -y curl'"
    exit 1
fi

cleanup() {
    local exit_code=$?
    echo ""
    echo ">>> [4/4] Genaktiverer air-gap (reload nftables fra $NFT_CONF)..."
    if ssh -t -o BatchMode=yes "$NX" "sudo nft -f $NFT_CONF"; then
        echo "    Air-gap genaktiveret."
    else
        echo "ADVARSEL: nftables reload fejlede — NX er muligvis åben! Genstart manuelt:"
        echo "  ssh $NX 'sudo systemctl restart nftables'"
    fi
    exit $exit_code
}
trap cleanup EXIT

echo ">>> [1/4] Finder eksisterende nftables-regler på NX..."
ssh -t -o BatchMode=yes "$NX" "sudo nft list ruleset" 2>&1 | head -30
echo ""

echo ">>> [1/4] Åbner air-gap: flush nftables ruleset på NX..."
ssh -t -o BatchMode=yes "$NX" "sudo nft flush ruleset"
echo "    Ruleset flushet."

echo ""
echo ">>> [2/4] Verificerer internet-adgang på NX..."
if ssh -t -o BatchMode=yes "$NX" "ping -c1 -W3 8.8.8.8 > /dev/null 2>&1"; then
    echo "    OK — NX kan nå 8.8.8.8"
else
    echo "FEJL: NX kan ikke nå 8.8.8.8 efter flush — tjek gateway/routing på NX"
    exit 2
fi

echo ""
echo ">>> [3/4] Kører kommando på NX: $INSTALL_CMD"
echo "------------------------------------------------------------"
ssh -t -o BatchMode=yes "$NX" "$INSTALL_CMD"
echo "------------------------------------------------------------"
echo "    Kommando afsluttet."

# cleanup() kaldes automatisk via trap og genindsætter air-gap
