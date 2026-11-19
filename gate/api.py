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
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, settings as default_settings
from .db import Database, iso, utcnow
from .events import EventBus
from .gatecontrol import build_controller
from .lanes import LaneManager
from .pipeline import GatePipeline
from .seed import seed_demo
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


class AppointmentIn(BaseModel):
    carrier_dot: str
    tractor_plate: str
    trailer_number: str
    seal_number: str | None = None
    scheduled_at: datetime | None = None
    dock_door: str = "D01"
    po_number: str | None = None
    pallet_count: int = 0


def create_app(cfg: Settings | None = None, start_workers: bool = True) -> FastAPI:
    cfg = cfg or default_settings
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    bus = EventBus()
    controller = build_controller(
        on_change=lambda lane, state: bus.publish({"type": "barrier", "lane_id": lane, "state": state}))
    pipeline = GatePipeline(db, cfg, controller, bus)
    lanes = LaneManager(pipeline, cfg.num_lanes, cfg.workers, bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if db.one("SELECT COUNT(*) AS n FROM carriers")["n"] == 0:
            seed_demo(db)
            log.info("Seeded demo carriers / ASNs / appointments")
        if start_workers:
            threading.Thread(target=ocr.warmup, daemon=True).start()
            lanes.start()
            if cfg.sim_autostart:
                lanes.sim_running.set()
        yield
        lanes.shutdown()

    app = FastAPI(title="Automated Gate Processing", version="1.0.0", lifespan=lifespan)
    app.state.db, app.state.lanes, app.state.pipeline, app.state.bus = db, lanes, pipeline, bus

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
                                trailer_number, scheduled_at, dock_door) VALUES (?,?,?,?,?,?,?)""",
                             (f"APT-M{n:06d}", carrier["id"], asn_id, clean(body.tractor_plate), trailer,
                              iso(when), body.dock_door))
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
        return out

    @app.post("/api/admin/reset")
    def reset():
        lanes.sim_running.clear()
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
