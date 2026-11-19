"""End-to-end processing of one truck pass at one lane.

frames -> OCR + plate localisation -> damage detection -> DB matching ->
decision -> barrier command -> persisted gate event (+ annotated images)
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime

import cv2
import numpy as np

from .config import Settings
from .db import Database, iso, utcnow
from .events import EventBus
from .matching import decide
from .vision.annotate import annotate
from .vision.damage import build_detector
from .vision.reader import extract_identifiers

log = logging.getLogger(__name__)


class GatePipeline:
    def __init__(self, db: Database, settings: Settings, controller, bus: EventBus | None = None):
        self.db = db
        self.settings = settings
        self.controller = controller
        self.bus = bus
        self.detector = build_detector(settings)
        settings.ensure_dirs()

    def process(self, lane_id: int, frames: list[np.ndarray], camera_names: list[str] | None = None,
                meta: dict | None = None, now: datetime | None = None) -> dict:
        t0 = time.perf_counter()
        now = now or utcnow()
        camera_names = camera_names or [f"cam{i + 1}" for i in range(len(frames))]
        meta = meta or {}

        readings, text_boxes, plate_regions = extract_identifiers(frames)
        damage = []
        for fi, img in enumerate(frames):
            damage.extend(self.detector.detect(img, text_boxes, fi))
        result = decide(self.db, self.settings, readings, damage, now)

        # images: raw + annotated, per camera
        stem = f"{now:%Y%m%d_%H%M%S}_L{lane_id}_{uuid.uuid4().hex[:6]}"
        annotated = annotate(frames, readings, damage, result.decision, lane_id, camera_names)
        images = []
        for i, (raw, ann) in enumerate(zip(frames, annotated)):
            raw_name, ann_name = f"{stem}_{camera_names[i]}_raw.jpg", f"{stem}_{camera_names[i]}_ann.jpg"
            cv2.imwrite(str(self.settings.image_dir / raw_name), raw, [cv2.IMWRITE_JPEG_QUALITY, 88])
            cv2.imwrite(str(self.settings.image_dir / ann_name), ann, [cv2.IMWRITE_JPEG_QUALITY, 88])
            images.append({"camera": camera_names[i], "raw": raw_name, "annotated": ann_name})

        processing_ms = int((time.perf_counter() - t0) * 1000)
        r = {k: readings.get(k) for k in ("plate", "trailer", "dot", "seal")}
        event = {
            "created_at": iso(now),
            "lane_id": lane_id,
            "decision": result.decision,
            "reasons": [x.to_dict() for x in result.reasons],
            "plate": r["plate"].value if r["plate"] else None,
            "plate_conf": r["plate"].confidence if r["plate"] else None,
            "trailer_number": r["trailer"].value if r["trailer"] else None,
            "trailer_conf": r["trailer"].confidence if r["trailer"] else None,
            "dot_number": r["dot"].value if r["dot"] else None,
            "dot_conf": r["dot"].confidence if r["dot"] else None,
            "seal_number": r["seal"].value if r["seal"] else None,
            "seal_conf": r["seal"].confidence if r["seal"] else None,
            "damage": [d.to_dict() for d in damage],
            "appointment_id": result.appointment["id"] if result.appointment else None,
            "asn_id": result.asn["id"] if result.asn else None,
            "carrier_name": result.carrier["name"] if result.carrier else None,
            "destination": result.destination,
            "gate_opened": int(result.opens_gate),
            "images": images,
            "processing_ms": processing_ms,
            "detail": {
                "readings": {k: v.to_dict() if v else None for k, v in readings.items()},
                "ocr_text": [{"text": b.text, "conf": round(b.confidence, 3), "frame": b.frame} for b in text_boxes],
                "plate_regions": plate_regions,
                "match_scores": result.scores,
                "detector": self.detector.name,
                "appointment_ref": result.appointment["appt_ref"] if result.appointment else None,
                **meta,
            },
        }
        event_id = self.db.insert_event(event)
        event["id"] = event_id

        if result.opens_gate and result.appointment:
            self._mark_arrived(result.appointment["id"], result.asn, result.decision, now)
        if result.opens_gate:
            self.controller.open(lane_id)

        log.info("Lane %s: %s plate=%s trailer=%s dot=%s seal=%s damage=%d (%d ms)", lane_id, result.decision,
                 event["plate"], event["trailer_number"], event["dot_number"], event["seal_number"],
                 len(damage), processing_ms)
        if self.bus:
            self.bus.publish({"type": "gate_event", "event": event})
        return event

    def _mark_arrived(self, appt_id: int, asn: dict | None, decision: str, now: datetime) -> None:
        self.db.execute("UPDATE appointments SET status='ARRIVED', arrived_at=? WHERE id=?", (iso(now), appt_id))
        if asn:
            self.db.execute("UPDATE asns SET status=? WHERE id=?",
                            ("EXCEPTION" if decision == "INSPECTION" else "ARRIVED", asn["id"]))

    def override(self, event_id: int, decision: str, by: str, note: str = "") -> dict:
        """A guard overrides an automated decision (e.g. after checking paperwork)."""
        ev = self.db.get_event(event_id)
        if ev is None:
            raise KeyError(event_id)
        now = utcnow()
        self.db.execute("""UPDATE gate_events SET override_decision=?, override_by=?, override_note=?,
                           override_at=? WHERE id=?""", (decision, by, note, iso(now), event_id))
        if decision in ("GRANTED", "INSPECTION"):
            if ev["appointment_id"]:
                asn = self.db.one("SELECT * FROM asns WHERE id=?", (ev["asn_id"],)) if ev["asn_id"] else None
                self._mark_arrived(ev["appointment_id"], asn, decision, now)
            self.controller.open(ev["lane_id"])
            self.db.execute("UPDATE gate_events SET gate_opened=1 WHERE id=?", (event_id,))
        ev = self.db.get_event(event_id)
        if self.bus:
            self.bus.publish({"type": "gate_event_updated", "event": ev})
        return ev
