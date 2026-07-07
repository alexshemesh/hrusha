#!/usr/bin/env bash
# Shared helpers for the deploy scripts. Source, don't execute.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export HRUSHA_UID="${HRUSHA_UID:-$(id -u)}"
export HRUSHA_GID="${HRUSHA_GID:-$(id -g)}"
HRUSHA_BIND="${HRUSHA_BIND:-127.0.0.1}"
HRUSHA_PORT="${HRUSHA_PORT:-8787}"
HRUSHA_CONFIG_FILE="${HRUSHA_CONFIG_FILE:-$HOME/.hrusha/config.yaml}"
HRUSHA_DATA_DIR="${HRUSHA_DATA_DIR:-$REPO_ROOT/deploy/data}"
HRUSHA_LOGS_DIR="${HRUSHA_LOGS_DIR:-$REPO_ROOT/deploy/logs}"
export HRUSHA_BIND HRUSHA_PORT HRUSHA_CONFIG_FILE HRUSHA_DATA_DIR HRUSHA_LOGS_DIR

# `docker compose` (plugin) and `docker-compose` (standalone v2) are the
# same tool packaged differently per host — support both transparently
compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null; then
    docker-compose "$@"
  else
    echo "docker compose v2 is required (plugin or standalone docker-compose)" >&2
    exit 1
  fi
}

require_docker() {
  command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }
  compose version >/dev/null
}

require_config() {
  [ -f "$HRUSHA_CONFIG_FILE" ] || {
    echo "config not found: $HRUSHA_CONFIG_FILE" >&2
    echo "create it first (see README.md, Configuration) — it is never committed" >&2
    exit 1
  }
}

ensure_dirs() {
  mkdir -p "$HRUSHA_DATA_DIR" "$HRUSHA_DATA_DIR/backups" "$HRUSHA_LOGS_DIR"
}

wait_healthy() {
  printf "waiting for /health "
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${HRUSHA_PORT}/health" >/dev/null 2>&1; then
      printf "\ndashboard: http://%s:%s/\n" "$HRUSHA_BIND" "$HRUSHA_PORT"
      return 0
    fi
    printf .
    sleep 2
  done
  printf "\nservice did not become healthy — inspect: docker compose logs hrusha\n" >&2
  return 1
}
