"""OCR engine wrapper.

Uses RapidOCR (PaddleOCR models running on ONNX Runtime): a real, accurate
OCR engine that installs with pip and needs no system packages or GPU.
One engine instance is kept per worker thread so lanes run OCR concurrently.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)
_local = threading.local()
_threads_per_engine = 0      # 0 = let onnxruntime use every core


def configure(workers: int) -> None:
    """Split CPU cores between concurrent OCR engines to avoid oversubscription."""
    global _threads_per_engine
    _threads_per_engine = max(1, (os.cpu_count() or 4) // max(1, workers))


@dataclass
class TextBox:
    text: str
    confidence: float
    box: list            # 4 points [[x,y],...] in source-image coordinates
    frame: int = 0

    @property
    def x0(self): return min(p[0] for p in self.box)
    @property
    def x1(self): return max(p[0] for p in self.box)
    @property
    def y0(self): return min(p[1] for p in self.box)
    @property
    def y1(self): return max(p[1] for p in self.box)
    @property
    def cx(self): return (self.x0 + self.x1) / 2
    @property
    def cy(self): return (self.y0 + self.y1) / 2
    @property
    def h(self): return self.y1 - self.y0

    def bbox(self) -> tuple[int, int, int, int]:
        return int(self.x0), int(self.y0), int(self.x1), int(self.y1)


def _engine():
    eng = getattr(_local, "engine", None)
    if eng is None:
        from rapidocr_onnxruntime import RapidOCR
        kw = {"intra_op_num_threads": _threads_per_engine} if _threads_per_engine else {}
        eng = RapidOCR(**kw)
        _local.engine = eng
        log.info("RapidOCR engine initialised for thread %s", threading.current_thread().name)
    return eng


def read_text(img: np.ndarray, frame: int = 0, offset=(0, 0), scale: float = 1.0) -> list[TextBox]:
    """Run OCR on an image. `offset`/`scale` map crop coordinates back to the frame."""
    result, _ = _engine()(img)
    out: list[TextBox] = []
    for box, text, conf in result or []:
        pts = [[float(x) / scale + offset[0], float(y) / scale + offset[1]] for x, y in box]
        out.append(TextBox(text=str(text).strip(), confidence=float(conf), box=pts, frame=frame))
    return out


def warmup() -> None:
    """Load models eagerly (first call otherwise takes ~1s)."""
    read_text(np.full((64, 256, 3), 255, np.uint8))
