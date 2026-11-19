"""Trailer damage detection.

Two detectors share one interface:

* ``PanelAnomalyDetector`` (default, no training needed) - classical computer
  vision. Large uniform surfaces (trailer side panels, rear doors) are
  segmented by region growing; anything *enclosed* by a panel that is not
  painted text and not a regular manufactured feature (rectangular placards,
  handles) is an anomaly. Anomalies are classified by colour and shape into
  dent / rust / hole / scratch with a severity.

* ``YoloOnnxDamageDetector`` - runs a YOLOv8-format ONNX model trained on
  trailer damage (set GATE_DAMAGE_MODEL=/path/model.onnx). Use this in
  production once labelled imagery from your own gates is available.

Seal integrity (missing / broken / mismatched bolt seal) is decided in the
matching step, from the OCR'd seal tag compared with the ASN.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict

import cv2
import numpy as np

from .ocr import TextBox

log = logging.getLogger(__name__)


@dataclass
class DamageFinding:
    kind: str            # dent | rust | hole | scratch | anomaly | <model class>
    severity: str        # minor | moderate | severe
    confidence: float
    bbox: list           # [x0, y0, x1, y1] in frame coords
    frame: int
    area_px: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["confidence"] = round(self.confidence, 3)
        return d


class PanelAnomalyDetector:
    name = "panel-anomaly-cv"

    def __init__(self, min_area: int = 500, color_tol: int = 26, seed_step: int = 40,
                 min_panel_frac: float = 0.04, max_area_frac: float = 0.03):
        self.min_area = min_area
        self.max_area_frac = max_area_frac
        self.color_tol = color_tol
        self.seed_step = seed_step
        self.min_panel_frac = min_panel_frac

    # -- panel segmentation -------------------------------------------------
    def _panels(self, img: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
        """Return (mask, mean_bgr) for each large uniform surface."""
        h, w = img.shape[:2]
        smooth = cv2.bilateralFilter(img, 7, 40, 7)
        visited = np.zeros((h, w), np.uint8)
        panels = []
        tol = (self.color_tol,) * 3
        for y in range(self.seed_step // 2, h, self.seed_step):
            for x in range(self.seed_step // 2, w, self.seed_step):
                if visited[y, x]:
                    continue
                mask = np.zeros((h + 2, w + 2), np.uint8)
                flags = 4 | cv2.FLOODFILL_FIXED_RANGE | cv2.FLOODFILL_MASK_ONLY | (255 << 8)
                area, *_ = cv2.floodFill(smooth, mask, (x, y), 0, tol, tol, flags)
                region = mask[1:-1, 1:-1]
                visited |= region
                if area < self.min_panel_frac * w * h:
                    continue
                pix = img[region > 0]
                if pix.std(axis=0).mean() > 11:      # textured (asphalt, foliage) - not a panel
                    continue
                panels.append((region, pix.mean(axis=0)))
        return panels

    # -- anomaly classification ---------------------------------------------
    def _classify(self, img, hole_mask, cnt, panel_bgr) -> tuple[str, float] | None:
        area = cv2.contourArea(cnt)
        x, y, cw, ch = cv2.boundingRect(cnt)
        hull_area = cv2.contourArea(cv2.convexHull(cnt)) or 1
        solidity = area / hull_area
        extent = area / max(1, cw * ch)
        (_, _), (rw, rh), _ = cv2.minAreaRect(cnt)
        elong = max(rw, rh) / max(1.0, min(rw, rh))

        # manufactured features are crisp rectangles: placards, lights, handles
        if extent > 0.88 and solidity > 0.95:
            return None

        pix = img[hole_mask > 0]
        if len(pix) == 0:
            return None
        mean_bgr = pix.mean(axis=0)
        hsv = cv2.cvtColor(np.uint8([[mean_bgr]]), cv2.COLOR_BGR2HSV)[0, 0]
        hue, sat, val = int(hsv[0]), int(hsv[1]), int(hsv[2])
        panel_v = float(max(panel_bgr)) or 1.0
        rel_v = val / panel_v

        pv = cv2.cvtColor(pix.reshape(-1, 1, 3), cv2.COLOR_BGR2HSV)[:, 0, 2].astype(np.float32)
        dark_frac = float(((pv < 60) & (pv < 0.3 * panel_v)).mean())

        if 3 <= hue <= 25 and sat >= 90:
            return "rust", 0.80 + min(0.15, (sat - 90) / 600)
        if elong >= 4.0 and rel_v < 0.75:
            return "scratch", 0.70
        if dark_frac > 0.35:
            return "hole", 0.75 + min(0.2, dark_frac / 5)
        panel_hsv = cv2.cvtColor(np.uint8([[panel_bgr]]), cv2.COLOR_BGR2HSV)[0, 0]
        same_hue = abs(int(panel_hsv[0]) - hue) < 12 or min(int(panel_hsv[1]), sat) < 60
        if rel_v < 0.80 and (sat < 90 or same_hue):
            irregular = 1.0 - min(solidity, extent)
            return "dent", 0.60 + min(0.3, irregular)
        return None

    def detect(self, img: np.ndarray, text_boxes: list[TextBox], frame: int = 0) -> list[DamageFinding]:
        h, w = img.shape[:2]
        text_mask = np.zeros((h, w), np.uint8)
        for b in text_boxes:
            if b.frame != frame:
                continue
            x0, y0, x1, y1 = b.bbox()
            cv2.rectangle(text_mask, (x0 - 8, y0 - 8), (x1 + 8, y1 + 8), 255, -1)

        findings: list[DamageFinding] = []
        for region, panel_bgr in self._panels(img):
            panel_area = int(region.sum() / 255)
            contours, hier = cv2.findContours(region, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
            if hier is None:
                continue
            for i, cnt in enumerate(contours):
                if hier[0][i][3] == -1:          # outer boundary, not a hole
                    continue
                area = cv2.contourArea(cnt)
                if area < self.min_area or area > self.max_area_frac * w * h:
                    continue                      # specks / whole objects enclosed by background
                hole = np.zeros((h, w), np.uint8)
                cv2.drawContours(hole, [cnt], -1, 255, -1)
                overlap = cv2.countNonZero(cv2.bitwise_and(hole, text_mask)) / max(1, cv2.countNonZero(hole))
                if overlap > 0.3:
                    continue                      # painted lettering
                cls = self._classify(img, hole, cnt, panel_bgr)
                if not cls:
                    continue
                kind, conf = cls
                frac = area / max(1, panel_area)
                if kind == "hole" or frac > 0.015:
                    severity = "severe"
                elif frac > 0.004:
                    severity = "moderate"
                else:
                    severity = "minor"
                x, y, cw, ch = cv2.boundingRect(cnt)
                findings.append(DamageFinding(kind, severity, conf, [x, y, x + cw, y + ch], frame, int(area)))
        return findings


class YoloOnnxDamageDetector:
    """YOLOv8 ONNX detector (output shape [1, 4+num_classes, N])."""
    name = "yolo-onnx"

    def __init__(self, model_path: str, classes: list[str], conf: float = 0.35, iou: float = 0.45):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0]
        shape = self.inp.shape
        self.size = int(shape[2]) if isinstance(shape[2], int) else 640
        self.classes = classes
        self.conf, self.iou = conf, iou

    def detect(self, img: np.ndarray, text_boxes: list[TextBox], frame: int = 0) -> list[DamageFinding]:
        h, w = img.shape[:2]
        r = self.size / max(h, w)
        nh, nw = int(round(h * r)), int(round(w * r))
        canvas = np.full((self.size, self.size, 3), 114, np.uint8)
        canvas[:nh, :nw] = cv2.resize(img, (nw, nh))
        blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = self.sess.run(None, {self.inp.name: blob})[0][0]       # (4+nc, N)
        out = out.T
        scores = out[:, 4:]
        cls_ids = scores.argmax(1)
        confs = scores.max(1)
        keep = confs >= self.conf
        boxes, confs, cls_ids = out[keep, :4], confs[keep], cls_ids[keep]
        xywh = [[float(cx - bw / 2) / r, float(cy - bh / 2) / r, float(bw) / r, float(bh) / r]
                for cx, cy, bw, bh in boxes]
        idx = cv2.dnn.NMSBoxes(xywh, confs.tolist(), self.conf, self.iou)
        findings = []
        for i in np.array(idx).flatten() if len(idx) else []:
            x, y, bw, bh = xywh[i]
            name = self.classes[cls_ids[i]] if cls_ids[i] < len(self.classes) else f"class_{cls_ids[i]}"
            frac = (bw * bh) / (w * h)
            sev = "severe" if frac > 0.02 or name == "hole" else "moderate" if frac > 0.005 else "minor"
            findings.append(DamageFinding(name, sev, float(confs[i]),
                                          [int(x), int(y), int(x + bw), int(y + bh)], frame, int(bw * bh)))
        return findings


def build_detector(settings):
    if settings.damage_model:
        try:
            det = YoloOnnxDamageDetector(settings.damage_model,
                                         [c.strip() for c in settings.damage_model_classes.split(",")])
            log.info("Using YOLO ONNX damage model %s", settings.damage_model)
            return det
        except Exception as e:     # fall back rather than leaving the gate without inspection
            log.error("Could not load damage model %s (%s); using CV detector", settings.damage_model, e)
    return PanelAnomalyDetector(min_area=settings.damage_min_area)
