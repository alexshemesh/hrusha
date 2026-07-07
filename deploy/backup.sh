#!/usr/bin/env bash
# Consistent SQLite backup into $HRUSHA_DATA_DIR/backups, keeping the 14
# most recent. Uses the sqlite backup API through the running container
# (safe while the service writes); falls back to a plain copy when the
# service is stopped.
#
#   ./deploy/backup.sh

source "$(dirname "$0")/lib.sh"

require_docker
ensure_dirs
STAMP="$(date -u +%Y%m%d-%H%M%S)"

if [ -n "$(compose ps -q --status running hrusha 2>/dev/null)" ]; then
  compose exec -T hrusha python - <<PY
import sqlite3
src = sqlite3.connect("/data/hrusha.db")
dst = sqlite3.connect("/data/backups/hrusha-$STAMP.db")
src.backup(dst)
dst.close()
src.close()
print("backup (online): /data/backups/hrusha-$STAMP.db")
PY
elif [ -f "$HRUSHA_DATA_DIR/hrusha.db" ]; then
  cp "$HRUSHA_DATA_DIR/hrusha.db" "$HRUSHA_DATA_DIR/backups/hrusha-$STAMP.db"
  echo "backup (cold copy): $HRUSHA_DATA_DIR/backups/hrusha-$STAMP.db"
else
  echo "nothing to back up yet ($HRUSHA_DATA_DIR/hrusha.db missing)"
  exit 0
fi

# prune: keep the newest 14
ls -1t "$HRUSHA_DATA_DIR/backups"/hrusha-*.db 2>/dev/null | tail -n +15 | while read -r old; do
  rm -- "$old"
  echo "pruned: $old"
done
