"""Multi-lane gate operation.

Every lane has its own camera set and is processed concurrently on a shared
worker pool, so several trucks are cleared in parallel. A lane is fed by:

  * the simulator   - synthetic trucks (default; no hardware needed)
  * a camera source - OpenCV VideoCapture (USB index or RTSP/HTTP URL) per
                      camera, triggered by motion on the first camera
  * a folder source - images dropped into a folder (e.g. by an existing
                      LPR/camera system's FTP upload)
  * the HTTP API    - POST /api/lanes/{id}/process with image files
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .pipeline import GatePipeline
from .sim.scenarios import EXPECTED, build_for_appointment, build_scenario
from .sim.synth import render_truck
from .vision import ocr

log = logging.getLogger(__name__)


@dataclass
class Lane:
    id: int
    name: str
    source: str = "simulator"        # simulator | camera | folder | api
    status: str = "IDLE"             # IDLE | PROCESSING | OFFLINE
    processed: int = 0
    last_event_id: int | None = None
    last_decision: str | None = None
    queue_depth: int = 0
    error: str | None = None
    config: dict = field(default_factory=dict)

    def to_dict(self, barrier: str) -> dict:
        return {"id": self.id, "name": self.name, "source": self.source, "status": self.status,
                "processed": self.processed, "last_event_id": self.last_event_id,
                "last_decision": self.last_decision, "queue_depth": self.queue_depth,
                "barrier": barrier, "error": self.error}


class LaneManager:
    def __init__(self, pipeline: GatePipeline, num_lanes: int, workers: int, bus=None):
        self.pipeline = pipeline
        self.bus = bus
        ocr.configure(workers)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lane-worker")
        self.lanes: dict[int, Lane] = {i: Lane(i, f"Lane {i}") for i in range(1, num_lanes + 1)}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.sim_running = threading.Event()
        self.sim_interval = pipeline.settings.sim_interval_s
        self._rng = random.Random()
        self._threads: list[threading.Thread] = []
        self._configure_sources()

    # -- configuration --------------------------------------------------------
    def _configure_sources(self) -> None:
        """GATE_LANE_SOURCES='{"1": {"type": "camera", "uris": ["rtsp://..side", "rtsp://..rear"]},
                               "2": {"type": "folder", "path": "C:/gate/lane2"}}'"""
        raw = os.environ.get("GATE_LANE_SOURCES")
        if not raw:
            return
        for lane_id, cfg in json.loads(raw).items():
            lane = self.lanes.get(int(lane_id))
            if lane:
                lane.source, lane.config = cfg["type"], cfg

    def start(self) -> None:
        t = threading.Thread(target=self._sim_loop, name="simulator", daemon=True)
        t.start()
        self._threads.append(t)
        for lane in self.lanes.values():
            if lane.source == "camera":
                t = threading.Thread(target=self._camera_loop, args=(lane,), name=f"camera-{lane.id}", daemon=True)
            elif lane.source == "folder":
                t = threading.Thread(target=self._folder_loop, args=(lane,), name=f"folder-{lane.id}", daemon=True)
            else:
                continue
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        self.sim_running.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)

    # -- submission -----------------------------------------------------------
    def submit(self, lane_id: int, frames: list[np.ndarray], camera_names: list[str] | None = None,
               meta: dict | None = None):
        lane = self.lanes[lane_id]
        with self._lock:
            lane.queue_depth += 1
        self._publish_lane(lane)
        return self.executor.submit(self._run, lane, frames, camera_names, meta)

    def _run(self, lane: Lane, frames, camera_names, meta) -> dict:
        with self._lock:
            lane.status = "PROCESSING"
        self._publish_lane(lane)
        try:
            ev = self.pipeline.process(lane.id, frames, camera_names, meta)
            with self._lock:
                lane.processed += 1
                lane.last_event_id = ev["id"]
                lane.last_decision = ev["decision"]
                lane.error = None
            return ev
        except Exception as e:
            log.exception("Lane %s processing failed", lane.id)
            with self._lock:
                lane.error = str(e)
            raise
        finally:
            with self._lock:
                lane.queue_depth -= 1
                lane.status = "PROCESSING" if lane.queue_depth > 0 else "IDLE"
            self._publish_lane(lane)

    def _publish_lane(self, lane: Lane) -> None:
        if self.bus:
            self.bus.publish({"type": "lane", "lane": lane.to_dict(self.pipeline.controller.get(lane.id))})

    def lane_states(self) -> list[dict]:
        return [l.to_dict(self.pipeline.controller.get(l.id)) for l in self.lanes.values()]

    # -- simulator --------------------------------------------------------------
    def simulate_one(self, lane_id: int, scenario: str | None = None):
        name, spec = build_scenario(self.pipeline.db, self._rng, scenario)
        frames = render_truck(spec, self._rng.randint(0, 2**31))
        meta = {"sim_scenario": name, "sim_expected": EXPECTED[name], "sim_truth": spec.to_dict()}
        return self.submit(lane_id, list(frames.values()), list(frames.keys()), meta)

    def simulate_appointment(self, appointment_id: int, rng: random.Random | None = None):
        """A tracked inbound truck reached the gate: render it and send it to the least busy lane."""
        rng = rng or self._rng
        name, expected, spec = build_for_appointment(self.pipeline.db, rng, appointment_id)
        lane_id = min(self.lanes.values(), key=lambda l: (l.queue_depth, l.processed)).id
        frames = render_truck(spec, rng.randint(0, 2**31))
        meta = {"sim_scenario": name, "sim_expected": expected, "sim_truth": spec.to_dict(),
                "source": "inbound-fleet"}
        return self.submit(lane_id, list(frames.values()), list(frames.keys()), meta)

    def _sim_loop(self) -> None:
        next_due = {lid: 0.0 for lid in self.lanes}
        while not self._stop.is_set():
            if self.sim_running.is_set():
                now = time.monotonic()
                for lid, lane in self.lanes.items():
                    if lane.source != "simulator" or lane.queue_depth > 0 or now < next_due[lid]:
                        continue
                    try:
                        self.simulate_one(lid)
                    except Exception:
                        log.exception("Simulator failed on lane %s", lid)
                    # staggered arrivals: each lane gets its own random headway
                    next_due[lid] = now + self.sim_interval * self._rng.uniform(0.6, 1.4)
            self._stop.wait(0.25)

    # -- real cameras -------------------------------------------------------------
    def _camera_loop(self, lane: Lane) -> None:
        """Motion-triggered capture. On the first camera, a truck entering the
        trigger zone raises the motion score; once the scene is still again
        (truck stopped at the barrier) one frame from every camera is processed."""
        uris = lane.config.get("uris", [])
        names = lane.config.get("names") or [f"cam{i + 1}" for i in range(len(uris))]
        caps = [cv2.VideoCapture(int(u) if str(u).isdigit() else u) for u in uris]
        if not caps or not all(c.isOpened() for c in caps):
            lane.status, lane.error = "OFFLINE", f"cannot open cameras {uris}"
            log.error("Lane %s: %s", lane.id, lane.error)
            return
        bg = cv2.createBackgroundSubtractorMOG2(history=300, detectShadows=False)
        moving, still_frames, cooldown_until = False, 0, 0.0
        thr = float(lane.config.get("motion_threshold", 0.04))
        while not self._stop.is_set():
            frames = []
            for c in caps:
                ok, f = c.read()
                if not ok:
                    lane.status, lane.error = "OFFLINE", "camera read failed"
                    time.sleep(1)
                    break
                frames.append(f)
            if len(frames) != len(caps):
                continue
            fg = bg.apply(cv2.resize(frames[0], (320, 180)))
            motion = float((fg > 0).mean())
            if motion > thr:
                moving, still_frames = True, 0
            elif moving:
                still_frames += 1
                if still_frames >= 10 and time.monotonic() > cooldown_until and lane.queue_depth == 0:
                    moving = False
                    cooldown_until = time.monotonic() + float(lane.config.get("cooldown_s", 5))
                    self.submit(lane.id, frames, names, {"source": "camera"})
            time.sleep(0.03)

    # -- folder drop ---------------------------------------------------------------
    def _folder_loop(self, lane: Lane) -> None:
        """Images named <pass-id>__<camera>.jpg are grouped into one truck pass;
        any other image is processed on its own. Processed files move to ./done."""
        folder = Path(lane.config["path"])
        done = folder / "done"
        done.mkdir(parents=True, exist_ok=True)
        while not self._stop.is_set():
            files = sorted(p for p in folder.iterdir()
                           if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp") and p.is_file())
            # wait until files are fully written (not modified for 1 s)
            files = [p for p in files if time.time() - p.stat().st_mtime > 1.0]
            groups: dict[str, list[Path]] = {}
            for p in files:
                key = p.stem.split("__")[0]
                groups.setdefault(key, []).append(p)
            for key, paths in groups.items():
                frames = [cv2.imread(str(p)) for p in paths]
                names = [p.stem.split("__")[1] if "__" in p.stem else "cam" for p in paths]
                pairs = [(f, n) for f, n in zip(frames, names) if f is not None]
                for p in paths:
                    p.replace(done / p.name)
                if pairs:
                    self.submit(lane.id, [f for f, _ in pairs], [n for _, n in pairs],
                                {"source": "folder", "files": [p.name for p in paths]})
            self._stop.wait(1.0)
