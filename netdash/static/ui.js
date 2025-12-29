export function setBadge(el, text, kind) {
  if (text && text.toLowerCase() === "ok") text = "OK";
  el.textContent = text;
  el.className = "badge " + (kind === "ok" ? "bOk" : (kind === "warn" ? "bWarn" : (kind === "bad" ? "bBad" : "")));
}

export function chip(text, ok) {
  const cls = ok ? "chip chipOk" : "chip chipBad";
  return `<span class="${cls}">${text}</span>`;
}

export function portsHtml(portsObj) {
  const entries = Object.entries(portsObj || {});
  if (!entries.length) return "";
  const parts = entries.map(([p, ok]) => chip(p, ok));
  return `<div class="ports">${parts.join("")}</div>`;
}

export function deviceTable(devs) {
  if (!devs || !devs.length) return `<div class="muted">None</div>`;

  let rows = devs.map(d => {
    const upBadge = d.up ? `<span class="badge bOk">Up</span>` : `<span class="badge bBad">Down</span>`;
    const name = d.name;
    const mac = d.mac ? `<div class="muted">MAC: <span class="mono">${d.mac}</span></div>` : "";
    const notes = d.notes ? `<div class="muted">${d.notes}</div>` : "";
    const missing = d.missing ? `<div class="muted">Expected device not currently detected</div>` : "";

    const interfacesHtml = (d.interfaces || []).map(i => {
      const pingBadge = i.ping ? `<span class="badge bOk">Ping</span>` : `<span class="badge bBad">Ping</span>`;
      const typeLabel = i.type === "Tailscale" ? `<span class="badge bWarn">TS</span>` : "";
      return `
        <div class="row" style="padding: 6px 0; border-top: 1px solid rgba(255,255,255,0.03);">
          <div style="flex: 1;">
            <span class="mono">${i.ip}</span> ${typeLabel}
            <span class="copyBtn" onclick="copyText('${i.ip}')">
              <img src="/static/icons/copy.svg" alt="" style="width:14px; height:14px; opacity:0.8;" />
              Copy IP
            </span>
            <div class="muted" style="font-size:10px;">${i.original_name}</div>
          </div>
          <div style="width: 80px;">${pingBadge}</div>
          <div style="flex: 2;">${portsHtml(i.ports)}</div>
        </div>
      `;
    }).join("");

    return `
      <tr>
        <td>
          <div style="font-weight:600;">${name}</div>
          ${mac}
          ${notes}
          ${missing}
        </td>
        <td style="vertical-align: top;">${upBadge}</td>
        <td colspan="2" style="padding: 0;">
          <div style="display: flex; flex-direction: column;">
            ${interfacesHtml}
          </div>
        </td>
      </tr>
    `;
  }).join("");

  return `
    <table class="table">
      <thead>
        <tr>
          <th style="width:35%;">Device</th>
          <th style="width:10%;">Status</th>
          <th style="width:15%;">Interface / Ping</th>
          <th>Ports</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

export function peerRow(p) {
  const st = p.online ? `<span class="badge bOk">Online</span>` : `<span class="badge bBad">Offline</span>`;
  const ips = (p.tailscale_ips || []).join(", ");
  const meta = `${(p.os || "").trim()}${ips ? " • " + ips : ""}`.trim();
  return `
    <div class="miniRow">
      <div class="miniLeft">
        <div class="miniName">${p.name}</div>
        <div class="miniMeta mono">${meta || "-"}</div>
      </div>
      <div>${st}</div>
    </div>
  `;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    const tooltip = document.getElementById('tooltip');
    tooltip.innerHTML = `<strong>Copied:</strong> ${text}`;
    tooltip.style.display = 'block';
    tooltip.style.left = (window.innerWidth - 180) + 'px';
    tooltip.style.top = '10px';
    setTimeout(() => { tooltip.style.display = 'none'; }, 1200);
  } catch (e) {
    console.error("Copy failed", e);
  }
}
