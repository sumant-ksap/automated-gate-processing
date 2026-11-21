import cv2
import pytest
from fastapi.testclient import TestClient

from gate.api import create_app
from gate.config import Settings
from gate.sim.synth import TruckSpec, render_truck


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cfg = Settings()
    cfg.data_dir = tmp_path_factory.mktemp("api")
    cfg.num_lanes = 2
    app = create_app(cfg, start_workers=False)
    with TestClient(app) as c:
        yield c


def test_health_and_lanes(client):
    assert client.get("/api/health").json()["status"] == "ok"
    lanes = client.get("/api/lanes").json()
    assert [l["id"] for l in lanes] == [1, 2]


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Automated Gate Processing" in r.text


def test_simulate_and_event_flow(client):
    ev = client.post("/api/lanes/1/simulate?scenario=healthy").json()
    assert ev["decision"] == "GRANTED"
    detail = client.get(f"/api/events/{ev['id']}").json()
    assert detail["appointment"]["status"] == "ARRIVED"
    img = client.get(f"/api/images/{ev['images'][0]['annotated']}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.get("/api/stats").json()["sim_correct"] >= 1


def test_upload_unscheduled_truck_is_denied(client):
    spec = TruckSpec("Bluewater Freight", "2847193", "8KTM214", "OHIO", "CSQU 305438 3", "SL1111111")
    frames = render_truck(spec, seed=1)
    files = [("files", (f"{k}.jpg", cv2.imencode(".jpg", v)[1].tobytes(), "image/jpeg")) for k, v in frames.items()]
    ev = client.post("/api/lanes/2/process", files=files).json()
    assert ev["decision"] == "DENIED"
    assert ev["plate"] == "8KTM214"


def test_create_appointment_then_truck_is_granted(client):
    body = {"carrier_dot": "3159042", "tractor_plate": "RD12345", "trailer_number": "CSQU3054383",
            "seal_number": "BS7654321", "dock_door": "D07"}
    r = client.post("/api/appointments", json=body)
    assert r.status_code == 201, r.text
    spec = TruckSpec("Redline Logistics", "3159042", "RD12345", "TEXAS", "CSQU 305438 3", "BS7654321")
    frames = render_truck(spec, seed=8)
    files = [("files", (f"{k}.jpg", cv2.imencode(".jpg", v)[1].tobytes(), "image/jpeg")) for k, v in frames.items()]
    ev = client.post("/api/lanes/1/process", files=files).json()
    assert ev["decision"] == "GRANTED", ev["reasons"]
    # the yard engine keeps the booked door unless congestion / availability gives a real reason to move;
    # either way this dry van must land at a dry dock (not a cold dock or a ground-level flatbed ramp)
    assert ev["destination"].startswith("Dock door") and "(dry dock)" in ev["destination"]
    yard = ev["detail"]["yard"]
    if "D07" not in ev["destination"]:
        assert any("booked door D07" in r for r in yard["reasons"]), yard["reasons"]


def test_bad_inputs(client):
    assert client.post("/api/appointments", json={"carrier_dot": "3159042", "tractor_plate": "X",
                                                  "trailer_number": "CSQU3054384"}).status_code == 400
    assert client.get("/api/images/..%2Fgate.db").status_code in (400, 404)
    assert client.post("/api/events/999999/override", json={"decision": "GRANTED", "by": "x"}).status_code == 404
