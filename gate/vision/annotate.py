"""Draw OCR reads, plate regions and damage findings on the camera frames."""
from __future__ import annotations

import cv2
import numpy as np

from .damage import DamageFinding
from .identifiers import Reading

DECISION_COLORS = {  # BGR
    "GRANTED": (60, 170, 60),
    "INSPECTION": (0, 160, 240),
    "HOLD": (40, 40, 210),
    "MANUAL_REVIEW": (0, 200, 230),
    "DENIED": (30, 30, 160),
}
LABELS = {"plate": "PLATE", "trailer": "TRAILER", "dot": "USDOT", "seal": "SEAL"}


def _label(img, text, x, y, color):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
    y = max(th + 6, y)
    cv2.rectangle(img, (x, y - th - 6), (x + tw + 8, y), color, -1)
    cv2.putText(img, text, (x + 4, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)


def annotate(frames: list[np.ndarray], readings: dict[str, Reading | None], damage: list[DamageFinding],
             decision: str, lane_id: int, camera_names: list[str]) -> list[np.ndarray]:
    out = []
    for fi, img in enumerate(frames):
        a = img.copy()
        for key, r in readings.items():
            if r is None or r.frame != fi or not r.box:
                continue
            pts = np.array(r.box, np.int32)
            cv2.polylines(a, [pts], True, (60, 200, 60), 2, cv2.LINE_AA)
            _label(a, f"{LABELS[key]} {r.value} ({r.confidence:.2f})", int(pts[:, 0].min()),
                   int(pts[:, 1].min()) - 2, (40, 140, 40))
        for d in damage:
            if d.frame != fi:
                continue
            x0, y0, x1, y1 = d.bbox
            col = (0, 0, 230) if d.severity == "severe" else (0, 140, 255) if d.severity == "moderate" else (0, 210, 255)
            cv2.rectangle(a, (x0 - 4, y0 - 4), (x1 + 4, y1 + 4), col, 3)
            _label(a, f"{d.kind.upper()} {d.severity} {d.confidence:.2f}", x0 - 4, y0 - 6, col)
        # decision banner
        color = DECISION_COLORS.get(decision, (90, 90, 90))
        cv2.rectangle(a, (0, 0), (a.shape[1], 34), color, -1)
        cam = camera_names[fi] if fi < len(camera_names) else f"cam{fi}"
        cv2.putText(a, f"LANE {lane_id} | {cam.upper()} CAMERA | {decision}", (10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2, cv2.LINE_AA)
        out.append(a)
    return out
