---
date: 2026-07-07T16:42
type: infra
status: merged
trigger: architecture
touches:
  - compose.yaml
  - deploy/
  - Dockerfile
  - hrusha/logs.py
  - hrusha/config.py
  - docs/DEPLOYMENT.md
  - .claude/skills/hrusha-ops/
related:
  - 2026-07-06-1620-phase5a-fastapi-dashboard.md
supersedes: null
commit: df802cf
pr: https://github.com/alexshemesh/hrusha/pull/13
---

# Phase 6: Docker compose deployment, ops scripts, AI runbook

## Context

Dashboard (5a) and vote scout (5b/5d) are daily-use; the operator wants
the service on a separate machine on the home network, deployed from a
repo checkout ("clone, docker build") — no registry. Requirements:
configs mounted, DB and logs on mounted host folders, port forwarded
for the UI, deployment/maintenance scripts, docs, an AI-operable
runbook — and bare-metal `hrusha serve` must keep working unchanged.

## Decision

- **compose.yaml, build-locally**: one service; `~/.hrusha/config.yaml`
  mounted read-only, `deploy/data` -> `/data` (ledger + backups),
  `deploy/logs` -> `/logs`; healthcheck on `/health`; every knob an env
  var with a safe default. Host port binding defaults to `127.0.0.1`
  (the app has no auth) — LAN exposure is the operator's explicit
  `HRUSHA_BIND=0.0.0.0` decision, documented with tunnel/Tailscale
  alternatives.
- **Two tiny env overrides keep one config for both worlds**:
  `HRUSHA_DB_PATH` (image sets `/data/hrusha.db`, beats config
  `db_path`) and `HRUSHA_LOG_DIR` (adds a size-rotated JSON mirror at
  `<dir>/hrusha.jsonl`; stderr stays primary so `docker logs` works).
  Bare-metal behavior is identical when the vars are unset.
- **Container runs as the invoking user** (`HRUSHA_UID/GID` compose
  override, set by deploy.sh) so the mounted data/log dirs stay owned
  by the operator — the classic bind-mount permission trap avoided by
  construction.
- **deploy/ scripts**, all sourcing a shared lib with a
  `docker compose`/`docker-compose` dual wrapper (hosts differ):
  deploy.sh (build, up, health-wait), upgrade.sh (backup -> git pull
  --ff-only -> rebuild -> health -> doctor), backup.sh (online-
  consistent sqlite backup API through the container, newest 14 kept),
  status.sh (containers, health, ledger size, recent errors).
- **AI runbook as a repo skill** (`.claude/skills/hrusha-ops/SKILL.md`):
  hard rules (secrets, no-auth exposure, backup-before-schema,
  gitleaks allowlist), path map, ops commands, the sync->doctor->
  heal->reprice pipeline, vote-scout timing, failure signatures.
  Operational knowledge stops living in chat history.

## Alternatives Considered

- **ghcr.io image via CI** (the original plan sketch) — deferred by
  operator choice: single deployment target, local build is one less
  moving part; revisit if a second consumer appears.
- **Named docker volumes for /data** — rejected: host bind mounts keep
  the SQLite file directly inspectable/backupable with plain tools,
  which is the point of a local-first ledger.
- **Logging via docker json-file driver only** — rejected: the mounted
  `/logs` mirror works identically bare-metal (env var) and survives
  container recreation without docker-specific tooling.

## Implementation Notes

- Verified live on this machine end-to-end: `deploy/deploy.sh` built
  and started the service (port 8788 to coexist with the dev server);
  fresh ledger page rendered; DB appeared host-side owned by the
  operator; uvicorn+app JSON logs mirrored to `deploy/logs/`;
  status.sh full report; backup.sh produced an online backup.
- `docker compose` plugin vs standalone `docker-compose` differs even
  between the operator's own machines — hence the wrapper.
- Dockerfile CMD graduated from the Phase-0 placeholder to
  `serve --host 0.0.0.0` — correct INSIDE the container; the host-side
  port binding is the real exposure boundary (serve's loud warning
  stays as an honest reminder in container logs).

## Follow-ups

- Phase 5e scheduler will make the container self-syncing; until then
  the dashboard refresh button is the sync trigger.
- upgrade.sh could snapshot `rules.yaml` alongside the DB once the
  scheduler exports it periodically.
