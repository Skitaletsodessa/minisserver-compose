#!/usr/bin/env bash
# Run BEFORE every release build: the signing keystore must be byte-identical to the backed-up one and must still open.
# Checks: (1) sha256 of the .jks == the value in the sidecar file next to it, (2) keytool -list opens it and the certificate
# SHA-256 equals the one pinned in the gateway. Prints no secret. Exit status 0 = safe to build, anything else = STOP.
set -u
DIR="${PORTAL_KEYS_DIR:-/c/Users/Skit/dev-keys}"
KT="${KEYTOOL:-/c/Users/Skit/dev-tools/jdk-17/bin/keytool.exe}"
JKS="$DIR/portal-release.jks"; SIDE="$DIR/portal-release.jks.sha256"; PROPS="${PORTAL_KEYSTORE_PROPS:-$DIR/keystore.properties}"
fail() { echo "FAIL: $*"; exit 1; }
[ -r "$JKS" ] && [ -r "$SIDE" ] && [ -r "$PROPS" ] || fail "missing $JKS, $SIDE or $PROPS"
want=$(awk 'NR==1{print $1}' "$SIDE"); pin=$(grep -m1 -oE '([0-9A-F]{2}:){31}[0-9A-F]{2}' "$SIDE")
have=$(sha256sum "$JKS" | cut -d' ' -f1)
[ "$have" = "$want" ] || fail "sha256 of portal-release.jks is $have, the backup says $want"
echo "OK   sha256 matches the backup record ($have)"
pw=$(grep -m1 '^storePassword=' "$PROPS" | cut -d= -f2- | tr -d '\r'); export PORTAL_STOREPASS="$pw"
out=$("$KT" -list -keystore "$(cygpath -m "$JKS")" -storepass:env PORTAL_STOREPASS 2>&1) || fail "keytool -list cannot open the keystore"
cert=$(echo "$out" | grep -m1 -oE '([0-9A-F]{2}:){31}[0-9A-F]{2}')
[ -n "$cert" ] && [ "$cert" = "$pin" ] || fail "certificate fingerprint '$cert' differs from the pinned '$pin'"
echo "OK   keytool -list opens it; certificate SHA-256 $cert equals the pinned one"
