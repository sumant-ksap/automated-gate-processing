"""Traffic scenarios for the simulator.

Each scenario builds a TruckSpec from the yard database (so most trucks really
have an appointment) and records the decision the gate *should* make, so the
dashboard can report end-to-end accuracy of the automated pipeline.
"""
from __future__ import annotations

import random
import threading
from datetime import date, timedelta

from ..db import Database, iso, utcnow
from ..seed import create_appointment, random_plate, random_seal, random_trailer, STATES
from .synth import Damage, TruckSpec

SCENARIOS = [
    # name, weight, expected decision
    ("healthy", 50, "GRANTED"),
    ("damaged", 14, "INSPECTION"),
    ("broken_seal", 6, "HOLD"),
    ("missing_seal", 3, "HOLD"),
    ("seal_mismatch", 5, "HOLD"),
    ("no_appointment", 9, "DENIED"),
    ("suspended_carrier", 3, "DENIED"),
    ("wrong_trailer", 5, "MANUAL_REVIEW"),
    ("unreadable", 5, "MANUAL_REVIEW"),
]
EXPECTED = {n: e for n, _, e in SCENARIOS}

CAB_COLORS = [(40, 40, 170), (150, 70, 20), (30, 110, 30), (60, 60, 60), (20, 120, 200), (230, 230, 230), (110, 30, 110)]
TRAILER_COLORS = [(232, 234, 236), (225, 228, 230), (200, 200, 205), (60, 120, 30), (150, 90, 30), (215, 215, 210)]


_reserved: set[str] = set()          # appointments already sent to a lane
_reserve_lock = threading.Lock()


def reset_reservations() -> None:
    with _reserve_lock:
        _reserved.clear()


def _open_appointment(db: Database, rng: random.Random) -> dict:
    """An appointment due around now, never handed to two lanes at once.
    Asks the 'TMS' for more appointments when the pool runs low."""
    with _reserve_lock:
        appt = _pick_appointment(db, rng)
        _reserved.add(appt["appt_ref"])
        return appt


def _pick_appointment(db: Database, rng: random.Random) -> dict:
    now = utcnow()
    lo, hi = iso(now - timedelta(minutes=25)), iso(now + timedelta(minutes=25))
    rows = db.query("""SELECT a.*, c.name AS carrier_name, c.dot_number, s.seal_number
                       FROM appointments a JOIN carriers c ON c.id = a.carrier_id
                       LEFT JOIN asns s ON s.id = a.asn_id
                       WHERE a.status = 'SCHEDULED' AND a.scheduled_at BETWEEN ? AND ?
                         AND a.id NOT IN (SELECT appointment_id FROM trips
                                          WHERE status = 'EN_ROUTE' AND appointment_id IS NOT NULL)
                         AND c.insurance_expiry >= date('now')""", (lo, hi))
    rows = [r for r in rows if r["appt_ref"] not in _reserved]
    if len(rows) < 6:
        for _ in range(6):
            create_appointment(db, rng, minutes_from_now=rng.randint(-20, 20))
        return _pick_appointment(db, rng)
    return rng.choice(rows)


def _random_damage(rng: random.Random) -> list[Damage]:
    out = []
    for _ in range(rng.choice([1, 1, 2])):
        view = rng.choice(["left", "right", "rear"])
        kind = rng.choice(["dent", "rust", "hole"] + (["scratch"] if view != "rear" else []))
        out.append(Damage(kind, view, rng.uniform(0.05, 0.95), rng.uniform(0.1, 0.9), rng.uniform(0.9, 1.3)))
    # keep side damages apart from each other
    side = [d for d in out if d.view in ("left", "right") and d.view == out[0].view]
    if len(side) == 2 and abs(side[0].rx - side[1].rx) < 0.3:
        side[1].rx = (side[0].rx + 0.5) % 1.0
    return out


def build_scenario(db: Database, rng: random.Random, name: str | None = None) -> tuple[str, TruckSpec]:
    if name is None:
        name = rng.choices([s[0] for s in SCENARIOS], weights=[s[1] for s in SCENARIOS])[0]
    cab, trl = rng.choice(CAB_COLORS), rng.choice(TRAILER_COLORS)

    if name in ("no_appointment", "suspended_carrier"):
        status = "SUSPENDED" if name == "suspended_carrier" else "ACTIVE"
        carrier = rng.choice(db.query("SELECT * FROM carriers WHERE status = ?", (status,)))
        owner = {"Desert Run Trucking": "DRT"}.get(carrier["name"], "ZZT")
        spec = TruckSpec(carrier["name"], carrier["dot_number"], random_plate(rng), rng.choice(STATES),
                         random_trailer(rng, owner), random_seal(rng), cab_color=cab, trailer_color=trl)
        return name, spec

    appt = _open_appointment(db, rng)
    spec = TruckSpec(appt["carrier_name"], appt["dot_number"], appt["tractor_plate"], rng.choice(STATES),
                     appt["trailer_number"], appt["seal_number"], cab_color=cab, trailer_color=trl)
    if name == "damaged":
        spec.damages = _random_damage(rng)
    elif name == "broken_seal":
        spec.seal_state = "broken"
    elif name == "missing_seal":
        spec.seal_state = "missing"
    elif name == "seal_mismatch":
        spec.seal_number = random_seal(rng)
    elif name == "wrong_trailer":
        spec.trailer_number = random_trailer(rng, "QQX")
    elif name == "unreadable":
        spec.quality = "poor"
    return name, spec


def build_for_appointment(db: Database, rng: random.Random, appointment_id: int) -> tuple[str, str, TruckSpec]:
    """A truck that drove in from the inbound fleet: mostly healthy, sometimes damaged / bad seal.
    Returns (scenario, expected_decision, spec)."""
    appt = db.one("""SELECT a.*, c.name AS carrier_name, c.dot_number, c.insurance_expiry, s.seal_number
                     FROM appointments a JOIN carriers c ON c.id = a.carrier_id
                     LEFT JOIN asns s ON s.id = a.asn_id WHERE a.id = ?""", (appointment_id,))
    with _reserve_lock:
        _reserved.add(appt["appt_ref"])
    spec = TruckSpec(appt["carrier_name"], appt["dot_number"], appt["tractor_plate"], rng.choice(STATES),
                     appt["trailer_number"], appt["seal_number"], cab_color=rng.choice(CAB_COLORS),
                     trailer_color=rng.choice(TRAILER_COLORS))
    if appt["insurance_expiry"] and appt["insurance_expiry"] < date.today().isoformat():
        return "insurance_lapsed", "DENIED", spec
    r = rng.random()
    if r < 0.12:
        spec.damages = _random_damage(rng)
        return "damaged", "INSPECTION", spec
    if r < 0.17:
        spec.seal_state = "broken"
        return "broken_seal", "HOLD", spec
    return "healthy", "GRANTED", spec
