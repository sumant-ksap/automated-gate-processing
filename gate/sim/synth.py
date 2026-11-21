"""Synthetic camera frames of trucks arriving at the gate.

Each arriving truck is captured by three cameras per lane:
  * front camera - license plate recognition (LPR), head-on
  * side camera  - tractor door (USDOT number) and trailer side panel
                   (trailer number, side damage)
  * rear camera - trailer doors (trailer number, bolt seal, door damage)

The frames are rendered with OpenCV and then degraded (noise, blur, lighting)
so the OCR / damage pipeline has to do real work. This lets the whole system
be demonstrated and tested without physical cameras.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, asdict

import cv2
import numpy as np

W, H = 1280, 720
FONT = cv2.FONT_HERSHEY_DUPLEX
FONT_BOLD = cv2.FONT_HERSHEY_SIMPLEX

DAMAGE_KINDS = ("dent", "rust", "hole", "scratch")


@dataclass
class Damage:
    kind: str                 # dent | rust | hole | scratch
    view: str = "side"        # side | rear
    # relative position inside the panel (0..1)
    rx: float = 0.5
    ry: float = 0.5
    size: float = 1.0


@dataclass
class TruckSpec:
    carrier_name: str
    dot_number: str
    plate: str
    plate_state: str
    trailer_number: str             # formatted ISO 6346, e.g. "NXTU 482913 4"
    seal_number: str | None
    seal_state: str = "intact"      # intact | broken | missing
    damages: list[Damage] = field(default_factory=list)
    cab_color: tuple = (40, 40, 170)
    trailer_color: tuple = (232, 234, 236)
    quality: str = "good"           # good | poor

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cab_color"] = list(self.cab_color)
        d["trailer_color"] = list(self.trailer_color)
        return d


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------
def _text(img, text, org, scale, color, thick, font=FONT):
    cv2.putText(img, text, org, font, scale, color, thick, cv2.LINE_AA)


def _text_size(text, scale, thick, font=FONT):
    (w, h), _ = cv2.getTextSize(text, font, scale, thick)
    return w, h


def _centered_text(img, text, cx, y, scale, color, thick, font=FONT):
    w, _ = _text_size(text, scale, thick, font)
    _text(img, text, (int(cx - w / 2), int(y)), scale, color, thick, font)


def _background(rng: random.Random) -> np.ndarray:
    img = np.zeros((H, W, 3), np.uint8)
    sky_top = np.array([215, 175, 120], np.float32) + rng.uniform(-15, 15)
    sky_bot = np.array([240, 220, 200], np.float32)
    horizon = 430
    for y in range(horizon):
        t = y / horizon
        img[y, :] = (sky_top * (1 - t) + sky_bot * t).astype(np.uint8)
    # asphalt
    road = np.full((H - horizon, W, 3), 85, np.uint8)
    noise = np.random.default_rng(rng.randint(0, 2**31)).normal(0, 9, road.shape)
    img[horizon:] = np.clip(road + noise, 0, 255).astype(np.uint8)
    # lane markings
    for x in range(0, W, 160):
        cv2.rectangle(img, (x, 650), (x + 80, 660), (200, 200, 200), -1)
    return img


def _blob(cx, cy, r, rng: random.Random, jag=0.35, n=22):
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        rr = r * (1 + rng.uniform(-jag, jag))
        pts.append((int(cx + rr * math.cos(a)), int(cy + 0.7 * rr * math.sin(a))))
    return np.array(pts, np.int32)


def _draw_damage(img, d: Damage, panel: tuple[int, int, int, int], panel_color, rng):
    x0, y0, x1, y1 = panel
    cx = int(x0 + 60 + d.rx * (x1 - x0 - 120))
    cy = int(y0 + 60 + d.ry * (y1 - y0 - 120))
    r = int(38 * d.size + rng.uniform(0, 10))
    pc = np.array(panel_color, np.float32)
    if d.kind == "dent":
        poly = _blob(cx, cy, r, rng, jag=0.25)
        cv2.fillPoly(img, [poly], tuple(int(v) for v in pc * 0.5), cv2.LINE_AA)
        inner = _blob(cx - r // 5, cy - r // 6, r * 0.55, rng, jag=0.2)
        cv2.fillPoly(img, [inner], tuple(int(v) for v in pc * 0.35), cv2.LINE_AA)
        # crease lines
        for _ in range(3):
            a = rng.uniform(0, math.pi)
            cv2.line(img, (int(cx - r * math.cos(a)), int(cy - r * 0.7 * math.sin(a))),
                     (int(cx + r * math.cos(a)), int(cy + r * 0.7 * math.sin(a))),
                     tuple(int(v) for v in pc * 0.3), 2, cv2.LINE_AA)
    elif d.kind == "rust":
        for _ in range(4):
            poly = _blob(cx + rng.randint(-r // 2, r // 2), cy + rng.randint(-r // 3, r // 3),
                         r * rng.uniform(0.45, 0.8), rng, jag=0.45)
            col = (rng.randint(20, 45), rng.randint(70, 100), rng.randint(140, 175))
            cv2.fillPoly(img, [poly], col, cv2.LINE_AA)
    elif d.kind == "hole":
        poly = _blob(cx, cy, r * 0.8, rng, jag=0.5, n=28)
        cv2.fillPoly(img, [poly], (18, 18, 22), cv2.LINE_AA)
        cv2.polylines(img, [poly], True, tuple(int(v) for v in pc * 0.55), 3, cv2.LINE_AA)
    elif d.kind == "scratch":
        cx = min(max(cx, x0 + 2 * r + 20), x1 - 260)
        for i in range(4):
            sx = cx - r * 2 + rng.randint(-10, 10)
            sy = cy + i * 9 + rng.randint(-4, 4)
            pts = [(sx + k * 18, sy + int(4 * math.sin(k / 2.0)) + rng.randint(-2, 2)) for k in range(14)]
            cv2.polylines(img, [np.array(pts, np.int32)], False, (35, 35, 40), 4, cv2.LINE_AA)


def _draw_ribs(img, panel, color, step=56):
    x0, y0, x1, y1 = panel
    rib = tuple(max(0, int(c) - 14) for c in color)
    for x in range(x0 + step, x1 - 10, step):
        cv2.line(img, (x, y0 + 4), (x, y1 - 4), rib, 2)


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------
def render_side(spec: TruckSpec, rng: random.Random) -> np.ndarray:
    img = _background(rng)
    tc = spec.trailer_color
    # trailer box
    panel = (400, 120, 1235, 500)
    cv2.rectangle(img, panel[:2], panel[2:], tc, -1)
    _draw_ribs(img, panel, tc)
    cv2.rectangle(img, panel[:2], panel[2:], (90, 90, 95), 3)
    # trailer chassis + wheels
    cv2.rectangle(img, (400, 500), (1235, 528), (45, 45, 50), -1)
    for wx in (980, 1080, 1150):
        cv2.circle(img, (wx, 560), 42, (25, 25, 25), -1)
        cv2.circle(img, (wx, 560), 18, (140, 140, 140), -1)
    # carrier name on trailer
    name = spec.carrier_name.upper()
    scale = min(2.2, 760 / max(1, _text_size(name, 1.0, 4)[0]))
    _centered_text(img, name, 820, 330, scale, (120, 60, 30), 4)
    # trailer number (upper right of the side panel, as painted on real trailers)
    _text(img, spec.trailer_number, (870, 180), 1.15, (20, 20, 20), 2)

    # tractor
    cc = spec.cab_color
    cab = np.array([(70, 520), (70, 300), (110, 200), (300, 200), (330, 300), (390, 300), (390, 520)], np.int32)
    cv2.fillPoly(img, [cab], cc, cv2.LINE_AA)
    cv2.polylines(img, [cab], True, (20, 20, 20), 2, cv2.LINE_AA)
    # window
    cv2.fillPoly(img, [np.array([(120, 215), (290, 215), (290, 290), (95, 290)], np.int32)], (110, 80, 60))
    # door panel with USDOT marking
    door = (140, 305, 325, 455)
    cv2.rectangle(img, door[:2], door[2:], tuple(min(255, int(c) + 25) for c in cc), -1)
    cv2.rectangle(img, door[:2], door[2:], (20, 20, 20), 2)
    cv2.rectangle(img, (150, 370), (318, 430), (245, 245, 245), -1)
    _centered_text(img, "USDOT", 234, 393, 0.62, (10, 10, 10), 2)
    _centered_text(img, spec.dot_number, 234, 423, 0.8, (10, 10, 10), 2)
    # wheels
    for wx in (185, 335):
        cv2.circle(img, (wx, 560), 45, (25, 25, 25), -1)
        cv2.circle(img, (wx, 560), 20, (150, 150, 150), -1)
    # front bumper (the license plate faces forward - read by the front camera)
    cv2.rectangle(img, (55, 470), (110, 520), (160, 160, 165), -1)

    for d in spec.damages:
        if d.view == "side":
            _draw_damage(img, d, (panel[0] + 10, 360, panel[2] - 10, panel[3] - 5), tc, rng)
    return img


def render_front(spec: TruckSpec, rng: random.Random) -> np.ndarray:
    """Front (LPR) camera: tractor head-on, license plate on the bumper."""
    img = _background(rng)
    cc = spec.cab_color
    dark = tuple(max(0, int(c) - 45) for c in cc)
    # trailer box visible behind / above the cab
    cv2.rectangle(img, (400, 60), (880, 180), spec.trailer_color, -1)
    cv2.rectangle(img, (400, 60), (880, 180), (90, 90, 95), 3)
    # cab body
    cab = np.array([(450, 560), (450, 190), (480, 140), (800, 140), (830, 190), (830, 560)], np.int32)
    cv2.fillPoly(img, [cab], cc, cv2.LINE_AA)
    cv2.polylines(img, [cab], True, (20, 20, 20), 2, cv2.LINE_AA)
    # windshield (split) + mirrors
    cv2.rectangle(img, (485, 165), (635, 290), (110, 80, 60), -1)
    cv2.rectangle(img, (645, 165), (795, 290), (110, 80, 60), -1)
    for x0, x1 in ((395, 440), (840, 885)):
        cv2.rectangle(img, (x0, 180), (x1, 270), (40, 40, 45), -1)
    # grille with horizontal slats
    cv2.rectangle(img, (545, 315), (735, 455), (150, 150, 155), -1)
    for y in range(325, 450, 14):
        cv2.line(img, (553, y), (727, y), (90, 90, 95), 4)
    # headlights
    for x0 in (470, 755):
        cv2.rectangle(img, (x0, 330), (x0 + 55, 375), (215, 235, 245), -1)
        cv2.rectangle(img, (x0, 330), (x0 + 55, 375), dark, 3)
    # bumper + plate
    cv2.rectangle(img, (430, 470), (850, 545), (170, 170, 175), -1)
    cv2.rectangle(img, (430, 470), (850, 545), (60, 60, 65), 2)
    plate = (550, 462, 730, 552)
    cv2.rectangle(img, plate[:2], plate[2:], (245, 245, 240), -1)
    cv2.rectangle(img, plate[:2], plate[2:], (30, 30, 30), 3)
    _centered_text(img, spec.plate_state.upper(), 640, 484, 0.5, (120, 40, 20), 1, FONT_BOLD)
    pscale = min(1.15, 160 / max(1, _text_size(spec.plate, 1.0, 3)[0]))
    _centered_text(img, spec.plate, 640, 534, pscale, (15, 15, 15), 3)
    # wheels
    for x0 in (455, 765):
        cv2.rectangle(img, (x0, 545), (x0 + 60, 640), (25, 25, 25), -1)
    return img


def render_rear(spec: TruckSpec, rng: random.Random) -> np.ndarray:
    img = _background(rng)
    tc = spec.trailer_color
    frame = (300, 70, 980, 560)
    cv2.rectangle(img, frame[:2], frame[2:], (70, 70, 75), -1)
    doors = [(320, 90, 636, 540), (644, 90, 960, 540)]
    for d in doors:
        cv2.rectangle(img, d[:2], d[2:], tc, -1)
        cv2.rectangle(img, d[:2], d[2:], (60, 60, 60), 2)
    # trailer number across the doors (upper band, above the locking bars)
    cv2.rectangle(img, (320, 90), (960, 165), tc, -1)
    _centered_text(img, spec.trailer_number, 640, 145, 1.2, (20, 20, 20), 2)
    # locking bars - run to the door bottom so they are not enclosed "holes"
    for bx in (400, 560, 724, 884):
        cv2.rectangle(img, (bx - 6, 172), (bx + 6, 540), (110, 110, 115), -1)
        cv2.rectangle(img, (bx - 22, 330), (bx + 6, 345), (95, 95, 100), -1)   # handle attached to bar
    # bumper / under-ride guard
    cv2.rectangle(img, (300, 590), (980, 615), (40, 40, 160), -1)
    for wx in (380, 900):
        cv2.rectangle(img, (wx - 50, 560), (wx + 50, 660), (25, 25, 25), -1)

    # bolt seal on the right door handle (tag hangs below, on the dark frame area)
    tag = (560, 600, 840, 700)
    if spec.seal_state == "intact" and spec.seal_number:
        cv2.line(img, (724, 345), (700, tag[1]), (60, 160, 230), 4)
        cv2.rectangle(img, tag[:2], tag[2:], (40, 200, 240), -1)
        cv2.rectangle(img, tag[:2], tag[2:], (20, 20, 20), 2)
        _text(img, "SEAL", (575, 642), 0.85, (10, 10, 10), 2)
        _text(img, spec.seal_number, (575, 685), 1.05, (10, 10, 10), 2)
    elif spec.seal_state == "broken":
        # cut cable dangling, tag hanging sideways and partly torn
        cv2.line(img, (724, 345), (735, 420), (60, 160, 230), 4)
        cv2.line(img, (712, 470), (690, 560), (60, 160, 230), 4)
        pts = np.array([(600, 610), (760, 640), (745, 700), (585, 672)], np.int32)
        cv2.fillPoly(img, [pts], (40, 170, 210))
        cv2.line(img, (650, 615), (690, 690), (30, 30, 30), 3)
    # missing: nothing drawn

    for d in spec.damages:
        if d.view == "rear":
            # clear spans between locking bars (left door / right door)
            span = (420, 360, 540, 540) if d.rx < 0.5 else (744, 360, 864, 540)   # below the handles
            _draw_damage(img, Damage(d.kind, "rear", 0.5, d.ry, min(d.size, 0.8)),
                         (span[0] - 60, span[1], span[2] + 60, span[3]), tc, rng)
    return img


def _degrade(img: np.ndarray, rng: random.Random, quality: str) -> np.ndarray:
    nrng = np.random.default_rng(rng.randint(0, 2**31))
    out = img.astype(np.float32)
    # global illumination
    gain = rng.uniform(0.9, 1.08) if quality == "good" else rng.uniform(0.45, 0.6)
    out *= gain
    sigma = 3.0 if quality == "good" else 14.0
    out += nrng.normal(0, sigma, out.shape)
    out = np.clip(out, 0, 255).astype(np.uint8)
    k = 3 if quality == "good" else 9
    out = cv2.GaussianBlur(out, (k, k), 0)
    if quality == "poor":
        # motion blur + heavy JPEG artifacts, emulating rain / dirty lens
        kern = np.zeros((15, 15), np.float32)
        kern[7, :] = 1 / 15
        out = cv2.filter2D(out, -1, kern)
        _, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 12])
        out = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return out


def render_truck(spec: TruckSpec, seed: int | None = None) -> dict[str, np.ndarray]:
    rng = random.Random(seed)
    return {
        "front": _degrade(render_front(spec, rng), rng, spec.quality),
        "side": _degrade(render_side(spec, rng), rng, spec.quality),
        "rear": _degrade(render_rear(spec, rng), rng, spec.quality),
    }
