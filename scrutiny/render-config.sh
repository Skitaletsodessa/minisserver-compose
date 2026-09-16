#!/bin/sh
# Renders config/scrutiny.yaml from the template, substituting secrets from .env.
# Run this after editing .env, before `docker compose up`.
set -eu
cd "$(dirname "$0")"
set -a
. ./.env
set +a
envsubst < config/scrutiny.yaml.template > config/scrutiny.yaml
echo "Wrote config/scrutiny.yaml"
