"""Proactive pre-check-in: verify credentials and build a digital check-in packet
before the truck reaches the gate, so the gate only has to confirm identity.

Checks performed (in a real deployment these call FMCSA / insurance / ERP APIs;
here they run against the YMS database):
  * carrier operating authority (ACTIVE)
  * carrier insurance on file and not expired
  * appointment is still open
  * ASN received, with a seal number to verify at the gate
  * trailer number is a valid ISO 6346 code
  * dock door free around the predicted arrival (re-assigned if not)
  * predicted arrival vs. booked slot (on time / early -> staging / late -> re-sequence)
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from ..config import Settings
from ..db import Database, iso, parse_iso, utcnow
from ..seed import DOCKS
from ..vision.identifiers import is_valid_iso6346


def _check(name: str, ok: bool, detail: str, severity: str = "info") -> dict:
    """severity: info (passed / note) | warn (gate will handle) | block (gate will refuse)."""
    return {"check": name, "ok": ok, "detail": detail, "severity": "info" if ok else severity}


def _free_door(db: Database, appt: dict, eta: datetime, minutes: int = 25) -> tuple[str, str | None]:
    """Return (door, conflicting appointment ref) - reassigns if the booked door is busy at the ETA."""
    lo, hi = iso(eta - timedelta(minutes=minutes)), iso(eta + timedelta(minutes=minutes))
    busy_rows = db.query("""SELECT a.dock_door, a.appt_ref FROM appointments a
                            LEFT JOIN trips t ON t.appointment_id = a.id AND t.status = 'EN_ROUTE'
                            WHERE a.id != ? AND a.status = 'SCHEDULED'
                              AND COALESCE(t.eta_at, a.scheduled_at) BETWEEN ? AND ?""",
                         (appt["id"], lo, hi))
    busy = {r["dock_door"]: r["appt_ref"] for r in busy_rows}
    if appt["dock_door"] not in busy:
        return appt["dock_door"], None
    for door in DOCKS:
        if door not in busy:
            return door, busy[appt["dock_door"]]
    return appt["dock_door"], busy[appt["dock_door"]]


def run_precheck(db: Database, settings: Settings, trip: dict, trigger: str) -> dict:
    now = utcnow()
    eta = parse_iso(trip["eta_at"]) if trip.get("eta_at") else now
    checks: list[dict] = []
    appt = carrier = asn = None
    if trip.get("appointment_id"):
        appt = db.one("SELECT * FROM appointments WHERE id = ?", (trip["appointment_id"],))
    if appt is None:
        checks.append(_check("Appointment", False, f"No appointment found for {trip.get('plate') or trip.get('vehicle_id')}"
                             " - truck will be turned away unless booked", "block"))
    else:
        carrier = db.one("SELECT * FROM carriers WHERE id = ?", (appt["carrier_id"],))
        asn = db.one("SELECT * FROM asns WHERE id = ?", (appt["asn_id"],)) if appt["asn_id"] else None
        checks.append(_check("Appointment", appt["status"] == "SCHEDULED",
                             f"{appt['appt_ref']} is {appt['status']}", "block"))

        checks.append(_check("Operating authority", carrier["status"] == "ACTIVE",
                             f"{carrier['name']} USDOT {carrier['dot_number']} / {carrier['mc_number']} is "
                             f"{carrier['status']}", "block"))
        exp = carrier.get("insurance_expiry")
        ins_ok = bool(exp) and exp >= date.today().isoformat()
        checks.append(_check("Insurance", ins_ok, f"Liability insurance valid until {exp}" if ins_ok else
                             f"Insurance expired {exp} - carrier must provide a new certificate" if exp else
                             "No insurance certificate on file", "block"))

        if asn:
            checks.append(_check("ASN", True, f"{asn['asn_number']} · {asn['po_number'] or '-'} · "
                                 f"{asn['pallet_count']} pallets from {asn['shipper'] or 'shipper'}"))
            checks.append(_check("Seal on file", bool(asn["seal_number"]),
                                 f"Seal {asn['seal_number']} will be verified by the rear camera"
                                 if asn["seal_number"] else "ASN has no seal number", "warn"))
        else:
            checks.append(_check("ASN", False, "No Advance Shipping Notice received yet", "warn"))

        tn = appt["trailer_number"] or ""
        checks.append(_check("Trailer number", is_valid_iso6346(tn), f"{tn} (ISO 6346 check digit valid)"
                             if is_valid_iso6346(tn) else f"'{tn}' is not a valid ISO 6346 number", "warn"))

        door, conflict = _free_door(db, appt, eta)
        if conflict and door != appt["dock_door"]:
            db.execute("UPDATE appointments SET dock_door = ? WHERE id = ?", (door, appt["id"]))
            checks.append(_check("Dock door", True, f"Door {appt['dock_door']} busy at predicted arrival "
                                 f"({conflict}) - re-assigned to {door}"))
            appt["dock_door"] = door
        elif conflict:
            checks.append(_check("Dock door", False, f"All doors busy around {eta:%H:%M} - truck will wait "
                                 "in staging", "warn"))
        else:
            checks.append(_check("Dock door", True, f"Door {door} free at predicted arrival"))

        delta = (eta - parse_iso(appt["scheduled_at"])).total_seconds() / 60
        if abs(delta) <= settings.on_time_minutes:
            checks.append(_check("Arrival vs slot", True, f"Predicted on time ({delta:+.0f} min)"))
        elif delta < 0:
            checks.append(_check("Arrival vs slot", True, f"Predicted {-delta:.0f} min early - staging lot reserved"))
        else:
            checks.append(_check("Arrival vs slot", True, f"Predicted {delta:.0f} min late - dock re-sequenced, "
                                 "late arrival will be accepted at the gate"))

    blocked = any(c["severity"] == "block" for c in checks)
    notes = any(c["severity"] == "warn" for c in checks)
    status = "BLOCKED" if blocked else "NOTES" if notes else "CLEARED"
    packet = {
        "appointment_ref": appt["appt_ref"] if appt else None,
        "carrier": carrier["name"] if carrier else None,
        "usdot": carrier["dot_number"] if carrier else None,
        "expected": {"plate": appt["tractor_plate"] if appt else trip.get("plate"),
                     "trailer": appt["trailer_number"] if appt else None,
                     "seal": asn["seal_number"] if asn else None},
        "asn": asn["asn_number"] if asn else None,
        "dock_door": appt["dock_door"] if appt else None,
        "eta": trip.get("eta_at"), "eta_window": [trip.get("eta_low_at"), trip.get("eta_high_at")],
        "gate_instruction": ("Confirm identity and seal only - credentials pre-verified" if status != "BLOCKED"
                             else "Do not admit - resolve blocking issues first"),
    }
    existing = db.one("SELECT id FROM precheck_packets WHERE trip_id = ?", (trip["id"],))
    if existing:
        db.execute("UPDATE precheck_packets SET updated_at=?, status=?, checks=?, packet=? WHERE id=?",
                   (iso(now), status, json.dumps(checks), json.dumps(packet), existing["id"]))
        pid = existing["id"]
    else:
        pid = db.execute("""INSERT INTO precheck_packets (trip_id, appointment_id, created_at, updated_at, status,
                            trigger, checks, packet) VALUES (?,?,?,?,?,?,?,?)""",
                         (trip["id"], appt["id"] if appt else None, iso(now), iso(now), status, trigger,
                          json.dumps(checks), json.dumps(packet)))
    return db.one("SELECT * FROM precheck_packets WHERE id = ?", (pid,))


def find_clusters(trips: list[dict], window_min: int, threshold: int, lanes: int,
                  manual_min: float, precleared_min: float) -> list[dict]:
    """Groups of trucks predicted to arrive within `window_min` of each other."""
    timed = sorted(((parse_iso(t["eta_at"]), t) for t in trips if t.get("eta_at") and t["status"] == "EN_ROUTE"),
                   key=lambda p: (p[0], p[1]["id"]))
    clusters, i = [], 0
    while i < len(timed):
        start = timed[i][0]
        group = [t for ts, t in timed[i:] if ts <= start + timedelta(minutes=window_min)]
        if len(group) >= threshold:
            n = len(group)
            end = parse_iso(group[-1]["eta_at"])
            span = max(window_min, (end - start).total_seconds() / 60)
            # simple queue estimate: work arriving minus lane capacity during the burst
            q_manual = max(0.0, n * manual_min / lanes - span)
            q_pre = max(0.0, n * precleared_min / lanes - span)
            clusters.append({
                "start": iso(start), "end": iso(end), "count": n,
                "trip_ids": [t["id"] for t in group],
                "plates": [t["plate"] for t in group],
                "lanes_needed_manual": min(lanes, -(-int(n * manual_min) // int(window_min))),
                "lanes_needed_precleared": max(1, min(lanes, -(-int(n * precleared_min) // int(window_min)))),
                "est_queue_manual_min": round(q_manual + manual_min, 1),
                "est_queue_precleared_min": round(q_pre + precleared_min, 1),
            })
            i += n
        else:
            i += 1
    return clusters
