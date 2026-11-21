"""Predictive arrival & pre-check-in."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gate.api import create_app
from gate.config import Settings
from gate.db import Database, iso, utcnow
from gate.matching import decide
from gate.predictive.eta_model import EtaModel, simulate_history
from gate.predictive.geo import destination
from gate.predictive.precheck import find_clusters, run_precheck
from gate.seed import create_appointment, seed_demo
from gate.vision.identifiers import Reading

import random


def test_eta_model_beats_static_eta():
    X, y, n = simulate_history(n_trips=150, seed=3)
    m = EtaModel()
    metrics = m.fit(X, y, n)
    assert metrics["holdout_mae_min"] < 0.6 * metrics["static_mae_min"]
    assert 0.65 <= metrics["window_coverage"] <= 0.95
    eta, lo, hi = m.predict(list(X[0]), float(n[0]))
    assert lo < eta < hi
    # round-trip persistence
    m2 = EtaModel.from_dict(m.to_dict())
    assert m2.predict(list(X[0]), float(n[0])) == pytest.approx((eta, lo, hi))


def test_find_clusters():
    now = utcnow()
    trips = [{"id": i, "status": "EN_ROUTE", "plate": f"P{i}", "eta_at": iso(now + timedelta(minutes=m))}
             for i, m in enumerate([5, 6, 6, 7, 8, 9, 30, 50])]
    cl = find_clusters(trips, window_min=10, threshold=6, lanes=4, manual_min=4, precleared_min=0.5)
    assert len(cl) == 1 and cl[0]["count"] == 6 and cl[0]["trip_ids"] == [0, 1, 2, 3, 4, 5]
    assert cl[0]["lanes_needed_precleared"] < cl[0]["lanes_needed_manual"]


@pytest.fixture()
def db_cfg(tmp_path):
    cfg = Settings()
    cfg.data_dir = tmp_path
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    seed_demo(db, n_appointments=5, seed=1)
    return db, cfg


def _trip(db, appt, minutes_out=12):
    now = utcnow()
    tid = db.execute("""INSERT INTO trips (appointment_id, plate, source, started_at, lat, lon, speed_kph,
                        distance_km, eta_at, eta_low_at, eta_high_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (appt["id"], appt["tractor_plate"], "telematics", iso(now), 32.9, -96.8, 70, 15,
                      iso(now + timedelta(minutes=minutes_out)), iso(now + timedelta(minutes=minutes_out - 2)),
                      iso(now + timedelta(minutes=minutes_out + 3))))
    return db.one("SELECT * FROM trips WHERE id=?", (tid,))


def test_precheck_clears_good_carrier_and_blocks_lapsed_insurance(db_cfg):
    db, cfg = db_cfg
    rng = random.Random(1)
    good = create_appointment(db, rng, minutes_from_now=12)
    p = run_precheck(db, cfg, _trip(db, good), "horizon")
    assert p["status"] == "CLEARED", p["checks"]
    assert p["packet"]["expected"]["trailer"] == good["trailer_number"]

    lapsed = db.one("SELECT * FROM carriers WHERE name='Gulfline Cartage'")
    bad = create_appointment(db, rng, minutes_from_now=12, carrier=lapsed)
    p = run_precheck(db, cfg, _trip(db, bad), "surge")
    assert p["status"] == "BLOCKED"
    assert any(c["check"] == "Insurance" and not c["ok"] for c in p["checks"])


def test_precheck_reassigns_busy_dock_door(db_cfg):
    db, cfg = db_cfg
    rng = random.Random(2)
    a = create_appointment(db, rng, minutes_from_now=12)
    b = create_appointment(db, rng, minutes_from_now=14)
    db.execute("UPDATE appointments SET dock_door='D05' WHERE id IN (?,?)", (a["id"], b["id"]))
    p = run_precheck(db, cfg, _trip(db, db.one("SELECT * FROM appointments WHERE id=?", (a["id"],))), "horizon")
    assert p["packet"]["dock_door"] != "D05"
    assert db.one("SELECT dock_door FROM appointments WHERE id=?", (a["id"],))["dock_door"] != "D05"


def _readings(appt, carrier, seal):
    r = lambda v: Reading(v, 0.99)
    return {"plate": r(appt["tractor_plate"]), "trailer": r(appt["trailer_number"]),
            "dot": r(carrier["dot_number"]), "seal": r(seal)}


def test_gate_accepts_forecast_late_truck_and_denies_lapsed_insurance(db_cfg):
    db, cfg = db_cfg
    rng = random.Random(3)
    appt = create_appointment(db, rng, minutes_from_now=-300)          # 5 h late: normally MANUAL_REVIEW
    carrier = db.one("SELECT * FROM carriers WHERE id=?", (appt["carrier_id"],))
    seal = db.one("SELECT seal_number FROM asns WHERE id=?", (appt["asn_id"],))["seal_number"]
    res = decide(db, cfg, _readings(appt, carrier, seal), [], utcnow())
    assert res.decision == "MANUAL_REVIEW"

    run_precheck(db, cfg, _trip(db, appt, minutes_out=1), "horizon")   # lateness was forecast en route
    res = decide(db, cfg, _readings(appt, carrier, seal), [], utcnow())
    assert res.decision == "GRANTED"
    codes = {r.code for r in res.reasons}
    assert {"PRE_CLEARED", "FORECAST_OFF_SCHEDULE"} <= codes and "Staging" in res.destination

    lapsed = db.one("SELECT * FROM carriers WHERE name='Gulfline Cartage'")
    appt2 = create_appointment(db, rng, minutes_from_now=0, carrier=lapsed)
    seal2 = db.one("SELECT seal_number FROM asns WHERE id=?", (appt2["asn_id"],))["seal_number"]
    res = decide(db, cfg, _readings(appt2, lapsed, seal2), [], utcnow())
    assert res.decision == "DENIED" and any(r.code == "INSURANCE_EXPIRED" for r in res.reasons)


def test_telematics_api_tracks_truck_until_arrival(tmp_path):
    cfg = Settings()
    cfg.data_dir = tmp_path
    cfg.num_lanes = 1
    app = create_app(cfg, start_workers=False)
    with TestClient(app) as client:
        svc = app.state.inbound
        body = {"carrier_dot": "2847193", "tractor_plate": "TX12345", "trailer_number": "CSQU3054383",
                "seal_number": "SL1234567", "scheduled_at": iso(utcnow() + timedelta(minutes=20))}
        assert client.post("/api/appointments", json=body).status_code == 201

        lat, lon = destination(cfg.facility_lat, cfg.facility_lon, 45, 15)
        r = client.post("/api/telematics/pings", json=[{"plate": "TX12345", "vehicle_id": "ELD-77",
                                                         "lat": lat, "lon": lon, "speed_kph": 72}]).json()
        trip_id = r[0]["trip_id"]
        assert r[0]["appointment_id"] is not None and r[0]["status"] == "EN_ROUTE"

        svc.tick()                                         # predict ETA, maybe pre-check
        board = client.get("/api/inbound").json()
        t = next(t for t in board["trips"] if t["id"] == trip_id)
        assert t["eta_at"] and t["eta_low_at"] <= t["eta_at"] <= t["eta_high_at"]
        assert 10 < t["distance_km"] < 25

        # truck reaches the gate
        r = client.post("/api/telematics/pings", json=[{"vehicle_id": "ELD-77", "lat": cfg.facility_lat,
                                                         "lon": cfg.facility_lon, "speed_kph": 5}]).json()
        assert r[0]["trip_id"] == trip_id and r[0]["status"] == "ARRIVED"
        assert client.get(f"/api/inbound/trips/{trip_id}").json()["arrived_at"]


def test_fleet_simulator_spawns_and_detects_convoy_surge(tmp_path):
    cfg = Settings()
    cfg.data_dir = tmp_path
    app = create_app(cfg, start_workers=False)
    with TestClient(app) as client:
        assert len(client.post("/api/fleet-sim/convoy?n=7").json()["spawned"]) == 7
        svc = app.state.inbound
        svc.tick()
        board = client.get("/api/inbound").json()
        assert len([t for t in board["trips"] if t["status"] == "EN_ROUTE"]) == 7
        assert board["clusters"] and board["clusters"][0]["count"] >= 6
        # surge members get pre-checked even though they are > 30 min horizon? (they're ~15 min out anyway)
        assert all(t["packet_status"] for t in board["trips"])


def test_missed_appointments_become_no_show_but_still_match_at_gate(db_cfg):
    db, cfg = db_cfg
    from gate.predictive.service import InboundService
    rng = random.Random(5)
    old = create_appointment(db, rng, minutes_from_now=-(cfg.late_minutes + 30))
    fresh = create_appointment(db, rng, minutes_from_now=10)
    svc = InboundService(db, cfg)
    assert svc.mark_no_shows(utcnow()) >= 1
    assert db.one("SELECT status FROM appointments WHERE id=?", (old["id"],))["status"] == "NO_SHOW"
    assert db.one("SELECT status FROM appointments WHERE id=?", (fresh["id"],))["status"] == "SCHEDULED"
    # the truck turns up anyway: identified, but a guard decides
    carrier = db.one("SELECT * FROM carriers WHERE id=?", (old["carrier_id"],))
    seal = db.one("SELECT seal_number FROM asns WHERE id=?", (old["asn_id"],))["seal_number"]
    res = decide(db, cfg, _readings(old, carrier, seal), [], utcnow())
    assert res.decision == "MANUAL_REVIEW" and any(r.code == "MISSED_APPOINTMENT" for r in res.reasons)
