"""Demo data: carriers, ASNs and dock appointments (all fictional)."""
from __future__ import annotations

import random
import string
from datetime import date, timedelta

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
    ("Gulfline Cartage", "2558304", "MC-661930", "ACTIVE"),       # insurance lapsed (see below)
]
INSURANCE_LAPSED = {"Gulfline Cartage"}
# fictional owner codes (ISO 6346 owner code + 'U' category)
OWNER_CODES = {
    "Bluewater Freight": "BWF", "Redline Logistics": "RDL", "Summit Haulage": "SMH",
    "Prairie Express": "PRX", "Coastal Carriers": "CST", "Ironhorse Transport": "IHT",
    "Northstar Lines": "NSL", "Desert Run Trucking": "DRT",
}
STATES = ["TEXAS", "OHIO", "CALIFORNIA", "ILLINOIS", "GEORGIA", "ARIZONA", "INDIANA", "NEVADA"]
SHIPPERS = ["Acme Components", "Globex Retail", "Initech Supply", "Umbrella Foods", "Stark Parts", "Wayne Paper"]
DOCKS = [f"D{n:02d}" for n in range(1, 25)]
# dock door equipment: cold docks for perishables, ground-level ramps for flatbeds, cross-dock doors
# next to the outbound side; the rest are standard dock-height doors for dry vans
DOOR_CAPS = {**{d: "REEFER" for d in DOCKS[0:4]}, **{d: "FLATBED" for d in DOCKS[4:6]},
             **{d: "" for d in DOCKS[6:20]}, **{d: "CROSS_DOCK" for d in DOCKS[20:24]}}


def random_load(rng: random.Random) -> tuple[str, str]:
    """(trailer_type, load_class) with a realistic mix."""
    ttype = rng.choices(["DRY_VAN", "REEFER", "FLATBED"], [0.65, 0.25, 0.10])[0]
    if ttype == "REEFER":
        return ttype, "PERISHABLE" if rng.random() < 0.85 else "STANDARD"
    if ttype == "DRY_VAN" and rng.random() < 0.15:
        return ttype, "CROSS_DOCK"
    return ttype, "STANDARD"


def planned_door(rng: random.Random, trailer_type: str, load_class: str) -> str:
    """Door the scheduler books at appointment time (the yard engine may change it at check-in)."""
    if load_class == "PERISHABLE":
        want = "REEFER"
    elif trailer_type == "FLATBED":
        want = "FLATBED"
    elif load_class == "CROSS_DOCK":
        want = "CROSS_DOCK"
    else:
        want = ""
    return rng.choice([d for d, cap in DOOR_CAPS.items() if cap == want])


def random_plate(rng: random.Random) -> str:
    fmt = rng.choice(["DLLLDDD", "LLDDDDL", "LLLDDDD", "DLLDDDD", "LLDLDDD"])
    letters = "ABCDEFGHJKLMNPRSTVWXYZ"   # avoid I/O/Q/U as many DMVs do
    return "".join(rng.choice(letters) if c == "L" else rng.choice(string.digits) for c in fmt)


def random_trailer(rng: random.Random, owner: str) -> str:
    body = f"{owner}U{rng.randint(0, 999999):06d}"
    return format_iso6346(body + str(iso6346_check_digit(body)))


DRIVER_LANGUAGES = (["en"] * 55) + (["es"] * 20) + (["pa"] * 12) + (["ru"] * 7) + (["hi"] * 6)


def random_driver_phone(rng: random.Random) -> str:
    """Fictional US numbers (555-0100..0199 is reserved for fiction)."""
    return f"+1{rng.choice(['214', '469', '972', '817'])}55501{rng.randint(0, 99):02d}"


def random_seal(rng: random.Random) -> str:
    return rng.choice(["SL", "BS", "HS"]) + f"{rng.randint(1000000, 9999999)}"


def ensure_carriers(db: Database) -> None:
    today = date.today()
    for name, dot, mc, status in CARRIERS:
        expiry = (today - timedelta(days=12) if name in INSURANCE_LAPSED else today + timedelta(days=200)).isoformat()
        if not db.one("SELECT id FROM carriers WHERE dot_number = ?", (dot,)):
            db.execute("INSERT INTO carriers (name, dot_number, mc_number, status, insurance_expiry) "
                       "VALUES (?,?,?,?,?)", (name, dot, mc, status, expiry))
        else:
            db.execute("UPDATE carriers SET insurance_expiry = ? WHERE dot_number = ? AND insurance_expiry IS NULL",
                       (expiry, dot))


def bookable_carriers(db: Database) -> list[dict]:
    """Carriers allowed to book appointments: active authority and valid insurance."""
    return db.query("SELECT * FROM carriers WHERE status = 'ACTIVE' AND insurance_expiry >= ?",
                    (date.today().isoformat(),))


def create_appointment(db: Database, rng: random.Random, minutes_from_now: int | None = None,
                       carrier: dict | None = None) -> dict:
    """Create an ASN + matching dock appointment (what the TMS/ERP would push to the YMS)."""
    if carrier is None:
        carrier = rng.choice(bookable_carriers(db))
    if minutes_from_now is None:
        minutes_from_now = rng.randint(-60, 240)
    when = utcnow() + timedelta(minutes=minutes_from_now)
    trailer = random_trailer(rng, OWNER_CODES.get(carrier["name"], "XXX"))
    seal = random_seal(rng)
    ttype, lclass = random_load(rng)
    due_by = iso(when + timedelta(minutes=rng.randint(45, 180))) if lclass == "CROSS_DOCK" else None
    n = db.one("SELECT COUNT(*) AS n FROM asns")["n"] + 1
    asn_id = db.execute(
        """INSERT INTO asns (asn_number, po_number, carrier_id, trailer_number, seal_number, pallet_count,
                             shipper, expected_arrival, trailer_type, load_class, due_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (f"ASN-{when:%y%m%d}-{n:05d}-{rng.randint(10, 99)}", f"PO-{rng.randint(4500000, 4599999)}", carrier["id"],
         trailer, seal, rng.randint(6, 30), rng.choice(SHIPPERS), iso(when), ttype, lclass, due_by))
    appt_id = db.execute(
        """INSERT INTO appointments (appt_ref, carrier_id, asn_id, tractor_plate, trailer_number, direction,
                                     scheduled_at, dock_door, driver_phone, driver_language)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (f"APT-{when:%m%d}-{n:05d}{rng.randint(0, 9)}", carrier["id"], asn_id, random_plate(rng), trailer,
         "INBOUND", iso(when), planned_door(rng, ttype, lclass), random_driver_phone(rng),
         rng.choice(DRIVER_LANGUAGES)))
    return db.one("SELECT * FROM appointments WHERE id = ?", (appt_id,))


def seed_demo(db: Database, n_appointments: int = 40, seed: int | None = None, reset: bool = False) -> None:
    rng = random.Random(seed)
    if reset:
        db.clear_all()
    ensure_carriers(db)
    for i in range(n_appointments):
        # spread around "now" so there are early, on-time and late arrivals
        create_appointment(db, rng, minutes_from_now=rng.randint(-90, 300))
