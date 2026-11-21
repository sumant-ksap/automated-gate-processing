"""Intelligent dock & spot allocation."""
import itertools
import random
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gate.api import create_app
from gate.config import Settings
from gate.db import Database, iso, utcnow
from gate.seed import create_appointment, seed_demo
from gate.yard.allocator import YardAllocator, hungarian


def test_hungarian_is_optimal():
    rng = random.Random(0)
    for _ in range(150):
        n = rng.randint(1, 5)
        m = rng.randint(n, 7)
        cost = [[rng.uniform(0, 10) for _ in range(m)] for _ in range(n)]
        a = hungarian(cost)
        best = min(sum(cost[i][p[i]] for i in range(n)) for p in itertools.permutations(range(m), n))
        assert len(set(a)) == n
        assert sum(cost[i][a[i]] for i in range(n)) == pytest.approx(best)


@pytest.fixture()
def yard(tmp_path):
    cfg = Settings()
    cfg.data_dir = tmp_path
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    seed_demo(db, n_appointments=3, seed=1)
    return db, cfg, YardAllocator(db, cfg)


def _appt(db, rng, ttype, lclass, minutes=0, due=None):
    a = create_appointment(db, rng, minutes_from_now=minutes)
    db.execute("UPDATE asns SET trailer_type=?, load_class=?, due_by=? WHERE id=?",
               (ttype, lclass, iso(utcnow() + timedelta(minutes=due)) if due else None, a["asn_id"]))
    return a


def _occupy_all_doors_except(db, keep: set[str]):
    db.execute(f"""UPDATE yard_resources SET status='OCCUPIED' WHERE kind='DOOR'
                   AND id NOT IN ({','.join('?' * len(keep))})""", tuple(keep))


def test_equipment_rules_are_respected(yard):
    db, cfg, al = yard
    rng = random.Random(1)
    per = _appt(db, rng, "REEFER", "PERISHABLE")
    flat = _appt(db, rng, "FLATBED", "STANDARD")
    dry = _appt(db, rng, "DRY_VAN", "STANDARD")
    out = {o["plate"]: o for o in al.check_in([per["id"], flat["id"], dry["id"]])}
    caps = {r["id"]: r["capabilities"] for r in db.query("SELECT id, capabilities FROM yard_resources")}
    assert caps[out[per["tractor_plate"]]["resource_id"]] == "REEFER"
    assert caps[out[flat["tractor_plate"]]["resource_id"]] == "FLATBED"
    assert caps[out[dry["tractor_plate"]]["resource_id"]] not in ("REEFER", "FLATBED")


def test_scarce_door_goes_to_the_most_urgent_load(yard):
    db, cfg, al = yard
    rng = random.Random(2)
    _occupy_all_doors_except(db, {"D10"})                   # one dry dock left
    standard = _appt(db, rng, "DRY_VAN", "STANDARD", minutes=30)
    urgent = _appt(db, rng, "DRY_VAN", "CROSS_DOCK", minutes=0, due=40)
    out = {o["plate"]: o for o in al.check_in([standard["id"], urgent["id"]])}
    assert out[urgent["tractor_plate"]]["resource_id"] == "D10"
    assert out[standard["tractor_plate"]]["kind"] == "SPOT"


def test_perishable_waits_on_a_powered_spot_then_moves_to_freed_cold_dock(yard):
    db, cfg, al = yard
    rng = random.Random(3)
    _occupy_all_doors_except(db, set(["D07"]))
    per = _appt(db, rng, "REEFER", "PERISHABLE")
    o = al.check_in([per["id"]])[0]
    assert o["kind"] == "SPOT" and o["resource_id"].startswith("P-A")      # powered row
    assert "POWER" in db.one("SELECT capabilities FROM yard_resources WHERE id=?", (o["resource_id"],))["capabilities"]
    db.execute("UPDATE yard_resources SET status='FREE' WHERE id='D02'")    # a cold dock frees up
    al.tick()
    v = db.one("SELECT * FROM yard_visits WHERE id=?", (o["visit_id"],))
    assert v["status"] == "AT_DOOR" and v["resource_id"] == "D02" and v["jockey_m"] > 0


def test_unload_finishes_and_frees_the_door(yard):
    db, cfg, al = yard
    rng = random.Random(4)
    a = _appt(db, rng, "DRY_VAN", "STANDARD")
    o = al.check_in([a["id"]])[0]
    assert o["kind"] == "DOOR"
    al.tick(now=utcnow() + timedelta(hours=2))
    assert db.one("SELECT status FROM yard_visits WHERE id=?", (o["visit_id"],))["status"] == "DEPARTED"
    assert db.one("SELECT status FROM yard_resources WHERE id=?", (o["resource_id"],))["status"] == "FREE"


def test_ai_plan_never_worse_than_fcfs_on_equipment_or_urgency(yard):
    db, cfg, al = yard
    for seed in range(4):
        r = al.simultaneous_check_in(10, random.Random(seed))
        assert r["ai"]["equipment_violations"] == 0
        assert r["ai"]["urgent_waiting"] <= r["fcfs"]["urgent_waiting"]
        assert all(p["resource_id"] is not None for p in r["assignments"])


def test_gate_grant_returns_yard_destination(tmp_path):
    cfg = Settings()
    cfg.data_dir = tmp_path
    cfg.num_lanes = 1
    app = create_app(cfg, start_workers=False)
    with TestClient(app) as client:
        ev = client.post("/api/lanes/1/simulate?scenario=healthy").json()
        assert ev["decision"] == "GRANTED"
        assert ev["destination"].startswith(("Dock door", "Yard spot"))
        assert ev["detail"]["yard"]["visit_id"]
        y = client.get("/api/yard").json()
        assert any(v["event_id"] == ev["id"] for v in y["visits"])
        b = client.post("/api/yard/simultaneous-checkin?n=6").json()
        assert len(b["assignments"]) == 6 and "fcfs" in b and "ai" in b
