"""Traffic and weather conditions around the facility.

``SimulatedConditions`` produces a realistic, changing environment:
  * recurring congestion by time of day (morning / evening rush, lunch)
  * random incidents (crashes, roadworks) that slow traffic in a radius
  * moving rain / snow / storm cells

``OpenMeteoWeather`` (optional, GATE_WEATHER=openmeteo) replaces simulated
weather with live data from the free Open-Meteo API (no API key needed).
Commercial traffic feeds (TomTom, HERE, Google) plug in by implementing
``traffic(lat, lon, when)`` with the same signature.
"""
from __future__ import annotations

import itertools
import json
import logging
import math
import random
import threading
import time
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime

from .geo import destination, haversine_km, interpolate

log = logging.getLogger(__name__)
_ids = itertools.count(1)


@dataclass
class Incident:
    id: int
    lat: float
    lon: float
    radius_km: float
    severity: float          # 0..1 extra congestion at the centre
    kind: str                # crash | roadworks | breakdown
    ends_ts: float


@dataclass
class WeatherCell:
    id: int
    lat: float
    lon: float
    radius_km: float
    kind: str                # rain | snow | storm
    intensity: float         # mm/h precipitation at the centre
    wind_kph: float
    heading: float           # direction the cell drifts to
    speed_kph: float
    ends_ts: float


def _gauss(x: float, mu: float, sigma: float) -> float:
    return math.exp(-0.5 * ((x - mu) / sigma) ** 2)


def time_of_day_traffic(when: datetime) -> float:
    """Recurring congestion index 0..1 from the local clock."""
    h = when.hour + when.minute / 60
    base = 0.10 + 0.50 * _gauss(h, 8.0, 1.0) + 0.55 * _gauss(h, 17.5, 1.2) + 0.15 * _gauss(h, 12.5, 1.5)
    if when.weekday() >= 5:
        base *= 0.5
    return min(base, 0.9)


class SimulatedConditions:
    def __init__(self, lat: float, lon: float, seed: int | None = None, area_km: float = 45,
                 incident_rate_per_h: float = 10, weather_rate_per_h: float = 3):
        self.lat, self.lon = lat, lon
        self.area_km = area_km
        self.rng = random.Random(seed)
        self.incident_rate = incident_rate_per_h
        self.weather_rate = weather_rate_per_h
        self.incidents: list[Incident] = []
        self.cells: list[WeatherCell] = []
        self._last_ts: float | None = None
        self._lock = threading.Lock()

    # -- evolution -----------------------------------------------------------
    def _random_point(self, max_km: float) -> tuple[float, float]:
        return destination(self.lat, self.lon, self.rng.uniform(0, 360), max_km * math.sqrt(self.rng.random()))

    def spawn_incident(self, ts: float, lat: float | None = None, lon: float | None = None) -> Incident:
        if lat is None:
            lat, lon = self._random_point(self.area_km * 0.8)
        kind = self.rng.choice(["crash", "crash", "roadworks", "breakdown"])
        inc = Incident(next(_ids), lat, lon, self.rng.uniform(2.5, 6), self.rng.uniform(0.3, 0.75), kind,
                       ts + self.rng.uniform(10, 35) * 60)
        self.incidents.append(inc)
        return inc

    def spawn_cell(self, ts: float) -> WeatherCell:
        kind = self.rng.choices(["rain", "storm", "snow"], [0.65, 0.25, 0.10])[0]
        intensity = {"rain": self.rng.uniform(1.5, 6), "storm": self.rng.uniform(6, 15),
                     "snow": self.rng.uniform(1, 4)}[kind]
        lat, lon = self._random_point(self.area_km)
        cell = WeatherCell(next(_ids), lat, lon, self.rng.uniform(6, 15), kind, intensity,
                           self.rng.uniform(45, 70) if kind == "storm" else self.rng.uniform(10, 35),
                           self.rng.uniform(0, 360), self.rng.uniform(10, 30), ts + self.rng.uniform(25, 70) * 60)
        self.cells.append(cell)
        return cell

    def step(self, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            dt = 0.0 if self._last_ts is None else max(0.0, ts - self._last_ts)
            self._last_ts = ts
            # Poisson arrivals of new incidents / weather
            if self.rng.random() < self.incident_rate * dt / 3600:
                self.spawn_incident(ts)
            if self.rng.random() < self.weather_rate * dt / 3600 and len(self.cells) < 3:
                self.spawn_cell(ts)
            for c in self.cells:
                c.lat, c.lon = destination(c.lat, c.lon, c.heading, c.speed_kph * dt / 3600)
            self.incidents = [i for i in self.incidents if i.ends_ts > ts]
            self.cells = [c for c in self.cells if c.ends_ts > ts
                          and haversine_km(self.lat, self.lon, c.lat, c.lon) < self.area_km * 1.6]

    # -- queries ---------------------------------------------------------------
    def traffic(self, lat: float, lon: float, when: datetime) -> float:
        t = time_of_day_traffic(when)
        # congestion is heavier near the city / facility
        t *= 1.0 + 0.4 * max(0.0, 1 - haversine_km(self.lat, self.lon, lat, lon) / 15)
        for inc in self.incidents:
            d = haversine_km(lat, lon, inc.lat, inc.lon)
            if d < inc.radius_km:
                t += inc.severity * (1 - d / inc.radius_km)
        return min(t, 0.95)

    def weather(self, lat: float, lon: float, when: datetime) -> dict:
        rain = snow = 0.0
        wind = 12.0
        for c in self.cells:
            d = haversine_km(lat, lon, c.lat, c.lon)
            if d < c.radius_km:
                f = 1 - d / c.radius_km
                if c.kind == "snow":
                    snow += c.intensity * f
                else:
                    rain += c.intensity * f
                wind = max(wind, c.wind_kph * (0.5 + 0.5 * f))
        return {"rain_mm": round(rain, 2), "snow_mm": round(snow, 2), "wind_kph": round(wind, 1)}

    def snapshot(self) -> dict:
        with self._lock:
            return {"incidents": [asdict(i) for i in self.incidents], "weather_cells": [asdict(c) for c in self.cells]}


class OpenMeteoWeather:
    """Live current weather from api.open-meteo.com (free, no key). Cached per ~10 km grid cell."""
    URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat:.2f}&longitude={lon:.2f}"
           "&current=rain,showers,snowfall,wind_speed_10m")

    def __init__(self, ttl_s: float = 600):
        self.ttl = ttl_s
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def weather(self, lat: float, lon: float, when: datetime) -> dict | None:
        key = (round(lat, 1), round(lon, 1))
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        try:
            with urllib.request.urlopen(self.URL.format(lat=key[0], lon=key[1]), timeout=4) as r:
                cur = json.load(r)["current"]
            w = {"rain_mm": float(cur.get("rain", 0)) + float(cur.get("showers", 0)),
                 "snow_mm": float(cur.get("snowfall", 0)) * 10,          # cm of snow -> ~mm water
                 "wind_kph": float(cur.get("wind_speed_10m", 0)), "live": True}
        except Exception as e:
            log.warning("Open-Meteo unavailable (%s); using simulated weather", e)
            return None
        with self._lock:
            self._cache[key] = (time.time(), w)
        return w


class Conditions:
    """Facade combining the simulated environment with optional live feeds."""

    def __init__(self, sim: SimulatedConditions, live_weather: OpenMeteoWeather | None = None):
        self.sim = sim
        self.live_weather = live_weather

    def step(self, ts: float | None = None) -> None:
        self.sim.step(ts)

    def traffic(self, lat, lon, when) -> float:
        return self.sim.traffic(lat, lon, when)

    def weather(self, lat, lon, when) -> dict:
        if self.live_weather:
            w = self.live_weather.weather(lat, lon, when)
            if w is not None:
                return w
        return self.sim.weather(lat, lon, when)

    def route_summary(self, lat: float, lon: float, dest_lat: float, dest_lon: float, when: datetime,
                      samples: int = 6) -> dict:
        """Conditions sampled along the remaining route to the facility."""
        tr, rain, snow, wind = [], [], [], []
        w: dict = {}
        for k in range(samples):
            plat, plon = interpolate(lat, lon, dest_lat, dest_lon, k / max(1, samples - 1))
            tr.append(self.traffic(plat, plon, when))
            # live weather is coarse (~10 km grid) and rate-limited: one lookup per route is enough
            if k == 0 or not self.live_weather:
                w = self.weather(plat, plon, when)
            rain.append(w["rain_mm"])
            snow.append(w["snow_mm"])
            wind.append(w["wind_kph"])
        return {"traffic_here": tr[0], "traffic_route": sum(tr) / len(tr), "traffic_max": max(tr),
                "rain_route": sum(rain) / len(rain), "snow_route": sum(snow) / len(snow), "wind_max": max(wind)}

    def snapshot(self) -> dict:
        s = self.sim.snapshot()
        s["live_weather"] = self.live_weather is not None
        return s
