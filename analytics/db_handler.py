"""SQLite BI hub for the Retail AI Core.

Stores every inference result locally (zero cloud cost) and provides the
query helpers used by the KPI engine. Uses the standard library only so the
test suite runs without any pip dependencies.

Schema (per knowledge base):
    telemetry_logs(id, timestamp, camera_id, raw_text, count,
                   congestion_level, staff_present)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         INTEGER NOT NULL,   -- epoch seconds (UTC)
    camera_id         TEXT    NOT NULL,
    raw_text          TEXT    NOT NULL DEFAULT '',
    count             INTEGER NOT NULL DEFAULT 0,
    congestion_level  TEXT    NOT NULL DEFAULT 'unknown',
    staff_present     INTEGER NOT NULL DEFAULT 0,  -- 0/1 boolean
    snapshot_path     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp ON telemetry_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_telemetry_camera   ON telemetry_logs(camera_id);
"""


class DBHandler:
    """Thin, dependency-free wrapper around a SQLite database file."""

    def __init__(self, db_path: str = "data/retail_ai.db") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ write
    def save_telemetry(
        self,
        camera_id: str,
        raw_text: str,
        count: int,
        congestion_level: str,
        staff_present: bool,
        snapshot_path: str = "",
        timestamp: Optional[int] = None,
    ) -> int:
        """Persist one inference result. Returns the new row id."""
        import time

        cur = self._conn.execute(
            "INSERT INTO telemetry_logs"
            " (timestamp, camera_id, raw_text, count, congestion_level,"
            "  staff_present, snapshot_path)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                int(timestamp) if timestamp is not None else int(time.time()),
                camera_id,
                raw_text,
                int(count),
                congestion_level,
                int(bool(staff_present)),
                snapshot_path,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------------------- read
    def fetch_rows(
        self,
        since: Optional[int] = None,
        camera_id: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        """All telemetry rows, optionally filtered by epoch floor / camera."""
        query = "SELECT * FROM telemetry_logs WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(int(since))
        if camera_id is not None:
            query += " AND camera_id = ?"
            params.append(camera_id)
        query += " ORDER BY timestamp ASC"
        return list(self._conn.execute(query, params).fetchall())

    def get_hourly_counts(self, since: Optional[int] = None) -> list[dict[str, Any]]:
        """Sum of people counts grouped per hour (epoch floor)."""
        rows = self.fetch_rows(since=since)
        buckets: dict[int, int] = {}
        for row in rows:
            hour = int(row["timestamp"]) - (int(row["timestamp"]) % 3600)
            buckets[hour] = buckets.get(hour, 0) + int(row["count"])
        return [
            {"hour": hour, "footfall": total}
            for hour, total in sorted(buckets.items())
        ]

    def get_daily_counts(self) -> list[dict[str, Any]]:
        """Sum of people counts grouped per day (UTC)."""
        import datetime as dt

        buckets: dict[str, int] = {}
        for row in self.fetch_rows():
            day = dt.datetime.fromtimestamp(
                int(row["timestamp"]), tz=dt.timezone.utc
            ).strftime("%Y-%m-%d")
            buckets[day] = buckets.get(day, 0) + int(row["count"])
        return [
            {"day": day, "footfall": total}
            for day, total in sorted(buckets.items())
        ]

    def get_peak_hours(self, top_n: int = 8) -> list[dict[str, Any]]:
        """Busiest hours of the day (0-23) across all history."""
        import datetime as dt

        buckets: dict[int, int] = {}
        for row in self.fetch_rows():
            hour = dt.datetime.fromtimestamp(
                int(row["timestamp"]), tz=dt.timezone.utc
            ).hour
            buckets[hour] = buckets.get(hour, 0) + int(row["count"])
        ranked = sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        return [{"hour": hour, "footfall": total} for hour, total in ranked]

    def prune_older_than(self, days: int = 90) -> int:
        """Delete telemetry older than `days` (retention hygiene)."""
        import time

        cutoff = int(time.time()) - days * 86400
        cur = self._conn.execute(
            "DELETE FROM telemetry_logs WHERE timestamp < ?", (cutoff,)
        )
        self._conn.commit()
        return int(cur.rowcount)
