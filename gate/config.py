"""Runtime configuration (environment variables with sensible defaults)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("GATE_DATA_DIR", str(ROOT / "data"))))
    num_lanes: int = field(default_factory=lambda: _env("GATE_NUM_LANES", 4))
    workers: int = field(default_factory=lambda: _env("GATE_WORKERS", 4))

    # OCR acceptance thresholds
    min_ocr_confidence: float = field(default_factory=lambda: _env("GATE_MIN_OCR_CONF", 0.60))

    # appointment window (minutes) - how early / late a truck may arrive
    early_minutes: int = field(default_factory=lambda: _env("GATE_EARLY_MIN", 120))
    late_minutes: int = field(default_factory=lambda: _env("GATE_LATE_MIN", 240))
    # arrivals within this many minutes of the appointment go straight to the dock
    on_time_minutes: int = field(default_factory=lambda: _env("GATE_ON_TIME_MIN", 30))

    # damage detection
    damage_min_area: int = field(default_factory=lambda: _env("GATE_DAMAGE_MIN_AREA", 500))
    # below this sharpness (variance of Laplacian) a side/rear frame is too poor to judge damage;
    # calibrate per camera: log the event detail "image_sharpness" values from known-good frames
    min_sharpness: float = field(default_factory=lambda: _env("GATE_MIN_SHARPNESS", 20.0))
    damage_model: str = field(default_factory=lambda: _env("GATE_DAMAGE_MODEL", ""))  # optional YOLO .onnx
    damage_model_classes: str = field(default_factory=lambda: _env("GATE_DAMAGE_CLASSES", "dent,rust,hole,scratch,broken_seal"))

    # simulator
    sim_interval_s: float = field(default_factory=lambda: _env("GATE_SIM_INTERVAL", 6.0))
    sim_autostart: bool = field(default_factory=lambda: _env("GATE_SIM_AUTOSTART", False))

    # predictive arrival & pre-check-in
    facility_name: str = field(default_factory=lambda: _env("GATE_FACILITY_NAME", "DC-01 Dallas"))
    facility_lat: float = field(default_factory=lambda: _env("GATE_FACILITY_LAT", 32.7767))
    facility_lon: float = field(default_factory=lambda: _env("GATE_FACILITY_LON", -96.7970))
    weather_provider: str = field(default_factory=lambda: _env("GATE_WEATHER", "sim"))     # sim | openmeteo
    precheck_horizon_min: int = field(default_factory=lambda: _env("GATE_PRECHECK_HORIZON_MIN", 30))
    surge_window_min: int = field(default_factory=lambda: _env("GATE_SURGE_WINDOW_MIN", 10))
    surge_threshold: int = field(default_factory=lambda: _env("GATE_SURGE_THRESHOLD", 6))
    manual_checkin_min: float = field(default_factory=lambda: _env("GATE_MANUAL_CHECKIN_MIN", 4.0))
    precleared_checkin_min: float = field(default_factory=lambda: _env("GATE_PRECLEARED_CHECKIN_MIN", 0.5))
    arrive_radius_km: float = field(default_factory=lambda: _env("GATE_ARRIVE_RADIUS_KM", 0.3))
    eta_history_trips: int = field(default_factory=lambda: _env("GATE_ETA_HISTORY_TRIPS", 500))
    eta_retrain_every: int = field(default_factory=lambda: _env("GATE_ETA_RETRAIN_EVERY", 5))
    inbound_tick_s: float = field(default_factory=lambda: _env("GATE_INBOUND_TICK_S", 2.0))
    fleet_target: int = field(default_factory=lambda: _env("GATE_FLEET_TARGET", 10))
    fleet_spawn_s: float = field(default_factory=lambda: _env("GATE_FLEET_SPAWN_S", 35.0))
    fleet_autostart: bool = field(default_factory=lambda: _env("GATE_FLEET_AUTOSTART", False))

    # dock & spot allocation: unloading runs this many times faster than real time (demo speed-up)
    yard_time_scale: float = field(default_factory=lambda: _env("GATE_YARD_TIME_SCALE", 10.0))

    # driver communication
    sms_provider: str = field(default_factory=lambda: _env("GATE_SMS_PROVIDER", "sim"))       # sim | twilio
    public_url: str = field(default_factory=lambda: _env("GATE_PUBLIC_URL", "http://localhost:8000"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gate.db"

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "images"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
