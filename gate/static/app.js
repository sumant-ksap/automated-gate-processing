"use strict";
const DECISIONS = ["GRANTED", "INSPECTION", "HOLD", "MANUAL_REVIEW", "DENIED"];
const LABEL = { GRANTED: "GRANTED", INSPECTION: "INSPECTION", HOLD: "SECURITY HOLD", MANUAL_REVIEW: "MANUAL REVIEW", DENIED: "DENIED" };
const state = { lanes: {}, events: [], scenarios: {}, uploadLane: null };
const $ = (s) => document.querySelector(s);

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
const fmtDT = (iso) => iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
const dec = (ev) => ev.override_decision || ev.decision;
const badge = (d, big) => `<span class="dec ${esc(d)} ${big ? "big" : ""}">${esc(LABEL[d] || d)}</span>`;
const img = (name) => `/api/images/${encodeURIComponent(name)}`;
const conf = (c) => c == null ? "" : `<span class="conf">${(c * 100).toFixed(0)}%</span>`;

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.json();
}

// ------------------------------------------------------------------ lanes
function renderLane(lane) {
  state.lanes[lane.id] = { ...(state.lanes[lane.id] || {}), ...lane };
  const l = state.lanes[lane.id];
  let el = document.getElementById(`lane-${lane.id}`);
  if (!el) {
    el = document.createElement("div");
    el.className = "lane";
    el.id = `lane-${lane.id}`;
    $("#lanes").appendChild(el);
  }
  const ev = l.event;
  const imgs = ev ? ev.images : [];
  const scen = Object.keys(state.scenarios).map((s) => `<option value="${esc(s)}">${esc(s.replace(/_/g, " "))}</option>`).join("");
  el.innerHTML = `
    <div class="lane-head">
      <div><b>${esc(l.name)}</b> <span class="lane-status ${esc(l.status)}">${esc(l.status.toLowerCase())}${l.queue_depth > 1 ? ` (${l.queue_depth - 1} queued)` : ""}</span></div>
      <span class="barrier ${esc(l.barrier)}">${l.barrier === "OPEN" ? "▲ BARRIER OPEN" : "■ BARRIER CLOSED"}</span>
    </div>
    <div class="lane-img" ${ev ? `data-ev="${ev.id}"` : ""}>
      ${imgs.length ? `<img src="${img(imgs[0].annotated)}" alt="lane camera">
        <div class="thumbs">${imgs.slice(1).map((i) => `<img src="${img(i.annotated)}" alt="">`).join("")}</div>`
        : `<div class="empty">${l.source === "simulator" ? "Waiting for a truck… start the simulator" : "Waiting for a truck…"}</div>`}
    </div>
    <div class="lane-body">
      ${ev ? `<div>${badge(dec(ev))} <span class="reason">${esc(ev.destination || "")}</span></div>
      <div class="ids">
        <div><span>Plate</span> <b>${esc(ev.plate || "—")}</b></div>
        <div><span>USDOT</span> <b>${esc(ev.dot_number || "—")}</b></div>
        <div><span>Trailer</span> <b>${esc(ev.trailer_number || "—")}</b></div>
        <div><span>Seal</span> <b>${esc(ev.seal_number || "—")}</b></div>
      </div>
      <div class="reason">${esc(ev.reasons.map((r) => r.message).join(" · "))}</div>`
        : `<div class="reason">Source: ${esc(l.source)} · processed ${l.processed}</div>`}
    </div>
    <div class="lane-actions">
      <select class="scen"><option value="">random truck</option>${scen}</select>
      <button class="primary sim1">Simulate truck</button>
      <button class="up" title="Process your own camera images (side / rear)">Upload images</button>
    </div>`;
  el.querySelector(".sim1").onclick = async (e) => {
    const s = el.querySelector(".scen").value;
    e.target.disabled = true;
    try { await api(`/api/lanes/${lane.id}/simulate${s ? `?scenario=${s}` : ""}`, { method: "POST" }); }
    catch (err) { alert(err.message); }
    finally { e.target.disabled = false; }
  };
  el.querySelector(".up").onclick = () => { state.uploadLane = lane.id; $("#uploader").click(); };
  const li = el.querySelector(".lane-img[data-ev]");
  if (li) li.onclick = () => openDetail(+li.dataset.ev);
}

$("#uploader").onchange = async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  try {
    const r = await fetch(`/api/lanes/${state.uploadLane}/process`, { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json()).detail);
    const ev = await r.json();
    openDetail(ev.id);
  } catch (err) { alert("Upload failed: " + err.message); }
};

// ----------------------------------------------------------------- events
function eventRow(ev, isNew) {
  return `<tr data-ev="${ev.id}" class="${isNew ? "new" : ""}">
    <td>${fmtTime(ev.created_at)}</td><td>${ev.lane_id}</td>
    <td>${badge(dec(ev))}${ev.override_decision ? ` <span class="ovr">override</span>` : ""}</td>
    <td class="mono">${esc(ev.plate || "—")}</td><td class="mono">${esc(ev.trailer_number || "—")}</td>
    <td class="mono">${esc(ev.dot_number || "—")}</td><td class="mono">${esc(ev.seal_number || "—")}</td>
    <td>${esc(ev.carrier_name || "—")}</td><td>${esc(ev.destination || "")}</td><td>${ev.processing_ms ?? ""}</td></tr>`;
}

function renderEvents(newId) {
  const fd = $("#fDecision").value, fl = $("#fLane").value;
  const rows = state.events.filter((e) => (!fd || dec(e) === fd) && (!fl || e.lane_id == fl)).slice(0, 150);
  $("#eventRows").innerHTML = rows.map((e) => eventRow(e, e.id === newId)).join("") ||
    `<tr><td colspan="10" class="reason">No events yet.</td></tr>`;
  renderExceptions();
}

$("#eventRows").onclick = (e) => { const tr = e.target.closest("tr[data-ev]"); if (tr) openDetail(+tr.dataset.ev); };
$("#fDecision").onchange = () => renderEvents();
$("#fLane").onchange = () => renderEvents();

function upsertEvent(ev) {
  const i = state.events.findIndex((e) => e.id === ev.id);
  if (i >= 0) state.events[i] = ev; else state.events.unshift(ev);
  state.events.sort((a, b) => b.id - a.id);
  const lane = state.lanes[ev.lane_id];
  if (lane && (!lane.event || lane.event.id <= ev.id)) renderLane({ ...lane, event: ev });
}

// ------------------------------------------------------------- exceptions
function openExceptions() {
  return state.events.filter((e) => ["HOLD", "MANUAL_REVIEW"].includes(e.decision) && !e.override_decision);
}
function renderExceptions() {
  const list = openExceptions();
  $("#excBadge").textContent = list.length;
  $("#kExc").textContent = list.length;
  $("#excList").innerHTML = list.map((ev) => `
    <div class="exc ${esc(ev.decision)}">
      ${ev.images.length ? `<img src="${img(ev.images[ev.decision === "HOLD" && ev.images[1] ? 1 : 0].annotated)}" data-ev="${ev.id}" alt="">` : "<div></div>"}
      <div>
        <div>${badge(ev.decision)} <b>Lane ${ev.lane_id}</b> · ${fmtTime(ev.created_at)} · ${esc(ev.carrier_name || "unknown carrier")}</div>
        <ul class="reasons">${ev.reasons.filter((r) => r.decision !== "GRANTED").map((r) => `<li>${esc(r.message)}</li>`).join("")}</ul>
        <div class="reason mono">plate ${esc(ev.plate || "—")} · trailer ${esc(ev.trailer_number || "—")} · USDOT ${esc(ev.dot_number || "—")} · seal ${esc(ev.seal_number || "—")}</div>
      </div>
      <div class="acts">
        <button class="primary" data-ov="GRANTED" data-id="${ev.id}">Approve entry</button>
        <button data-ov="INSPECTION" data-id="${ev.id}">Send to inspection</button>
        <button data-ov="DENIED" data-id="${ev.id}">Deny / turn away</button>
        <button class="ghost" data-open="${ev.id}">Details</button>
      </div>
    </div>`).join("") || `<div class="empty-note">No open exceptions 🎉</div>`;
}
$("#excList").onclick = async (e) => {
  const b = e.target.closest("[data-ov],[data-open],img[data-ev]");
  if (!b) return;
  if (b.dataset.open || b.dataset.ev) return openDetail(+(b.dataset.open || b.dataset.ev));
  const by = prompt("Guard name / badge for the audit trail:", localStorage.getItem("guard") || "");
  if (!by) return;
  try { localStorage.setItem("guard", by); } catch {}
  await override(+b.dataset.id, b.dataset.ov, by, "");
};

async function override(id, decision, by, note) {
  try {
    const ev = await api(`/api/events/${id}/override`, { method: "POST", body: JSON.stringify({ decision, by, note }) });
    upsertEvent(ev); renderEvents(); refreshStats();
    return ev;
  } catch (err) { alert(err.message); }
}

// ---------------------------------------------------------------- detail
async function openDetail(id) {
  const ev = await api(`/api/events/${id}`);
  const d = ev.detail || {};
  const rd = d.readings || {};
  const row = (label, key, val) => `<tr><td>${label}</td><td class="mono">${esc(val || "—")} ${rd[key] ? conf(rd[key].confidence) : ""}
      ${rd[key] && rd[key].corrected ? `<span class="conf">(corrected from "${esc(rd[key].raw)}")</span>` : ""}</td></tr>`;
  $("#dTitle").innerHTML = `Event #${ev.id} · Lane ${ev.lane_id} · ${fmtDT(ev.created_at)}`;
  $("#dBody").innerHTML = `
    <div>${badge(dec(ev), true)} <b>${esc(ev.destination || "")}</b> ${ev.gate_opened ? "· barrier opened" : "· barrier kept closed"}
      ${ev.override_decision ? `<div class="ovr">Overridden from ${esc(ev.decision)} by ${esc(ev.override_by)} at ${fmtDT(ev.override_at)} ${esc(ev.override_note || "")}</div>` : ""}</div>
    <div class="imgs">${ev.images.map((i) => `<figure><a href="${img(i.raw)}" target="_blank"><img src="${img(i.annotated)}" alt=""></a>
      <figcaption>${esc(i.camera)} camera · click for raw frame</figcaption></figure>`).join("")}</div>
    <div class="grid2">
      <div><h4>Identification (OCR)</h4><table class="kv">
        ${row("License plate", "plate", ev.plate)}${row("Trailer number", "trailer", ev.trailer_number)}
        ${row("USDOT number", "dot", ev.dot_number)}${row("Seal number", "seal", ev.seal_number)}
        <tr><td>Carrier</td><td>${esc(ev.carrier_name || "—")}</td></tr>
        <tr><td>Processing</td><td>${ev.processing_ms} ms · ${esc(d.detector || "")}</td></tr>
      </table></div>
      <div><h4>Database match</h4><table class="kv">
        <tr><td>Appointment</td><td>${ev.appointment ? `${esc(ev.appointment.appt_ref)} · ${fmtDT(ev.appointment.scheduled_at)} · dock ${esc(ev.appointment.dock_door)}` : "—"}</td></tr>
        <tr><td>Booked plate / trailer</td><td class="mono">${ev.appointment ? `${esc(ev.appointment.tractor_plate)} / ${esc(ev.appointment.trailer_number)}` : "—"}</td></tr>
        <tr><td>ASN</td><td>${ev.asn ? `${esc(ev.asn.asn_number)} · ${esc(ev.asn.po_number || "")} · ${ev.asn.pallet_count} pallets` : "—"}</td></tr>
        <tr><td>Expected seal</td><td class="mono">${esc(ev.asn ? ev.asn.seal_number : "—")}</td></tr>
        ${d.sim_scenario ? `<tr><td>Simulator truth</td><td>${esc(d.sim_scenario)} → expected ${badge(d.sim_expected)}</td></tr>` : ""}
      </table></div>
    </div>
    <div class="grid2">
      <div><h4>Decision reasons</h4><ul class="reasons">${ev.reasons.map((r) => `<li><code>${esc(r.code)}</code> ${esc(r.message)}</li>`).join("")}</ul></div>
      <div><h4>Damage findings</h4>${ev.damage.length ? `<table class="kv">${ev.damage.map((x) =>
        `<tr><td>${esc(x.kind)} · ${esc(x.severity)}</td><td>${(x.confidence * 100).toFixed(0)}% · ${esc(ev.images[x.frame] ? ev.images[x.frame].camera : "")} camera · ${x.area_px} px²</td></tr>`).join("")}</table>`
        : `<p class="reason">No damage detected.</p>`}</div>
    </div>
    <div><h4>Guard override</h4>
      <form class="ovform" id="ovForm">
        <select name="decision">${DECISIONS.map((x) => `<option ${x === dec(ev) ? "selected" : ""}>${x}</option>`).join("")}</select>
        <input name="by" placeholder="Guard name" required value="${esc(localStorage.getItem("guard") || "")}">
        <input name="note" placeholder="Note (e.g. paperwork checked)" style="flex:1">
        <button class="primary">Apply</button>
      </form></div>
    <details><summary class="reason">All OCR text (${(d.ocr_text || []).length})</summary>
      <p class="mono reason">${(d.ocr_text || []).map((t) => `${esc(t.text)} (${t.conf})`).join(" · ")}</p></details>`;
  $("#ovForm").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try { localStorage.setItem("guard", f.get("by")); } catch {}
    if (await override(ev.id, f.get("decision"), f.get("by"), f.get("note"))) openDetail(ev.id);
  };
  if (!$("#detail").open) $("#detail").showModal();
}

// ----------------------------------------------------------- appointments
async function loadAppointments() {
  const st = $("#fStatus").value;
  const rows = await api(`/api/appointments?limit=300${st ? `&status=${st}` : ""}`);
  $("#apptRows").innerHTML = rows.map((a) => `<tr>
    <td class="mono">${esc(a.appt_ref)}</td><td>${fmtDT(a.scheduled_at)}</td><td>${esc(a.carrier_name)}</td>
    <td class="mono">${esc(a.dot_number)}</td><td class="mono">${esc(a.tractor_plate)}</td><td class="mono">${esc(a.trailer_number)}</td>
    <td class="mono">${esc(a.asn_number || "")}</td><td class="mono">${esc(a.seal_number || "")}</td><td>${esc(a.dock_door)}</td>
    <td>${esc(a.status)}${a.arrived_at ? ` · ${fmtTime(a.arrived_at)}` : ""}</td></tr>`).join("") ||
    `<tr><td colspan="10" class="reason">No appointments.</td></tr>`;
}
$("#fStatus").onchange = loadAppointments;

$("#apptForm").onsubmit = async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  if (!f.seal_number) delete f.seal_number;
  if (f.scheduled_at) f.scheduled_at = new Date(f.scheduled_at).toISOString(); else delete f.scheduled_at;
  try {
    const a = await api("/api/appointments", { method: "POST", body: JSON.stringify(f) });
    $("#apptMsg").className = "msg ok"; $("#apptMsg").textContent = `Created ${a.appt_ref}`;
    e.target.reset(); loadAppointments();
  } catch (err) { $("#apptMsg").className = "msg err"; $("#apptMsg").textContent = err.message; }
};

// ------------------------------------------------------------------ stats
async function refreshStats() {
  const s = await api("/api/stats");
  $("#kTotal").textContent = s.total;
  $("#kAuto").textContent = `${Math.round(s.auto_rate * 100)}%`;
  $("#kMs").textContent = `${s.avg_processing_ms} ms`;
  $("#kPending").textContent = s.pending_appointments;
  $("#kSim").textContent = s.sim_total ? `${Math.round((100 * s.sim_correct) / s.sim_total)}% (${s.sim_correct}/${s.sim_total})` : "–";
}

// --------------------------------------------------------------- simulator
$("#simToggle").onchange = (e) => api("/api/simulator", { method: "POST", body: JSON.stringify({ running: e.target.checked, interval_s: +$("#simInterval").value }) });
$("#simInterval").onchange = (e) => api("/api/simulator", { method: "POST", body: JSON.stringify({ running: $("#simToggle").checked, interval_s: +e.target.value }) });
$("#resetBtn").onclick = async () => {
  if (!confirm("Delete all gate events and reseed demo appointments?")) return;
  await api("/api/admin/reset", { method: "POST" });
  location.reload();
};

// ------------------------------------------------------------------- tabs
document.querySelectorAll(".tab").forEach((t) => t.onclick = () => {
  document.querySelectorAll(".tab,.tabpane").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  $(`#tab-${t.dataset.tab}`).classList.add("active");
  if (t.dataset.tab === "appointments") loadAppointments();
});

// ---------------------------------------------------------------- websocket
let statsTimer = null;
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { $("#conn").className = "pill on"; $("#conn").textContent = "live"; };
  ws.onclose = () => { $("#conn").className = "pill off"; $("#conn").textContent = "offline"; setTimeout(connect, 2000); };
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "gate_event" || msg.type === "gate_event_updated") {
      upsertEvent(msg.event); renderEvents(msg.type === "gate_event" ? msg.event.id : null);
      clearTimeout(statsTimer); statsTimer = setTimeout(refreshStats, 300);
    } else if (msg.type === "lane") {
      renderLane(msg.lane);
    } else if (msg.type === "barrier") {
      const l = state.lanes[msg.lane_id]; if (l) renderLane({ ...l, barrier: msg.state });
    } else if (msg.type === "simulator") {
      $("#simToggle").checked = msg.running;
    } else if (msg.type === "reset") {
      location.reload();
    }
  };
}

async function init() {
  DECISIONS.forEach((d) => $("#fDecision").insertAdjacentHTML("beforeend", `<option value="${d}">${LABEL[d]}</option>`));
  const sim = await api("/api/simulator");
  state.scenarios = sim.scenarios;
  $("#simToggle").checked = sim.running;
  $("#simInterval").value = String(sim.interval_s);
  const [lanes, events, carriers] = await Promise.all([api("/api/lanes"), api("/api/events?limit=200"), api("/api/carriers")]);
  state.events = events;
  lanes.forEach((l) => {
    $("#fLane").insertAdjacentHTML("beforeend", `<option value="${l.id}">${esc(l.name)}</option>`);
    renderLane({ ...l, event: events.find((e) => e.lane_id === l.id) });
  });
  $("#carrierSel").innerHTML = carriers.map((c) => `<option value="${esc(c.dot_number)}">${esc(c.name)} (${esc(c.dot_number)})${c.status !== "ACTIVE" ? " – " + esc(c.status) : ""}</option>`).join("");
  renderEvents();
  refreshStats();
  connect();
}
init();
