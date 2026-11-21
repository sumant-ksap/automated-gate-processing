"""Ground-truth truck motion used by the fleet simulator and for training history.

The ETA model never sees these formulas - it only sees what a real system
would observe (GPS pings, traffic index, weather) and must *learn* how they
translate into travel time, including noise it cannot observe (driver
behaviour, unplanned fuel / rest stops).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from .conditions import Conditions
from .geo import ROAD_FACTOR, bearing_deg, destination, road_km

URBAN_KM = 8.0          # last road-km to the facility are city streets
HWY_KPH = 88.0
CITY_KPH = 45.0


def freeflow_kph(remaining_road_km: float) -> float:
    return CITY_KPH if remaining_road_km < URBAN_KM else HWY_KPH


def naive_minutes(road_km_left: float) -> float:
    """Static ETA: distance / free-flow speed (what a basic TMS would show)."""
    urban = min(road_km_left, URBAN_KM)
    return (urban / CITY_KPH + max(0.0, road_km_left - URBAN_KM) / HWY_KPH) * 60


@dataclass
class SimVehicle:
    driver_factor: float = 1.0          # some drivers are faster than others
    stop_at_km: float | None = None     # remaining road-km where an unplanned stop happens
    stop_minutes: float = 0.0
    stopped_until: float | None = None
    stopped_done: bool = False

    @classmethod
    def random(cls, rng: random.Random, start_road_km: float) -> "SimVehicle":
        v = cls(driver_factor=min(1.15, max(0.85, rng.gauss(1.0, 0.06))))
        if rng.random() < 0.10 and start_road_km > 6:
            v.stop_at_km = rng.uniform(3, start_road_km - 2)
            v.stop_minutes = rng.uniform(4, 14)
        return v


def true_speed_kph(cond: Conditions, lat: float, lon: float, remaining_road_km: float, when: datetime,
                   veh: SimVehicle, rng: random.Random) -> float:
    traffic = cond.traffic(lat, lon, when)
    w = cond.weather(lat, lon, when)
    f = (1 - 0.65 * traffic) * max(0.45, 1 - 0.028 * w["rain_mm"] - 0.09 * w["snow_mm"])
    if w["wind_kph"] > 45:
        f *= 0.92
    return max(3.0, freeflow_kph(remaining_road_km) * f * veh.driver_factor * (1 + rng.gauss(0, 0.04)))


def advance(lat: float, lon: float, dest: tuple[float, float], veh: SimVehicle, cond: Conditions,
            when: datetime, ts: float, dt_s: float, rng: random.Random) -> tuple[float, float, float, float, float]:
    """Move one time step. Returns (lat, lon, speed_kph, heading, remaining_road_km)."""
    remaining = road_km(lat, lon, *dest)
    heading = bearing_deg(lat, lon, *dest)
    if veh.stopped_until is not None:
        if ts < veh.stopped_until:
            return lat, lon, 0.0, heading, remaining
        veh.stopped_until, veh.stopped_done = None, True
    if veh.stop_at_km is not None and not veh.stopped_done and remaining <= veh.stop_at_km:
        veh.stopped_until = ts + veh.stop_minutes * 60
        return lat, lon, 0.0, heading, remaining
    v = true_speed_kph(cond, lat, lon, remaining, when, veh, rng)
    step_road = min(remaining, v * dt_s / 3600)
    nlat, nlon = destination(lat, lon, heading, step_road / ROAD_FACTOR)
    return nlat, nlon, v, heading, max(0.0, remaining - step_road)
