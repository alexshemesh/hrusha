#!/usr/bin/env bash
# One-page health report: container state, /health, ledger size, backups,
# and the most recent errors from the mounted log mirror.
#
#   ./deploy/status.sh

source "$(dirname "$0")/lib.sh"

require_docker

echo "== containers"
compose ps

echo
echo "== health"
if curl -fsS "http://127.0.0.1:${HRUSHA_PORT}/health" 2>/dev/null; then
  echo " (http://127.0.0.1:${HRUSHA_PORT}/)"
else
  echo "UNREACHABLE on 127.0.0.1:${HRUSHA_PORT}"
fi

echo
echo "== ledger"
if [ -f "$HRUSHA_DATA_DIR/hrusha.db" ]; then
  du -h "$HRUSHA_DATA_DIR/hrusha.db"
else
  echo "no ledger yet ($HRUSHA_DATA_DIR/hrusha.db)"
fi
ls -1t "$HRUSHA_DATA_DIR/backups"/hrusha-*.db 2>/dev/null | head -3 | sed 's/^/backup: /' || true

echo
echo "== recent errors (last 5, from $HRUSHA_LOGS_DIR/hrusha.jsonl)"
if [ -f "$HRUSHA_LOGS_DIR/hrusha.jsonl" ]; then
  grep '"level": "ERROR"' "$HRUSHA_LOGS_DIR/hrusha.jsonl" | tail -5 || echo "none"
else
  echo "no log file yet"
fi
