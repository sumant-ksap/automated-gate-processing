"""Identifier extraction: OCR + license plate localisation + multi-camera fusion.

Given all camera frames captured for one truck pass, returns the best reading
for each identifier: license plate, trailer number, USDOT number, seal number.
"""
from __future__ import annotations

import cv2
import numpy as np

from . import identifiers as ids
from .identifiers import Reading
from .ocr import TextBox, read_text

LABELS = {"USDOT": "dot", "DOT": "dot", "US DOT": "dot", "SEAL": "seal", "SEAL NO": "seal", "SEAL#": "seal"}


def find_plate_regions(img: np.ndarray, max_regions: int = 4) -> list[tuple[int, int, int, int]]:
    """Locate license-plate-like regions: bright, bordered rectangles of plate proportions."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cands = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cw * ch
        if area < 0.002 * w * h or area > 0.06 * w * h:
            continue
        aspect = cw / max(ch, 1)
        if not 1.6 <= aspect <= 3.2:
            continue
        rect_fill = cv2.contourArea(c) / area
        if rect_fill < 0.75:
            continue
        roi = gray[y:y + ch, x:x + cw]
        # plates contain dark characters on a light background
        dark_ratio = float((roi < roi.mean() * 0.6).mean())
        if not 0.04 <= dark_ratio <= 0.5:
            continue
        cands.append((rect_fill * min(dark_ratio, 0.25), (x, y, x + cw, y + ch)))
    cands.sort(key=lambda t: -t[0])
    out: list[tuple[int, int, int, int]] = []
    for _, b in cands:
        if all(_iou(b, o) < 0.3 for o in out):
            out.append(b)
        if len(out) >= max_regions:
            break
    return out


def _iou(a, b) -> float:
    ix0, iy0, ix1, iy1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    if inter == 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua


def _label_value_pairs(boxes: list[TextBox]) -> list[tuple[str, TextBox, TextBox]]:
    """Join a label box ("USDOT", "SEAL") with the value printed below/right of it."""
    pairs = []
    for lb in boxes:
        key = ids.clean(lb.text)
        kind = None
        for label, k in LABELS.items():
            if key == ids.clean(label):
                kind = k
        if not kind:
            continue
        best, best_d = None, 1e9
        for vb in boxes:
            if vb is lb or vb.frame != lb.frame:
                continue
            below = 0 <= vb.y0 - lb.y0 <= 3.5 * lb.h and abs(vb.cx - lb.cx) < max(vb.x1 - vb.x0, 3 * lb.h) \
                or (vb.y0 >= lb.y0 and abs(vb.x0 - lb.x0) < 2 * lb.h and vb.y0 - lb.y1 < 3 * lb.h)
            right = abs(vb.cy - lb.cy) < 0.6 * lb.h and 0 <= vb.x0 - lb.x1 <= 6 * lb.h
            if below or right:
                d = abs(vb.cx - lb.cx) + abs(vb.cy - lb.cy)
                if d < best_d:
                    best, best_d = vb, d
        if best is not None:
            pairs.append((kind, lb, best))
    return pairs


def _better(cur: Reading | None, new: Reading) -> Reading:
    if cur is None:
        return new
    # uncorrected, higher-confidence readings win; agreeing reads reinforce
    if cur.value == new.value:
        cur.confidence = min(0.999, max(cur.confidence, new.confidence) + 0.02)
        cur.notes.append(f"confirmed by frame {new.frame}")
        return cur
    score_cur = cur.confidence - (0.15 if cur.corrected else 0)
    score_new = new.confidence - (0.15 if new.corrected else 0)
    return new if score_new > score_cur else cur


def extract_identifiers(frames: list[np.ndarray]) -> tuple[dict[str, Reading | None], list[TextBox], list[dict]]:
    """OCR every frame and fuse the results.

    Returns (readings, all_text_boxes, plate_regions) where readings has keys
    plate, trailer, dot, seal.
    """
    results: dict[str, Reading | None] = {"plate": None, "trailer": None, "dot": None, "seal": None}
    all_boxes: list[TextBox] = []
    plate_regions: list[dict] = []

    for fi, img in enumerate(frames):
        boxes = read_text(img, frame=fi)
        all_boxes.extend(boxes)

        # 1. single boxes
        for b in boxes:
            if (r := ids.parse_trailer(b.text)):
                results["trailer"] = _better(results["trailer"], Reading(
                    r[0], b.confidence * (0.9 if r[1] else 1.0), b.text, r[1], b.box, fi))
            if (r := ids.parse_dot(b.text)):
                results["dot"] = _better(results["dot"], Reading(
                    r[0], b.confidence * (0.9 if r[1] else 1.0), b.text, r[1], b.box, fi))
            if (r := ids.parse_seal(b.text)):
                results["seal"] = _better(results["seal"], Reading(
                    r[0], b.confidence * (0.9 if r[1] else 1.0), b.text, r[1], b.box, fi))

        # 2. label + value pairs ("USDOT" / "2847193")
        for kind, lb, vb in _label_value_pairs(boxes):
            joined = f"{lb.text} {vb.text}"
            parser = ids.parse_dot if kind == "dot" else ids.parse_seal
            if (r := parser(joined)):
                conf = min(lb.confidence, vb.confidence) * (0.9 if r[1] else 1.0)
                box = [[min(lb.x0, vb.x0), min(lb.y0, vb.y0)], [max(lb.x1, vb.x1), min(lb.y0, vb.y0)],
                       [max(lb.x1, vb.x1), max(lb.y1, vb.y1)], [min(lb.x0, vb.x0), max(lb.y1, vb.y1)]]
                results[kind] = _better(results[kind], Reading(r[0], conf, joined, r[1], box, fi))

        # 3. license plate: localise, upscale, OCR the crop
        used_text = {id(b) for b in boxes if ids.parse_dot(b.text) or ids.parse_trailer(b.text)}
        for (x0, y0, x1, y1) in find_plate_regions(img):
            pad = 6
            x0p, y0p = max(0, x0 - pad), max(0, y0 - pad)
            crop = img[y0p:y1 + pad, x0p:x1 + pad]
            if crop.size == 0:
                continue
            scale = max(1.0, 240 / crop.shape[1])
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            region = {"frame": fi, "bbox": [x0, y0, x1, y1], "text": []}
            for b in read_text(crop, frame=fi, offset=(x0p, y0p), scale=scale):
                region["text"].append(b.text)
                if (p := ids.looks_like_plate(b.text)):
                    rd = Reading(p, b.confidence, b.text, False, b.box, fi, ["plate region"])
                    results["plate"] = _better(results["plate"], rd)
            plate_regions.append(region)

        # 4. fallback: plate-shaped text anywhere in the frame
        if results["plate"] is None:
            for b in boxes:
                if id(b) in used_text:
                    continue
                if (p := ids.looks_like_plate(b.text)) and b.confidence > 0.85:
                    results["plate"] = _better(results["plate"], Reading(
                        p, b.confidence * 0.8, b.text, False, b.box, fi, ["no plate region"]))

    return results, all_boxes, plate_regions
