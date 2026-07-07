#!/bin/bash
nft flush ruleset

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

# Qdrant — localhost ONLY
nft add rule inet filter input ip saddr 127.0.0.1 tcp dport 6333 accept

# ICMP (ping)
nft add rule inet filter input ip protocol icmp accept
firewall
