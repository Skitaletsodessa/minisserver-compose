#!/bin/bash
# Regenerates /srv/compose/adguard/conf/AdGuardHome.yaml from the committed
# AdGuardHome.yaml.example, with a freshly generated admin password.
#
# Uses AdGuardHome's own install API (via a disposable container) to produce the
# password hash, rather than reimplementing bcrypt - guaranteed compatible with
# whatever AGH itself expects, not an assumption about hash format/cost.
#
# The plaintext password is written ONLY to /home/skit/adguard-admin-password.txt
# (mode 600) for Ivan to read directly on the server - never printed here, never
# committed, never put in a report.
set -euo pipefail

CONF_DIR=/srv/compose/adguard/conf
EXAMPLE="$CONF_DIR/AdGuardHome.yaml.example"
TARGET="$CONF_DIR/AdGuardHome.yaml"
IMAGE="adguard/adguardhome:v0.107.79"
PW_FILE=/home/skit/adguard-admin-password.txt

if [ -e "$TARGET" ]; then
  echo "Refusing to overwrite existing $TARGET - remove it first if you really want a fresh config." >&2
  exit 1
fi

PW=$(openssl rand -base64 24 | tr -d '=+/' | head -c 28)

TMPWORK=$(mktemp -d)
cp "$EXAMPLE" "$TARGET"
chmod 600 "$TARGET"

docker run -d --name agh-setup-temp \
  -p 127.0.0.1:3999:3000 \
  -v "$CONF_DIR":/opt/adguardhome/conf \
  -v "$TMPWORK":/opt/adguardhome/work \
  "$IMAGE" >/dev/null

sleep 3

curl -sf -X POST http://127.0.0.1:3999/control/install/configure \
  -H 'Content-Type: application/json' \
  -d "{\"web\":{\"ip\":\"127.0.0.1\",\"port\":3001},\"dns\":{\"ip\":\"127.0.0.1\",\"port\":5399},\"username\":\"admin\",\"password\":\"$PW\"}" \
  >/dev/null

docker stop agh-setup-temp >/dev/null
docker rm agh-setup-temp >/dev/null
rm -rf "$TMPWORK"

# install/configure only wrote the user/hash and the (temporary) web/dns addresses
# it was told - restore the real addresses from the example, keep the new hash.
python3 - "$TARGET" "$EXAMPLE" <<'PYEOF'
import sys
import yaml

target_path, example_path = sys.argv[1], sys.argv[2]
with open(target_path) as f:
    fresh = yaml.safe_load(f)
with open(example_path) as f:
    example = yaml.safe_load(f)

example["users"] = fresh["users"]
with open(target_path, "w") as f:
    yaml.dump(example, f, default_flow_style=False, sort_keys=False)
PYEOF

chmod 600 "$TARGET"
echo "$PW" > "$PW_FILE"
chmod 600 "$PW_FILE"

echo "Wrote $TARGET and $PW_FILE (mode 600, not printed here)."
