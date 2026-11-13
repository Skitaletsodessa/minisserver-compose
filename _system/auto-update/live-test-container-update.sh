#!/bin/bash
# Task 35 C2: end-to-end proof of container-update.py on a SCRATCH stack (never touches a real service): a local registry, a toy image in three versions
# (1.0.0 running, 1.0.1 good, 1.0.2 with a failing HEALTHCHECK), a scratch compose file. Proves: dry-run changes nothing, a good update is applied and health-checked,
# a bad one is rolled back byte for byte with the service healthy again, and the compose text is exactly restored.   Run:  bash live-test-container-update.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d /tmp/au-live.XXXXXX)
REG=127.0.0.1:5057
PORT=5057
pass=0; failn=0
ok()  { pass=$((pass + 1)); echo "PASS $*"; }
bad() { failn=$((failn + 1)); echo "FAIL $*"; }
cleanup() {
  for sd in stack own flt; do [ -d "$W/$sd" ] && (cd "$W/$sd" && docker compose down >/dev/null 2>&1); done
  docker rm -f au-test-app au-test-own au-test-flt >/dev/null 2>&1
  docker rmi -f $(docker images --format "{{.Repository}}:{{.Tag}}" | grep -E "^127.0.0.1:5057/|^au-test") >/dev/null 2>&1
  docker rm -f au-test-registry >/dev/null 2>&1
  for v in 1.0.0 1.0.1 1.0.2; do docker rmi -f $REG/test/app:$v >/dev/null 2>&1; done
  rm -rf "$W"
}
[ -z "${KEEP:-}" ] && trap cleanup EXIT

docker rm -f au-test-registry >/dev/null 2>&1
docker run -d --name au-test-registry -p 127.0.0.1:$PORT:5000 registry:2 >/dev/null || { echo "cannot start the local registry"; exit 1; }
for i in $(seq 1 20); do curl -sf http://$REG/v2/ >/dev/null && break; sleep 1; done

mkimg() {   # version healthcheck-command
  d=$(mktemp -d "$W/img.XXXX")
  printf 'FROM busybox:1.36\nRUN echo %s > /v\nHEALTHCHECK --interval=2s --timeout=2s --retries=1 CMD %s\nCMD ["sh","-c","while true; do sleep 1; done"]\n' "$1" "$2" > "$d/Dockerfile"
  docker build -q -t $REG/test/app:$1 "$d" >/dev/null && docker push -q $REG/test/app:$1 >/dev/null
}
mkimg 1.0.0 "cat /v"
mkdir -p "$W/stack"
cat > "$W/stack/compose.yaml" <<EOF
services:
  app:
    image: $REG/test/app:1.0.0
    container_name: au-test-app
    restart: unless-stopped
EOF
cat > "$W/conf.json" <<EOF
{"entries":[{"name":"au-test","dir":"$W/stack","policy":"patch","members":[{"service":"app","image":"$REG/test/app","container":"au-test-app"}],"containers":["au-test-app"],"cmd":"docker exec au-test-app cat /v >/dev/null","timeout":60}]}
EOF
(cd "$W/stack" && docker compose up -d >/dev/null 2>&1)
sleep 6
export NOTIFY="echo ALERT:"
run() { python3 "$HERE/container-update.py" --conf "$W/conf.json" --log "$W/log.jsonl" --force --no-post "$@"; }

# 1. nothing newer
out=$(run); echo "$out" | grep -q "current" && ok "no newer version: current" || bad "current: $out"

# 2. a good 1.0.1 exists: dry-run shows it and changes nothing
mkimg 1.0.1 "cat /v"
cp "$W/stack/compose.yaml" "$W/compose.before"
out=$(run --dry-run); echo "$out" | grep -q "would-update" && ok "dry-run names the update" || bad "dry-run: $out"
cmp -s "$W/stack/compose.yaml" "$W/compose.before" && [ "$(docker inspect -f '{{.Config.Image}}' au-test-app)" = "$REG/test/app:1.0.0" ] && ok "dry-run changed nothing" || bad "dry-run changed something"

# 3. the real update
out=$(run); echo "$out" | grep -q " updated " && ok "the good update is applied" || bad "update: $out"
grep -q "test/app:1.0.1" "$W/stack/compose.yaml" && [ "$(docker inspect -f '{{.Config.Image}}' au-test-app)" = "$REG/test/app:1.0.1" ] && ok "compose and the running container are on 1.0.1" || bad "not on 1.0.1: $(docker inspect -f '{{.Config.Image}}' au-test-app)"
[ "$(docker inspect -f '{{.State.Health.Status}}' au-test-app)" = "healthy" ] && ok "the new container is healthy" || bad "not healthy"
grep -q '"result": "updated"' "$W/log.jsonl" && ok "the outcome is logged" || bad "no log line"

# 4. a bad 1.0.2 (its HEALTHCHECK fails): rolled back, compose restored byte for byte, service healthy again, ONE alert
mkimg 1.0.2 "false"
cp "$W/stack/compose.yaml" "$W/compose.good"
out=$(run); echo "$out" | grep -q "rolled-back" && ok "the bad update is rolled back" || bad "rollback: $out"
cmp -s "$W/stack/compose.yaml" "$W/compose.good" && ok "compose text restored byte for byte" || bad "compose differs after rollback"
[ "$(docker inspect -f '{{.Config.Image}}' au-test-app)" = "$REG/test/app:1.0.1" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' au-test-app)" = "healthy" ] && ok "the service runs 1.0.1 and is healthy again" || bad "service not restored"
[ "$(echo "$out" | grep -c '^ALERT:')" = 1 ] && ok "exactly one alert" || bad "alerts: $(echo "$out" | grep -c ALERT)"
grep -q '"result": "rolled-back"' "$W/log.jsonl" && ok "the rollback is logged" || bad "rollback not logged"

# ── policy `digest`: a floating tag (like jellyfin 12.4 or valkey 9) that moves to a new image ──
mkfloat() {   # healthcheck command; pushes test/flt:1 as a NEW image each time; prints its digest
  d=$(mktemp -d "$W/flt.XXXX")
  printf 'FROM busybox:1.36\nRUN echo %s > /v\nHEALTHCHECK --interval=2s --timeout=2s --retries=1 CMD %s\nCMD ["sh","-c","while true; do sleep 1; done"]\n' "$RANDOM$RANDOM" "$1" > "$d/Dockerfile"
  docker build -q -t $REG/test/flt:1 "$d" >/dev/null && docker push -q $REG/test/flt:1 >/dev/null 2>&1
  docker image inspect --format '{{index .RepoDigests 0}}' $REG/test/flt:1 | cut -d@ -f2
}
mkfloat "cat /v" >/dev/null
mkdir -p "$W/flt"
cat > "$W/flt/compose.yaml" <<EOF
services:
  flt:
    image: $REG/test/flt:1
    container_name: au-test-flt
EOF
cat > "$W/conf3.json" <<EOF
{"entries":[{"name":"au-flt","dir":"$W/flt","policy":"digest","members":[{"service":"flt","image":"$REG/test/flt","container":"au-test-flt"}],"containers":["au-test-flt"],"timeout":60}]}
EOF
(cd "$W/flt" && docker compose up -d >/dev/null 2>&1); sleep 6
run3() { python3 "$HERE/container-update.py" --conf "$W/conf3.json" --log "$W/log3.jsonl" --force --no-post "$@"; }
runimg() { docker inspect -f '{{.Image}}' au-test-flt; }
out=$(run3); echo "$out" | grep -q "current" && ok "digest: an unpinned tag that did not move is current" || bad "digest current: $out"
old_img=$(runimg)
mkfloat "cat /v" >/dev/null
out=$(run3); echo "$out" | grep -q " updated " && [ "$(runimg)" != "$old_img" ] && grep -q "test/flt:1@sha256:" "$W/flt/compose.yaml" && ok "digest: the moved tag is applied and pinned by digest" || bad "digest update: $out"
cp "$W/flt/compose.yaml" "$W/flt.good"; good_img=$(runimg)
mkfloat "false" >/dev/null
out=$(run3); echo "$out" | grep -q "rolled-back" && ok "digest: an unhealthy moved tag is rolled back" || bad "digest rollback: $out"
cmp -s "$W/flt/compose.yaml" "$W/flt.good" && [ "$(runimg)" = "$good_img" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' au-test-flt)" = "healthy" ] && ok "digest: compose restored, the previous image runs and is healthy" || bad "digest rollback state: $(runimg) vs $good_img"
(cd "$W/flt" && docker compose down >/dev/null 2>&1); docker rmi -f $REG/test/flt:1 >/dev/null 2>&1

# ── policy `build`: our own image on a digest-pinned base ──
mkbase() {   # commands for the base image; pushes it as test/base:1 (a NEW digest each time); prints the digest on the last line
  d=$(mktemp -d "$W/base.XXXX")
  printf 'FROM busybox:1.36\nRUN %s\n' "$1" > "$d/Dockerfile"
  docker build -q -t $REG/test/base:1 "$d" >/dev/null && docker push -q $REG/test/base:1 >/dev/null 2>&1
  docker image inspect --format '{{index .RepoDigests 0}}' $REG/test/base:1 | cut -d@ -f2
}
D1=$(mkbase "echo a > /ok && echo a > /health" | tail -1)
mkdir -p "$W/own"
printf 'FROM %s/test/base@%s\nHEALTHCHECK --interval=2s --timeout=2s --retries=1 CMD cat /health\nCMD ["sh","-c","while true; do sleep 1; done"]\n' "$REG" "$D1" > "$W/own/Dockerfile"
cat > "$W/own/compose.yaml" <<EOF
services:
  own:
    build: .
    image: au-test-own:local
    container_name: au-test-own
EOF
cat > "$W/conf2.json" <<EOF
{"entries":[{"name":"au-own","dir":"$W/own","policy":"build","base_tag":"$REG/test/base:1","service":"own","image":"au-test-own:local","smoke":"docker run --rm --entrypoint sh {image} -c 'cat /ok'","containers":["au-test-own"],"timeout":60}]}
EOF
(cd "$W/own" && docker compose up -d --build >/dev/null 2>&1); sleep 6
run2() { python3 "$HERE/container-update.py" --conf "$W/conf2.json" --log "$W/log2.jsonl" --force --no-post "$@"; }
out=$(run2); echo "$out" | grep -q "current" && ok "build: base unchanged -> current" || bad "build current: $out"
D2=$(mkbase "echo b > /ok && echo b > /health" | tail -1)
before=$(docker inspect -f '{{.Image}}' au-test-own)
out=$(run2 --dry-run); echo "$out" | grep -q "would-update" && ok "build: dry-run names the new base" || bad "build dry-run: $out"
out=$(run2); echo "$out" | grep -q " updated " && ok "build: new base rebuilt and deployed" || bad "build update: $out"
grep -q "$D2" "$W/own/Dockerfile" && [ "$(docker inspect -f '{{.Image}}' au-test-own)" != "$before" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' au-test-own)" = "healthy" ] && ok "build: Dockerfile on the new digest, new image healthy" || bad "build state wrong"
# a base whose smoke test fails: nothing is deployed, Dockerfile byte-identical, old container untouched
cp "$W/own/Dockerfile" "$W/df.good"; cur=$(docker inspect -f '{{.Image}}' au-test-own)
D3=$(mkbase "echo c > /health" | tail -1)
out=$(run2); echo "$out" | grep -q " failed " && ok "build: a failing smoke test is not deployed" || bad "smoke: $out"
cmp -s "$W/own/Dockerfile" "$W/df.good" && [ "$(docker inspect -f '{{.Image}}' au-test-own)" = "$cur" ] && ok "build: Dockerfile restored, old container untouched" || bad "smoke rollback wrong"
[ "$(echo "$out" | grep -c '^ALERT:')" = 1 ] && ok "build: one alert for the failed smoke test" || bad "smoke alerts"
# a base that passes the smoke test but breaks the health check after deployment: rolled back, previous image again
D4=$(mkbase "echo d > /ok" | tail -1)
out=$(run2); echo "$out" | grep -q "rolled-back" && ok "build: a failing health check is rolled back" || bad "health rollback: $out"
cmp -s "$W/own/Dockerfile" "$W/df.good" && [ "$(docker inspect -f '{{.State.Health.Status}}' au-test-own)" = "healthy" ] && ok "build: previous Dockerfile and a healthy service again" || bad "health rollback state: $(docker inspect -f '{{.State.Health.Status}}' au-test-own)"
[ -n "${KEEP:-}" ] || { (cd "$W/own" && docker compose down >/dev/null 2>&1); docker rmi -f au-test-own:local $REG/test/base:1 >/dev/null 2>&1; }

echo "$pass passed, $failn failed"
[ "$failn" = 0 ]
