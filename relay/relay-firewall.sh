#!/bin/bash
# Task 23 rule 2, network layer: the relay container (172.30.0.0/24) may reach the internet and nothing else.
# Idempotent; run by relay-firewall.service after Docker starts (and again whenever Docker restarts).
#
# FOUND BY THE ATTACK SUITE: iptables rules in DOCKER-USER are NOT enough. Tailscale's own `ts-forward` chain sits
# in front of DOCKER-USER and ACCEPTs everything forwarded out of tailscale0, so the container could reach the
# tailnet (100.100.100.100, other nodes) straight through the "block". The primary lock is therefore an nftables
# table hooked at priority -10, i.e. evaluated BEFORE any iptables-nft chain (priority 0), whatever order Tailscale
# or Docker insert their chains in. The iptables rules stay as a second, redundant layer.
#
#   forward : container -> anywhere private/special : drop        (this also covers the tailnet 100.64.0.0/10)
#   input   : container -> the host itself (any of its addresses): NEW connections dropped; replies to the published
#             loopback port are ESTABLISHED and unaffected
# The application layer (relay.py) refuses the same addresses before it ever connects - that is the first lock.
set -euo pipefail
SUBNET=172.30.0.0/24
PRIVATE="0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4"

nft delete table inet relay_guard 2>/dev/null || true
nft -f - <<NFT
table inet relay_guard {
  chain forward {
    type filter hook forward priority -10; policy accept;
    ip saddr $SUBNET ip daddr { $(echo $PRIVATE | sed 's/ /, /g') } counter drop
  }
  chain input {
    type filter hook input priority -10; policy accept;
    ip saddr $SUBNET ct state new counter drop
  }
}
NFT

# second layer (iptables-nft): same policy in DOCKER-USER and INPUT
iptables -N RELAY-EGRESS 2>/dev/null || iptables -F RELAY-EGRESS
for d in $PRIVATE; do iptables -A RELAY-EGRESS -d "$d" -j DROP; done
iptables -A RELAY-EGRESS -j RETURN
iptables -C DOCKER-USER -s "$SUBNET" -j RELAY-EGRESS 2>/dev/null || iptables -I DOCKER-USER 1 -s "$SUBNET" -j RELAY-EGRESS
iptables -C INPUT -s "$SUBNET" -m conntrack --ctstate NEW -j DROP 2>/dev/null || iptables -I INPUT 1 -s "$SUBNET" -m conntrack --ctstate NEW -j DROP
echo "relay-firewall: nft table relay_guard + iptables layer in place"
