"use strict";
// Predictive arrival & pre-check-in tab. Relies on helpers from app.js ($, esc, api, fmtTime, fmtDT).
const inbound = { data: null, model: null };
const R_KM = 45;                 // radar radius
const SVGNS = "http://www.w3.org/2000/svg";

function project(lat, lon, f) {
  const x = (lon - f.lon) * Math.cos((f.lat * Math.PI) / 180) * 111.32;
  const y = (lat - f.lat) * 110.57;
  const s = 240 / R_KM;
  return [260 + x * s, 260 - y * s];
}

function pkStatus(t) { return t.packet_status || "NONE"; }
const pkColor = { CLEARED: "var(--GRANTED)", NOTES: "var(--MANUAL_REVIEW)", BLOCKED: "var(--HOLD)", NONE: "var(--accent)" };
const pkLabel = { CLEARED: "PRE-CLEARED", NOTES: "CLEARED · NOTES", BLOCKED: "BLOCKED", NONE: "pending" };

function renderRadar(d) {
  const svg = $("#radar");
  const f = d.facility;
  const kmPx = 240 / R_KM;
  let h = "";
  [10, 20, 30, 40].forEach((km) => {
    h += `<circle class="ring" cx="260" cy="260" r="${km * kmPx}"/>
          <text class="ring-label" x="${264 + km * kmPx * 0.707}" y="${256 - km * kmPx * 0.707}">${km} km</text>`;
  });
  for (const c of d.conditions.weather_cells) {
    const [x, y] = project(c.lat, c.lon, f);
    const col = c.kind === "snow" ? "#e6edf3" : c.kind === "storm" ? "#a371f7" : "#4ea1ff";
    h += `<circle cx="${x}" cy="${y}" r="${c.radius_km * kmPx}" fill="${col}" fill-opacity="0.16" stroke="${col}" stroke-opacity="0.4">
            <title>${esc(c.kind)} ${c.intensity.toFixed(1)} mm/h, wind ${Math.round(c.wind_kph)} km/h</title></circle>`;
  }
  for (const i of d.conditions.incidents) {
    const [x, y] = project(i.lat, i.lon, f);
    h += `<circle cx="${x}" cy="${y}" r="${i.radius_km * kmPx}" fill="var(--HOLD)" fill-opacity="0.07" stroke="var(--HOLD)" stroke-dasharray="3 3" stroke-opacity="0.6"/>
          <path d="M${x} ${y - 7} L${x + 6} ${y + 5} L${x - 6} ${y + 5} Z" fill="var(--HOLD)"><title>${esc(i.kind)} – severity ${Math.round(i.severity * 100)}%</title></path>`;
  }
  const surgeIds = new Set(d.clusters.flatMap((c) => c.trip_ids));
  h += `<rect x="252" y="252" width="16" height="16" rx="3" fill="var(--text)"/>
        <text class="ring-label" x="272" y="276">${esc(f.name)}</text>`;
  for (const t of d.trips) {
    if (t.status !== "EN_ROUTE" || t.lat == null) continue;
    const [x, y] = project(t.lat, t.lon, f);
    const st = pkStatus(t);
    const stopped = (t.speed_kph || 0) < 2;
    if (surgeIds.has(t.id)) h += `<circle cx="${x}" cy="${y}" r="11" fill="none" stroke="var(--INSPECTION)" stroke-width="2"/>`;
    h += `<circle class="truck" data-trip="${t.id}" cx="${x}" cy="${y}" r="6.5" fill="${stopped ? "var(--panel)" : pkColor[st]}"
            stroke="${pkColor[st]}" stroke-width="${stopped ? 2.5 : 1.5}">
            <title>${esc(t.plate)} · ${esc(t.carrier_name || "")} · ETA ${fmtTime(t.eta_at)}${stopped ? " · STOPPED" : ""}</title></circle>
          <text class="truck-label" x="${x + 9}" y="${y + 3}">${esc(t.plate || "")}</text>`;
  }
  svg.innerHTML = h;
}

function deltaCell(m) {
  if (m == null) return "—";
  const cls = Math.abs(m) <= 30 ? "ontime" : m > 0 ? "late" : "early";
  return `<span class="${cls}">${m > 0 ? "+" : ""}${m} min</span>`;
}

function condCell(c) {
  if (!c) return "";
  const parts = [`traffic ${Math.round(c.traffic_route * 100)}%`];
  if (c.rain_mm > 0.2) parts.push(`🌧 ${c.rain_mm} mm/h`);
  if (c.snow_mm > 0.2) parts.push(`❄ ${c.snow_mm}`);
  if (c.wind_kph > 45) parts.push(`💨 ${c.wind_kph} km/h`);
  if (c.stopped) parts.push("⏸ stopped");
  return parts.join(" · ");
}

function renderBoard(d) {
  $("#inboundRows").innerHTML = d.trips.map((t) => {
    const arrived = t.status === "ARRIVED";
    const st = pkStatus(t);
    return `<tr data-trip="${t.id}">
      <td><b class="mono">${esc(t.plate || t.vehicle_id || "?")}</b><div class="reason">${esc(t.carrier_name || "unscheduled")} · ${esc(t.source)}</div></td>
      <td class="mono">${esc(t.appt_ref || "—")}<div class="reason">slot ${fmtTime(t.scheduled_at)} · door ${esc(t.dock_door || "—")}</div></td>
      <td>${arrived ? `<b>Arrived ${fmtTime(t.arrived_at)}</b>` :
        `<b>${fmtTime(t.eta_at)}</b> <span class="window">${fmtTime(t.eta_low_at)}–${fmtTime(t.eta_high_at)}</span>`}</td>
      <td>${deltaCell(t.slot_delta_min)}</td>
      <td class="reason">${arrived ? "" : fmtTime(t.static_eta_at)}</td>
      <td>${arrived ? "at facility" : `${(t.distance_km || 0).toFixed(1)} km · ${Math.round(t.speed_kph || 0)} km/h`}</td>
      <td class="reason">${arrived ? "" : condCell(t.conditions)}</td>
      <td><span class="pk ${st}">${pkLabel[st]}</span>${t.packet_trigger === "surge" ? ` <span class="reason">surge</span>` : ""}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="reason">No inbound trucks. Turn on the inbound fleet simulator or POST GPS pings to /api/telematics/pings.</td></tr>`;
}

function renderClusters(d) {
  $("#surgeBadge").hidden = !d.clusters.length;
  $("#clusters").innerHTML = d.clusters.map((c) => `
    <div class="surge">
      <b>⚠ ${c.count} trucks</b> predicted ${fmtTime(c.start)}–${fmtTime(c.end)}
      <div class="reason">${c.precleared}/${c.count} pre-cleared${c.blocked ? ` · ${c.blocked} blocked` : ""} · ${c.plates.map(esc).join(", ")}</div>
      <div class="cmp">
        <span class="reason">Manual check-in</span><span>${c.lanes_needed_manual} lane(s), est. dwell up to ${c.est_queue_manual_min} min</span>
        <span class="reason">With pre-check packets</span><span>${c.lanes_needed_precleared} lane(s), est. dwell ${c.est_queue_precleared_min} min</span>
      </div>
    </div>`).join("") || `<p class="reason">No surge predicted. Alerts appear when several trucks are forecast to arrive within the same window.</p>`;
}

function renderModel(m) {
  if (!m) return;
  inbound.model = m;
  const o = m.offline || {};
  const max = Math.max(o.static_mae_min || 1, o.holdout_mae_min || 1);
  const bar = (v, cls) => `<span class="mbar ${cls}" style="width:${Math.max(4, (v / max) * 140)}px"></span> ${v} min`;
  const live = m.live || {};
  $("#modelCard").innerHTML = !m.ready ? (m.training ? "Training on trip history…" : "Not trained") : `
    <div class="cmp">
      <span>AI model error</span><span>${bar(o.holdout_mae_min, "")}</span>
      <span>Static ETA error</span><span>${bar(o.static_mae_min, "static")}</span>
    </div>
    <p>Average error on held-out trips: <b>${o.holdout_mae_min} min</b> vs <b>${o.static_mae_min} min</b> for distance ÷ speed.
      The predicted window contains the actual arrival ${Math.round((o.window_coverage || 0) * 100)}% of the time.</p>
    <p>Live: ${live.trips ? `<b>${live.mae_min} min</b> avg error (static ${live.static_mae_min}) over ${live.trips} arrived trucks` : "no arrivals scored yet"}
      · trained on ${o.n_samples} samples (${o.n_real_samples} real) ${m.training ? "· <b>retraining…</b>" : ""}</p>
    <p>Strongest signals: ${(o.top_features || []).slice(0, 4).map((f) => esc(f.feature.replace(/_/g, " "))).join(", ")}</p>
    <button id="retrainBtn" class="ghost">Retrain now</button>`;
  const rb = $("#retrainBtn");
  if (rb) rb.onclick = () => api("/api/inbound/model/retrain", { method: "POST" }).then(() => { rb.textContent = "Retraining…"; });
}

function renderInbound(d) {
  inbound.data = d;
  $("#kInbound").textContent = d.trips.filter((t) => t.status === "EN_ROUTE").length;
  $("#fleetToggle").checked = d.fleet_running;
  $("#facilityName").textContent = `· ${d.facility.name}`;
  renderClusters(d);
  if (!$("#tab-inbound").classList.contains("active")) return;
  renderRadar(d);
  renderBoard(d);
  if (d.model) renderModel(d.model);
}

async function openTrip(id) {
  const t = await api(`/api/inbound/trips/${id}`);
  const p = t.packet;
  const icon = (c) => c.ok ? "✅" : c.severity === "block" ? "⛔" : "⚠️";
  $("#dTitle").textContent = `Inbound truck ${t.plate || t.vehicle_id} · trip #${t.id}`;
  const preds = t.predictions.slice(-12).reverse();
  $("#dBody").innerHTML = `
    <div class="grid2">
      <div><h4>Trip</h4><table class="kv">
        <tr><td>Status</td><td>${esc(t.status)}${t.arrived_at ? ` · arrived ${fmtTime(t.arrived_at)}` : ""}</td></tr>
        <tr><td>Appointment</td><td>${t.appointment ? `${esc(t.appointment.appt_ref)} · ${esc(t.appointment.carrier_name)} · slot ${fmtTime(t.appointment.scheduled_at)}` : "none"}</td></tr>
        <tr><td>Predicted arrival</td><td>${t.status === "EN_ROUTE" ? `<b>${fmtTime(t.eta_at)}</b> (${fmtTime(t.eta_low_at)}–${fmtTime(t.eta_high_at)})` : "—"}</td></tr>
        <tr><td>Static ETA</td><td>${t.status === "EN_ROUTE" ? fmtTime(t.static_eta_at) : "—"}</td></tr>
        <tr><td>Position</td><td>${(t.distance_km || 0).toFixed(1)} km out · ${Math.round(t.speed_kph || 0)} km/h · source ${esc(t.source)}</td></tr>
        <tr><td>Conditions</td><td>${condCell(t.conditions)}</td></tr>
      </table></div>
      <div><h4>Digital check-in packet ${p ? `<span class="pk ${p.status}">${pkLabel[p.status]}</span>` : ""}</h4>
        ${p ? `<ul class="checks">${p.checks.map((c) => `<li>${icon(c)} <b>${esc(c.check)}</b> – ${esc(c.detail)}</li>`).join("")}</ul>
          <table class="kv">
            <tr><td>Expected plate / trailer</td><td class="mono">${esc(p.packet.expected.plate || "—")} / ${esc(p.packet.expected.trailer || "—")}</td></tr>
            <tr><td>Expected seal</td><td class="mono">${esc(p.packet.expected.seal || "—")}</td></tr>
            <tr><td>Dock door</td><td>${esc(p.packet.dock_door || "—")}</td></tr>
            <tr><td>Gate instruction</td><td>${esc(p.packet.gate_instruction)}</td></tr>
            <tr><td>Prepared</td><td>${fmtTime(p.created_at)} (${esc(p.trigger)}) · updated ${fmtTime(p.updated_at)}</td></tr>
          </table>`
        : `<p class="reason">Not prepared yet. Packets are built when the truck is within ${30} min of arrival or part of a predicted surge.</p>`}
      </div>
    </div>
    <div><h4>Prediction history</h4><table class="kv">
      <tr><td>Made at · km left</td><td>AI ETA · static ETA${t.arrived_at ? " · actual " + fmtTime(t.arrived_at) : ""}</td></tr>
      ${preds.map((r) => `<tr><td>${fmtTime(r.made_at)} · ${r.remaining_km.toFixed(1)} km</td><td>${fmtTime(r.eta_at)} · ${fmtTime(r.static_eta_at)}</td></tr>`).join("")}
    </table></div>`;
  if (!$("#detail").open) $("#detail").showModal();
}

$("#inboundRows").onclick = (e) => { const tr = e.target.closest("tr[data-trip]"); if (tr) openTrip(+tr.dataset.trip); };
$("#radar").onclick = (e) => { const c = e.target.closest("[data-trip]"); if (c) openTrip(+c.dataset.trip); };
$("#fleetToggle").onchange = (e) => api("/api/fleet-sim", { method: "POST", body: JSON.stringify({ running: e.target.checked }) });
$("#convoyBtn").onclick = async (e) => {
  e.target.disabled = true;
  try { await api("/api/fleet-sim/convoy", { method: "POST" }); } finally { e.target.disabled = false; }
};
document.querySelector('.tab[data-tab="inbound"]').addEventListener("click", async () => {
  renderInbound(await api("/api/inbound"));
});

window.onInboundMessage = (msg) => {
  if (msg.type === "inbound") renderInbound({ ...msg, model: null });
  else if (msg.type === "eta_model") { if ($("#tab-inbound").classList.contains("active")) renderModel(msg.model); }
  else if (msg.type === "fleet") $("#fleetToggle").checked = msg.running;
};

(async () => {
  try { const d = await api("/api/inbound"); renderInbound(d); renderModel(d.model); } catch (e) { console.warn(e); }
  setInterval(async () => {   // model metrics change slowly; refresh them occasionally
    if ($("#tab-inbound").classList.contains("active")) { try { renderModel(await api("/api/inbound/model")); } catch {} }
  }, 15000);
})();

// deep links: /#inbound, /#exceptions, /#appointments
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => history.replaceState(null, "", `#${t.dataset.tab}`)));
if (location.hash) { const t = document.querySelector(`.tab[data-tab="${location.hash.slice(1)}"]`); if (t) t.click(); }
