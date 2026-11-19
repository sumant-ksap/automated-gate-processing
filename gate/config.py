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
    damage_model: str = field(default_factory=lambda: _env("GATE_DAMAGE_MODEL", ""))  # optional YOLO .onnx
    damage_model_classes: str = field(default_factory=lambda: _env("GATE_DAMAGE_CLASSES", "dent,rust,hole,scratch,broken_seal"))

    # simulator
    sim_interval_s: float = field(default_factory=lambda: _env("GATE_SIM_INTERVAL", 6.0))
    sim_autostart: bool = field(default_factory=lambda: _env("GATE_SIM_AUTOSTART", False))

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
