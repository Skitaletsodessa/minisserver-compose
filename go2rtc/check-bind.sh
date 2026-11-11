#!/bin/sh
# Task 34: assert from the kernel's socket table that go2rtc listens on 127.0.0.1 ONLY (API 18984, restream 18554) and that no other go2rtc port
# (8555 WebRTC; 8443 is Tailscale Funnel on the tailnet address, not ours) exists anywhere. Run after EVERY start/restart of the container.  exit 0 = ok, 1 = a listener where it must not be.
bad=0
for p in 18984 18554; do
  l=$(ss -Htln 2>/dev/null | awk '{print $4}' | grep -E ":$p\$")
  if [ "$l" != "127.0.0.1:$p" ]; then echo "FAIL port $p listeners: [$l] (want exactly 127.0.0.1:$p)"; bad=1; fi
done
for p in 8555 1984 8554; do
  l=$(ss -Htuln 2>/dev/null | awk '{print $5}' | grep -E ":$p\$" | grep -v -E '^127\.0\.0\.1:')
  if [ -n "$l" ]; then echo "FAIL unexpected host listener on port $p: [$l]"; bad=1; fi
done
[ $bad -eq 0 ] && echo "OK go2rtc 127.0.0.1:18984 and 127.0.0.1:18554 only"
exit $bad
