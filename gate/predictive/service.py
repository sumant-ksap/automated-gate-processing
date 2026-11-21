"""Predictive arrival & pre-check-in service.

Every couple of seconds it:
  1. ingests GPS pings (from the telematics API or the simulated fleet)
  2. re-predicts each inbound truck's arrival window with the ETA model
  3. detects arrival surges (several trucks predicted within a short window)
  4. runs pre-check-in for trucks inside the horizon or inside a surge
  5. on arrival: scores the predictions, stores training samples, retrains
     the model periodically, and (simulated trucks) drives the truck into a
     gate lane so the camera pipeline processes it.
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

from ..config import Settings
from ..db import Database, iso, parse_iso, utcnow
from ..seed import create_appointment
from ..vision.identifiers import clean
from .conditions import Conditions, OpenMeteoWeather, SimulatedConditions
from .eta_model import EtaModel, SpeedHistory, build_features, simulate_history
from .geo import destination, road_km
from .precheck import find_clusters, run_precheck
from .world import SimVehicle, advance, freeflow_kph, naive_minutes

log = logging.getLogger(__name__)


@dataclass
class TripState:
    speeds: SpeedHistory = field(default_factory=SpeedHistory)
    snaps: list = field(default_factory=list)          # (ts, features, static_min) for training
    last_snap: float = 0.0
    last_logged: float = 0.0
    last_precheck: float = 0.0
    vehicle: SimVehicle | None = None                  # only for simulated trucks


class InboundService:
    def __init__(self, db: Database, settings: Settings, bus=None, lanes=None, seed: int | None = None):
        self.db, self.settings, self.bus, self.lanes = db, settings, bus, lanes
        self.facility = (settings.facility_lat, settings.facility_lon)
        live = OpenMeteoWeather() if settings.weather_provider == "openmeteo" else None
        self.cond = Conditions(SimulatedConditions(*self.facility, seed=seed), live)
        self.rng = random.Random(seed)
        self.model_path = settings.data_dir / "eta_model.json"
        self.model = EtaModel.load(self.model_path)
        self.training = False
        self.state: dict[int, TripState] = {}
        self.clusters: list[dict] = []
        self.fleet_running = threading.Event()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._last_tick: float | None = None
        self._last_housekeeping = 0.0
        self.notifier = None
        self._last_spawn = 0.0
        self._arrivals_since_train = 0
        self._sim_counter = 0
        # simulated trucks from a previous run can't continue (their driving state was in memory)
        db.execute("UPDATE trips SET status='CANCELLED' WHERE status='EN_ROUTE' AND source='sim'")

    def reset(self) -> None:
        with self._lock:
            self.fleet_running.clear()
            self.state.clear()
            self.clusters = []
            self._sim_counter = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.model.ready:
            self.retrain_async()
        threading.Thread(target=self._loop, name="inbound", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("inbound tick failed")
            self._stop.wait(self.settings.inbound_tick_s)

    # ------------------------------------------------------------------ model
    def retrain(self) -> dict:
        self.training = True
        try:
            X, y, n = simulate_history(self.settings.eta_history_trips, facility=self.facility)
            real = self.db.query("SELECT features, target, static_min FROM eta_samples")
            if real:
                X = np.vstack([X, np.array([r["features"] for r in real], float)])
                y = np.concatenate([y, [r["target"] for r in real]])
                n = np.concatenate([n, [r["static_min"] for r in real]])
            model = EtaModel()
            n_real_trips = self.db.one("SELECT COUNT(*) AS n FROM trips WHERE status='ARRIVED'")["n"]
            metrics = model.fit(X, y, n, n_trips=self.settings.eta_history_trips + n_real_trips, n_real=len(real))
            model.save(self.model_path)
            self.model = model
            log.info("ETA model trained: MAE %.2f min (static %.2f min) on %d samples",
                     metrics["holdout_mae_min"], metrics["static_mae_min"], metrics["n_samples"])
            if self.bus:
                self.bus.publish({"type": "eta_model", "model": self.model_info()})
            return metrics
        finally:
            self.training = False

    def retrain_async(self) -> None:
        if not self.training:
            self.training = True
            threading.Thread(target=self.retrain, name="eta-train", daemon=True).start()

    def model_info(self) -> dict:
        live = self.db.one("""SELECT COUNT(*) AS n,
                 AVG(ABS(julianday(p.eta_at) - julianday(t.arrived_at)) * 1440) AS mae,
                 AVG(ABS(julianday(p.static_eta_at) - julianday(t.arrived_at)) * 1440) AS static_mae,
                 COUNT(DISTINCT t.id) AS trips
              FROM eta_predictions p JOIN trips t ON t.id = p.trip_id WHERE t.status = 'ARRIVED'""")
        return {"ready": self.model.ready, "training": self.training, "offline": self.model.metrics,
                "live": {"predictions": live["n"], "trips": live["trips"],
                         "mae_min": round(live["mae"], 2) if live["mae"] is not None else None,
                         "static_mae_min": round(live["static_mae"], 2) if live["static_mae"] is not None else None},
                "algorithm": "Ridge regression on log(actual/static travel time), P10-P90 residual window"}

    # ------------------------------------------------------------------ telematics
    def _find_or_create_trip(self, ping: dict, now) -> dict:
        if ping.get("trip_id"):
            t = self.db.one("SELECT * FROM trips WHERE id = ?", (ping["trip_id"],))
            if t:
                return t
        appt = None
        if ping.get("appointment_ref"):
            appt = self.db.one("SELECT * FROM appointments WHERE appt_ref = ?", (ping["appointment_ref"],))
        plate = clean(ping.get("plate") or "") or None
        vid = ping.get("vehicle_id")
        t = None
        if appt:
            t = self.db.one("SELECT * FROM trips WHERE appointment_id=? AND status='EN_ROUTE'", (appt["id"],))
        if t is None and (vid or plate):
            t = self.db.one("""SELECT * FROM trips WHERE status='EN_ROUTE' AND
                               ((vehicle_id IS NOT NULL AND vehicle_id = ?) OR (plate IS NOT NULL AND plate = ?))
                               ORDER BY id DESC LIMIT 1""", (vid, plate))
        if t:
            return t
        if appt is None and plate:
            appt = self.db.one("""SELECT * FROM appointments WHERE status='SCHEDULED' AND tractor_plate = ?
                                  ORDER BY ABS(julianday(scheduled_at) - julianday(?)) LIMIT 1""", (plate, iso(now)))
        tid = self.db.execute("""INSERT INTO trips (appointment_id, vehicle_id, plate, source, started_at)
                                 VALUES (?,?,?,?,?)""",
                              (appt["id"] if appt else None, vid, plate or (appt["tractor_plate"] if appt else None),
                               ping.get("source", "telematics"), iso(now)))
        return self.db.one("SELECT * FROM trips WHERE id = ?", (tid,))

    def ingest_ping(self, ping: dict) -> dict:
        """Accept one GPS position report. Returns the updated trip."""
        now = parse_iso(ping["timestamp"]) if ping.get("timestamp") else utcnow()
        with self._lock:
            trip = self._find_or_create_trip(ping, now)
            if trip["status"] != "EN_ROUTE":
                return trip
            lat, lon = float(ping["lat"]), float(ping["lon"])
            remaining = road_km(lat, lon, *self.facility)
            speed = float(ping.get("speed_kph") or 0.0)
            st = self.state.setdefault(trip["id"], TripState())
            st.speeds.add(now.timestamp(), speed, freeflow_kph(remaining))
            self.db.execute("""UPDATE trips SET lat=?, lon=?, speed_kph=?, heading=?, distance_km=?, last_ping_at=?
                               WHERE id=?""", (lat, lon, speed, ping.get("heading"), round(remaining, 2), iso(now),
                                               trip["id"]))
            trip = self.db.one("SELECT * FROM trips WHERE id = ?", (trip["id"],))
            if remaining <= self.settings.arrive_radius_km:
                self._arrive(trip, now)
                trip = self.db.one("SELECT * FROM trips WHERE id = ?", (trip["id"],))
            return trip

    # ------------------------------------------------------------------ prediction
    def _predict(self, trip: dict, now: datetime) -> None:
        st = self.state.setdefault(trip["id"], TripState())
        local = now.astimezone().replace(tzinfo=None)
        remaining = trip["distance_km"]
        summ = self.cond.route_summary(trip["lat"], trip["lon"], *self.facility, local)
        feats = build_features(remaining, summ, st.speeds.ratio(), local)
        static = naive_minutes(remaining)
        eta, lo, hi = self.model.predict(feats, static)
        weather = self.cond.weather(trip["lat"], trip["lon"], local)
        cond = {"traffic_here": round(summ["traffic_here"], 2), "traffic_route": round(summ["traffic_route"], 2),
                "rain_mm": round(summ["rain_route"], 1), "snow_mm": round(summ["snow_route"], 1),
                "wind_kph": round(summ["wind_max"]), "stopped": (trip["speed_kph"] or 0) < 2,
                "here": weather}
        self.db.execute("""UPDATE trips SET eta_at=?, eta_low_at=?, eta_high_at=?, static_eta_at=?, conditions=?
                           WHERE id=?""",
                        (iso(now + timedelta(minutes=eta)), iso(now + timedelta(minutes=lo)),
                         iso(now + timedelta(minutes=hi)), iso(now + timedelta(minutes=static)),
                         json.dumps(cond), trip["id"]))
        ts = now.timestamp()
        if ts - st.last_snap >= 30:
            st.snaps.append((ts, feats, static))
            st.last_snap = ts
        if ts - st.last_logged >= 60:
            self.db.execute("""INSERT INTO eta_predictions (trip_id, made_at, remaining_km, eta_at, static_eta_at)
                               VALUES (?,?,?,?,?)""", (trip["id"], iso(now), remaining,
                                                       iso(now + timedelta(minutes=eta)),
                                                       iso(now + timedelta(minutes=static))))
            st.last_logged = ts

    def _arrive(self, trip: dict, now: datetime) -> None:
        self.db.execute("UPDATE trips SET status='ARRIVED', arrived_at=?, distance_km=0 WHERE id=?",
                        (iso(now), trip["id"]))
        st = self.state.pop(trip["id"], None)
        if st:
            for ts, feats, static in st.snaps:
                actual = (now.timestamp() - ts) / 60
                if actual > 0.5 and static > 0.3:
                    self.db.execute("INSERT INTO eta_samples (created_at, features, target, static_min) VALUES (?,?,?,?)",
                                    (iso(now), json.dumps(feats), math.log(actual / static), static))
        self._arrivals_since_train += 1
        if self._arrivals_since_train >= self.settings.eta_retrain_every:
            self._arrivals_since_train = 0
            self.retrain_async()
        log.info("Trip %s (%s) arrived at the facility", trip["id"], trip["plate"])
        if self.bus:
            self.bus.publish({"type": "arrival", "trip_id": trip["id"], "plate": trip["plate"]})
        # simulated trucks drive on into a gate lane
        if trip["source"] == "sim" and trip["appointment_id"] and self.lanes is not None:
            try:
                self.lanes.simulate_appointment(trip["appointment_id"], self.rng)
            except Exception:
                log.exception("could not send trip %s to the gate", trip["id"])

    # ------------------------------------------------------------------ fleet simulator
    def spawn_trip(self, dist_km: float | None = None, bearing: float | None = None, carrier: dict | None = None,
                   late_bias: float = 0.0) -> dict:
        rng = self.rng
        dist = dist_km if dist_km is not None else rng.uniform(4, 35)
        brg = bearing if bearing is not None else rng.uniform(0, 360)
        lat, lon = destination(*self.facility, brg, dist)
        static = naive_minutes(road_km(lat, lon, *self.facility))
        if carrier is None and rng.random() < 0.06:
            carrier = self.db.one("SELECT * FROM carriers WHERE status='ACTIVE' AND insurance_expiry < ?",
                                  (date.today().isoformat(),))
        # the slot was booked earlier; reality (traffic, weather, stops) decides whether it's met
        minutes = int(static * rng.uniform(1.05, 1.45) + rng.uniform(-3, 5) - late_bias)
        appt = create_appointment(self.db, rng, minutes_from_now=max(2, minutes), carrier=carrier)
        self._sim_counter += 1
        now = utcnow()
        tid = self.db.execute("""INSERT INTO trips (appointment_id, vehicle_id, plate, source, started_at, lat, lon,
                                 speed_kph, distance_km, last_ping_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                              (appt["id"], f"SIM-{self._sim_counter:04d}", appt["tractor_plate"], "sim", iso(now),
                               lat, lon, 0.0, round(road_km(lat, lon, *self.facility), 2), iso(now)))
        self.state[tid] = TripState(vehicle=SimVehicle.random(rng, road_km(lat, lon, *self.facility)))
        self._last_spawn = time.time()
        return self.db.one("SELECT * FROM trips WHERE id = ?", (tid,))

    def spawn_convoy(self, n: int = 6) -> list[dict]:
        """Several trucks close together -> an arrival surge in a few minutes."""
        brg, dist = self.rng.uniform(0, 360), self.rng.uniform(9, 16)
        out = []
        for i in range(n):
            out.append(self.spawn_trip(dist + self.rng.uniform(-1.2, 1.2), brg + self.rng.uniform(-4, 4),
                                       late_bias=self.rng.uniform(0, 6)))
        return out

    def _fleet_step(self, now_ts: float, dt: float) -> None:
        sim_trips = self.db.query("SELECT * FROM trips WHERE status='EN_ROUTE' AND source='sim'")
        if not sim_trips and self._sim_counter == 0:
            for _ in range(6):
                self.spawn_trip(dist_km=self.rng.uniform(2, 40))
        elif len(sim_trips) < self.settings.fleet_target and now_ts - self._last_spawn > self.settings.fleet_spawn_s:
            if self.rng.random() < 0.12:
                self.spawn_convoy(self.rng.randint(6, 7))
            else:
                self.spawn_trip()
        local = datetime.fromtimestamp(now_ts)
        for t in self.db.query("SELECT * FROM trips WHERE status='EN_ROUTE' AND source='sim'"):
            st = self.state.get(t["id"])
            if st is None or st.vehicle is None:
                continue
            lat, lon, v, hdg, _ = advance(t["lat"], t["lon"], self.facility, st.vehicle, self.cond, local,
                                          now_ts, dt, self.rng)
            self.ingest_ping({"trip_id": t["id"], "lat": lat, "lon": lon, "speed_kph": round(v, 1),
                              "heading": round(hdg), "source": "sim"})

    # ------------------------------------------------------------------ tick
    def mark_no_shows(self, now: datetime) -> int:
        """Appointments past the late window with no truck at the gate or on the road become NO_SHOW."""
        cutoff = iso(now - timedelta(minutes=self.settings.late_minutes))
        with self.db.tx() as c:
            n = c.execute("""UPDATE appointments SET status = 'NO_SHOW'
                             WHERE status = 'SCHEDULED' AND scheduled_at < ?
                               AND id NOT IN (SELECT appointment_id FROM trips
                                              WHERE status = 'EN_ROUTE' AND appointment_id IS NOT NULL)""",
                          (cutoff,)).rowcount
        if n:
            log.info("Marked %d appointment(s) as NO_SHOW", n)
        return n

    def tick(self) -> None:
        with self._lock:
            now = utcnow()
            ts = now.timestamp()
            dt = 0.0 if self._last_tick is None else min(10.0, ts - self._last_tick)
            self._last_tick = ts
            self.cond.step(ts)
            if ts - self._last_housekeeping >= 60:
                self.mark_no_shows(now)
                self._last_housekeeping = ts
            if self.fleet_running.is_set() and dt > 0:
                self._fleet_step(ts, dt)
            trips = self.db.query("SELECT * FROM trips WHERE status='EN_ROUTE' AND lat IS NOT NULL")
            for t in trips:
                self._predict(t, now)
            trips = self.db.query("SELECT * FROM trips WHERE status='EN_ROUTE' AND eta_at IS NOT NULL")
            s = self.settings
            self.clusters = find_clusters(trips, s.surge_window_min, s.surge_threshold, s.num_lanes,
                                          s.manual_checkin_min, s.precleared_checkin_min)
            in_surge = {tid for c in self.clusters for tid in c["trip_ids"]}
            horizon = now + timedelta(minutes=s.precheck_horizon_min)
            for t in trips:
                st = self.state.setdefault(t["id"], TripState())
                due = parse_iso(t["eta_at"]) <= horizon or t["id"] in in_surge
                if due and ts - st.last_precheck >= 60:
                    packet = run_precheck(self.db, s, t, "surge" if t["id"] in in_surge else "horizon")
                    if self.notifier is not None and packet["created_at"] == packet["updated_at"]:
                        try:
                            self.notifier.on_precheck(packet)      # first packet -> tell the driver
                        except Exception:
                            log.exception("pre-check notification failed")
                    st.last_precheck = ts
            for c in self.clusters:
                ids = c["trip_ids"]
                rows = self.db.query(f"""SELECT status FROM precheck_packets WHERE trip_id IN
                                         ({','.join('?' * len(ids))})""", ids)
                c["precleared"] = sum(1 for r in rows if r["status"] != "BLOCKED")
                c["blocked"] = sum(1 for r in rows if r["status"] == "BLOCKED")
        if self.bus:
            self.bus.publish({"type": "inbound", **self.snapshot(light=True)})

    # ------------------------------------------------------------------ views
    def board(self, include_done_minutes: int = 20) -> list[dict]:
        since = iso(utcnow() - timedelta(minutes=include_done_minutes))
        rows = self.db.query("""SELECT t.*, a.appt_ref, a.scheduled_at, a.dock_door, c.name AS carrier_name,
                                       p.id AS packet_id, p.status AS packet_status, p.trigger AS packet_trigger
                                FROM trips t
                                LEFT JOIN appointments a ON a.id = t.appointment_id
                                LEFT JOIN carriers c ON c.id = a.carrier_id
                                LEFT JOIN precheck_packets p ON p.trip_id = t.id
                                WHERE t.status = 'EN_ROUTE' OR (t.status = 'ARRIVED' AND t.arrived_at >= ?)
                                ORDER BY CASE t.status WHEN 'EN_ROUTE' THEN 0 ELSE 1 END,
                                         COALESCE(t.eta_at, t.arrived_at)""", (since,))
        for r in rows:
            r["slot_delta_min"] = None
            ref = r["arrived_at"] or r["eta_at"]
            if ref and r["scheduled_at"]:
                r["slot_delta_min"] = round((parse_iso(ref) - parse_iso(r["scheduled_at"])).total_seconds() / 60)
        return rows

    def snapshot(self, light: bool = False) -> dict:
        return {"trips": self.board(), "clusters": self.clusters, "conditions": self.cond.snapshot(),
                "facility": {"name": self.settings.facility_name, "lat": self.facility[0], "lon": self.facility[1]},
                "fleet_running": self.fleet_running.is_set(),
                **({} if light else {"model": self.model_info()})}
