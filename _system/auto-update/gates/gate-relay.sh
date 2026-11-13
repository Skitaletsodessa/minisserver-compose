#!/bin/bash
# Task 35b-2: attack suite of the fetch relay against a rebuilt image BEFORE it replaces production.
#   1. unit_attacks.py (41 checks, in process) runs with the NEW image's python.
#   2. two throw-away containers of the new image with production's hardening (read-only, no caps, limits, the relay network so relay_guard applies; state in tmpfs,
#      fresh RANDOM secrets, production's non-secret limits): "relay-gate" with the real allow-list on 127.0.0.1:18092, "relay-gate-test" with the test allow-list on :18093;
#      blackbox_attacks.py runs against them (BB_* variables). Production (port 18090, container `relay`, its state and secret) is never touched.
# usage: gate-relay.sh IMAGE      exit 0 = all green. Never run with -x (it handles the throw-away secrets).
IMG=${1:?image}
R=/srv/compose/relay
W=$(mktemp -d /tmp/relay-gate.XXXXXX)
cleanup() { docker rm -f relay-gate relay-gate-test >/dev/null 2>&1; rm -rf "$W"; }
trap cleanup EXIT
docker rm -f relay-gate relay-gate-test >/dev/null 2>&1

echo "== unit attacks (new image's python)"
docker run --rm --user 10001 -e PYTHONDONTWRITEBYTECODE=1 -v $R:$R:ro -w $R --entrypoint python "$IMG" tests/unit_attacks.py > "$W/unit.out" 2>&1
rc=$?; tail -3 "$W/unit.out"
[ $rc -eq 0 ] || { grep FAIL "$W/unit.out" | head -5; exit 1; }

mkenv() {   # source env file -> throw-away env file: same limits, random secret, no proxy trust
  { grep -E '^RELAY_(DAILY_CAP|RATE_PER_SEC|BURST|HOST_CONCURRENCY)=' "$1"; echo "RELAY_SECRET=$(openssl rand -hex 32)"; } > "$2"; chmod 600 "$2"
}
mkenv $R/.env "$W/env.prod"
mkenv $R/.env.test "$W/env.test"
start() {   # name port envfile allowlist
  docker run -d --name "$1" --network relay_relay-net --read-only --tmpfs /tmp:size=16m,noexec,nosuid,nodev --tmpfs /state:uid=10001,gid=10001,size=16m \
    --cap-drop ALL --security-opt no-new-privileges:true --memory 192m --cpus 0.5 --pids-limit 64 --dns 1.1.1.1 --dns 8.8.8.8 \
    -p 127.0.0.1:$2:8080 --env-file "$3" -v "$4":/app/allowlist.txt:ro "$IMG" >/dev/null
}
start relay-gate 18092 "$W/env.prod" $R/allowlist.txt || { echo "gate: cannot start relay-gate"; exit 2; }
start relay-gate-test 18093 "$W/env.test" $R/allowlist.test.txt || { echo "gate: cannot start relay-gate-test"; exit 2; }
for c in relay-gate relay-gate-test; do
  up=0
  for i in $(seq 1 30); do
    docker exec $c python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" >/dev/null 2>&1 && { up=1; break; }
    sleep 1
  done
  [ $up -eq 1 ] || { echo "gate: $c did not become healthy"; docker logs --tail 5 $c 2>&1; exit 2; }
done

echo "== black-box attacks (throw-away instances)"
BB_PROD_PORT=18092 BB_TEST_PORT=18093 BB_PROD_ENV="$W/env.prod" BB_TEST_ENV="$W/env.test" BB_PROD_CONTAINER=relay-gate BB_TEST_CONTAINER=relay-gate-test \
  python3 $R/tests/blackbox_attacks.py > "$W/bb.out" 2>&1
rc=$?; tail -2 "$W/bb.out"
[ $rc -eq 0 ] || { grep FAIL "$W/bb.out" | head -8; exit 1; }
echo "gate: relay green"
