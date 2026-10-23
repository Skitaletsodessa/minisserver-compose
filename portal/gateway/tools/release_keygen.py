#!/usr/bin/env python3
"""Makes the update channel's MANIFEST signing key (Ed25519) on the HOST, once (Task 30c, ADR 0010):

  python3 tools/release_keygen.py [SECRETS_DIR]        default /srv/apps/portal/gateway-secrets

Writes release-manifest.key (the 32-byte seed as hex, mode 600: this is the private key) and release-manifest.pub (the public key as hex). Refuses to overwrite an existing key.
Prints ONLY the public key (hex and base64): that is what the Android app embeds. The private seed is never printed. It is NOT the APK signing key (that one never leaves
Ivan's desktop). The secrets directory is part of the Tier-0 backup set (restic, local and R2), so the key survives a rebuilt server."""
import base64
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from gw import ed25519  # noqa: E402


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "/srv/apps/portal/gateway-secrets"
    key, pub = os.path.join(d, "release-manifest.key"), os.path.join(d, "release-manifest.pub")
    if os.path.exists(key):
        print("refusing: %s already exists (rotate by hand, deliberately)" % key)
        return 1
    seed = secrets.token_bytes(32)
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(seed.hex() + "\n")
    pk = ed25519.public_key(seed)
    with open(pub, "w") as f:
        f.write(pk.hex() + "\n")
    os.chmod(pub, 0o644)
    print("public key (hex):    ", pk.hex())
    print("public key (base64): ", base64.b64encode(pk).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
