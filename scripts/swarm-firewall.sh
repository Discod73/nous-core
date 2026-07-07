#!/usr/bin/env bash
# swarm-firewall.sh — nftables for NOUS (Pi 5)
#
# Ansvarsfordeling:
#   nftables     → LAN-adgang og NX-forward-blokering
#   Tailscale ACL → port-adgang per enhed på tailscale0
#
# Interface-layout:
#   tailscale0      = Tailscale VPN
#   eth0            = LAN + vej til NX
#   br-d24c46fa85e4 = Docker-bridge (Qdrant m.fl.)

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

TAILSCALE_IF="tailscale0"
NX_IF="eth0"
DOCKER_BR="br-d24c46fa85e4"

echo "[1/4] Verificerer interfaces..."
for iface in "$TAILSCALE_IF" "$NX_IF"; do
    if ! ip link show "$iface" &>/dev/null; then
        echo "FEJL: interface $iface eksisterer ikke — afbryder"
        exit 1
    fi
done
echo "    OK: $TAILSCALE_IF og $NX_IF fundet"

echo ""
echo "[2/4] Rydder op (gammel konfiguration)..."
nft delete table inet nous-swarm 2>/dev/null && echo "    nous-swarm slettet" || echo "    nous-swarm fandtes ikke — OK"

# Gammel script satte inet filter input til policy drop med Tailscale-regler.
# nous-swarm på prioritet -100 filtrerer alene; pakker der accepteres derfra
# fortsætter til inet filter (prioritet 0) og må ikke blokeres der.
if nft list chain inet filter input &>/dev/null; then
    nft flush chain inet filter input
    nft add rule inet filter input accept
    echo "    inet filter input: flushed + accept-all (transparent)"
else
    echo "    inet filter input fandtes ikke — OK"
fi

echo ""
echo "[3/4] Opretter nous-swarm tabel..."
nft -f - <<EOF
table inet nous-swarm {

    chain input {
        # Prioritet -100: kører FØR inet filter (0) og ts-input
        type filter hook input priority -100; policy drop;

        ct state invalid drop
        ct state established,related accept
        iif "lo" accept

        # Tailscale ACL styrer hvem der må — nftables stoler på det
        iif "${TAILSCALE_IF}" accept

        # Docker-bridges
        iif "${DOCKER_BR}" accept
        iif "docker0" accept

        # LAN: kun nødvendige porte
        ip saddr 192.168.1.0/24 tcp dport 22   accept
        ip saddr 127.0.0.1      tcp dport 6333  accept
        ip saddr 192.168.1.0/24 tcp dport 8384  accept
        ip saddr 192.168.1.0/24 tcp dport 8000  accept
        ip saddr 192.168.1.0/24 tcp dport 80    accept
        ip saddr 192.168.1.0/24 tcp dport 443   accept
        ip protocol icmp accept

        # Alt andet droppes (policy)
    }

    chain forward {
        # Prioritet -100: kører FØR ts-forward
        type filter hook forward priority -100; policy drop;

        ct state established,related accept

        # KRITISK: swarm-peer kan aldrig nå NX via Pi 5
        iif "${TAILSCALE_IF}" oif "${NX_IF}" drop

        # LAN og Docker kan nå NX
        iif "${NX_IF}"     oif "${NX_IF}"     accept
        iif "${DOCKER_BR}" oif "${NX_IF}"     accept
        iif "docker0"      oif "${NX_IF}"     accept

        # Alt andet droppes (policy)
    }
}
EOF
echo "    nous-swarm tabel aktiv"

echo ""
echo "[4/4] Verificerer..."
echo ""
echo "=== nous-swarm: input ==="
nft list chain inet nous-swarm input
echo ""
echo "=== nous-swarm: forward ==="
nft list chain inet nous-swarm forward
echo ""
echo "============================================================"
echo " Firewall aktiv."
echo ""
echo " Test manuelt:"
echo "   Fra Tailscale-peer:  curl http://<TAILSCALE_IP>:8020/swarm/health"
echo "                        → Tailscale ACL bestemmer"
echo "   Fra Tailscale-peer:  curl http://<NX_IP>:11434"
echo "                        → skal fejle (NX ureachable via Pi 5)"
echo "   Fra LAN:             ssh pi@<LAN_IP>"
echo "                        → skal virke"
echo ""
echo " For persistens: kald fra /etc/rc.local eller en systemd-unit"
echo "============================================================"
