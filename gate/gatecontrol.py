"""Gate barrier control.

``SimulatedGateController`` keeps barrier state in memory (and auto-closes
after the truck has passed). ``WebhookGateController`` additionally POSTs
open/close commands to a PLC / IoT relay HTTP endpoint
(GATE_CONTROLLER_URL=http://plc.local/gate), which is how most boom-barrier
controllers are integrated.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from typing import Callable

log = logging.getLogger(__name__)


class SimulatedGateController:
    def __init__(self, auto_close_s: float = 8.0, on_change: Callable[[int, str], None] | None = None):
        self.auto_close_s = auto_close_s
        self.state: dict[int, str] = {}
        self._timers: dict[int, threading.Timer] = {}
        self._lock = threading.Lock()
        self.on_change = on_change

    def _set(self, lane_id: int, state: str):
        with self._lock:
            self.state[lane_id] = state
        log.info("Lane %s barrier %s", lane_id, state)
        if self.on_change:
            self.on_change(lane_id, state)

    def open(self, lane_id: int) -> None:
        with self._lock:
            t = self._timers.pop(lane_id, None)
            if t:
                t.cancel()
        self._set(lane_id, "OPEN")
        timer = threading.Timer(self.auto_close_s, self.close, args=(lane_id,))
        timer.daemon = True
        with self._lock:
            self._timers[lane_id] = timer
        timer.start()

    def close(self, lane_id: int) -> None:
        self._set(lane_id, "CLOSED")

    def get(self, lane_id: int) -> str:
        return self.state.get(lane_id, "CLOSED")


class WebhookGateController(SimulatedGateController):
    def __init__(self, url: str, **kw):
        super().__init__(**kw)
        self.url = url

    def _set(self, lane_id: int, state: str):
        body = json.dumps({"lane": lane_id, "command": state}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as e:
            log.error("Gate controller %s failed for lane %s: %s", self.url, lane_id, e)
        super()._set(lane_id, state)


def build_controller(on_change=None):
    url = os.environ.get("GATE_CONTROLLER_URL")
    if url:
        return WebhookGateController(url, on_change=on_change)
    return SimulatedGateController(on_change=on_change)
