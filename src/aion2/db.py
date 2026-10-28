"""SQLite snapshot storage.

Per the decision recorded in docs/endpoints.md section 4, we store Accuracy as
COMPONENTS rather than as a single derived total. `gear_accuracy` is explicitly
"flat Accuracy readable from gear", never a claim about the in-game number.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Character

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at        TEXT    NOT NULL,
    region          TEXT    NOT NULL,
    server_id       INTEGER NOT NULL,
    character_id    TEXT    NOT NULL,
    character_name  TEXT    NOT NULL,
    class_name      TEXT    NOT NULL,
    level           INTEGER NOT NULL,
    combat_power    INTEGER NOT NULL,
    item_level      INTEGER NOT NULL,
    gear_accuracy   REAL    NOT NULL,
    wasted_lines    INTEGER NOT NULL,
    payload         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_char_time
    ON snapshots (character_id, taken_at DESC);

-- One row per stat per snapshot. This is the "track components" decision made
-- concrete: every contribution is stored separately so a trend can be built
-- without ever inventing an aggregate the API does not publish.
CREATE TABLE IF NOT EXISTS stat_components (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    slot_pos    INTEGER NOT NULL,
    item_name   TEXT    NOT NULL,
    source      TEXT    NOT NULL,   -- main | sub | stone
    stat_id     TEXT    NOT NULL,
    value       REAL    NOT NULL,
    grade       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_components_snapshot
    ON stat_components (snapshot_id, stat_id);
"""


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def save_snapshot(
    db_path: str | Path,
    char: Character,
    *,
    region: str,
    character_id: str,
    raw: dict[str, Any] | None = None,
) -> int:
    init_db(db_path)
    taken_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO snapshots (
                taken_at, region, server_id, character_id, character_name,
                class_name, level, combat_power, item_level, gear_accuracy,
                wasted_lines, payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                taken_at,
                region,
                char.server_id,
                character_id,
                char.name,
                char.class_name,
                char.level,
                char.combat_power,
                char.item_level,
                char.gear_accuracy,
                len(char.wasted_lines),
                json.dumps(raw or {}, ensure_ascii=False),
            ),
        )
        snapshot_id = int(cur.lastrowid or 0)

        rows = [
            (
                snapshot_id,
                item.slot_pos,
                item.name,
                line.source,
                line.stat_id,
                line.value,
                line.grade,
            )
            for item in char.items
            for line in item.all_lines
        ]
        conn.executemany(
            """
            INSERT INTO stat_components
                (snapshot_id, slot_pos, item_name, source, stat_id, value, grade)
            VALUES (?,?,?,?,?,?,?)
            """,
            rows,
        )
    return snapshot_id


def latest_snapshot(db_path: str | Path, character_id: str | None = None) -> sqlite3.Row | None:
    if not Path(db_path).exists():
        return None
    with connect(db_path) as conn:
        if character_id:
            cur = conn.execute(
                "SELECT * FROM snapshots WHERE character_id = ? "
                "ORDER BY taken_at DESC LIMIT 1",
                (character_id,),
            )
        else:
            cur = conn.execute("SELECT * FROM snapshots ORDER BY taken_at DESC LIMIT 1")
        return cur.fetchone()


def history(
    db_path: str | Path, stat: str = "combat_power", days: int = 90
) -> list[dict[str, Any]]:
    """Time series for a top-level snapshot column."""
    allowed = {
        "combat_power",
        "item_level",
        "gear_accuracy",
        "wasted_lines",
        "level",
    }
    if stat not in allowed:
        raise ValueError(f"stat must be one of {sorted(allowed)}")
    if not Path(db_path).exists():
        return []
    with connect(db_path) as conn:
        cur = conn.execute(
            f"""
            SELECT taken_at, {stat} AS value
            FROM snapshots
            WHERE taken_at >= datetime('now', ?)
            ORDER BY taken_at
            """,
            (f"-{int(days)} days",),
        )
        return [dict(r) for r in cur.fetchall()]


def seconds_since_last_snapshot(db_path: str | Path, character_id: str) -> float | None:
    row = latest_snapshot(db_path, character_id)
    if row is None:
        return None
    taken = datetime.fromisoformat(row["taken_at"])
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - taken).total_seconds()
