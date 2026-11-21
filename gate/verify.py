"""Acceptance check of the 4 feature areas, bullet by bullet, on a fresh temporary database.

    python -m gate verify
"""
import logging
import random
import tempfile
import time
from collections import Counter
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from .api import create_app
from .comms.i18n import LANGUAGES
from .comms.notifier import compose
from .config import Settings
from .db import parse_iso
from .predictive.eta_model import simulate_history
from .sim.scenarios import EXPECTED, build_for_appointment, build_scenario
from .sim.synth import render_truck


def line(title):
    print(f"\n=== {title}")


def main(seed: int = 2026) -> None:
    logging.disable(logging.WARNING)
    cfg = Settings()
    cfg.data_dir = Path(tempfile.mkdtemp(prefix="gate-accept-"))
    cfg.num_lanes = 4
    app = create_app(cfg, start_workers=False)
    client = TestClient(app).__enter__()
    db, pipe, lanes, inbound, yard = (app.state.db, app.state.pipeline, app.state.lanes,
                                      app.state.inbound, app.state.yard)
    rng = random.Random(seed)

    # ------------------------------------------------------------------ 1. automated gate
    line("1. Automated gate processing")
    names = [n for n in EXPECTED]
    ocr = Counter()
    dmg = Counter()
    dec_ok = dec_n = 0
    gate_ok = gate_n = 0
    times = []
    for i in range(72):
        name = names[i % len(names)]
        _, spec = build_scenario(db, rng, name)
        frames = render_truck(spec, rng.randint(0, 2**31))
        ev = pipe.process(1 + i % 4, list(frames.values()), list(frames.keys()))
        times.append(ev["processing_ms"])
        dec_n += 1
        dec_ok += ev["decision"] == EXPECTED[name]
        if spec.quality == "good":
            for field, got, want in (("plate", ev["plate"], spec.plate), ("trailer", ev["trailer_number"], spec.trailer_number),
                                     ("dot", ev["dot_number"], spec.dot_number)):
                ocr[field + "_n"] += 1
                ocr[field] += got == want
                ocr[field + "_fuzzy"] += got == want or (got is not None and sum(a != b for a, b in zip(got, want)) <= 1
                                                         and len(got) == len(want))
            if spec.seal_state == "intact":
                ocr["seal_n"] += 1
                ocr["seal"] += ev["seal_number"] == spec.seal_number
        if name == "damaged":
            dmg["damaged_n"] += 1
            dmg["damaged_found"] += any(d["severity"] in ("moderate", "severe") for d in ev["damage"])
        elif spec.quality == "good":
            dmg["clean_n"] += 1
            dmg["false_alarm"] += any(d["severity"] in ("moderate", "severe") for d in ev["damage"])
        if name in ("broken_seal", "missing_seal"):
            dmg["seal_n"] += 1
            dmg["seal_flagged"] += any(r["code"] == "SEAL_MISSING" for r in ev["reasons"])
        if ev["decision"] == "GRANTED":
            gate_n += 1
            appt = db.one("SELECT status FROM appointments WHERE id=?", (ev["appointment_id"],))
            gate_ok += ev["gate_opened"] == 1 and appt["status"] == "ARRIVED"
    for f in ("plate", "trailer", "dot", "seal"):
        print(f"  OCR {f:8} exact {ocr[f]}/{ocr[f + '_n']}" + (f"   (exact or 1 look-alike char: {ocr[f + '_fuzzy']})" if f != "seal" else ""))
    print(f"  Damage found on damaged trailers: {dmg['damaged_found']}/{dmg['damaged_n']}")
    print(f"  False damage alarms on clean trailers: {dmg['false_alarm']}/{dmg['clean_n']}")
    print(f"  Broken / missing seals flagged: {dmg['seal_flagged']}/{dmg['seal_n']}")
    print(f"  Correct gate decision (all 9 situations): {dec_ok}/{dec_n}")
    print(f"  GRANTED -> barrier opened + appointment marked arrived: {gate_ok}/{gate_n}")
    print(f"  Decision time per truck (3 cameras): median {sorted(times)[len(times) // 2]} ms, max {max(times)} ms")

    # multiple lanes at once
    futs, t0 = [], time.perf_counter()
    for lane in range(1, 5):
        _, spec = build_scenario(db, rng, "healthy")
        fr = render_truck(spec, rng.randint(0, 2**31))
        futs.append(lanes.submit(lane, list(fr.values()), list(fr.keys())))
    evs = [f.result() for f in futs]
    wall = (time.perf_counter() - t0) * 1000
    serial = sum(e["processing_ms"] for e in evs)
    print(f"  4 lanes at once: wall {wall:.0f} ms vs {serial} ms if one after another "
          f"-> {serial / wall:.1f}x parallel, decisions {[e['decision'] for e in evs]}")

    # ------------------------------------------------------------------ 2. predictive arrival
    line("2. Predictive arrival & pre-check-in")
    m = inbound.retrain()
    print(f"  ETA model (held-out simulated trips): avg error {m['holdout_mae_min']} min vs static {m['static_mae_min']} min,"
          f" window contains actual {m['window_coverage'] * 100:.0f}%")
    X, y, n = simulate_history(120, seed=4242)          # completely new trips, never seen in training
    pred = np.array([inbound.model.predict(list(x), s)[0] for x, s in zip(X, n)])
    lo_hi = [inbound.model.predict(list(x), s)[1:] for x, s in zip(X, n)]
    actual = n * np.exp(y)
    cov = np.mean([lo <= a <= hi for (lo, hi), a in zip(lo_hi, actual)])
    print(f"  ETA on 120 brand-new trips: avg error {np.mean(np.abs(pred - actual)):.2f} min vs static "
          f"{np.mean(np.abs(n - actual)):.2f} min, window coverage {cov * 100:.0f}%")
    r = client.post("/api/telematics/pings", json=[{"vehicle_id": "ELD-9", "plate": "ZZ1234Z", "lat": 32.95, "lon": -96.70,
                                                     "speed_kph": 75}]).json()
    inbound.tick()
    t = client.get(f"/api/inbound/trips/{r[0]['trip_id']}").json()
    print(f"  GPS ping via telematics API -> trip tracked, ETA {t['eta_at'][11:16]} window "
          f"{t['eta_low_at'][11:16]}-{t['eta_high_at'][11:16]} UTC, conditions {sorted(t['conditions'])[:4]}")
    convoy = inbound.spawn_convoy(7)
    inbound.tick()
    snap = inbound.snapshot()
    cl = snap["clusters"]
    ids = {c["id"] for c in convoy}
    packets = db.query(f"SELECT status, trigger FROM precheck_packets WHERE trip_id IN ({','.join('?' * len(ids))})",
                       tuple(ids))
    print(f"  Convoy of 7 -> surge alerts {[(c['count'], c['lanes_needed_manual'], c['lanes_needed_precleared']) for c in cl]}"
          f" (count, lanes manual, lanes pre-cleared)")
    print(f"  Digital check-in packets prepared before arrival: {len(packets)}/7  statuses {Counter(p['status'] for p in packets)}"
          f" triggers {Counter(p['trigger'] for p in packets)}")
    # one convoy truck reaches the gate
    tr = db.one("SELECT * FROM trips WHERE id=?", (convoy[0]["id"],))
    _, _, spec = build_for_appointment(db, rng, tr["appointment_id"])
    fr = render_truck(spec, 7)
    ev = pipe.process(1, list(fr.values()), list(fr.keys()))
    print(f"  Convoy truck at the gate: {ev['decision']} in {ev['processing_ms']} ms, reasons "
          f"{[x['code'] for x in ev['reasons']]}")

    # ------------------------------------------------------------------ 3. dock & spot allocation
    line("3. Intelligent dock & spot allocation")
    tot = {"ai": Counter(), "fcfs": Counter()}
    prio_ok = prio_n = 0
    for k in range(10):
        db.execute("UPDATE yard_resources SET status='FREE', visit_id=NULL")
        yard.simultaneous_check_in(16, random.Random(500 + k))         # busy yard
        b = yard.simultaneous_check_in(8, random.Random(k))
        for s in ("ai", "fcfs"):
            for key in ("travel_m", "equipment_violations", "urgent_waiting", "max_trucks_one_zone", "at_doors"):
                tot[s][key] += b[s][key]
        for a in b["assignments"]:
            prio_n += 1
            prio_ok += a["resource_id"] is not None
    print("  10 batches of 8 simultaneous check-ins into a busy yard (totals):")
    for key, label in (("equipment_violations", "equipment violations"), ("urgent_waiting", "urgent loads unfairly waiting"),
                       ("at_doors", "sent straight to a suitable door"), ("travel_m", "yard travel (m)"),
                       ("max_trucks_one_zone", "peak trucks per zone (sum)")):
        print(f"    {label:36} AI {tot['ai'][key]:7}   first-come-first-served {tot['fcfs'][key]:7}")
    batch = yard.last_batch["assignments"]
    print("  Example explanations:")
    for a in batch[:3]:
        print(f"    {a['plate']} -> {a['destination']} | {a['reasons'][0]}")

    # ------------------------------------------------------------------ 4. driver communication
    line("4. Automated driver communication")
    evs = db.query("SELECT * FROM gate_events WHERE appointment_id IS NOT NULL")
    sent = lang_ok = 0
    lat_max = 0.0
    turn_ok = turn_n = 0
    for e in evs:
        msgs = db.query("SELECT * FROM driver_messages WHERE event_id=? ORDER BY id", (e["id"],))
        if not msgs:
            continue
        sent += 1
        m0 = msgs[0]
        d = (parse_iso(m0["created_at"]) - parse_iso(e["created_at"])).total_seconds()
        lat_max = max(lat_max, d)
        a = db.one("SELECT driver_language FROM appointments WHERE id=?", (e["appointment_id"],))
        lang_ok += m0["language"] == a["driver_language"]
        if m0["kind"] in ("door", "spot"):
            turn_n += 1
            res = (e["detail"].get("yard") or {}).get("resource_id")
            turn_ok += res is not None and res in m0["body"] and "1. " in m0["body"] and "2. " in m0["body"]
    print(f"  Gate events with an appointment that produced a driver message: {sent}/{len(evs)}")
    print(f"  Message language = driver's preferred language: {lang_ok}/{sent}")
    print(f"  Door/spot messages with turn-by-turn steps + the assigned door/spot: {turn_ok}/{turn_n}")
    print(f"  Truck at cameras -> SMS sent (includes reading time, 4 lanes busy): max {lat_max:.1f} s")
    print(f"  Languages in use: {dict(Counter(m['language'] for m in db.query('SELECT language FROM driver_messages')))}")
    ctx = {"carrier": "Bluewater Freight", "plate": "7KXR392", "door": "D07", "dtype": ""}
    ok_lang = [lg for lg in LANGUAGES if "D07" in compose("door", lg, ctx, [("north", 230), ("left",), ("door_final", "D07")], None)]
    print(f"  Templates render in all languages: {ok_lang}")
    m = db.one("SELECT m.*, a.driver_token FROM driver_messages m JOIN appointments a ON a.id=m.appointment_id "
               "WHERE m.kind IN ('door','spot') ORDER BY m.id DESC LIMIT 1")
    page = client.get(f"/api/driver/{m['driver_token']}").json()
    print(f"  Driver phone page: instruction '{page['current']['payload']['headline']}', route points "
          f"{len(page['current']['payload']['route'])}, destination {page['current']['payload']['dest']['id']}")
    ack = client.post(f"/api/driver/{m['driver_token']}/ack").json()
    client.post("/api/sms/inbound", data={"From": m["phone"], "Body": "ES"})
    print(f"  Driver confirms in app: {ack['acknowledged']}; SMS reply 'ES' -> language now "
          f"{db.one('SELECT driver_language FROM appointments WHERE id=?', (m['appointment_id'],))['driver_language']}")
    print("\nDONE")
