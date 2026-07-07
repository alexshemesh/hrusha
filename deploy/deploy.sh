#!/usr/bin/env bash
# First deploy (and redeploy) on this host: build the image from the
# checkout, start the dashboard, wait for health.
#
#   ./deploy/deploy.sh                     # localhost:8787, dirs under deploy/
#   HRUSHA_BIND=0.0.0.0 ./deploy/deploy.sh # expose on the LAN — the dashboard
#                                          # has NO auth; trusted networks only
#
# All knobs are env vars documented in compose.yaml / docs/DEPLOYMENT.md.

source "$(dirname "$0")/lib.sh"

require_docker
require_config
ensure_dirs

compose build
compose up -d
wait_healthy
