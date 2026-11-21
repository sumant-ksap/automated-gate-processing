"""Instant database matching + gate decision engine.

Cross-references what the cameras read (plate, trailer, USDOT, seal) against
carriers, Advance Shipping Notices (ASNs) and dock appointments, and turns the
result plus damage findings into one gate decision:

  GRANTED        gate opens, truck routed to its dock door (or staging if early)
  INSPECTION     gate opens, truck routed to the inspection bay (damage found)
  HOLD           gate stays closed, security called (seal broken/missing/mismatch)
  MANUAL_REVIEW  gate stays closed, guard verifies (unreadable / conflicting IDs)
  DENIED         gate stays closed, truck sent to turn-around (no appointment,
                 suspended carrier)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .config import Settings
from .db import Database, parse_iso
from .vision.damage import DamageFinding
from .vision.identifiers import Reading, clean, levenshtein, plate_variants

PRIORITY = ["DENIED", "HOLD", "MANUAL_REVIEW", "INSPECTION", "GRANTED"]
OPENS_GATE = {"GRANTED", "INSPECTION"}


@dataclass
class Reason:
    code: str
    message: str
    decision: str          # the decision this reason forces (or GRANTED for informational)

    def to_dict(self):
        return {"code": self.code, "message": self.message, "decision": self.decision}


@dataclass
class MatchResult:
    decision: str
    reasons: list[Reason]
    appointment: dict | None = None
    asn: dict | None = None
    carrier: dict | None = None
    destination: str = ""
    scores: list[dict] = field(default_factory=list)

    @property
    def opens_gate(self) -> bool:
        return self.decision in OPENS_GATE


def _plate_match(read: str, expected: str | None) -> int:
    """2 = exact, 1 = look-alike / one edit away, 0 = no match."""
    if not read or not expected:
        return 0
    exp = clean(expected)
    if read == exp:
        return 2
    if exp in plate_variants(read) or levenshtein(read, exp) <= 1:
        return 1
    return 0


def _usable(r: Reading | None, min_conf: float) -> Reading | None:
    return r if r is not None and r.confidence >= min_conf else None


def decide(db: Database, settings: Settings, readings: dict[str, Reading | None],
           damage: list[DamageFinding], now: datetime, uninspected: list[str] | tuple = ()) -> MatchResult:
    reasons: list[Reason] = []
    min_conf = settings.min_ocr_confidence
    plate = _usable(readings.get("plate"), min_conf)
    trailer = _usable(readings.get("trailer"), min_conf)
    dot = _usable(readings.get("dot"), min_conf)
    seal = _usable(readings.get("seal"), min_conf)

    for key, r in readings.items():
        if r is not None and r.confidence < min_conf:
            reasons.append(Reason("LOW_CONFIDENCE", f"{key} read '{r.value}' below confidence "
                                  f"threshold ({r.confidence:.2f})", "GRANTED"))

    if not any((plate, trailer, dot)):
        reasons.append(Reason("UNREADABLE", "No identifier could be read (camera obstructed, dirty "
                              "lens or weather) - guard must verify paperwork", "MANUAL_REVIEW"))
        return _finish(reasons, None, None, None, [])

    # -- carrier by USDOT -------------------------------------------------------
    carrier = None
    if dot:
        carrier = db.one("SELECT * FROM carriers WHERE dot_number = ?", (dot.value,))
        if carrier is None:
            reasons.append(Reason("UNKNOWN_CARRIER", f"USDOT {dot.value} is not a registered carrier", "DENIED"))
        elif carrier["status"] != "ACTIVE":
            reasons.append(Reason("CARRIER_SUSPENDED", f"{carrier['name']} (USDOT {dot.value}) is "
                                  f"{carrier['status']}", "DENIED"))
            return _finish(reasons, None, None, carrier, [])
        elif _insurance_lapsed(carrier):
            reasons.append(Reason("INSURANCE_EXPIRED", f"{carrier['name']} insurance expired "
                                  f"{carrier['insurance_expiry']}", "DENIED"))
            return _finish(reasons, None, None, carrier, [])
    else:
        reasons.append(Reason("NO_DOT", "USDOT number not read", "GRANTED"))

    # -- score every open appointment ------------------------------------------------
    candidates = db.query("""SELECT a.*, c.dot_number, c.name AS carrier_name
                             FROM appointments a JOIN carriers c ON c.id = a.carrier_id
                             WHERE a.status IN ('SCHEDULED', 'NO_SHOW')""")
    scored = []
    for a in candidates:
        s = {"trailer": 0, "plate": 0, "dot": 0}
        if trailer and a["trailer_number"] and clean(a["trailer_number"]) == clean(trailer.value):
            s["trailer"] = 3
        if plate:
            s["plate"] = _plate_match(plate.value, a["tractor_plate"])
        if dot and a["dot_number"] == dot.value:
            s["dot"] = 2
        total = sum(s.values())
        if total:
            scored.append((total, s, a))
    scored.sort(key=lambda t: -t[0])
    score_log = [{"appointment": a["appt_ref"], "score": t, **s} for t, s, a in scored[:5]]

    best = None
    if scored:
        total, s, a = scored[0]
        strong = [k for k in ("trailer", "plate") if s[k] > 0]
        # require two independent identifiers to agree
        if (s["trailer"] and (s["plate"] or s["dot"])) or (s["plate"] and s["dot"]):
            best = (s, a)
        elif strong:
            # only one identifier matches - look for a conflict that explains it
            if s["trailer"] and plate and not s["plate"]:
                msg = f"Trailer {trailer.value} matches {a['appt_ref']} but plate {plate.value} " \
                      f"does not (expected {a['tractor_plate']})"
            elif s["plate"] and trailer and not s["trailer"]:
                msg = f"Plate {plate.value} matches {a['appt_ref']} but trailer {trailer.value} " \
                      f"does not (expected {a['trailer_number']})"
            else:
                msg = f"Only one identifier matches {a['appt_ref']} - cannot confirm identity"
            reasons.append(Reason("IDENTITY_CONFLICT", msg, "MANUAL_REVIEW"))
            return _finish(reasons, a, None, carrier, score_log)

    if best is None and trailer:
        # the trailer already came through the gate: duplicate / cloned identity
        dup = db.one("""SELECT a.appt_ref, a.arrived_at FROM appointments a
                        WHERE a.status = 'ARRIVED' AND REPLACE(a.trailer_number, ' ', '') = ?
                        ORDER BY a.arrived_at DESC LIMIT 1""", (clean(trailer.value),))
        if dup:
            reasons.append(Reason("DUPLICATE_ARRIVAL", f"Trailer {trailer.value} already checked in on "
                                  f"{dup['appt_ref']} at {dup['arrived_at']} - possible cloned ID or "
                                  f"duplicate entry", "MANUAL_REVIEW"))
            return _finish(reasons, None, None, carrier, score_log)

    if best is None:
        read = [(k, r) for k, r in (("plate", plate), ("trailer", trailer), ("dot", dot)) if r]
        ids = ", ".join(f"{k}={r.value}" for k, r in read)
        if len(read) < 2:
            # one (possibly misread) identifier is not enough evidence to turn a truck away
            reasons.append(Reason("PARTIAL_READ", f"Only {ids} could be read and it matches no appointment - "
                                  "guard must verify paperwork", "MANUAL_REVIEW"))
        else:
            reasons.append(Reason("NO_APPOINTMENT", f"No scheduled appointment matches ({ids})", "DENIED"))
        return _finish(reasons, None, None, carrier, score_log)

    s, appt = best
    if trailer and not s["trailer"]:
        reasons.append(Reason("TRAILER_MISMATCH", f"Trailer {trailer.value} is not the trailer on "
                              f"{appt['appt_ref']} (expected {appt['trailer_number']})", "MANUAL_REVIEW"))
    if plate and not s["plate"] and appt["tractor_plate"]:
        reasons.append(Reason("TRACTOR_MISMATCH", f"Tractor plate {plate.value} differs from booked "
                              f"{appt['tractor_plate']} (tractor swap?)", "MANUAL_REVIEW"))
    if s["plate"] == 1:
        reasons.append(Reason("PLATE_FUZZY", f"Plate read '{plate.value}' accepted as "
                              f"'{appt['tractor_plate']}' (look-alike characters)", "GRANTED"))
    if carrier is None:
        carrier = db.one("SELECT * FROM carriers WHERE id = ?", (appt["carrier_id"],))
        if carrier and _insurance_lapsed(carrier):
            reasons.append(Reason("INSURANCE_EXPIRED", f"{carrier['name']} insurance expired "
                                  f"{carrier['insurance_expiry']}", "DENIED"))
    elif carrier["id"] != appt["carrier_id"]:
        reasons.append(Reason("IDENTITY_CONFLICT", f"USDOT {dot.value} belongs to {carrier['name']}, "
                              f"appointment is for {appt['carrier_name']}", "MANUAL_REVIEW"))
    asn = db.one("SELECT * FROM asns WHERE id = ?", (appt["asn_id"],)) if appt["asn_id"] else None

    # -- pre-check-in packet (prepared while the truck was en route) -------------
    packet = db.one("""SELECT * FROM precheck_packets WHERE appointment_id = ?
                       ORDER BY id DESC LIMIT 1""", (appt["id"],))
    forecast = packet is not None and packet["status"] != "BLOCKED"
    if forecast:
        reasons.append(Reason("PRE_CLEARED", f"Pre-cleared before arrival (packet #{packet['id']}, "
                              f"{packet['trigger']}) - credentials already verified", "GRANTED"))

    # -- appointment window -----------------------------------------------------
    sched = parse_iso(appt["scheduled_at"])
    delta_min = (now - sched).total_seconds() / 60       # +late / -early
    destination = f"Dock door {appt['dock_door']}"
    if forecast and abs(delta_min) > settings.on_time_minutes:
        word = "early" if delta_min < 0 else "late"
        reasons.append(Reason("FORECAST_OFF_SCHEDULE", f"{abs(delta_min):.0f} min {word}, as predicted en route - "
                              "dock re-sequenced, routed via staging", "GRANTED"))
        destination = f"Staging lot (then dock door {appt['dock_door']})"
    elif delta_min < -settings.early_minutes:
        reasons.append(Reason("TOO_EARLY", f"Arrived {-delta_min:.0f} min before appointment "
                              f"(max {settings.early_minutes})", "MANUAL_REVIEW"))
    elif delta_min > settings.late_minutes:
        reasons.append(Reason("MISSED_APPOINTMENT", f"Arrived {delta_min:.0f} min after appointment "
                              f"(max {settings.late_minutes})", "MANUAL_REVIEW"))
    elif abs(delta_min) > settings.on_time_minutes:
        word = "early" if delta_min < 0 else "late"
        reasons.append(Reason("OFF_SCHEDULE", f"{abs(delta_min):.0f} min {word} - routed to staging lot",
                              "GRANTED"))
        destination = f"Staging lot (then dock door {appt['dock_door']})"

    # -- seal verification against the ASN ----------------------------------------
    if asn and asn["seal_number"]:
        raw_seal = readings.get("seal")
        if raw_seal is None:
            reasons.append(Reason("SEAL_MISSING", f"No intact bolt seal detected - ASN {asn['asn_number']} "
                                  f"expects seal {asn['seal_number']}", "HOLD"))
        elif clean(raw_seal.value) != clean(asn["seal_number"]):
            if levenshtein(clean(raw_seal.value), clean(asn["seal_number"])) <= 1 and raw_seal.confidence < 0.9:
                reasons.append(Reason("SEAL_FUZZY", f"Seal read '{raw_seal.value}' accepted as "
                                      f"'{asn['seal_number']}' (one character off, low confidence)", "GRANTED"))
            else:
                reasons.append(Reason("SEAL_MISMATCH", f"Seal {raw_seal.value} does not match ASN seal "
                                      f"{asn['seal_number']} - possible tampering", "HOLD"))
        elif seal is None:
            reasons.append(Reason("SEAL_LOW_CONF", "Seal number matched but with low confidence", "GRANTED"))

    # -- damage ------------------------------------------------------------------
    significant = [d for d in damage if d.severity in ("moderate", "severe")]
    if significant:
        kinds = ", ".join(sorted({f"{d.severity} {d.kind}" for d in significant}))
        reasons.append(Reason("DAMAGE", f"Trailer damage detected ({kinds}) - routed to inspection bay",
                              "INSPECTION"))
    elif damage:
        reasons.append(Reason("MINOR_DAMAGE", f"{len(damage)} minor surface finding(s) logged", "GRANTED"))
    if uninspected:
        reasons.append(Reason("NOT_INSPECTED", f"Trailer could not be inspected ({', '.join(uninspected)} camera "
                              "image too blurred or dark) - routed to inspection bay", "INSPECTION"))

    return _finish(reasons, appt, asn, carrier, score_log, destination)


def _insurance_lapsed(carrier: dict) -> bool:
    exp = carrier.get("insurance_expiry")
    return bool(exp) and exp < date.today().isoformat()


def _finish(reasons, appt, asn, carrier, scores, destination: str = "") -> MatchResult:
    decision = "GRANTED"
    for r in reasons:
        if PRIORITY.index(r.decision) < PRIORITY.index(decision):
            decision = r.decision
    dest = {
        "GRANTED": destination or "Yard",
        "INSPECTION": "Inspection bay",
        "HOLD": "Security hold - remain at gate",
        "MANUAL_REVIEW": "Guard booth - manual verification",
        "DENIED": "Turn-around lane",
    }[decision]
    if decision == "GRANTED" and appt is not None:
        reasons.insert(0,Reason("VERIFIED", "Identity, appointment, seal and trailer condition verified", "GRANTED"))
    return MatchResult(decision, reasons, appt, asn, carrier, dest, scores)
