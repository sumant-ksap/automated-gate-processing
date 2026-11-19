"""Demo data: carriers, ASNs and dock appointments (all fictional)."""
from __future__ import annotations

import random
import string
from datetime import timedelta

from .db import Database, iso, utcnow
from .vision.identifiers import format_iso6346, iso6346_check_digit

CARRIERS = [
    ("Bluewater Freight", "2847193", "MC-771203", "ACTIVE"),
    ("Redline Logistics", "3159042", "MC-882641", "ACTIVE"),
    ("Summit Haulage", "1638255", "MC-559310", "ACTIVE"),
    ("Prairie Express", "2290716", "MC-640087", "ACTIVE"),
    ("Coastal Carriers", "3471880", "MC-903155", "ACTIVE"),
    ("Ironhorse Transport", "1905527", "MC-418822", "ACTIVE"),
    ("Northstar Lines", "2764019", "MC-735561", "ACTIVE"),
    ("Desert Run Trucking", "3012648", "MC-287409", "SUSPENDED"),
]
# fictional owner codes (ISO 6346 owner code + 'U' category)
OWNER_CODES = {
    "Bluewater Freight": "BWF", "Redline Logistics": "RDL", "Summit Haulage": "SMH",
    "Prairie Express": "PRX", "Coastal Carriers": "CST", "Ironhorse Transport": "IHT",
    "Northstar Lines": "NSL", "Desert Run Trucking": "DRT",
}
STATES = ["TEXAS", "OHIO", "CALIFORNIA", "ILLINOIS", "GEORGIA", "ARIZONA", "INDIANA", "NEVADA"]
SHIPPERS = ["Acme Components", "Globex Retail", "Initech Supply", "Umbrella Foods", "Stark Parts", "Wayne Paper"]
DOCKS = [f"D{n:02d}" for n in range(1, 25)]


def random_plate(rng: random.Random) -> str:
    fmt = rng.choice(["DLLLDDD", "LLDDDDL", "LLLDDDD", "DLLDDDD", "LLDLDDD"])
    letters = "ABCDEFGHJKLMNPRSTVWXYZ"   # avoid I/O/Q/U as many DMVs do
    return "".join(rng.choice(letters) if c == "L" else rng.choice(string.digits) for c in fmt)


def random_trailer(rng: random.Random, owner: str) -> str:
    body = f"{owner}U{rng.randint(0, 999999):06d}"
    return format_iso6346(body + str(iso6346_check_digit(body)))


def random_seal(rng: random.Random) -> str:
    return rng.choice(["SL", "BS", "HS"]) + f"{rng.randint(1000000, 9999999)}"


def ensure_carriers(db: Database) -> None:
    for name, dot, mc, status in CARRIERS:
        if not db.one("SELECT id FROM carriers WHERE dot_number = ?", (dot,)):
            db.execute("INSERT INTO carriers (name, dot_number, mc_number, status) VALUES (?,?,?,?)",
                       (name, dot, mc, status))


def create_appointment(db: Database, rng: random.Random, minutes_from_now: int | None = None,
                       carrier: dict | None = None) -> dict:
    """Create an ASN + matching dock appointment (what the TMS/ERP would push to the YMS)."""
    if carrier is None:
        carrier = rng.choice(db.query("SELECT * FROM carriers WHERE status = 'ACTIVE'"))
    if minutes_from_now is None:
        minutes_from_now = rng.randint(-60, 240)
    when = utcnow() + timedelta(minutes=minutes_from_now)
    trailer = random_trailer(rng, OWNER_CODES.get(carrier["name"], "XXX"))
    seal = random_seal(rng)
    n = db.one("SELECT COUNT(*) AS n FROM asns")["n"] + 1
    asn_id = db.execute(
        """INSERT INTO asns (asn_number, po_number, carrier_id, trailer_number, seal_number, pallet_count,
                             shipper, expected_arrival) VALUES (?,?,?,?,?,?,?,?)""",
        (f"ASN-{when:%y%m%d}-{n:05d}-{rng.randint(10, 99)}", f"PO-{rng.randint(4500000, 4599999)}", carrier["id"],
         trailer, seal, rng.randint(6, 30), rng.choice(SHIPPERS), iso(when)))
    appt_id = db.execute(
        """INSERT INTO appointments (appt_ref, carrier_id, asn_id, tractor_plate, trailer_number, direction,
                                     scheduled_at, dock_door) VALUES (?,?,?,?,?,?,?,?)""",
        (f"APT-{when:%m%d}-{n:05d}{rng.randint(0, 9)}", carrier["id"], asn_id, random_plate(rng), trailer,
         "INBOUND", iso(when), rng.choice(DOCKS)))
    return db.one("SELECT * FROM appointments WHERE id = ?", (appt_id,))


def seed_demo(db: Database, n_appointments: int = 40, seed: int | None = None, reset: bool = False) -> None:
    rng = random.Random(seed)
    if reset:
        db.clear_all()
    ensure_carriers(db)
    for i in range(n_appointments):
        # spread around "now" so there are early, on-time and late arrivals
        create_appointment(db, rng, minutes_from_now=rng.randint(-90, 300))
