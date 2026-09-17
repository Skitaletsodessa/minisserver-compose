#!/bin/bash
# Renews the Tailscale HTTPS cert used by Caddy. `tailscale cert` is safe to
# re-run idempotently - it only actually renews when the cert is within its
# renewal window, otherwise it's a no-op. Caddy watches the file and reloads
# automatically on change (Caddy's file storage/TLS handling picks up
# modified cert files without a restart), but we restart it anyway to be sure.
set -euo pipefail

DOMAIN=minisserver.tail6bf4d5.ts.net
CERT_DIR=/srv/compose/caddy/certs

cd "$CERT_DIR"
sudo tailscale cert "$DOMAIN"
sudo chown skit:skit "$DOMAIN.crt" "$DOMAIN.key"

cd /srv/compose/caddy
docker compose restart caddy
