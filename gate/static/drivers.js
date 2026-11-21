"use strict";
// Driver messages tab. Uses helpers from app.js ($, esc, api, fmtTime).
const drivers = { data: null, timer: null };
const MSG_KIND = { door: "Go to door", spot: "Park & wait", move: "Move to door", inspection: "Inspection", hold: "Security hold",
                   review: "Guard check", denied: "Entry refused", done: "Unloading done", pre: "Pre-check OK", pre_blocked: "Pre-check problem" };

function renderMessages(d) {
  drivers.data = d;
  const s = d.stats;
  $("#mSent").textContent = s.sent;
  $("#mDelivered").textContent = `${s.delivered}${s.failed ? ` (${s.failed} failed)` : ""}`;
  $("#mAck").textContent = `${Math.round(s.ack_rate * 100)}%`;
  $("#mLangs").textContent = Object.entries(s.languages).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${k.toUpperCase()} ${n}`).join(" · ") || "–";
  $("#msgRows").innerHTML = d.messages.map((m) => `
    <tr data-msg="${m.id}">
      <td>${fmtTime(m.created_at)}</td>
      <td><b class="mono">${esc(m.tractor_plate || "")}</b><div class="reason mono">${esc(m.phone)}</div></td>
      <td>${esc(d.languages[m.language] || m.language)}</td>
      <td>${esc(MSG_KIND[m.kind] || m.kind)}</td>
      <td>${esc(m.status.toLowerCase())}${m.channel === "sim" ? ` <span class="reason">(sim)</span>` : ""}</td>
      <td>${m.acked_at ? `✓ ${fmtTime(m.acked_at)}` : m.reply ? esc(m.reply) : ""}</td>
      <td class="reason msg-preview">${esc(m.body.split("\n")[m.kind === "door" || m.kind === "spot" ? 1 : 0])}</td>
    </tr>`).join("") || `<tr><td colspan="7" class="reason">No messages yet. They are sent when trucks are checked in at the gate.</td></tr>`;
}

function openMessage(id) {
  const m = drivers.data.messages.find((x) => x.id === id);
  if (!m) return;
  $("#dTitle").textContent = `Message to ${m.tractor_plate || m.phone} · ${m.carrier_name || ""}`;
  $("#dBody").innerHTML = `
    <div class="phone">
      <div class="bubble">${esc(m.body)}</div>
      <div class="bubble-meta">${esc(m.phone)} · ${esc(drivers.data.languages[m.language] || m.language)} · ${esc(m.status.toLowerCase())}
        via ${esc(m.channel === "sim" ? "simulated SMS" : m.channel)}${m.acked_at ? ` · confirmed ${fmtTime(m.acked_at)}` : ""}</div>
      ${m.driver_link ? `<a href="${esc(m.driver_link)}" target="_blank" rel="noopener"><button class="primary" style="width:100%">Open the driver's phone page</button></a>` : ""}
    </div>`;
  if (!$("#detail").open) $("#detail").showModal();
}

async function refreshMessages() {
  if (!$("#tab-drivers").classList.contains("active")) return;
  try { renderMessages(await api("/api/messages?limit=150")); } catch (e) { console.warn(e); }
}

$("#msgRows").onclick = (e) => { const tr = e.target.closest("tr[data-msg]"); if (tr) openMessage(+tr.dataset.msg); };
document.querySelector('.tab[data-tab="drivers"]').addEventListener("click", () => { $("#helpBadge").hidden = true; refreshMessages(); });

const prevHandler = window.onInboundMessage;
window.onInboundMessage = (msg) => {
  if (msg.type === "message") {
    if (!drivers.timer) drivers.timer = setTimeout(() => { drivers.timer = null; refreshMessages(); }, 1500);
    return;
  }
  if (msg.type === "driver_help") { $("#helpBadge").hidden = false; return; }
  if (prevHandler) prevHandler(msg);
};
if (location.hash === "#drivers") refreshMessages();
