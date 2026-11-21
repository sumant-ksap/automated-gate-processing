"""Dynamic ETA model.

A regularised regression (ridge) learns how live conditions stretch travel
time compared with a static distance / free-flow estimate:

    log(actual_minutes / static_minutes) = w . features

Features come only from what a real deployment observes: remaining distance,
GPS speed over the last minutes, traffic index along the route, weather along
the route and time of day. Prediction intervals (the "arrival window") come
from the model's own out-of-sample residuals.

The model is bootstrapped from simulated trip history and then retrained
continuously from every truck that actually arrives at the gate, so it adapts
to the real facility.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .conditions import Conditions, SimulatedConditions
from .geo import destination, road_km
from .world import SimVehicle, advance, freeflow_kph, naive_minutes, URBAN_KM

FEATURES = ["traffic_route", "traffic_route_sq", "traffic_here", "traffic_max", "rain", "snow", "high_wind",
            "observed_speed_ratio", "has_observation", "observed_x_near", "urban_fraction", "log_distance",
            "hour_sin", "hour_cos"]


def build_features(remaining_road_km: float, summary: dict, observed_ratio: float | None, when: datetime) -> list[float]:
    d = max(remaining_road_km, 0.2)
    t = summary["traffic_route"]
    h = when.hour + when.minute / 60
    obs = observed_ratio if observed_ratio is not None else 0.0
    has = 1.0 if observed_ratio is not None else 0.0
    return [t, t * t, summary["traffic_here"], summary["traffic_max"],
            min(summary["rain_route"], 20) / 10, min(summary["snow_route"], 10) / 5,
            1.0 if summary["wind_max"] > 45 else 0.0,
            obs, has, obs * math.exp(-d / 6), min(d, URBAN_KM) / d, math.log(d),
            math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24)]


class EtaModel:
    def __init__(self):
        self.w: np.ndarray | None = None
        self.mu: np.ndarray | None = None
        self.sd: np.ndarray | None = None
        self.q_lo = -0.15
        self.q_hi = 0.25
        self.metrics: dict = {}

    @property
    def ready(self) -> bool:
        return self.w is not None

    def _design(self, X: np.ndarray) -> np.ndarray:
        Z = (X - self.mu) / self.sd
        return np.hstack([np.ones((len(Z), 1)), Z])

    def _solve(self, X: np.ndarray, y: np.ndarray, lam: float) -> None:
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0)
        self.sd[self.sd < 1e-9] = 1.0
        A = self._design(X)
        reg = lam * np.eye(A.shape[1])
        reg[0, 0] = 0.0                       # don't shrink the intercept
        self.w = np.linalg.solve(A.T @ A + reg, A.T @ y)

    def fit(self, X: np.ndarray, y: np.ndarray, naive: np.ndarray, lam: float = 1.0,
            seed: int = 0, n_trips: int | None = None, n_real: int = 0) -> dict:
        rng = np.random.default_rng(seed)
        idx = rng.permutation(len(X))
        cut = int(len(X) * 0.8)
        tr, ho = idx[:cut], idx[cut:]
        self._solve(X[tr], y[tr], lam)
        resid = y[ho] - self._design(X[ho]) @ self.w
        self.q_lo, self.q_hi = float(np.quantile(resid, 0.10)), float(np.quantile(resid, 0.90))
        actual = naive[ho] * np.exp(y[ho])
        pred = naive[ho] * np.exp(self._design(X[ho]) @ self.w)
        lo, hi = pred * math.exp(self.q_lo), pred * math.exp(self.q_hi)
        metrics = {
            "holdout_mae_min": round(float(np.mean(np.abs(pred - actual))), 2),
            "static_mae_min": round(float(np.mean(np.abs(naive[ho] - actual))), 2),
            "window_coverage": round(float(np.mean((actual >= lo) & (actual <= hi))), 3),
            "n_samples": int(len(X)), "n_real_samples": int(n_real),
            "n_trips": n_trips, "trained_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._solve(X, y, lam)                # final model on all data
        coefs = sorted(zip(FEATURES, self.w[1:]), key=lambda t: -abs(t[1]))
        metrics["top_features"] = [{"feature": f, "weight": round(float(c), 3)} for f, c in coefs[:6]]
        self.metrics = metrics
        return metrics

    def predict(self, feats: list[float], static_min: float) -> tuple[float, float, float]:
        """(eta, earliest, latest) in minutes."""
        if not self.ready:
            return static_min, static_min * 0.85, static_min * 1.4
        z = float(self._design(np.array([feats], float))[0] @ self.w)
        z = max(-0.5, min(2.5, z))
        eta = static_min * math.exp(z)
        return eta, eta * math.exp(self.q_lo), eta * math.exp(self.q_hi)

    def to_dict(self) -> dict:
        return {"w": self.w.tolist(), "mu": self.mu.tolist(), "sd": self.sd.tolist(), "q_lo": self.q_lo,
                "q_hi": self.q_hi, "metrics": self.metrics, "features": FEATURES}

    @classmethod
    def from_dict(cls, d: dict) -> "EtaModel":
        m = cls()
        if d.get("features") != FEATURES:
            return m                           # feature set changed - retrain
        m.w, m.mu, m.sd = np.array(d["w"]), np.array(d["mu"]), np.array(d["sd"])
        m.q_lo, m.q_hi, m.metrics = d["q_lo"], d["q_hi"], d["metrics"]
        return m

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict()))

    @classmethod
    def load(cls, path: Path) -> "EtaModel":
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except Exception:
            return cls()


class SpeedHistory:
    """Rolling window of GPS speeds -> observed speed / free-flow ratio."""

    def __init__(self, window_s: float = 300):
        self.window = window_s
        self.points: list[tuple[float, float, float]] = []   # (ts, speed, freeflow)

    def add(self, ts: float, speed: float, freeflow: float) -> None:
        self.points.append((ts, speed, freeflow))
        cut = ts - self.window
        while self.points and self.points[0][0] < cut:
            self.points.pop(0)

    def ratio(self) -> float | None:
        if len(self.points) < 2:
            return None
        return float(np.mean([s / f for _, s, f in self.points]))


def simulate_history(n_trips: int = 500, seed: int = 7, facility=(32.7767, -96.7970),
                     snapshot_every_s: float = 120, dt_s: float = 30) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Simulated past trips -> (features, log-ratio targets, static minutes)."""
    rng = random.Random(seed)
    X, y, naive = [], [], []
    base_day = datetime(2026, 1, 5)
    for _ in range(n_trips):
        start = base_day + timedelta(days=rng.randint(0, 6), minutes=rng.randint(0, 24 * 60 - 1))
        ts = start.timestamp()
        sim = SimulatedConditions(*facility, seed=rng.randint(0, 2**31))
        sim.step(ts)
        for _ in range(rng.choice([0, 1, 1, 2, 3])):
            sim.spawn_incident(ts)
        if rng.random() < 0.45:
            sim.spawn_cell(ts)
        cond = Conditions(sim)
        lat, lon = destination(*facility, rng.uniform(0, 360), rng.uniform(3, 40))
        veh = SimVehicle.random(rng, road_km(lat, lon, *facility))
        hist = SpeedHistory()
        snaps: list[tuple[float, list[float], float]] = []
        t, next_snap = ts, ts
        while True:
            when = datetime.fromtimestamp(t)
            remaining = road_km(lat, lon, *facility)
            if remaining < 0.3 or t - ts > 6 * 3600:
                break
            if t >= next_snap:
                summ = cond.route_summary(lat, lon, *facility, when)
                snaps.append((t, build_features(remaining, summ, hist.ratio(), when), naive_minutes(remaining)))
                next_snap = t + snapshot_every_s
            lat, lon, v, _, _ = advance(lat, lon, facility, veh, cond, when, t, dt_s, rng)
            hist.add(t, v, freeflow_kph(remaining))
            t += dt_s
            sim.step(t)
        for snap_t, f, nm in snaps:
            actual = (t - snap_t) / 60
            if actual > 0.5 and nm > 0.3:
                X.append(f)
                y.append(math.log(actual / nm))
                naive.append(nm)
    return np.array(X, float), np.array(y, float), np.array(naive, float)
