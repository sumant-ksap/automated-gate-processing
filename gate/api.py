"""REST + WebSocket API and the operator dashboard."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, settings as default_settings
from .db import Database, iso, utcnow
from .events import EventBus
from .gatecontrol import build_controller
from .lanes import LaneManager
from .pipeline import GatePipeline
from .predictive.service import InboundService
from .yard.allocator import YardAllocator, layout
from .comms.notifier import Notifier, verify_twilio_signature
from .comms.i18n import LANGUAGES
from .comms.directions import INSPECTION_BAY
from .seed import ensure_carriers, seed_demo
from .sim.scenarios import EXPECTED, reset_reservations
from .vision import ocr
from .vision.identifiers import clean, format_iso6346, is_valid_iso6346

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class OverrideIn(BaseModel):
    decision: str = Field(pattern="^(GRANTED|INSPECTION|HOLD|MANUAL_REVIEW|DENIED)$")
    by: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=500)


class SimulatorIn(BaseModel):
    running: bool
    interval_s: float | None = Field(default=None, ge=1, le=600)


class PingIn(BaseModel):
    """One GPS position report from an ELD / telematics provider (Samsara, Motive, Geotab ... webhook)."""
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_kph: float = Field(default=0, ge=0, le=200)
    heading: float | None = None
    timestamp: datetime | None = None
    vehicle_id: str | None = Field(default=None, max_length=40)
    plate: str | None = Field(default=None, max_length=12)
    appointment_ref: str | None = Field(default=None, max_length=40)


class FleetIn(BaseModel):
    running: bool


class AppointmentIn(BaseModel):
    carrier_dot: str
    tractor_plate: str
    trailer_number: str
    seal_number: str | None = None
    scheduled_at: datetime | None = None
    dock_door: str = "D01"
    po_number: str | None = None
    pallet_count: int = 0
    driver_phone: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")   # E.164
    driver_language: str = Field(default="en", pattern="^(en|es|hi|pa|ru)$")


class LanguageIn(BaseModel):
    language: str = Field(pattern="^(en|es|hi|pa|ru)$")


def create_app(cfg: Settings | None = None, start_workers: bool = True) -> FastAPI:
    cfg = cfg or default_settings
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    bus = EventBus()
    controller = build_controller(
        on_change=lambda lane, state: bus.publish({"type": "barrier", "lane_id": lane, "state": state}))
    pipeline = GatePipeline(db, cfg, controller, bus)
    lanes = LaneManager(pipeline, cfg.num_lanes, cfg.workers, bus)
    inbound = InboundService(db, cfg, bus, lanes)
    yard = YardAllocator(db, cfg, bus)
    notifier = Notifier(db, cfg, bus)
    pipeline.notifier = yard.notifier = inbound.notifier = notifier
    pipeline.allocator = yard

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if db.one("SELECT COUNT(*) AS n FROM carriers")["n"] == 0:
            seed_demo(db)
            log.info("Seeded demo carriers / ASNs / appointments")
        ensure_carriers(db)          # upgrades older databases (insurance dates, new carriers)
        if start_workers:
            threading.Thread(target=ocr.warmup, daemon=True).start()
            lanes.start()
            if cfg.sim_autostart:
                lanes.sim_running.set()
            inbound.start()
            yard.start()
            if cfg.fleet_autostart:
                inbound.fleet_running.set()
        yield
        inbound.stop()
        yard.stop()
        lanes.shutdown()

    app = FastAPI(title="Automated Gate Processing", version="1.0.0", lifespan=lifespan)
    app.state.db, app.state.lanes, app.state.pipeline, app.state.bus = db, lanes, pipeline, bus
    app.state.inbound = inbound
    app.state.yard = yard
    app.state.notifier = notifier

    # ---------------------------------------------------------------- lanes
    @app.get("/api/health")
    def health():
        return {"status": "ok", "lanes": len(lanes.lanes), "detector": pipeline.detector.name,
                "simulator": lanes.sim_running.is_set()}

    @app.get("/api/lanes")
    def get_lanes():
        return lanes.lane_states()

    def _lane(lane_id: int):
        if lane_id not in lanes.lanes:
            raise HTTPException(404, f"lane {lane_id} not found")

    @app.post("/api/lanes/{lane_id}/simulate")
    async def simulate(lane_id: int, scenario: str | None = Query(None)):
        _lane(lane_id)
        if scenario and scenario not in EXPECTED:
            raise HTTPException(400, f"scenario must be one of {sorted(EXPECTED)}")
        fut = await asyncio.to_thread(lanes.simulate_one, lane_id, scenario)
        return await asyncio.wrap_future(fut)

    @app.post("/api/lanes/{lane_id}/process")
    async def process_upload(lane_id: int, files: list[UploadFile] = File(...)):
        """Process images from a lane's cameras (one truck pass; 1..n images)."""
        _lane(lane_id)
        frames, names = [], []
        for f in files:
            data = np.frombuffer(await f.read(), np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                raise HTTPException(400, f"{f.filename} is not a readable image")
            if max(img.shape[:2]) > 1920:      # keep OCR fast on large photos
                s = 1920 / max(img.shape[:2])
                img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            frames.append(img)
            names.append(re.sub(r"[^A-Za-z0-9_-]", "", Path(f.filename or "cam").stem.split("__")[-1])[:20] or "cam")
        fut = lanes.submit(lane_id, frames, names, {"source": "upload"})
        return await asyncio.wrap_future(fut)

    # ------------------------------------------------------------ simulator
    @app.get("/api/simulator")
    def sim_state():
        return {"running": lanes.sim_running.is_set(), "interval_s": lanes.sim_interval,
                "scenarios": EXPECTED}

    @app.post("/api/simulator")
    def sim_set(body: SimulatorIn):
        if body.interval_s:
            lanes.sim_interval = body.interval_s
        (lanes.sim_running.set if body.running else lanes.sim_running.clear)()
        bus.publish({"type": "simulator", "running": body.running, "interval_s": lanes.sim_interval})
        return sim_state()

    # --------------------------------------------------------------- events
    @app.get("/api/events")
    def events(limit: int = Query(50, le=500), lane_id: int | None = None, decision: str | None = None):
        return db.recent_events(limit, lane_id, decision)

    @app.get("/api/events/{event_id}")
    def event(event_id: int):
        ev = db.get_event(event_id)
        if not ev:
            raise HTTPException(404, "event not found")
        if ev["appointment_id"]:
            ev["appointment"] = db.one("SELECT * FROM appointments WHERE id=?", (ev["appointment_id"],))
        if ev["asn_id"]:
            ev["asn"] = db.one("SELECT * FROM asns WHERE id=?", (ev["asn_id"],))
        return ev

    @app.post("/api/events/{event_id}/override")
    def override(event_id: int, body: OverrideIn):
        try:
            return pipeline.override(event_id, body.decision, body.by, body.note)
        except KeyError:
            raise HTTPException(404, "event not found")

    @app.get("/api/images/{name}")
    def image(name: str):
        if not re.fullmatch(r"[A-Za-z0-9_\-]+\.jpg", name):
            raise HTTPException(400, "bad image name")
        path = cfg.image_dir / name
        if not path.exists():
            raise HTTPException(404, "image not found")
        return FileResponse(path, media_type="image/jpeg")

    # ---------------------------------------------------------- yard data
    @app.get("/api/carriers")
    def carriers():
        return db.query("SELECT * FROM carriers ORDER BY name")

    @app.get("/api/appointments")
    def appointments(status: str | None = None, limit: int = Query(200, le=1000)):
        return db.appointments(status, limit)

    @app.post("/api/appointments", status_code=201)
    def add_appointment(body: AppointmentIn):
        carrier = db.one("SELECT * FROM carriers WHERE dot_number=?", (body.carrier_dot,))
        if not carrier:
            raise HTTPException(400, f"unknown carrier USDOT {body.carrier_dot}")
        if not is_valid_iso6346(body.trailer_number):
            raise HTTPException(400, "trailer_number must be a valid ISO 6346 code (check digit)")
        when = body.scheduled_at or utcnow()
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        trailer = format_iso6346(body.trailer_number)
        n = db.one("SELECT COUNT(*) AS n FROM asns")["n"] + 1
        asn_id = db.execute("""INSERT INTO asns (asn_number, po_number, carrier_id, trailer_number, seal_number,
                                pallet_count, expected_arrival) VALUES (?,?,?,?,?,?,?)""",
                            (f"ASN-M{n:06d}", body.po_number, carrier["id"], trailer,
                             clean(body.seal_number) if body.seal_number else None, body.pallet_count, iso(when)))
        appt_id = db.execute("""INSERT INTO appointments (appt_ref, carrier_id, asn_id, tractor_plate,
                                trailer_number, scheduled_at, dock_door, driver_phone, driver_language)
                                VALUES (?,?,?,?,?,?,?,?,?)""",
                             (f"APT-M{n:06d}", carrier["id"], asn_id, clean(body.tractor_plate), trailer,
                              iso(when), body.dock_door, body.driver_phone, body.driver_language))
        return db.one("SELECT * FROM appointments WHERE id=?", (appt_id,))

    @app.get("/api/asns")
    def asns(limit: int = Query(200, le=1000)):
        return db.query("""SELECT s.*, c.name AS carrier_name FROM asns s JOIN carriers c ON c.id=s.carrier_id
                           ORDER BY s.expected_arrival LIMIT ?""", (limit,))

    @app.get("/api/stats")
    def stats(hours: int = 24):
        since = iso(utcnow() - timedelta(hours=hours))
        out = db.stats(since)
        sim = db.query("""SELECT json_extract(detail, '$.sim_expected') AS exp, decision
                          FROM gate_events WHERE created_at >= ? AND json_extract(detail, '$.sim_expected') IS NOT NULL""",
                       (since,))
        out["sim_total"] = len(sim)
        out["sim_correct"] = sum(1 for r in sim if r["exp"] == r["decision"])
        out["pre_cleared"] = db.one("""SELECT COUNT(*) AS n FROM gate_events WHERE created_at >= ?
                                       AND reasons LIKE '%PRE_CLEARED%'""", (since,))["n"]
        out["inbound_en_route"] = db.one("SELECT COUNT(*) AS n FROM trips WHERE status='EN_ROUTE'")["n"]
        return out

    # ------------------------------------------------- predictive arrival & pre-check-in
    @app.get("/api/inbound")
    def inbound_board():
        """Inbound trucks with predicted arrival windows, surge alerts, conditions and ETA model metrics."""
        return inbound.snapshot()

    @app.post("/api/telematics/pings")
    def telematics(pings: list[PingIn]):
        """Ingest GPS pings. Trucks are matched to appointments by appointment_ref, vehicle_id or plate."""
        out = []
        for p in pings:
            d = p.model_dump()
            if d["timestamp"] is not None:
                ts = d["timestamp"] if d["timestamp"].tzinfo else d["timestamp"].replace(tzinfo=timezone.utc)
                d["timestamp"] = iso(ts)
            t = inbound.ingest_ping(d)
            out.append({"trip_id": t["id"], "status": t["status"], "appointment_id": t["appointment_id"],
                        "distance_km": t["distance_km"]})
        return out

    @app.get("/api/inbound/trips/{trip_id}")
    def trip_detail(trip_id: int):
        t = db.one("SELECT * FROM trips WHERE id=?", (trip_id,))
        if not t:
            raise HTTPException(404, "trip not found")
        t["packet"] = db.one("SELECT * FROM precheck_packets WHERE trip_id=? ORDER BY id DESC LIMIT 1", (trip_id,))
        t["appointment"] = db.one("""SELECT a.*, c.name AS carrier_name, c.dot_number FROM appointments a
                                     JOIN carriers c ON c.id=a.carrier_id WHERE a.id=?""", (t["appointment_id"],)) \
            if t["appointment_id"] else None
        t["predictions"] = db.query("SELECT made_at, remaining_km, eta_at, static_eta_at FROM eta_predictions "
                                    "WHERE trip_id=? ORDER BY id", (trip_id,))
        return t

    @app.get("/api/inbound/model")
    def eta_model():
        return inbound.model_info()

    @app.post("/api/inbound/model/retrain")
    def eta_retrain():
        inbound.retrain_async()
        return {"status": "training"}

    # ------------------------------------------------- intelligent dock & spot allocation
    @app.get("/api/yard")
    def yard_state():
        """Doors, spots, trucks in the yard, recent moves, zone congestion and the last batch comparison."""
        return yard.snapshot()

    @app.post("/api/yard/simultaneous-checkin")
    def yard_batch(n: int = Query(8, ge=2, le=15)):
        """Demo: n trucks clear the gate at the same moment; returns the AI plan vs first-come-first-served."""
        import random as _random
        return yard.simultaneous_check_in(n, _random.Random())

    @app.get("/api/yard/visits/{visit_id}")
    def yard_visit(visit_id: int):
        v = db.one("SELECT * FROM yard_visits WHERE id=?", (visit_id,))
        if not v:
            raise HTTPException(404, "visit not found")
        v["moves"] = db.query("SELECT * FROM yard_moves WHERE visit_id=? ORDER BY id", (visit_id,))
        return v

    # ------------------------------------------------- automated driver communication
    @app.get("/api/messages")
    def messages(limit: int = Query(100, le=500), appointment_id: int | None = None):
        sql = """SELECT m.*, a.appt_ref, a.tractor_plate, a.driver_token, c.name AS carrier_name
                 FROM driver_messages m LEFT JOIN appointments a ON a.id = m.appointment_id
                 LEFT JOIN carriers c ON c.id = a.carrier_id"""
        params: list = []
        if appointment_id:
            sql += " WHERE m.appointment_id = ?"
            params.append(appointment_id)
        rows = db.query(sql + " ORDER BY m.id DESC LIMIT ?", params + [limit])
        for r in rows:
            r["driver_link"] = notifier.link(r.pop("driver_token")) if r.get("driver_token") else None
        return {"messages": rows, "stats": notifier.stats(utcnow() - timedelta(hours=24)), "languages": LANGUAGES}

    def _driver(token: str) -> dict:
        a = notifier.by_token(token) if token else None
        if not a:
            raise HTTPException(404, "unknown or expired link")
        return a

    @app.get("/api/driver/{token}")
    def driver_view(token: str):
        """Everything the driver's phone page needs (the token in the SMS link is the only credential)."""
        a = _driver(token)
        msgs = db.query("""SELECT id, created_at, kind, body, payload, status, acked_at, language
                           FROM driver_messages WHERE appointment_id=? ORDER BY id DESC LIMIT 20""", (a["id"],))
        visit = db.one("SELECT * FROM yard_visits WHERE appointment_id=? ORDER BY id DESC LIMIT 1", (a["id"],))
        return {"appointment": {"ref": a["appt_ref"], "carrier": a["carrier_name"], "plate": a["tractor_plate"],
                                "trailer": a["trailer_number"], "status": a["status"], "scheduled_at": a["scheduled_at"]},
                "language": a["driver_language"] or "en", "languages": LANGUAGES,
                "current": msgs[0] if msgs else None, "history": msgs[1:],
                "visit": {"status": visit["status"], "resource_id": visit["resource_id"]} if visit else None,
                "yard": {"resources": [{k: r[k] for k in ("id", "kind", "x", "y", "capabilities")} for r in layout()],
                         "gate": {"x": 200.0, "y": 260.0},
                         "inspection_bay": {"x": INSPECTION_BAY[0], "y": INSPECTION_BAY[1]}}}

    @app.post("/api/driver/{token}/ack")
    def driver_ack(token: str):
        a = _driver(token)
        m = notifier.ack(a["id"], via="app")
        return {"acknowledged": bool(m)}

    @app.post("/api/driver/{token}/language")
    def driver_language(token: str, body: LanguageIn):
        a = _driver(token)
        m = notifier.set_language(a["id"], body.language)
        return {"language": body.language, "resent_message_id": m["id"] if m else None}

    @app.post("/api/sms/inbound")
    async def sms_inbound(request: Request):
        """Webhook for replies (Twilio format: From, Body). Replies: OK, EN/ES/HI/PA/RU, HELP."""
        form = {k: str(v) for k, v in (await request.form()).items()}
        if notifier.twilio_token and not verify_twilio_signature(
                notifier.twilio_token, str(request.url), form, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(403, "bad signature")
        reply = notifier.handle_reply(form.get("From", ""), form.get("Body", ""))
        from xml.sax.saxutils import escape
        xml = f"<Response>{f'<Message>{escape(reply)}</Message>' if reply else ''}</Response>"
        return Response(content=xml, media_type="application/xml")

    @app.get("/api/fleet-sim")
    def fleet_state():
        return {"running": inbound.fleet_running.is_set()}

    @app.post("/api/fleet-sim")
    def fleet_set(body: FleetIn):
        (inbound.fleet_running.set if body.running else inbound.fleet_running.clear)()
        bus.publish({"type": "fleet", "running": body.running})
        return fleet_state()

    @app.post("/api/fleet-sim/convoy")
    def fleet_convoy(n: int = Query(6, ge=2, le=10)):
        """Send a group of trucks that will arrive together (demonstrates surge detection)."""
        trips = inbound.spawn_convoy(n)
        return {"spawned": [t["id"] for t in trips]}

    @app.post("/api/admin/reset")
    def reset():
        lanes.sim_running.clear()
        inbound.reset()
        yard.reset()
        db.clear_all()
        reset_reservations()
        seed_demo(db)
        for p in cfg.image_dir.glob("*.jpg"):
            p.unlink(missing_ok=True)
        bus.publish({"type": "reset"})
        return {"status": "reset"}

    # ------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = bus.subscribe()
        try:
            while True:
                msg = await q.get()
                await websocket.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.unsubscribe(q)

    # Swagger UI only shows a file picker for `format: binary`; newer FastAPI emits
    # OpenAPI 3.1 `contentMediaType` instead, so add the format for /docs and Postman.
    default_openapi = app.openapi

    def openapi_with_file_pickers():
        if app.openapi_schema:
            return app.openapi_schema
        schema = default_openapi()

        def fix(node):
            if isinstance(node, dict):
                if node.get("type") == "string" and node.get("contentMediaType") == "application/octet-stream":
                    node["format"] = "binary"
                for v in node.values():
                    fix(v)
            elif isinstance(node, list):
                for v in node:
                    fix(v)
        fix(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_with_file_pickers

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
