#!/bin/bash
# Flush kun NOUS's egne tabeller — bevar Docker's ip/ip6-tabeller så bridge-netværk overlever
nft flush table inet filter 2>/dev/null || true
nft delete table inet filter 2>/dev/null || true

nft add table inet filter
nft add chain inet filter input { type filter hook input priority 0 \; policy drop \; }
nft add chain inet filter forward { type filter hook forward priority 0 \; policy drop \; }
nft add chain inet filter output { type filter hook output priority 0 \; policy accept \; }

# Loopback
nft add rule inet filter input iif lo accept

# Etablerede forbindelser
nft add rule inet filter input ct state established,related accept

# SSH fra LAN
nft add rule inet filter input ip saddr 192.168.1.0/24 tcp dport 22 accept

# Qdrant — localhost ONLY (NOUS API, uid=1000)
nft add rule inet filter input ip saddr 127.0.0.1 tcp dport 6333 accept

# ICMP (ping)
nft add rule inet filter input ip protocol icmp accept

# HTTP + HTTPS fra LAN
nft add rule inet filter input ip saddr 192.168.1.0/24 tcp dport 80 accept
nft add rule inet filter input ip saddr 192.168.1.0/24 tcp dport 443 accept

# Tailscale
nft add rule inet filter input iif tailscale0 accept

# ── Browser-container isolation ───────────────────────────────────────────────
# Browser-containeren kører som root (uid=0) med --network=host.
# Block root-processer fra at nå Qdrant (6333/6334) og den isolerede
# C4B-Qdrant-instans (7333/7334) direkte på loopback.
# NOUS API kører som uid=1000 (nous) og er IKKE berørt.
nft add rule inet filter output meta skuid 0 ip daddr 127.0.0.1 tcp dport 6333 drop
nft add rule inet filter output meta skuid 0 ip daddr 127.0.0.1 tcp dport 6334 drop
nft add rule inet filter output meta skuid 0 ip daddr 127.0.0.1 tcp dport 7333 drop
nft add rule inet filter output meta skuid 0 ip daddr 127.0.0.1 tcp dport 7334 drop
