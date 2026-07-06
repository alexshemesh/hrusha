"""Back up and restore local tagging knowledge to ~/.hrusha/rules.yaml.

The ledger DB is derived state — rebuildable from chain — except for two
things that exist nowhere else: hand-added tag rules (personal swap
counterparties, protocols without adapters) and manual tags. Losing the
DB loses them silently, so they get a private YAML backup next to
config.yaml. The file names the operator's counterparties, so it must
never enter the repository (same rule as config.yaml).

Manual tags are keyed by (tx_hash, log_index, kind) — the events UNIQUE
constraint — not by event id, which changes across DB rebuilds. Import
is idempotent: existing rules (by canonical match) and tags are skipped.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from hrusha.ledger.tags import ensure_rule, set_manual_tag

DEFAULT_RULES_PATH = Path("~/.hrusha/rules.yaml")


@dataclass(frozen=True)
class ExportStats:
    rules: int
    manual_tags: int


@dataclass(frozen=True)
class ImportStats:
    rules_added: int = 0
    rules_existing: int = 0
    tags_added: int = 0
    tags_missing_event: int = 0  # event not in the ledger (yet) — re-import after sync


def export_local(conn: sqlite3.Connection, path: Path) -> ExportStats:
    rules = [
        {
            "priority": priority,
            "match": json.loads(match_json),
            "tags": [t.strip() for t in tags_csv.split(",") if t.strip()],
            "source": source,
            "enabled": bool(enabled),
        }
        for priority, match_json, tags_csv, source, enabled in conn.execute(
            "SELECT priority, match_json, tags, source, enabled FROM tag_rules"
            " ORDER BY priority, id"
        ).fetchall()
    ]
    manual_tags = [
        {"tx_hash": tx_hash, "log_index": log_index, "kind": kind, "tag": tag}
        for tx_hash, log_index, kind, tag in conn.execute(
            """
            SELECT e.tx_hash, e.log_index, e.kind, t.tag
            FROM tags t JOIN events e ON e.id = t.event_id
            WHERE t.origin = 'manual' ORDER BY e.id, t.tag
            """
        ).fetchall()
    ]
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)  # names personal counterparties: owner-only, like config.yaml
    path.write_text(
        "# hrusha local rules and manual tags — private backup, never commit this\n"
        + yaml.safe_dump(
            {"rules": rules, "manual_tags": manual_tags},
            sort_keys=False,
            default_flow_style=False,
        )
    )
    return ExportStats(rules=len(rules), manual_tags=len(manual_tags))


def import_local(conn: sqlite3.Connection, path: Path) -> ImportStats:
    path = path.expanduser()
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping with 'rules' / 'manual_tags'")
    rules_added = rules_existing = tags_added = tags_missing = 0
    for rule in raw.get("rules") or []:
        created = ensure_rule(
            conn,
            priority=int(rule["priority"]),
            match=dict(rule["match"]),
            tags=list(rule["tags"]),
            source=rule.get("source"),
        )
        if created and not rule.get("enabled", True):
            with conn:
                conn.execute(
                    "UPDATE tag_rules SET enabled = 0 WHERE match_json = ?",
                    (json.dumps(dict(rule["match"]), sort_keys=True),),
                )
        rules_added += int(created)
        rules_existing += int(not created)
    for entry in raw.get("manual_tags") or []:
        row = conn.execute(
            "SELECT id FROM events WHERE tx_hash = ? AND log_index = ? AND kind = ?",
            (entry["tx_hash"], int(entry["log_index"]), entry["kind"]),
        ).fetchone()
        if row is None:
            tags_missing += 1
            continue
        set_manual_tag(conn, row[0], str(entry["tag"]))
        tags_added += 1
    return ImportStats(
        rules_added=rules_added,
        rules_existing=rules_existing,
        tags_added=tags_added,
        tags_missing_event=tags_missing,
    )
