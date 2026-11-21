"""Turn-by-turn yard directions from the gate (or a parking spot) to a door / spot.

The yard road network is a simple grid: a main road runs north from the gate,
an apron runs east-west in front of the dock doors, and a lane runs along the
south side of each parking row. Directions are produced as language-neutral
steps that i18n renders, plus a polyline the driver page draws on its map.
"""
from __future__ import annotations

from ..yard.allocator import GATE_XY

DOCK_APRON_Y = 30.0
INSPECTION_BAY = (330.0, 240.0)


def _r10(m: float) -> int:
    return int(round(m / 10.0) * 10) or 10


def route(resource: dict, start: tuple[float, float] = GATE_XY, from_spot: bool = False) -> tuple[list[tuple], list]:
    """Steps like ("north", 230), ("left",), ("continue", 80), ("door_final", "D07") and map points."""
    sx, sy = start
    x, y = float(resource["x"]), float(resource["y"])
    is_door = resource["kind"] == "DOOR"
    lane_y = DOCK_APRON_Y if is_door else y + 20
    steps: list[tuple] = []
    pts = [[sx, sy]]
    if from_spot:
        # pull out of the spot onto its row lane first
        pts.append([sx, sy + 20])
        sy = sy + 20
    if sy - lane_y > 5:
        steps.append(("north", _r10(sy - lane_y)))
    pts.append([sx, lane_y])
    dx = x - sx
    if abs(dx) >= 6:
        steps.append(("left",) if dx < 0 else ("right",))
        steps.append(("continue", _r10(abs(dx))))
    pts += [[x, lane_y], [x, y]]
    if is_door:
        steps.append(("door_final", resource["id"]))
    else:
        # spots are north of their lane: heading east they are on the left, heading west on the right
        steps.append(("spot_left" if dx >= 0 else "spot_right", resource["id"]))
        if "POWER" in (resource.get("capabilities") or ""):
            steps.append(("power",))
    return steps, pts


def inspection_route() -> list:
    gx, gy = GATE_XY
    return [[gx, gy], [INSPECTION_BAY[0], gy], [INSPECTION_BAY[0], INSPECTION_BAY[1]]]
