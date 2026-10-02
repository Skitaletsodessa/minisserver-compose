#!/bin/bash
# Run ON THE PI (deploy-pi.sh does it). First start of the replica: builds conf/AdGuardHome.yaml from the
# committed example with a freshly generated admin password. The hash comes from AdGuard Home's own install
# API, run in a throw-away container on an EMPTY config dir (with a ready config present AGH would not enter
# install mode). The plaintext password goes ONLY to ~/adguard-admin-password.txt (mode 600), never printed.
# Idempotent: does nothing if the config exists.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=$(awk '/image:/{print $2}' compose.yaml)
TARGET=conf/AdGuardHome.yaml
EXAMPLE=conf/AdGuardHome.yaml.example
PW_FILE=$HOME/adguard-admin-password.txt
[ -e "$TARGET" ] && { echo "setup: $TARGET exists, nothing to do"; exit 0; }
mkdir -p work
PW=$(openssl rand -base64 24 | tr -d '=+/' | head -c 28)
TMP=$(mktemp -d)
docker rm -f agh-setup-temp >/dev/null 2>&1 || true
docker run -d --name agh-setup-temp -p 127.0.0.1:3999:3000 \
  -v "$TMP/conf":/opt/adguardhome/conf -v "$TMP/work":/opt/adguardhome/work "$IMAGE" >/dev/null
trap 'docker rm -f agh-setup-temp >/dev/null 2>&1; sudo rm -rf "$TMP"' EXIT
for i in $(seq 1 60); do curl -sf -o /dev/null http://127.0.0.1:3999/ && break; sleep 1; done
curl -sf -X POST http://127.0.0.1:3999/control/install/configure -H 'Content-Type: application/json' \
  -d "{\"web\":{\"ip\":\"127.0.0.1\",\"port\":3001},\"dns\":{\"ip\":\"127.0.0.1\",\"port\":5399},\"username\":\"admin\",\"password\":\"$PW\"}" >/dev/null
docker stop agh-setup-temp >/dev/null
sudo cat "$TMP/conf/AdGuardHome.yaml" > "$TMP/fresh.yaml"
python3 - "$TMP/fresh.yaml" "$EXAMPLE" "$TARGET" <<'PY'
import sys, yaml
fresh = yaml.safe_load(open(sys.argv[1])); ex = yaml.safe_load(open(sys.argv[2]))
ex["users"] = fresh["users"]
yaml.dump(ex, open(sys.argv[3], "w"), default_flow_style=False, sort_keys=False)
PY
chmod 600 "$TARGET"; echo "$PW" > "$PW_FILE"; chmod 600 "$PW_FILE"
echo "setup: wrote $TARGET and $PW_FILE (mode 600, not printed)"
