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
    status      TEXT NOT NULL DEFAULT 'ACTIVE',     -- ACTIVE | SUSPENDED
    insurance_expiry TEXT                           -- YYYY-MM-DD
);
CREATE TABLE IF NOT EXISTS asns (
    id              INTEGER PRIMARY KEY,
    asn_number      TEXT NOT NULL UNIQUE,
    po_number       TEXT,
    carrier_id      INTEGER NOT NULL REFERENCES carriers(id),
    trailer_number  TEXT NOT NULL,
    seal_number     TEXT,
    pallet_count    INTEGER DEFAULT 0,
    trailer_type    TEXT NOT NULL DEFAULT 'DRY_VAN',  -- DRY_VAN | REEFER | FLATBED
    load_class      TEXT NOT NULL DEFAULT 'STANDARD', -- STANDARD | PERISHABLE | CROSS_DOCK
    due_by          TEXT,                       -- cross-dock: outbound departure the freight must make
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
    status          TEXT NOT NULL DEFAULT 'SCHEDULED', -- SCHEDULED | ARRIVED | NO_SHOW | CANCELLED
    arrived_at      TEXT,
    driver_phone    TEXT,                       -- E.164, from the carrier / TMS
    driver_language TEXT DEFAULT 'en',          -- en | es | hi | pa | ru
    driver_token    TEXT                        -- secret for the driver's live instruction page
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
CREATE TABLE IF NOT EXISTS trips (            -- inbound trucks tracked by GPS telematics
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER REFERENCES appointments(id),
    vehicle_id      TEXT,
    plate           TEXT,
    source          TEXT NOT NULL,              -- sim | telematics
    status          TEXT NOT NULL DEFAULT 'EN_ROUTE',  -- EN_ROUTE | ARRIVED | CANCELLED
    started_at      TEXT NOT NULL,
    last_ping_at    TEXT,
    lat REAL, lon REAL, speed_kph REAL, heading REAL,
    distance_km     REAL,                       -- remaining road distance
    eta_at TEXT, eta_low_at TEXT, eta_high_at TEXT, static_eta_at TEXT,
    conditions      TEXT,                       -- JSON: traffic / weather along route
    arrived_at      TEXT
);
CREATE TABLE IF NOT EXISTS eta_predictions (  -- prediction log, scored when the truck arrives
    id INTEGER PRIMARY KEY,
    trip_id INTEGER NOT NULL REFERENCES trips(id),
    made_at TEXT NOT NULL, remaining_km REAL, eta_at TEXT, static_eta_at TEXT
);
CREATE TABLE IF NOT EXISTS eta_samples (      -- real trips used to retrain the ETA model
    id INTEGER PRIMARY KEY, created_at TEXT, features TEXT, target REAL, static_min REAL
);
CREATE TABLE IF NOT EXISTS precheck_packets (  -- digital check-in packets
    id              INTEGER PRIMARY KEY,
    trip_id         INTEGER REFERENCES trips(id),
    appointment_id  INTEGER REFERENCES appointments(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL,              -- CLEARED | NOTES | BLOCKED
    trigger         TEXT,                       -- horizon | surge
    checks          TEXT NOT NULL,              -- JSON list
    packet          TEXT NOT NULL               -- JSON
);
CREATE TABLE IF NOT EXISTS yard_resources (   -- dock doors and parking spots
    id          TEXT PRIMARY KEY,               -- D01 ... / P-A01 ...
    kind        TEXT NOT NULL,                  -- DOOR | SPOT
    zone        TEXT NOT NULL,                  -- WEST | CENTRAL | EAST
    x REAL NOT NULL, y REAL NOT NULL,           -- metres, yard coordinates
    capabilities TEXT NOT NULL DEFAULT '',      -- REEFER, FLATBED, CROSS_DOCK, POWER (comma list)
    status      TEXT NOT NULL DEFAULT 'FREE',   -- FREE | OCCUPIED | OUT_OF_SERVICE
    visit_id    INTEGER
);
CREATE TABLE IF NOT EXISTS yard_visits (      -- a trailer inside the yard, gate-in to departure
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER REFERENCES appointments(id),
    event_id        INTEGER,
    plate TEXT, trailer_number TEXT, carrier_name TEXT,
    trailer_type    TEXT, load_class TEXT, pallet_count INTEGER,
    priority        REAL,
    checked_in_at   TEXT NOT NULL,
    detention_at    TEXT,                       -- when the carrier starts charging detention
    due_by          TEXT,
    status          TEXT NOT NULL,              -- AT_SPOT | AT_DOOR | DEPARTED | UNASSIGNED
    resource_id     TEXT,
    door_at TEXT, unload_done_at TEXT, departed_at TEXT,
    jockey_m        REAL NOT NULL DEFAULT 0,
    reasons         TEXT NOT NULL DEFAULT '[]', -- JSON: why this door / spot
    batch_id        TEXT
);
CREATE TABLE IF NOT EXISTS yard_moves (
    id INTEGER PRIMARY KEY, visit_id INTEGER, created_at TEXT NOT NULL,
    from_id TEXT, to_id TEXT, zone TEXT, distance_m REAL
);
CREATE TABLE IF NOT EXISTS driver_messages (  -- SMS / driver-app instructions
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    appointment_id  INTEGER REFERENCES appointments(id),
    visit_id INTEGER, event_id INTEGER,
    phone           TEXT NOT NULL,
    language        TEXT NOT NULL,
    channel         TEXT NOT NULL,              -- sim | twilio
    kind            TEXT NOT NULL,              -- door | spot | move | inspection | hold | review | denied | done | pre
    body            TEXT NOT NULL,
    payload         TEXT NOT NULL,              -- JSON: steps, route, destination (re-render in another language)
    status          TEXT NOT NULL,              -- QUEUED | SENT | DELIVERED | FAILED
    provider_id TEXT, error TEXT, sent_at TEXT,
    acked_at        TEXT,
    reply           TEXT
);
CREATE INDEX IF NOT EXISTS ix_msgs_appt ON driver_messages(appointment_id);
CREATE INDEX IF NOT EXISTS ix_msgs_phone ON driver_messages(phone);
CREATE INDEX IF NOT EXISTS ix_visits_status ON yard_visits(status);
CREATE INDEX IF NOT EXISTS ix_moves_created ON yard_moves(created_at);
CREATE INDEX IF NOT EXISTS ix_trips_status ON trips(status);
CREATE INDEX IF NOT EXISTS ix_packets_appt ON precheck_packets(appointment_id);
CREATE INDEX IF NOT EXISTS ix_events_created ON gate_events(created_at);
CREATE INDEX IF NOT EXISTS ix_appt_status ON appointments(status, scheduled_at);
"""

JSON_COLS = {"reasons", "damage", "images", "detail", "conditions", "checks", "packet", "features", "payload"}


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
        self._migrate()
        self._conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(carriers)").fetchall()}
        if cols and "insurance_expiry" not in cols:
            self._conn.execute("ALTER TABLE carriers ADD COLUMN insurance_expiry TEXT")
            self._conn.commit()
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(appointments)").fetchall()}
        for name, ddl in (("driver_phone", "TEXT"), ("driver_language", "TEXT DEFAULT 'en'"), ("driver_token", "TEXT")):
            if cols and name not in cols:
                self._conn.execute(f"ALTER TABLE appointments ADD COLUMN {name} {ddl}")
                self._conn.commit()
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(asns)").fetchall()}
        for name, ddl in (("trailer_type", "TEXT NOT NULL DEFAULT 'DRY_VAN'"),
                          ("load_class", "TEXT NOT NULL DEFAULT 'STANDARD'"), ("due_by", "TEXT")):
            if cols and name not in cols:
                self._conn.execute(f"ALTER TABLE asns ADD COLUMN {name} {ddl}")
                self._conn.commit()

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
            c.execute("UPDATE yard_resources SET status='FREE', visit_id=NULL WHERE status='OCCUPIED'")
            for t in ("driver_messages", "yard_moves", "yard_visits", "precheck_packets", "eta_predictions", "trips", "gate_events",
                      "appointments", "asns",
                      "carriers"):
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
