"""End-to-end: synthetic camera frames -> OCR -> damage -> matching -> decision."""
import random

import pytest

from gate.config import Settings
from gate.db import Database
from gate.gatecontrol import SimulatedGateController
from gate.pipeline import GatePipeline
from gate.seed import seed_demo
from gate.sim.scenarios import EXPECTED, build_scenario
from gate.sim.synth import Damage, TruckSpec, render_truck
from gate.vision.damage import PanelAnomalyDetector
from gate.vision.reader import extract_identifiers


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    cfg = Settings()
    cfg.data_dir = tmp_path_factory.mktemp("gate")
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    seed_demo(db, n_appointments=20, seed=3)
    ctrl = SimulatedGateController(auto_close_s=0.05)
    return db, GatePipeline(db, cfg, ctrl), ctrl


SPEC = dict(carrier_name="Bluewater Freight", dot_number="2847193", plate="7KXR392", plate_state="TEXAS",
            trailer_number="CSQU 305438 3", seal_number="SL4821930")


def test_reader_reads_all_identifiers():
    frames = render_truck(TruckSpec(**SPEC), seed=1)
    r, _, _ = extract_identifiers([frames["side"], frames["rear"]])
    assert r["plate"].value == "7KXR392"
    assert r["trailer"].value == "CSQU 305438 3"
    assert r["dot"].value == "2847193"
    assert r["seal"].value == "SL4821930"


@pytest.mark.parametrize("kind,view", [("dent", "side"), ("rust", "rear"), ("hole", "side"), ("scratch", "side")])
def test_damage_detector_finds_damage(kind, view):
    spec = TruckSpec(**SPEC, damages=[Damage(kind, view, 0.4, 0.5)])
    frames = [*render_truck(spec, seed=2).values()]
    _, boxes, _ = extract_identifiers(frames)
    det = PanelAnomalyDetector()
    found = [f for i, img in enumerate(frames) for f in det.detect(img, boxes, i)]
    assert any(f.kind == kind for f in found), found


def test_damage_detector_clean_truck_has_no_findings():
    frames = [*render_truck(TruckSpec(**SPEC), seed=4).values()]
    _, boxes, _ = extract_identifiers(frames)
    det = PanelAnomalyDetector()
    assert [f for i, img in enumerate(frames) for f in det.detect(img, boxes, i)] == []


@pytest.mark.parametrize("scenario", sorted(EXPECTED))
def test_scenario_decisions(env, scenario):
    db, pipe, ctrl = env
    rng = random.Random(hash(scenario) & 0xFFFF)
    _, spec = build_scenario(db, rng, scenario)
    frames = render_truck(spec, seed=rng.randint(0, 10**6))
    ev = pipe.process(2, [frames["side"], frames["rear"]], ["side", "rear"])
    assert ev["decision"] == EXPECTED[scenario], ev["reasons"]
    assert bool(ev["gate_opened"]) == (EXPECTED[scenario] in ("GRANTED", "INSPECTION"))
    if ev["gate_opened"] and ev["appointment_id"]:
        appt = db.one("SELECT status FROM appointments WHERE id=?", (ev["appointment_id"],))
        assert appt["status"] == "ARRIVED"


def test_override_opens_gate(env):
    db, pipe, ctrl = env
    rng = random.Random(9)
    _, spec = build_scenario(db, rng, "wrong_trailer")
    frames = render_truck(spec, seed=5)
    ev = pipe.process(3, [frames["side"], frames["rear"]], ["side", "rear"])
    assert ev["decision"] == "MANUAL_REVIEW" and not ev["gate_opened"]
    ev2 = pipe.override(ev["id"], "GRANTED", "guard-17", "paperwork verified")
    assert ev2["override_decision"] == "GRANTED" and ev2["gate_opened"] == 1
    assert ctrl.get(3) == "OPEN"
