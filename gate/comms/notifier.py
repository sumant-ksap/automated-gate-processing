"""Automated driver communication.

Every gate / yard decision that concerns a driver becomes a message on the
driver's phone, in the driver's preferred language:

  * SMS (simulated by default; Twilio when GATE_SMS_PROVIDER=twilio and
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM are set)
  * a live driver page (/driver.html?t=<token>) linked from the SMS: current
    instruction, turn-by-turn steps, yard map with the route, language picker,
    confirm button - no app install needed

Drivers can reply OK (confirm), a language code (EN / ES / HI / PA / RU) or HELP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..db import Database, iso, parse_iso, utcnow
from .directions import INSPECTION_BAY, inspection_route, route
from .i18n import ACK_WORDS, LANGUAGES, T, t

log = logging.getLogger(__name__)
NEEDS_ACK = {"door", "spot", "move", "inspection", "hold", "review"}


# ------------------------------------------------------------------ SMS providers
class SimulatedSms:
    name = "sim"

    def send(self, phone: str, body: str) -> tuple[str, str | None, str | None]:
        return "DELIVERED", f"SIM-{uuid.uuid4().hex[:10]}", None


class TwilioSms:
    """Twilio Programmable Messaging REST API (https://www.twilio.com/docs/messaging/api)."""
    name = "twilio"

    def __init__(self, sid: str, token: str, sender: str):
        self.sid, self.token, self.sender = sid, token, sender

    def send(self, phone: str, body: str) -> tuple[str, str | None, str | None]:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        data = urllib.parse.urlencode({"To": phone, "From": self.sender, "Body": body}).encode()
        req = urllib.request.Request(url, data=data)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{self.sid}:{self.token}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                out = json.load(r)
            return "SENT", out.get("sid"), None
        except Exception as e:
            return "FAILED", None, str(e)[:300]


def verify_twilio_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    """X-Twilio-Signature check for inbound webhooks."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


# ------------------------------------------------------------------ composer
def render_step(lang: str, step: tuple) -> str:
    kind = step[0]
    if kind in ("north", "continue"):
        return t(lang, kind, m=step[1])
    if kind in ("left", "right", "power"):
        return t(lang, kind)
    if kind == "door_final":
        return t(lang, "door_final", door=step[1])
    if kind in ("spot_left", "spot_right"):
        return t(lang, kind, spot=step[1])
    return ""


def headline(kind: str, lang: str, ctx: dict) -> str:
    """The one-line main instruction (shown big on the driver page)."""
    return compose(kind, lang, ctx, [], None).splitlines()[1 if kind in ("door", "spot") else 0]


def compose(kind: str, lang: str, ctx: dict, steps: list, link: str | None) -> str:
    lines = []
    if kind in ("door", "spot"):
        lines.append(t(lang, "greet", carrier=ctx.get("carrier", ""), plate=ctx.get("plate", "")))
    if kind == "door":
        lines.append(t(lang, "door", door=ctx["door"], dtype=t(lang, "dt_" + (ctx.get("dtype") or ""))))
    elif kind == "spot":
        lines.append(t(lang, "spot", spot=ctx["spot"]))
    elif kind == "move":
        lines.append(t(lang, "move", door=ctx["door"]))
    elif kind in ("inspection", "hold", "review"):
        lines.append(t(lang, kind))
    elif kind == "denied":
        lines.append(t(lang, "denied", reason=t(lang, "r_" + ctx["code"])))
    elif kind == "done":
        lines.append(t(lang, "done", door=ctx["door"]))
    elif kind == "pre":
        lines.append(t(lang, "pre", appt=ctx["appt"], eta=ctx["eta"]))
    elif kind == "pre_blocked":
        lines.append(t(lang, "pre_blocked", appt=ctx["appt"], issue=t(lang, "r_" + ctx["code"])))
    lines += [f"{i}. {render_step(lang, tuple(s))}" for i, s in enumerate(steps, 1)]
    if kind in NEEDS_ACK:
        lines.append(t(lang, "ack"))
    if link:
        lines.append(t(lang, "link", link=link))
    return "\n".join(lines)


def _reason_code(code: str) -> str:
    return code if f"r_{code}" in T["en"] else "NO_APPOINTMENT"


def _local_hhmm(iso_ts: str | None) -> str:
    return parse_iso(iso_ts).astimezone().strftime("%H:%M") if iso_ts else "-"


class Notifier:
    def __init__(self, db: Database, settings, bus=None):
        self.db, self.settings, self.bus = db, settings, bus
        sid, token, sender = (os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))
        if settings.sms_provider == "twilio" and sid and token and sender:
            self.provider = TwilioSms(sid, token, sender)
            self.twilio_token = token
        else:
            if settings.sms_provider == "twilio":
                log.warning("GATE_SMS_PROVIDER=twilio but TWILIO_* variables missing - using simulated SMS")
            self.provider = SimulatedSms()
            self.twilio_token = None
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sms")

    # -------------------------------------------------------------- helpers
    def _appt(self, appointment_id: int) -> dict | None:
        a = self.db.one("""SELECT a.*, c.name AS carrier_name FROM appointments a
                           JOIN carriers c ON c.id = a.carrier_id WHERE a.id = ?""", (appointment_id,))
        if a and not a.get("driver_token"):
            a["driver_token"] = secrets.token_urlsafe(9)
            self.db.execute("UPDATE appointments SET driver_token=? WHERE id=?", (a["driver_token"], appointment_id))
        return a

    def link(self, token: str) -> str:
        return f"{self.settings.public_url.rstrip('/')}/driver.html?t={token}"

    def _deliver(self, msg_id: int, phone: str, body: str) -> None:
        status, pid, err = self.provider.send(phone, body)
        self.db.execute("UPDATE driver_messages SET status=?, provider_id=?, error=?, sent_at=? WHERE id=?",
                        (status, pid, err, iso(utcnow()), msg_id))
        if self.bus:
            self.bus.publish({"type": "message", "id": msg_id, "status": status})

    def send(self, appointment_id: int, kind: str, ctx: dict, steps: list | None = None,
             route_pts: list | None = None, dest: dict | None = None, event_id: int | None = None,
             visit_id: int | None = None, lang: str | None = None, prefix: str | None = None) -> dict | None:
        a = self._appt(appointment_id)
        if not a or not a.get("driver_phone"):
            return None
        lang = lang or a.get("driver_language") or "en"
        ctx = {"carrier": a["carrier_name"], "plate": a["tractor_plate"], **ctx}
        steps = steps or []
        body = compose(kind, lang, ctx, steps, self.link(a["driver_token"]))
        if prefix:
            body = prefix + "\n" + body
        payload = {"ctx": ctx, "steps": [list(s) for s in steps], "route": route_pts, "dest": dest,
                   "headline": headline(kind, lang, ctx)}
        mid = self.db.execute("""INSERT INTO driver_messages (created_at, appointment_id, visit_id, event_id, phone,
                                 language, channel, kind, body, payload, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                              (iso(utcnow()), appointment_id, visit_id, event_id, a["driver_phone"], lang,
                               self.provider.name, kind, body, json.dumps(payload), "QUEUED"))
        if isinstance(self.provider, SimulatedSms):
            self._deliver(mid, a["driver_phone"], body)
        else:
            self.pool.submit(self._deliver, mid, a["driver_phone"], body)
        if self.bus:
            self.bus.publish({"type": "message", "id": mid})
        return self.db.one("SELECT * FROM driver_messages WHERE id=?", (mid,))

    # -------------------------------------------------------------- hooks
    def on_gate_event(self, event: dict, appointment: dict | None, yard: dict | None) -> dict | None:
        if not appointment:
            return None
        decision = event.get("override_decision") or event["decision"]
        aid, eid = appointment["id"], event.get("id")
        if decision == "GRANTED" and yard and yard.get("resource_id"):
            res = self.db.one("SELECT * FROM yard_resources WHERE id=?", (yard["resource_id"],))
            steps, pts = route(res)
            dest = {"id": res["id"], "kind": res["kind"], "x": res["x"], "y": res["y"]}
            if res["kind"] == "DOOR":
                return self.send(aid, "door", {"door": res["id"], "dtype": res["capabilities"] or ""}, steps, pts,
                                 dest, eid, yard.get("visit_id"))
            return self.send(aid, "spot", {"spot": res["id"]}, steps, pts, dest, eid, yard.get("visit_id"))
        if decision == "INSPECTION":
            return self.send(aid, "inspection", {}, [], inspection_route(),
                             {"id": "INSPECTION", "kind": "BAY", "x": INSPECTION_BAY[0], "y": INSPECTION_BAY[1]}, eid)
        if decision == "HOLD":
            return self.send(aid, "hold", {}, event_id=eid)
        if decision == "MANUAL_REVIEW":
            return self.send(aid, "review", {}, event_id=eid)
        if decision == "DENIED":
            code = next((r["code"] for r in event.get("reasons", []) if r["decision"] == "DENIED"), "NO_APPOINTMENT")
            return self.send(aid, "denied", {"code": _reason_code(code)}, event_id=eid)
        return None

    def on_move_to_door(self, visit: dict, door: dict, start: tuple[float, float]) -> dict | None:
        steps, pts = route(door, start=start, from_spot=True)
        return self.send(visit["appointment_id"], "move", {"door": door["id"]}, steps, pts,
                         {"id": door["id"], "kind": "DOOR", "x": door["x"], "y": door["y"]}, visit_id=visit["id"])

    def on_departed(self, visit: dict) -> dict | None:
        return self.send(visit["appointment_id"], "done", {"door": visit["resource_id"]}, visit_id=visit["id"])

    def on_precheck(self, packet: dict) -> dict | None:
        if not packet.get("appointment_id"):
            return None
        p = packet["packet"]
        if packet["status"] == "BLOCKED":
            failed = next((c for c in packet["checks"] if c["severity"] == "block"), None)
            code = {"Insurance": "INSURANCE_EXPIRED", "Operating authority": "CARRIER_SUSPENDED"}.get(
                failed["check"] if failed else "", "NO_APPOINTMENT")
            return self.send(packet["appointment_id"], "pre_blocked", {"appt": p["appointment_ref"], "code": code})
        return self.send(packet["appointment_id"], "pre", {"appt": p["appointment_ref"], "eta": _local_hhmm(p["eta"])})

    # -------------------------------------------------------------- driver actions
    def by_token(self, token: str) -> dict | None:
        return self.db.one("""SELECT a.*, c.name AS carrier_name FROM appointments a
                              JOIN carriers c ON c.id = a.carrier_id WHERE a.driver_token = ?""", (token,))

    def ack(self, appointment_id: int, via: str = "app") -> dict | None:
        m = self.db.one("""SELECT * FROM driver_messages WHERE appointment_id=? AND kind IN
                           ('door','spot','move','inspection','hold','review') ORDER BY id DESC LIMIT 1""",
                        (appointment_id,))
        if m and not m["acked_at"]:
            self.db.execute("UPDATE driver_messages SET acked_at=?, reply=COALESCE(reply, ?) WHERE id=?",
                            (iso(utcnow()), via, m["id"]))
            if self.bus:
                self.bus.publish({"type": "message", "id": m["id"]})
        return m

    def set_language(self, appointment_id: int, lang: str) -> dict | None:
        """Switch the driver's language and re-send the latest instruction in it."""
        if lang not in LANGUAGES:
            raise ValueError(lang)
        self.db.execute("UPDATE appointments SET driver_language=? WHERE id=?", (lang, appointment_id))
        last = self.db.one("SELECT * FROM driver_messages WHERE appointment_id=? ORDER BY id DESC LIMIT 1",
                           (appointment_id,))
        if not last:
            return None
        p = last["payload"]
        return self.send(appointment_id, last["kind"], p["ctx"], [tuple(s) for s in p["steps"]], p.get("route"),
                         p.get("dest"), last["event_id"], last["visit_id"], lang, prefix=t(lang, "lang_set"))

    def handle_reply(self, phone: str, body: str) -> str:
        """Inbound SMS. Returns a short confirmation (sent back as the SMS reply)."""
        m = self.db.one("SELECT * FROM driver_messages WHERE phone=? ORDER BY id DESC LIMIT 1", (phone,))
        if not m:
            return ""
        word = (body or "").strip().upper()
        self.db.execute("UPDATE driver_messages SET reply=? WHERE id=?", (body[:160], m["id"]))
        if word in ACK_WORDS:
            self.ack(m["appointment_id"], via="sms")
            return "✓"
        if word.lower() in LANGUAGES:
            self.set_language(m["appointment_id"], word.lower())
            return ""
        if word == "HELP":
            if self.bus:
                self.bus.publish({"type": "driver_help", "appointment_id": m["appointment_id"], "phone": phone})
            return "A yard coordinator will call you."
        return ""

    def stats(self, since: datetime) -> dict:
        rows = self.db.query("SELECT kind, language, status, acked_at FROM driver_messages WHERE created_at >= ?",
                             (iso(since),))
        need = [r for r in rows if r["kind"] in NEEDS_ACK]
        langs: dict[str, int] = {}
        for r in rows:
            langs[r["language"]] = langs.get(r["language"], 0) + 1
        return {"sent": len(rows), "delivered": sum(1 for r in rows if r["status"] in ("DELIVERED", "SENT")),
                "failed": sum(1 for r in rows if r["status"] == "FAILED"),
                "ack_rate": round(sum(1 for r in need if r["acked_at"]) / len(need), 3) if need else 0.0,
                "languages": langs, "provider": self.provider.name}
