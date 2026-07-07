#!/usr/bin/env bash
# Upgrade the running deployment to the latest main: backup first, then
# pull, rebuild, restart, verify health, and reconcile the ledger.
#
#   ./deploy/upgrade.sh
#
# Schema migrations run automatically when the new code opens the ledger.
# If doctor reports mismatches afterwards that is usually indexer drift,
# not the upgrade — see docs/DEPLOYMENT.md, Troubleshooting.

source "$(dirname "$0")/lib.sh"

require_docker
require_config

echo "== backup before anything else"
"$REPO_ROOT/deploy/backup.sh"

echo "== pulling latest main"
git pull --ff-only

echo "== rebuild + restart"
ensure_dirs
compose build
compose up -d
wait_healthy

echo "== ledger reconciliation (doctor)"
if compose exec -T hrusha hrusha doctor; then
  echo "doctor: ledger reconciles"
else
  echo "doctor reported mismatches — run sync from the dashboard, then" >&2
  echo "  docker compose exec hrusha hrusha heal   # if gaps persist" >&2
fi
