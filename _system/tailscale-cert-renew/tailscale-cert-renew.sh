#!/bin/bash
# Renews the Tailscale HTTPS cert used by Caddy. `tailscale cert` is safe to
# re-run idempotently - it only renews when the cert is within its renewal window.
# Caddy is restarted only if the cert actually changed; an unchanged cert does not
# need a restart (Caddy would reload automatically on file change anyway).
set -euo pipefail

DOMAIN=minisserver.tail6bf4d5.ts.net
CERT_DIR=/srv/compose/caddy/certs

cd "$CERT_DIR"
BEFORE=$(sha256sum "$DOMAIN.crt" 2>/dev/null || echo none)
sudo tailscale cert "$DOMAIN"
sudo chown skit:skit "$DOMAIN.crt" "$DOMAIN.key"
AFTER=$(sha256sum "$DOMAIN.crt" 2>/dev/null || echo none)

if [ "$BEFORE" != "$AFTER" ]; then
    echo "cert changed — restarting caddy"
    cd /srv/compose/caddy
    docker compose restart caddy
else
    echo "cert unchanged — skipping caddy restart"
fi
