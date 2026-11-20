"""SQLite CMDB: diff the source against stored rows and apply the diff."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from cmdbsync.sources import Record

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    key           TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    region        TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    name          TEXT NOT NULL,
    arn           TEXT NOT NULL,
    tags_json     TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    first_seen    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    retired_at    TEXT
)
"""


class SyncRefused(RuntimeError):
    pass


@dataclass
class Plan:
    insert: list[Record] = field(default_factory=list)
    update: list[Record] = field(default_factory=list)
    revive: list[Record] = field(default_factory=list)
    retire: list[str] = field(default_factory=list)  # keys

    @property
    def empty(self) -> bool:
        return not (self.insert or self.update or self.revive or self.retire)

    def lines(self) -> list[str]:
        out = [f"+ {r.key}" for r in self.insert]
        out += [f"~ {r.key}" for r in self.update]
        out += [f"^ {r.key} (back, was retired)" for r in self.revive]
        out += [f"- {k} (retire)" for k in self.retire]
        return out

    def summary(self) -> str:
        return (
            f"{len(self.insert)} new, {len(self.update)} changed, "
            f"{len(self.revive)} revived, {len(self.retire)} retired"
        )


class Sink(Protocol):
    def plan(self, records: list[Record], allow_empty_source: bool) -> Plan: ...
    def apply(self, plan: Plan, now: datetime) -> None: ...


def build_plan(
    records: list[Record], stored: dict[str, tuple[str, str]], allow_empty_source: bool
) -> Plan:
    """stored maps key -> (content_hash, status)."""
    by_key: dict[str, Record] = {}
    for r in records:
        if r.key in by_key:
            raise ValueError(f"source returned {r.key} twice")
        by_key[r.key] = r

    active = [k for k, (_, status) in stored.items() if status == "active"]
    if not by_key and active and not allow_empty_source:
        # An empty source usually means a broken query or permissions, not an empty estate.
        raise SyncRefused(
            f"source returned nothing but {len(active)} resources are active; "
            "refusing to retire them all (use --allow-empty-source if intended)"
        )

    plan = Plan()
    for key, rec in sorted(by_key.items()):
        if key not in stored:
            plan.insert.append(rec)
        else:
            old_hash, status = stored[key]
            if status == "retired":
                plan.revive.append(rec)
            elif old_hash != rec.content_hash:
                plan.update.append(rec)
    plan.retire = sorted(k for k in active if k not in by_key)
    return plan


class SqliteSink:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _stored(self) -> dict[str, tuple[str, str]]:
        if not self.path.exists():
            return {}
        # Read-only so a dry run can never write, even by accident.
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT key, content_hash, status FROM resources").fetchall()
        finally:
            conn.close()
        return {k: (h, s) for k, h, s in rows}

    def plan(self, records: list[Record], allow_empty_source: bool) -> Plan:
        return build_plan(records, self._stored(), allow_empty_source)

    def apply(self, plan: Plan, now: datetime) -> None:
        ts = now.isoformat()
        conn = sqlite3.connect(self.path)
        try:
            with conn:  # one transaction: all of the plan or none of it
                conn.execute(SCHEMA)
                for r in plan.insert:
                    conn.execute(
                        "INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,NULL)",
                        (
                            r.key,
                            r.account_id,
                            r.region,
                            r.resource_type,
                            r.resource_id,
                            r.name,
                            r.arn,
                            json.dumps(r.tags),
                            r.content_hash,
                            ts,
                            ts,
                        ),
                    )
                for r in plan.update + plan.revive:
                    conn.execute(
                        "UPDATE resources SET name=?, arn=?, tags_json=?, content_hash=?, "
                        "status='active', updated_at=?, retired_at=NULL WHERE key=?",
                        (r.name, r.arn, json.dumps(r.tags), r.content_hash, ts, r.key),
                    )
                for key in plan.retire:
                    # Soft retire: the row stays for history.
                    conn.execute(
                        "UPDATE resources SET status='retired', retired_at=?, updated_at=? "
                        "WHERE key=?",
                        (ts, ts, key),
                    )
        finally:
            conn.close()


class ServiceNowSink:
    """Interface placeholder. Not implemented."""

    def plan(self, records, allow_empty_source):
        raise NotImplementedError("ServiceNow is not implemented")

    def apply(self, plan, now):
        raise NotImplementedError("ServiceNow is not implemented")
