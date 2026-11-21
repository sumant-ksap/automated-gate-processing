"use strict";
// Yard & docks tab. Uses helpers from app.js ($, esc, api, fmtTime).
const yard = { data: null, timer: null };
const CAP_COLOR = { REEFER: "#4ea1ff", FLATBED: "#d29922", CROSS_DOCK: "#a371f7", "": "#8b9bb0", POWER: "#2ea043" };
const LOAD_COLOR = { PERISHABLE: "var(--HOLD)", CROSS_DOCK: "var(--MANUAL_REVIEW)", STANDARD: "var(--accent)" };
const TYPE_LABEL = { DRY_VAN: "dry van", REEFER: "reefer ❄", FLATBED: "flatbed" };

function loadBadge(v) { return `<span class="load ${esc(v.load_class)}">${esc(v.load_class.replace("_", "-").toLowerCase())}</span>`; }

function renderYardMap(d) {
  let h = "";
  const zones = [["WEST", 0, 140], ["CENTRAL", 140, 270], ["EAST", 270, 420]];
  const maxLoad = Math.max(3, ...Object.values(d.zone_load));
  for (const [z, x0, x1] of zones) {
    const op = 0.02 + 0.10 * (d.zone_load[z] || 0) / maxLoad;
    h += `<rect x="${x0}" y="62" width="${x1 - x0}" height="210" fill="var(--INSPECTION)" fill-opacity="${op.toFixed(3)}"
            stroke="var(--line)" stroke-dasharray="2 3"/>
          <text x="${x0 + 4}" y="284" fill="var(--muted)" font-size="8">${z.toLowerCase()} · ${d.zone_load[z] || 0} moves/10 min</text>`;
  }
  h += `<rect x="0" y="-36" width="420" height="30" fill="var(--panel2)" stroke="var(--line)"/>
        <text x="6" y="-24" fill="var(--muted)" font-size="9">WAREHOUSE · dock face</text>`;
  for (const r of d.resources) {
    const cap = r.capabilities || "";
    const occ = r.status === "OCCUPIED";
    const fill = occ ? (LOAD_COLOR[r.load_class] || "var(--accent)") : "transparent";
    const title = `${r.id} ${cap ? "(" + cap.toLowerCase() + ")" : ""} ${occ ? "– " + (r.plate || "") + " " + (r.load_class || "") : "– free"}`;
    if (r.kind === "DOOR") {
      h += `<rect class="res" data-visit="${r.visit_id || ""}" x="${r.x - 6}" y="-6" width="12" height="24" rx="2" fill="${fill}"
              stroke="${CAP_COLOR[cap] || "#8b9bb0"}" stroke-width="2"><title>${esc(title)}</title></rect>
            <text x="${r.x}" y="-9" fill="var(--muted)" font-size="6" text-anchor="middle">${r.id.slice(1)}</text>`;
    } else {
      h += `<rect class="res" data-visit="${r.visit_id || ""}" x="${r.x - 6}" y="${r.y - 11}" width="12" height="22" rx="2"
              fill="${fill}" stroke="${cap === "POWER" ? CAP_COLOR.POWER : "var(--line)"}" stroke-width="${cap === "POWER" ? 1.6 : 1}">
              <title>${esc(title)}</title></rect>`;
    }
    if (occ && r.plate && r.kind === "DOOR") {   // vertical plate label under each door (spots: hover / table)
      h += `<text transform="translate(${r.x + 2},22) rotate(90)" fill="var(--text)" font-size="6">${esc(r.plate.slice(0, 8))}</text>`;
    }
  }
  const byId = Object.fromEntries(d.resources.map((r) => [r.id, r]));
  for (const m of d.moves.slice(0, 20)) {
    const a = m.from_id === "GATE" ? d.gate : byId[m.from_id];
    const b = byId[m.to_id];
    if (!a || !b) continue;
    h += `<polyline points="${a.x},${a.y} ${a.x},${b.y + 20} ${b.x},${b.y + 20} ${b.x},${b.y}" fill="none"
            stroke="var(--accent)" stroke-opacity="0.45" stroke-dasharray="3 2"/>`;
  }
  h += `<rect x="${d.gate.x - 18}" y="${d.gate.y - 6}" width="36" height="12" rx="2" fill="var(--text)"/>
        <text x="${d.gate.x}" y="${d.gate.y + 3}" fill="var(--bg)" font-size="8" text-anchor="middle" font-weight="700">GATE</text>`;
  $("#yardMap").innerHTML = h;
}

function renderYardRows(d) {
  const now = Date.now();
  $("#yardRows").innerHTML = d.visits.map((v) => {
    const det = v.detention_at ? Math.round((new Date(v.detention_at) - now) / 60000) : null;
    return `<tr data-visit="${v.id}">
      <td><b class="mono">${esc(v.plate || "")}</b><div class="reason">${esc(v.carrier_name || "")}</div></td>
      <td>${loadBadge(v)} <span class="reason">${esc(TYPE_LABEL[v.trailer_type] || v.trailer_type)} · ${v.pallet_count} pal</span></td>
      <td><b>${Math.round(v.priority)}</b></td>
      <td class="mono">${esc(v.resource_id || "—")}</td>
      <td>${esc(v.status.replace("_", " ").toLowerCase())}</td>
      <td class="${det != null && det < 30 ? "late" : "reason"}">${det == null ? "" : det <= 0 ? "charging" : `in ${det} min`}</td>
      <td class="reason">${esc((v.reasons || []).slice(0, 2).join(" · "))}</td></tr>`;
  }).join("") || `<tr><td colspan="7" class="reason">Yard is empty. Trucks appear here when the gate grants entry, or press Simultaneous check-in.</td></tr>`;
}

function renderBatch(b) {
  if (!b) return;
  const row = (label, key, lowerIsBetter = true) => {
    const a = b.ai[key], f = b.fcfs[key];
    const cls = a === f ? "" : (a < f) === lowerIsBetter ? "better" : "worse";
    return `<tr><td>${label}</td><td class="${cls}">${a}</td><td>${f}</td></tr>`;
  };
  $("#batchCard").innerHTML = `
    <div>${b.trucks} trucks checked in at ${fmtTime(b.at)}</div>
    <table class="cmp-table">
      <tr><th></th><th>AI engine</th><th>First-come-first-served</th></tr>
      ${row("Equipment violations", "equipment_violations")}
      ${row("Urgent loads parked while a less urgent truck got a door", "urgent_waiting")}
      ${row("Truck travel in the yard (m)", "travel_m")}
      ${row("Most trucks sent into one zone", "max_trucks_one_zone")}
      ${row("Sent straight to a suitable door", "at_doors", false)}
    </table>
    ${b.fcfs.issues.length ? `<div class="reason">FCFS mistakes: ${b.fcfs.issues.map(esc).join("; ")}</div>` : ""}`;
}

async function openVisit(id) {
  const v = await api(`/api/yard/visits/${id}`);
  $("#dTitle").textContent = `Yard visit · ${v.plate} · ${v.carrier_name || ""}`;
  $("#dBody").innerHTML = `
    <div class="grid2">
      <div><table class="kv">
        <tr><td>Load</td><td>${loadBadge(v)} ${esc(TYPE_LABEL[v.trailer_type] || v.trailer_type)} · ${v.pallet_count} pallets</td></tr>
        <tr><td>Priority score</td><td>${Math.round(v.priority)}</td></tr>
        <tr><td>Location / status</td><td>${esc(v.resource_id || "—")} · ${esc(v.status)}</td></tr>
        <tr><td>Checked in</td><td>${fmtTime(v.checked_in_at)}</td></tr>
        <tr><td>Detention starts</td><td>${fmtTime(v.detention_at)}</td></tr>
        ${v.due_by ? `<tr><td>Cross-dock departure</td><td>${fmtTime(v.due_by)}</td></tr>` : ""}
        <tr><td>Yard jockey distance</td><td>${Math.round(v.jockey_m)} m</td></tr>
      </table></div>
      <div><h4>Why the engine chose this</h4><ul class="reasons">${(v.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
        <h4>Moves</h4><ul class="reasons">${v.moves.map((m) => `<li>${fmtTime(m.created_at)} ${esc(m.from_id)} → ${esc(m.to_id)} (${Math.round(m.distance_m)} m)</li>`).join("")}</ul></div>
    </div>`;
  if (!$("#detail").open) $("#detail").showModal();
}

function renderYard(d) {
  yard.data = d;
  const k = d.kpis;
  $("#yDoors").textContent = `${k.doors_busy}/${k.doors_total}`;
  $("#yCold").textContent = `${k.cold_docks_busy}/4`;
  $("#yWait").textContent = k.waiting_in_yard;
  $("#yPerish").textContent = k.perishables_waiting;
  $("#yardScale").textContent = k.time_scale;
  renderYardMap(d);
  renderYardRows(d);
  renderBatch(d.last_batch);
}

async function refreshYard() {
  if (!$("#tab-yard").classList.contains("active")) return;
  try { renderYard(await api("/api/yard")); } catch (e) { console.warn(e); }
}

$("#batchBtn").onclick = async (e) => {
  e.target.disabled = true;
  try { const b = await api(`/api/yard/simultaneous-checkin?n=${$("#batchN").value}`, { method: "POST" }); await refreshYard(); renderBatch(b); }
  catch (err) { alert(err.message); }
  finally { e.target.disabled = false; }
};
$("#yardRows").onclick = (e) => { const tr = e.target.closest("tr[data-visit]"); if (tr) openVisit(+tr.dataset.visit); };
$("#yardMap").onclick = (e) => { const r = e.target.closest("[data-visit]"); if (r && r.dataset.visit) openVisit(+r.dataset.visit); };
document.querySelector('.tab[data-tab="yard"]').addEventListener("click", refreshYard);

// websocket hook: yard changes are frequent; refresh at most every 1.5 s
const prevInbound = window.onInboundMessage;
window.onInboundMessage = (msg) => {
  if (msg.type === "yard") {
    if (!yard.timer) yard.timer = setTimeout(() => { yard.timer = null; refreshYard(); }, 1500);
    return;
  }
  if (prevInbound) prevInbound(msg);
};
setInterval(refreshYard, 10000);   // detention countdowns
if (location.hash === "#yard") refreshYard();
