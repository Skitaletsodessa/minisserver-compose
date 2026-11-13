#!/bin/bash
# Task 35 C tests for reboot-if-needed.sh and post-update-run.sh with fakes (no real reboot, no Telegram, no git). Run: bash test-auto-update.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"
pass=0; failn=0
ok()   { pass=$((pass + 1)); echo "PASS $*"; }
bad()  { failn=$((failn + 1)); echo "FAIL $*"; }
# fake dig: answers 192.0.2.1 unless FAKE_DIG=dead
cat > "$T/bin/dig" <<'EOF'
#!/bin/bash
[ "${FAKE_DIG:-ok}" = dead ] && exit 0
echo 192.0.2.1
EOF
# fake notifier: records its arguments
cat > "$T/notify" <<EOF
#!/bin/bash
echo "\$*" >> "$T/notify.log"
EOF
chmod +x "$T/bin/dig" "$T/notify"
export PATH="$T/bin:$PATH"
env_common=(DEFAULTS_FILE=/dev/null NAME=testhost PEER_DNS=192.0.2.9 NOTIFY="$T/notify" STATE_DIR="$T/state" PEER_RETRY_WAIT=0 REQ_FILE="$T/reboot-required" DRY=1)

# 1. nothing to do
: > "$T/notify.log"; rm -f "$T/reboot-required"
out=$(env "${env_common[@]}" bash "$HERE/reboot-if-needed.sh")
echo "$out" | grep -q "no reboot required" && ok "no reboot-required: nothing happens" || bad "no reboot-required: $out"
grep -q "clear reboot-postponed-testhost" "$T/notify.log" && ok "a stale postponement alert is cleared" || bad "clear not sent"

# 2. reboot required, peer answers: it would reboot (dry run) and remembers why
: > "$T/notify.log"; echo "*** System restart required ***" > "$T/reboot-required"; printf 'linux-image-6.12.116\n' > "$T/reboot-required.pkgs"
out=$(env "${env_common[@]}" bash "$HERE/reboot-if-needed.sh")
echo "$out" | grep -q "DRY RUN - would reboot now (packages: linux-image-6.12.116)" && ok "peer answers: reboot would happen, packages named" || bad "reboot path: $out"
grep -q '"reason_pkgs": "linux-image-6.12.116"' "$T/state/pending-reboot.json" && ok "the pending-reboot record names the package" || bad "no pending record"
[ ! -s "$T/notify.log" ] && ok "no message when everything is fine" || bad "unexpected message: $(cat "$T/notify.log")"

# 3. reboot required, the peer's AdGuard does not answer: postponed + ONE tier-B alert, no reboot
: > "$T/notify.log"; rm -f "$T/state/pending-reboot.json"
out=$(env "${env_common[@]}" FAKE_DIG=dead bash "$HERE/reboot-if-needed.sh")
echo "$out" | grep -q "postponed" && ok "dead peer DNS: postponed" || bad "postpone: $out"
grep -q "alert --tier B reboot-postponed-testhost" "$T/notify.log" && ok "dead peer DNS: tier-B alert" || bad "no alert: $(cat "$T/notify.log")"
[ ! -e "$T/state/pending-reboot.json" ] && ok "dead peer DNS: nothing recorded as rebooting" || bad "recorded a reboot"

# 4. outside the window (not DRY): left for the next slot
: > "$T/notify.log"
out=$(env "${env_common[@]}" DRY=0 LATEST_HHMM=0000 bash "$HERE/reboot-if-needed.sh")
echo "$out" | grep -q "left for the next slot" && ok "after the latest time: left for the next slot" || bad "window: $out"

# 5. post-update-run: OK path and FAIL path
cat > "$T/check-ok" <<'EOF'
#!/bin/bash
echo "== x"; echo "  OK    fine"; exit 0
EOF
cat > "$T/check-fail" <<'EOF'
#!/bin/bash
echo "== x"; echo "  OK    fine"; echo "  FAIL  public door answered 502 (expected 200)"; echo "  FAIL  HA listens on: 0.0.0.0:8123"; exit 2
EOF
chmod +x "$T/check-ok" "$T/check-fail"
cat > "$T/apt-history" <<'EOF'
Start-Date: 2026-11-10  06:03:10
Commandline: /usr/bin/unattended-upgrade
Upgrade: vim-tiny:amd64 (2:9.1.1230-2+deb13u1, 2:9.1.1230-2+deb13u2), zlib1g:amd64 (1:1.3.dfsg+really1.3.1-1+b1, 1:1.3.dfsg+really1.3.1-1+deb13u1)
End-Date: 2026-11-10  06:03:40
EOF
echo '{"at": "2026-11-10 11:00:05", "service": "caddy", "result": "updated", "to": "sha256:abcdef0123456789abcdef"}' > "$T/ctr.jsonl"
printf '{"at":"x","reason_pkgs":"p","kernel_before":"6.12.115"}' > "$T/state/pending-reboot.json"
common=(DEFAULTS_FILE=/dev/null NOTIFY="$T/notify" STATE_DIR="$T/state" SUMMARY="$T/summary.log" APT_HISTORY="$T/apt-history" UPDLOG="$T/ctr.jsonl" AU_DIR="$HERE" NOGIT=1)
: > "$T/notify.log"; rm -f "$T/state/last-run"
out=$(env "${common[@]}" CHECK="$T/check-ok" bash "$HERE/post-update-run.sh" boot)
echo "$out" | grep -q "check: OK" && echo "$out" | grep -q "vim-tiny" && echo "$out" | grep -q "caddy" && ok "summary line names apt and container updates, check OK" || bad "summary: $out"
grep -q "clear post-update" "$T/notify.log" && ok "OK run clears the alert" || bad "no clear"
: > "$T/notify.log"; rm -f "$T/state/last-run"
out=$(env "${common[@]}" CHECK="$T/check-fail" bash "$HERE/post-update-run.sh" apt)
echo "$out" | grep -q "check: FAIL(2)" && ok "FAIL run: summary says FAIL(2)" || bad "fail summary: $out"
grep -q "alert --tier B post-update-" "$T/notify.log" && grep -q "vim-tiny" "$T/notify.log" && grep -q "502" "$T/notify.log" && ok "FAIL run: ONE tier-B message naming the update and the failure" || bad "fail alert: $(cat "$T/notify.log")"
[ "$(wc -l < "$T/notify.log")" = 1 ] && ok "exactly one message" || bad "message count: $(wc -l < "$T/notify.log")"
# the marker moves: a second run sees nothing new
: > "$T/notify.log"
out=$(env "${common[@]}" CHECK="$T/check-ok" bash "$HERE/post-update-run.sh" manual)
echo "$out" | grep -q "apt: - | containers: - " && ok "nothing new since the last run: '-'" || bad "marker: $out"

echo "$pass passed, $failn failed"
[ "$failn" = 0 ]
