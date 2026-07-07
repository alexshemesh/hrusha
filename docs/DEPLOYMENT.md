# Deployment

hrusha runs two ways, and both stay first-class:

- **Bare-metal**: `hrusha serve` from a venv, config in `~/.hrusha/`,
  exactly as in development. Nothing below changes that.
- **Docker (compose)**: the same code in a container with everything
  stateful mounted from the host — config read-only, SQLite ledger and
  JSON logs on host directories you can inspect, back up, and move.

The image contains **code only**. Config, ledger, logs and backups
live on the host; deleting the container loses nothing.

## Layout

| what | host (defaults) | container |
|---|---|---|
| config (read-only) | `~/.hrusha/config.yaml` | `/config/config.yaml` |
| SQLite ledger | `./deploy/data/hrusha.db` | `/data/hrusha.db` |
| DB backups | `./deploy/data/backups/` | `/data/backups/` |
| JSON logs | `./deploy/logs/hrusha.jsonl` | `/logs/hrusha.jsonl` |
| dashboard | `http://127.0.0.1:8787/` | `:8787` inside |

The container sets `HRUSHA_DB_PATH=/data/hrusha.db`, which overrides
`db_path` from the config file — so the **same config.yaml** works
bare-metal and mounted. Every default is an env var; see the header of
[compose.yaml](../compose.yaml).

## First deploy on a machine

```bash
git clone git@github.com:alexshemesh/hrusha.git && cd hrusha
# put your config at ~/.hrusha/config.yaml (never committed — see README)
./deploy/deploy.sh
```

The script checks docker + config, creates the data/log dirs, builds
the image from the checkout, starts the service, and waits for
`/health`. The ledger starts empty on a new machine: press **refresh**
on the dashboard for the first sync (it rebuilds from chain data), then
restore your manual tags:

```bash
# copy ~/.hrusha/rules.yaml from the old machine, then:
docker compose cp ~/.hrusha/rules.yaml hrusha:/tmp/rules.yaml
docker compose exec hrusha hrusha rules import --path /tmp/rules.yaml
```

## Reaching the dashboard over the LAN

By default the port binds to `127.0.0.1` on the host — invisible to the
network. The dashboard has **no authentication**, so exposing it is an
explicit decision:

```bash
HRUSHA_BIND=0.0.0.0 ./deploy/deploy.sh   # trusted home LAN only
```

Better options if the network isn't fully trusted: keep `127.0.0.1`
and reach it through an SSH tunnel (`ssh -L 8787:127.0.0.1:8787 box`)
or a Tailscale/WireGuard address bound instead of `0.0.0.0`.

## Day-2 operations

```bash
./deploy/status.sh    # containers, /health, ledger size, recent errors
./deploy/backup.sh    # consistent online SQLite backup, keeps newest 14
./deploy/upgrade.sh   # backup -> git pull --ff-only -> rebuild -> health -> doctor
docker compose logs -f hrusha        # live stderr (same JSON as /logs mirror)
docker compose exec hrusha hrusha doctor   # any CLI command works this way
```

Schema migrations run automatically when new code opens the ledger —
`upgrade.sh` needs no migration step, which is why it backs up first.

**Restore from backup**: stop, swap the file, start.

```bash
docker compose down
cp deploy/data/backups/hrusha-<stamp>.db deploy/data/hrusha.db
docker compose up -d
```

## Troubleshooting

| symptom | meaning | fix |
|---|---|---|
| `doctor` exit 5 / mismatches | Blockscout indexer gaps (chronic) | dashboard refresh (sync), then `hrusha heal` |
| unpriced legs / $0 income rows | price cache poisoned by past throttling | `hrusha reprice` |
| /votes: "GoPlus … unreachable" banner | token-safety API was down during scan | rescan; flags may be missing meanwhile |
| /votes numbers look stale | scan predates the running epoch | banner says so — rescan |
| container up, page unreachable from LAN | port bound to 127.0.0.1 (default) | redeploy with `HRUSHA_BIND` (see above) |
| `permission denied` on /data | dirs created by a different user | rerun via `deploy/deploy.sh` (it pins the container to your uid/gid) |

Bare-metal parity check: `HRUSHA_LOG_DIR=/tmp/hlogs hrusha serve` gives
the identical file-mirrored logging without Docker.
