#!/bin/sh
# Task 28 rule 2: assert, from the kernel's own socket table (not from compose or docker inspect), that the gateway listens on loopback ONLY
# and that the only other socket on its public port is Caddy's tailnet-address site. Run after EVERY start/restart of the gateway or Caddy.
#   exit 0  both assertions hold      exit 1  something listens where it must not (the offending lines are printed)
LOOP=127.0.0.1:17444
TAILNET=100.76.61.8:17443
bad=0
# 1. port 17444 (the gateway): exactly 127.0.0.1:17444, nothing on 0.0.0.0, [::], the LAN or the tailnet
l1=$(ss -Htln 2>/dev/null | awk '{print $4}' | grep -E ':17444$')
if [ "$l1" != "$LOOP" ]; then echo "FAIL gateway port 17444 listeners: [$l1] (want exactly $LOOP)"; bad=1; fi
# 2. port 17443 (Caddy's tailnet site): exactly the tailnet address, TCP (HTTP/3 is UDP and is checked below)
l2=$(ss -Htln 2>/dev/null | awk '{print $4}' | grep -E ':17443$')
if [ "$l2" != "$TAILNET" ]; then echo "FAIL port 17443 TCP listeners: [$l2] (want exactly $TAILNET)"; bad=1; fi
u2=$(ss -Huln 2>/dev/null | awk '{print $4}' | grep -E ':17443$' | sort -u)
if [ -n "$u2" ] && [ "$u2" != "$TAILNET" ]; then echo "FAIL port 17443 UDP listeners: [$u2] (want $TAILNET or none)"; bad=1; fi
[ $bad -eq 0 ] && echo "OK gateway $LOOP only; Caddy site $TAILNET only"
exit $bad
