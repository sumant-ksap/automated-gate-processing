"""SQLite persistence for the Yard Management System (YMS) gate module."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS carriers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    dot_number  TEXT NOT NULL UNIQUE,
    mc_number   TEXT,
    status      TEXT NOT NULL DEFAULT 'ACTIVE'      -- ACTIVE | SUSPENDED
);
CREATE TABLE IF NOT EXISTS asns (
    id              INTEGER PRIMARY KEY,
    asn_number      TEXT NOT NULL UNIQUE,
    po_number       TEXT,
    carrier_id      INTEGER NOT NULL REFERENCES carriers(id),
    trailer_number  TEXT NOT NULL,
    seal_number     TEXT,
    pallet_count    INTEGER DEFAULT 0,
    shipper         TEXT,
    expected_arrival TEXT,
    status          TEXT NOT NULL DEFAULT 'EXPECTED'  -- EXPECTED | ARRIVED | EXCEPTION
);
CREATE TABLE IF NOT EXISTS appointments (
    id              INTEGER PRIMARY KEY,
    appt_ref        TEXT NOT NULL UNIQUE,
    carrier_id      INTEGER NOT NULL REFERENCES carriers(id),
    asn_id          INTEGER REFERENCES asns(id),
    tractor_plate   TEXT,
    trailer_number  TEXT,
    direction       TEXT NOT NULL DEFAULT 'INBOUND',  -- INBOUND | OUTBOUND
    scheduled_at    TEXT NOT NULL,
    dock_door       TEXT,
    status          TEXT NOT NULL DEFAULT 'SCHEDULED', -- SCHEDULED | ARRIVED | CANCELLED
    arrived_at      TEXT
);
CREATE TABLE IF NOT EXISTS gate_events (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    lane_id         INTEGER NOT NULL,
    decision        TEXT NOT NULL,
    reasons         TEXT NOT NULL,              -- JSON list
    plate           TEXT, plate_conf REAL,
    trailer_number  TEXT, trailer_conf REAL,
    dot_number      TEXT, dot_conf REAL,
    seal_number     TEXT, seal_conf REAL,
    damage          TEXT NOT NULL DEFAULT '[]', -- JSON list
    appointment_id  INTEGER REFERENCES appointments(id),
    asn_id          INTEGER REFERENCES asns(id),
    carrier_name    TEXT,
    destination     TEXT,
    gate_opened     INTEGER NOT NULL DEFAULT 0,
    images          TEXT NOT NULL DEFAULT '[]', -- JSON list of {raw, annotated}
    processing_ms   INTEGER,
    detail          TEXT,                       -- JSON blob (all OCR text, match scores ...)
    override_decision TEXT, override_by TEXT, override_note TEXT, override_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_created ON gate_events(created_at);
CREATE INDEX IF NOT EXISTS ix_appt_status ON appointments(status, scheduled_at);
"""

JSON_COLS = {"reasons", "damage", "images", "detail"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params=()) -> int:
        with self.tx() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in JSON_COLS & d.keys():
            if isinstance(d[k], str):
                d[k] = json.loads(d[k])
        return d

    # -- domain helpers -----------------------------------------------------
    def clear_all(self) -> None:
        with self.tx() as c:
            for t in ("gate_events", "appointments", "asns", "carriers"):
                c.execute(f"DELETE FROM {t}")

    def insert_event(self, ev: dict) -> int:
        cols = list(ev.keys())
        vals = [json.dumps(ev[k]) if k in JSON_COLS else ev[k] for k in cols]
        sql = f"INSERT INTO gate_events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        return self.execute(sql, vals)

    def get_event(self, event_id: int) -> dict | None:
        return self.one("SELECT * FROM gate_events WHERE id = ?", (event_id,))

    def recent_events(self, limit: int = 50, lane_id: int | None = None, decision: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM gate_events WHERE 1=1", []
        if lane_id is not None:
            sql += " AND lane_id = ?"
            params.append(lane_id)
        if decision:
            sql += " AND COALESCE(override_decision, decision) = ?"
            params.append(decision)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    def appointments(self, status: str | None = None, limit: int = 200) -> list[dict]:
        sql = """SELECT a.*, c.name AS carrier_name, c.dot_number, s.asn_number, s.seal_number,
                        s.po_number, s.pallet_count
                 FROM appointments a JOIN carriers c ON c.id = a.carrier_id
                 LEFT JOIN asns s ON s.id = a.asn_id"""
        params: list = []
        if status:
            sql += " WHERE a.status = ?"
            params.append(status)
        sql += " ORDER BY a.scheduled_at LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    def stats(self, since: str) -> dict:
        rows = self.query("""SELECT COALESCE(override_decision, decision) AS d, COUNT(*) AS n,
                                    AVG(processing_ms) AS ms
                             FROM gate_events WHERE created_at >= ? GROUP BY d""", (since,))
        by = {r["d"]: r["n"] for r in rows}
        total = sum(by.values())
        ms = self.one("SELECT AVG(processing_ms) AS ms FROM gate_events WHERE created_at >= ?", (since,))
        pending = self.one("SELECT COUNT(*) AS n FROM appointments WHERE status = 'SCHEDULED'")
        auto = by.get("GRANTED", 0) + by.get("INSPECTION", 0)
        return {
            "total": total,
            "by_decision": by,
            "auto_rate": round(auto / total, 3) if total else 0.0,
            "avg_processing_ms": round(ms["ms"] or 0),
            "pending_appointments": pending["n"],
        }
