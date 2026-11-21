"""Automated driver communication."""
import base64
import hashlib
import hmac
import random
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gate.api import create_app
from gate.comms.directions import route
from gate.comms.i18n import LANGUAGES, T
from gate.comms.notifier import compose, verify_twilio_signature
from gate.config import Settings
from gate.db import utcnow
from gate.seed import create_appointment


def test_every_language_has_every_message():
    keys = set(T["en"])
    for lang in LANGUAGES:
        missing = keys - set(T[lang])
        assert not missing, (lang, missing)
        for k in keys:   # placeholders must match English so .format() never fails
            assert sorted(T[lang][k].count(p) for p in ("{door}", "{spot}", "{m}", "{appt}")) == \
                   sorted(T["en"][k].count(p) for p in ("{door}", "{spot}", "{m}", "{appt}")), (lang, k)


def test_directions_turn_the_right_way():
    west_door = {"id": "D02", "kind": "DOOR", "x": 36.0, "y": 0.0, "capabilities": "REEFER"}
    steps, pts = route(west_door)
    assert steps[0][0] == "north" and steps[1] == ("left",) and steps[-1] == ("door_final", "D02")
    assert pts[0] == [200.0, 260.0] and pts[-1] == [36.0, 0.0]
    east_spot = {"id": "P-B18", "kind": "SPOT", "x": 343.0, "y": 130.0, "capabilities": ""}
    steps, _ = route(east_spot)
    assert ("right",) in steps and steps[-1] == ("spot_left", "P-B18")       # heading east: north side is left
    power_spot = {"id": "P-A03", "kind": "SPOT", "x": 58.0, "y": 90.0, "capabilities": "POWER"}
    steps, _ = route(power_spot)
    assert steps[-2] == ("spot_right", "P-A03") and steps[-1] == ("power",)


def test_compose_in_each_language():
    ctx = {"carrier": "Bluewater Freight", "plate": "7KXR392", "door": "D07", "dtype": ""}
    steps = [("north", 230), ("left",), ("continue", 80), ("door_final", "D07")]
    es = compose("door", "es", ctx, steps, "http://x/driver.html?t=abc")
    assert "PUERTA D07" in es and "1. Siga recto" in es and "Gire a la izquierda" in es and es.endswith("t=abc")
    for lang in LANGUAGES:
        text = compose("door", lang, ctx, steps, None)
        assert "D07" in text and "4. " in text


@pytest.fixture()
def app_client(tmp_path):
    cfg = Settings()
    cfg.data_dir = tmp_path
    cfg.num_lanes = 1
    app = create_app(cfg, start_workers=False)
    with TestClient(app) as client:
        yield app, client


def _granted_truck(app, client, lang="pa"):
    db = app.state.db
    ev = client.post("/api/lanes/1/simulate?scenario=healthy").json()
    assert ev["decision"] == "GRANTED"
    return ev, db.one("SELECT * FROM appointments WHERE id=?", (ev["appointment_id"],))


def test_gate_grant_sends_instructions_and_driver_page_works(app_client):
    app, client = app_client
    ev, appt = _granted_truck(app, client)
    msgs = client.get(f"/api/messages?appointment_id={appt['id']}").json()["messages"]
    assert len(msgs) == 1
    m = msgs[0]
    assert m["kind"] in ("door", "spot") and m["status"] == "DELIVERED" and m["language"] == appt["driver_language"]
    assert m["phone"] == appt["driver_phone"] and m["driver_link"].endswith(appt["driver_token"])
    yard_id = ev["detail"]["yard"]["resource_id"]
    assert yard_id in m["body"]

    page = client.get(f"/api/driver/{appt['driver_token']}").json()
    assert page["current"]["payload"]["dest"]["id"] == yard_id and page["current"]["payload"]["route"]
    assert client.get("/api/driver/not-a-token").status_code == 404

    # driver switches language -> the same instruction is re-sent in Spanish
    new_lang = "es" if appt["driver_language"] != "es" else "ru"
    r = client.post(f"/api/driver/{appt['driver_token']}/language", json={"language": new_lang}).json()
    assert r["resent_message_id"]
    page = client.get(f"/api/driver/{appt['driver_token']}").json()
    assert page["language"] == new_lang and page["current"]["language"] == new_lang
    assert T[new_lang]["lang_set"] in page["current"]["body"]

    assert client.post(f"/api/driver/{appt['driver_token']}/ack").json()["acknowledged"]
    assert client.get("/api/messages").json()["stats"]["ack_rate"] > 0


def test_sms_replies(app_client):
    app, client = app_client
    _, appt = _granted_truck(app, client)
    phone = appt["driver_phone"]
    r = client.post("/api/sms/inbound", data={"From": phone, "Body": "ok"})
    assert r.status_code == 200 and "<Message>" in r.text
    latest = app.state.db.one("SELECT * FROM driver_messages WHERE phone=? ORDER BY id DESC LIMIT 1", (phone,))
    assert latest["acked_at"] and latest["reply"]
    client.post("/api/sms/inbound", data={"From": phone, "Body": "HI"})
    assert app.state.db.one("SELECT driver_language FROM appointments WHERE id=?", (appt["id"],))["driver_language"] == "hi"
    assert "call you" in client.post("/api/sms/inbound", data={"From": phone, "Body": "HELP"}).text


def test_hold_and_inspection_messages(app_client):
    app, client = app_client
    for scenario, kind in (("broken_seal", "hold"), ("damaged", "inspection"), ("wrong_trailer", "review")):
        ev = client.post(f"/api/lanes/1/simulate?scenario={scenario}").json()
        msgs = client.get(f"/api/messages?appointment_id={ev['appointment_id']}").json()["messages"]
        assert msgs and msgs[0]["kind"] == kind, (scenario, ev["decision"])


def test_yard_moves_and_departure_notify_driver(app_client):
    app, client = app_client
    db, yard = app.state.db, app.state.yard
    db.execute("UPDATE yard_resources SET status='OCCUPIED' WHERE kind='DOOR'")      # yard full
    appt = create_appointment(db, random.Random(5), minutes_from_now=0)
    db.execute("UPDATE asns SET trailer_type='DRY_VAN', load_class='STANDARD' WHERE id=?", (appt["asn_id"],))
    placed = yard.check_in([appt["id"]])[0]
    assert placed["kind"] == "SPOT"
    db.execute("UPDATE yard_resources SET status='FREE' WHERE id='D12'")
    yard.tick()
    kinds = [m["kind"] for m in db.query("SELECT kind FROM driver_messages WHERE appointment_id=? ORDER BY id",
                                         (appt["id"],))]
    assert kinds == ["move"]
    yard.tick(now=utcnow() + timedelta(hours=3))
    kinds = [m["kind"] for m in db.query("SELECT kind FROM driver_messages WHERE appointment_id=? ORDER BY id",
                                         (appt["id"],))]
    assert kinds == ["move", "done"]


def test_twilio_signature_check():
    token, url = "secret-token", "https://gate.example.com/api/sms/inbound"
    params = {"From": "+12145550100", "Body": "OK", "To": "+18175550199"}
    payload = url + "".join(k + params[k] for k in sorted(params))
    good = base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
    assert verify_twilio_signature(token, url, params, good)
    assert not verify_twilio_signature(token, url, {**params, "Body": "HELP"}, good)
    assert not verify_twilio_signature(token, url, params, "")
