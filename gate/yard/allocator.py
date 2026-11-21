"""Intelligent dock & spot allocation.

When trucks clear the gate, the engine decides where each one goes - a dock
door now, or a parking spot to wait - by solving one *joint* assignment for
every truck checking in at the same time (minimum-cost bipartite matching,
Hungarian algorithm). The cost of putting truck *t* on resource *r* weighs:

  * load priority      - perishable, urgent cross-dock and detention-sensitive
                         loads earn a large bonus for getting a door now
  * equipment          - hard rules (perishables only at cold docks, flatbeds
                         only at ground-level doors, perishables wait on powered
                         reefer spots) and soft rules (keep scarce cold / cross-dock
                         doors for the loads that need them)
  * yard travel        - metres the truck / yard jockey drives (gate -> resource,
                         and spot -> its future door)
  * congestion         - recent yard moves per zone, plus a penalty when one batch
                         would send too many trucks into the same zone (gridlock)

A greedy first-come-first-served (FCFS) dispatcher is computed on the same
situation so the dashboard can show what the optimisation actually saves.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..db import Database, iso, parse_iso, utcnow
from ..seed import DOCKS, DOOR_CAPS

log = logging.getLogger(__name__)

# --------------------------------------------------------------------- layout (metres)
GATE_XY = (200.0, 260.0)
SPOT_ROWS = {"A": 90.0, "B": 130.0, "C": 170.0}
SPOTS_PER_ROW = 20
POWER_SPOTS = {f"P-A{i:02d}" for i in range(1, 9)}          # reefer plug-in spots, west end of row A
DETENTION_FREE_MIN = 120                                    # carrier free time before detention charges
INFEASIBLE = 1e6
NO_RESOURCE = 1e5
MAX_PER_ZONE_PER_BATCH = 3

DOOR_LABEL = {"REEFER": "cold dock", "FLATBED": "ground-level ramp", "CROSS_DOCK": "cross-dock", "": "dry dock"}


def zone_of(x: float) -> str:
    return "WEST" if x < 140 else "CENTRAL" if x < 270 else "EAST"


def layout() -> list[dict]:
    res = []
    for i, d in enumerate(DOCKS):
        x = 20 + i * 16
        res.append({"id": d, "kind": "DOOR", "zone": zone_of(x), "x": x, "y": 0.0, "capabilities": DOOR_CAPS[d]})
    for row, y in SPOT_ROWS.items():
        for j in range(SPOTS_PER_ROW):
            sid = f"P-{row}{j + 1:02d}"
            x = 20 + j * 19
            res.append({"id": sid, "kind": "SPOT", "zone": zone_of(x), "x": x, "y": y,
                        "capabilities": "POWER" if sid in POWER_SPOTS else ""})
    return res


def ensure_yard(db: Database) -> None:
    for r in layout():
        if not db.one("SELECT id FROM yard_resources WHERE id = ?", (r["id"],)):
            db.execute("INSERT INTO yard_resources (id, kind, zone, x, y, capabilities) VALUES (?,?,?,?,?,?)",
                       (r["id"], r["kind"], r["zone"], r["x"], r["y"], r["capabilities"]))


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Yard roads run on a grid: Manhattan distance in metres."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


# --------------------------------------------------------------------- min-cost assignment
def hungarian(cost: list[list[float]]) -> list[int]:
    """Optimal assignment for an n x m cost matrix with n <= m. Returns column index per row."""
    n, m = len(cost), len(cost[0]) if cost else 0
    if n == 0:
        return []
    assert n <= m, "more rows than columns"
    INF = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [INF] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    ans = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            ans[p[j] - 1] = j - 1
    return ans


# --------------------------------------------------------------------- rules
@dataclass
class Truck:
    visit_id: int | None
    appointment_id: int | None
    plate: str
    trailer_type: str
    load_class: str
    pallets: int
    booked_door: str | None
    detention_at: datetime
    due_by: datetime | None
    origin: tuple[float, float] = GATE_XY          # gate, or its current spot
    current_spot: str | None = None
    priority: float = 1.0
    priority_why: list[str] = field(default_factory=list)


def score_priority(t: Truck, now: datetime) -> None:
    p, why = 1.0, []
    if t.load_class == "PERISHABLE":
        p += 3
        why.append("perishable load")
    if t.load_class == "CROSS_DOCK":
        p += 2
        if t.due_by is not None:
            mins = (t.due_by - now).total_seconds() / 60
            if mins < 90:
                p += 2
                why.append(f"cross-dock, outbound departs in {max(0, mins):.0f} min")
            else:
                why.append("cross-dock load")
        else:
            why.append("cross-dock load")
    det = (t.detention_at - now).total_seconds() / 60
    if det < 30:
        p += 2
        why.append(f"detention charges in {max(0, det):.0f} min")
    elif det < 60:
        p += 1
        why.append(f"detention charges in {det:.0f} min")
    t.priority, t.priority_why = p, why


def caps(r: dict) -> set[str]:
    return {c for c in (r["capabilities"] or "").split(",") if c}


def door_feasible(t: Truck, r: dict) -> str | None:
    """None if allowed, else the reason it is not."""
    c = caps(r)
    if t.trailer_type == "FLATBED" and "FLATBED" not in c:
        return "flatbed needs a ground-level door"
    if t.trailer_type != "FLATBED" and "FLATBED" in c:
        return "ground-level ramp has no dock height"
    if t.load_class == "PERISHABLE" and "REEFER" not in c:
        return "perishable load needs a cold dock"
    return None


def spot_feasible(t: Truck, r: dict) -> str | None:
    if t.load_class == "PERISHABLE" and "POWER" not in caps(r):
        return "perishable trailer must stay plugged in"
    return None


class YardAllocator:
    def __init__(self, db: Database, settings, bus=None):
        self.db, self.settings, self.bus = db, settings, bus
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.last_batch: dict | None = None
        self.notifier = None                       # set by the app: driver SMS / app messages
        ensure_yard(db)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        threading.Thread(target=self._loop, name="yard", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("yard tick failed")
            self._stop.wait(2.0)

    def reset(self) -> None:
        with self._lock:
            self.last_batch = None

    # ------------------------------------------------------------------ helpers
    def _resources(self, kind: str | None = None, free_only: bool = True) -> list[dict]:
        sql, params = "SELECT * FROM yard_resources WHERE 1=1", []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if free_only:
            sql += " AND status = 'FREE'"
        return self.db.query(sql + " ORDER BY id", params)

    def _zone_load(self, now: datetime) -> dict[str, float]:
        rows = self.db.query("SELECT zone, COUNT(*) AS n FROM yard_moves WHERE created_at >= ? GROUP BY zone",
                             (iso(now - timedelta(minutes=10)),))
        load = {"WEST": 0.0, "CENTRAL": 0.0, "EAST": 0.0}
        for r in rows:
            load[r["zone"]] = float(r["n"])
        return load

    def _truck_from_appointment(self, appointment_id: int, now: datetime) -> Truck:
        a = self.db.one("""SELECT a.*, s.trailer_type, s.load_class, s.pallet_count, s.due_by
                           FROM appointments a LEFT JOIN asns s ON s.id = a.asn_id WHERE a.id = ?""",
                        (appointment_id,))
        sched = parse_iso(a["scheduled_at"])
        # free time runs from the appointment slot; a carrier that is itself very late earns no detention
        clock_start = sched if now - sched <= timedelta(minutes=DETENTION_FREE_MIN) else now
        t = Truck(None, appointment_id, a["tractor_plate"], a["trailer_type"] or "DRY_VAN",
                  a["load_class"] or "STANDARD", a["pallet_count"] or 10, a["dock_door"],
                  clock_start + timedelta(minutes=DETENTION_FREE_MIN),
                  parse_iso(a["due_by"]) if a["due_by"] else None)
        score_priority(t, now)
        return t

    def _nearest_door_dist(self, t: Truck, spot: dict, doors_all: list[dict]) -> float:
        ok = [d for d in doors_all if door_feasible(t, d) is None]
        return min((dist((spot["x"], spot["y"]), (d["x"], d["y"])) for d in ok), default=400.0)

    def _cost(self, t: Truck, r: dict, zone_load: dict, zone_pen: dict, doors_all: list[dict]) -> tuple[float, list[str]]:
        why: list[str] = []
        if r["kind"] == "DOOR":
            bad = door_feasible(t, r)
            if bad:
                return INFEASIBLE, [bad]
            travel = dist(t.origin, (r["x"], r["y"]))
            c = travel / 100 - 10 * t.priority
            cap = caps(r)
            if "REEFER" in cap and t.load_class != "PERISHABLE":
                c += 6
                why.append("keeps cold dock free for perishables")
            if "CROSS_DOCK" in cap and t.load_class != "CROSS_DOCK":
                c += 3
            if t.load_class == "CROSS_DOCK" and "CROSS_DOCK" not in cap:
                c += 4
            if r["id"] == t.booked_door:
                c -= 2.0          # dock labour is planned around the booked door: change it only for a real gain
        else:
            bad = spot_feasible(t, r)
            if bad:
                return INFEASIBLE, [bad]
            travel = dist(t.origin, (r["x"], r["y"])) + 0.5 * self._nearest_door_dist(t, r, doors_all)
            c = travel / 100
            if t.trailer_type == "REEFER" and "POWER" not in caps(r):
                c += 2
        c += 1.5 * zone_load.get(r["zone"], 0) + zone_pen.get(r["zone"], 0)
        return c, why

    def _solve(self, trucks: list[Truck], options: list[dict], now: datetime,
               stay_cols: dict[int, int] | None = None) -> list[dict | None]:
        """Joint assignment of trucks to options (+ a 'no resource' fallback per truck).
        stay_cols maps truck index -> index of an extra 'stay where you are' column."""
        zone_load = self._zone_load(now)
        doors_all = self._resources("DOOR", free_only=False)
        stay_cols = stay_cols or {}
        n_stay = len(stay_cols)
        zone_pen: dict[str, float] = {}
        best = None
        for _ in range(3):                                       # congestion-aware re-solve
            m = len(options) + n_stay + len(trucks)
            cost = []
            for i, t in enumerate(trucks):
                row = [self._cost(t, r, zone_load, zone_pen, doors_all)[0] for r in options]
                row += [INFEASIBLE] * n_stay
                if i in stay_cols:
                    row[len(options) + stay_cols[i]] = 0.0
                row += [NO_RESOURCE if k == i else INFEASIBLE for k in range(len(trucks))]
                cost.append(row)
            assign = hungarian(cost) if trucks and m else []
            best = assign
            counts: dict[str, int] = {}
            for i, j in enumerate(assign):
                if j < len(options):
                    counts[options[j]["zone"]] = counts.get(options[j]["zone"], 0) + 1
            over = {z: n - MAX_PER_ZONE_PER_BATCH for z, n in counts.items() if n > MAX_PER_ZONE_PER_BATCH}
            if not over:
                break
            for z, k in over.items():
                zone_pen[z] = zone_pen.get(z, 0) + 1.5 * k
        out: list[dict | None] = []
        for i, j in enumerate(best or []):
            if j < len(options):
                out.append(options[j])
            elif j < len(options) + n_stay:
                out.append({"id": "STAY"})
            else:
                out.append(None)
        return out

    def _explain(self, t: Truck, r: dict | None, doors_free: list[dict]) -> list[str]:
        if r is None:
            return ["No compatible door or spot free - hold in gate staging lane"]
        why = []
        if t.priority_why:
            why.append(f"Priority {t.priority:.0f}: " + ", ".join(t.priority_why))
        if r["kind"] == "DOOR":
            label = DOOR_LABEL.get(r["capabilities"] or "", "dock")
            why.append(f"{r['id']} is a {label} - compatible with {t.trailer_type.replace('_', ' ').lower()}"
                       f"{' (perishable)' if t.load_class == 'PERISHABLE' else ''}")
            why.append(f"{dist(t.origin, (r['x'], r['y'])):.0f} m from {'gate' if t.origin == GATE_XY else 'spot'}, "
                       f"{r['zone'].lower()} zone")
            cap = caps(r)
            if "REEFER" in cap and t.load_class != "PERISHABLE":
                why.append("no dry dock free - cold dock lent out because this load is urgent")
            if t.booked_door and r["id"] != t.booked_door:
                why.append(f"booked door {t.booked_door} was not the best free option")
        else:
            compatible_free = [d for d in doors_free if door_feasible(t, d) is None]
            why.append("No compatible door free right now" if not compatible_free else
                       "Higher-priority loads took the compatible doors")
            if "POWER" in caps(r):
                why.append(f"{r['id']} has shore power - reefer unit stays plugged in")
            why.append(f"{r['zone'].lower()} zone, close to the doors it will need; moves to a door automatically")
        return why

    # ------------------------------------------------------------------ actions
    def _record_move(self, visit_id: int, from_id: str, r: dict, distance: float, now: datetime) -> None:
        self.db.execute("INSERT INTO yard_moves (visit_id, created_at, from_id, to_id, zone, distance_m) "
                        "VALUES (?,?,?,?,?,?)", (visit_id, iso(now), from_id, r["id"], r["zone"], distance))

    def _place(self, t: Truck, r: dict | None, now: datetime, reasons: list[str], batch_id: str | None,
               event_id: int | None = None) -> dict:
        a = self.db.one("""SELECT a.appt_ref, a.trailer_number, c.name AS carrier FROM appointments a
                           JOIN carriers c ON c.id = a.carrier_id WHERE a.id = ?""", (t.appointment_id,)) or {}
        status = "UNASSIGNED" if r is None else "AT_DOOR" if r["kind"] == "DOOR" else "AT_SPOT"
        travel = dist(GATE_XY, (r["x"], r["y"])) if r else 0.0
        vid = self.db.execute(
            """INSERT INTO yard_visits (appointment_id, event_id, plate, trailer_number, carrier_name, trailer_type,
               load_class, pallet_count, priority, checked_in_at, detention_at, due_by, status, resource_id, door_at,
               jockey_m, reasons, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.appointment_id, event_id, t.plate, a.get("trailer_number"), a.get("carrier"), t.trailer_type,
             t.load_class, t.pallets, t.priority, iso(now), iso(t.detention_at),
             iso(t.due_by) if t.due_by else None, status, r["id"] if r else None,
             iso(now) if status == "AT_DOOR" else None, 0.0, json.dumps(reasons), batch_id))
        if r:
            self.db.execute("UPDATE yard_resources SET status='OCCUPIED', visit_id=? WHERE id=?", (vid, r["id"]))
            self._record_move(vid, "GATE", r, travel, now)
        return {"visit_id": vid, "resource_id": r["id"] if r else None,
                "kind": r["kind"] if r else None, "status": status, "reasons": reasons,
                "priority": t.priority, "plate": t.plate, "destination": self.destination_text(t, r)}

    @staticmethod
    def destination_text(t: Truck, r: dict | None) -> str:
        if r is None:
            return "Gate staging lane - waiting for a compatible door or spot"
        if r["kind"] == "DOOR":
            return f"Dock door {r['id']} ({DOOR_LABEL.get(r['capabilities'] or '', 'dock')})"
        return f"Yard spot {r['id']}{' (power)' if 'POWER' in caps(r) else ''} - wait for door"

    def check_in(self, appointment_ids: list[int], now: datetime | None = None,
                 batch_id: str | None = None) -> list[dict]:
        """Allocate trucks that just cleared the gate (jointly, if several arrive together)."""
        now = now or utcnow()
        with self._lock:
            done = {r["appointment_id"] for r in self.db.query(
                "SELECT appointment_id FROM yard_visits WHERE status != 'DEPARTED' AND appointment_id IS NOT NULL")}
            trucks = [self._truck_from_appointment(a, now) for a in appointment_ids if a not in done]
            if not trucks:
                return []
            doors_free = self._resources("DOOR")
            options = doors_free + self._resources("SPOT")
            plan = self._solve(trucks, options, now)
            out = []
            for t, r in zip(trucks, plan):
                out.append(self._place(t, r, now, self._explain(t, r, doors_free), batch_id))
            self._publish()
            return out

    def link_event(self, visit_id: int, event_id: int) -> None:
        self.db.execute("UPDATE yard_visits SET event_id=? WHERE id=?", (event_id, visit_id))

    def tick(self, now: datetime | None = None) -> None:
        """Finish unloads, free doors, and move waiting trucks from spots to doors (highest priority first)."""
        now = now or utcnow()
        scale = max(0.1, float(self.settings.yard_time_scale))
        with self._lock:
            changed = False
            for v in self.db.query("SELECT * FROM yard_visits WHERE status='AT_DOOR'"):
                unload_min = (15 + 1.2 * (v["pallet_count"] or 10)) / scale
                if parse_iso(v["door_at"]) + timedelta(minutes=unload_min) <= now:
                    self.db.execute("UPDATE yard_visits SET status='DEPARTED', unload_done_at=?, departed_at=? "
                                    "WHERE id=?", (iso(now), iso(now), v["id"]))
                    self.db.execute("UPDATE yard_resources SET status='FREE', visit_id=NULL WHERE id=?",
                                    (v["resource_id"],))
                    changed = True
                    self._notify("on_departed", v)
            waiting = self.db.query("""SELECT v.*, r.x, r.y FROM yard_visits v JOIN yard_resources r
                                       ON r.id = v.resource_id WHERE v.status='AT_SPOT'""")
            doors_free = self._resources("DOOR")
            if waiting and doors_free:
                trucks = []
                for v in waiting:
                    t = self._truck_from_appointment(v["appointment_id"], now)
                    t.visit_id, t.origin, t.current_spot = v["id"], (v["x"], v["y"]), v["resource_id"]
                    trucks.append(t)
                plan = self._solve(trucks, doors_free, now, stay_cols={i: i for i in range(len(trucks))})
                for t, r in zip(trucks, plan):
                    if r is None or r.get("id") == "STAY":
                        continue
                    d = dist(t.origin, (r["x"], r["y"]))
                    reasons = [f"Door {r['id']} freed up - moved from {t.current_spot} by yard jockey ({d:.0f} m)"]
                    reasons += self._explain(t, r, doors_free)[:2]
                    self.db.execute("UPDATE yard_resources SET status='FREE', visit_id=NULL WHERE id=?",
                                    (t.current_spot,))
                    self.db.execute("UPDATE yard_resources SET status='OCCUPIED', visit_id=? WHERE id=?",
                                    (t.visit_id, r["id"]))
                    self.db.execute("""UPDATE yard_visits SET status='AT_DOOR', resource_id=?, door_at=?,
                                       jockey_m = jockey_m + ?, reasons=? WHERE id=?""",
                                    (r["id"], iso(now), d, json.dumps(reasons), t.visit_id))
                    self._record_move(t.visit_id, t.current_spot, r, d, now)
                    changed = True
                    self._notify("on_move_to_door", {"id": t.visit_id, "appointment_id": t.appointment_id}, r,
                                 t.origin)
            if changed:
                self._publish()

    # ------------------------------------------------------------------ demo: simultaneous check-in
    def fcfs_plan(self, trucks: list[Truck]) -> list[dict | None]:
        """What a busy human dispatcher typically does: in arrival order, the booked door if free,
        otherwise the first free door on the list, otherwise the first free spot."""
        doors = self._resources("DOOR")
        spots = self._resources("SPOT")
        taken: set[str] = set()
        plan = []
        for t in trucks:
            r = next((d for d in doors if d["id"] == t.booked_door and d["id"] not in taken), None)
            r = r or next((d for d in doors if d["id"] not in taken), None)
            r = r or next((s for s in spots if s["id"] not in taken), None)
            if r:
                taken.add(r["id"])
            plan.append(r)
        return plan

    @staticmethod
    def evaluate(trucks: list[Truck], plan: list[dict | None]) -> dict:
        travel = violations = 0.0
        zone_counts: dict[str, int] = {}
        at_door, at_spot, issues = [], [], []
        for t, r in zip(trucks, plan):
            if r is None:
                continue
            travel += dist(GATE_XY, (r["x"], r["y"]))
            zone_counts[r["zone"]] = zone_counts.get(r["zone"], 0) + 1
            bad = door_feasible(t, r) if r["kind"] == "DOOR" else spot_feasible(t, r)
            if bad:
                violations += 1
                issues.append(f"{t.plate}: {bad} ({r['id']})")
            (at_door if r["kind"] == "DOOR" else at_spot).append((t, r))
        # urgent trucks parked while a less urgent truck got a door the urgent one could have used
        bumped = sum(1 for t, _ in at_spot if t.priority >= 3 and any(
            o.priority < t.priority and door_feasible(t, d) is None for o, d in at_door))
        return {"travel_m": round(travel), "equipment_violations": int(violations), "issues": issues,
                "urgent_waiting": bumped, "max_trucks_one_zone": max(zone_counts.values(), default=0),
                "at_doors": sum(1 for t, d in at_door if door_feasible(t, d) is None),   # suitable doors only
                "at_spots": len(at_spot)}

    def simultaneous_check_in(self, n: int, rng) -> dict:
        """Demo: n trucks clear the gate at the same moment (creates their appointments/ASNs)."""
        from ..seed import create_appointment
        now = utcnow()
        appts = []
        for _ in range(n):
            # some carriers have been waiting outside a while -> detention clocks are running
            appts.append(create_appointment(self.db, rng, minutes_from_now=-rng.randint(0, 110)))
        for a in appts:
            self.db.execute("UPDATE appointments SET status='ARRIVED', arrived_at=? WHERE id=?", (iso(now), a["id"]))
        with self._lock:
            trucks = [self._truck_from_appointment(a["id"], now) for a in appts]
            fcfs = self.evaluate(trucks, self.fcfs_plan(trucks))
            doors_free = self._resources("DOOR")
            plan = self._solve(trucks, doors_free + self._resources("SPOT"), now)
            ai = self.evaluate(trucks, plan)
            batch_id = uuid.uuid4().hex[:8]
            placed = [self._place(t, r, now, self._explain(t, r, doors_free), batch_id) for t, r in zip(trucks, plan)]
            self.last_batch = {"batch_id": batch_id, "at": iso(now), "trucks": n, "ai": ai, "fcfs": fcfs,
                               "assignments": placed}
            self._publish()
            return self.last_batch

    # ------------------------------------------------------------------ views
    def snapshot(self) -> dict:
        now = utcnow()
        res = self.db.query("""SELECT r.*, v.plate, v.load_class, v.trailer_type, v.priority, v.carrier_name
                               FROM yard_resources r LEFT JOIN yard_visits v ON v.id = r.visit_id ORDER BY r.id""")
        visits = self.db.query("""SELECT * FROM yard_visits WHERE status != 'DEPARTED'
                                  ORDER BY priority DESC, checked_in_at""")
        recent = self.db.query("""SELECT v.*, NULL AS x FROM yard_visits v WHERE v.status = 'DEPARTED'
                                  AND v.departed_at >= ? ORDER BY v.departed_at DESC LIMIT 10""",
                               (iso(now - timedelta(minutes=30)),))
        moves = self.db.query("SELECT * FROM yard_moves WHERE created_at >= ? ORDER BY id DESC LIMIT 40",
                              (iso(now - timedelta(minutes=5)),))
        doors = [r for r in res if r["kind"] == "DOOR"]
        stats = self.db.one("""SELECT COUNT(*) AS n, AVG(jockey_m) AS jockey FROM yard_visits
                               WHERE checked_in_at >= ?""", (iso(now - timedelta(hours=24)),))
        return {
            "resources": res, "visits": visits, "recent_departures": recent, "moves": moves,
            "gate": {"x": GATE_XY[0], "y": GATE_XY[1]},
            "zone_load": self._zone_load(now), "last_batch": self.last_batch,
            "kpis": {
                "doors_busy": sum(1 for d in doors if d["status"] == "OCCUPIED"), "doors_total": len(doors),
                "cold_docks_busy": sum(1 for d in doors if d["status"] == "OCCUPIED" and d["capabilities"] == "REEFER"),
                "waiting_in_yard": sum(1 for v in visits if v["status"] == "AT_SPOT"),
                "perishables_waiting": sum(1 for v in visits if v["status"] == "AT_SPOT"
                                           and v["load_class"] == "PERISHABLE"),
                "checked_in_24h": stats["n"], "time_scale": self.settings.yard_time_scale,
            },
        }

    def _notify(self, hook: str, *args) -> None:
        if self.notifier is None:
            return
        try:
            getattr(self.notifier, hook)(*args)
        except Exception:
            log.exception("driver notification %s failed", hook)

    def _publish(self) -> None:
        if self.bus:
            self.bus.publish({"type": "yard"})
