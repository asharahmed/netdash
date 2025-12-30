import { fetchStatus } from "./api.js";
import { initGraph, setGraphData, resetGraphView } from "./visualization.js";
import { setBadge, deviceTable, sortDevices, peerRow, copyText } from "./ui.js";

const loadingMessages = [
  "Rerouting the packet stream...",
  "Bribing the router for more bandwidth...",
  "Searching for hidden pixels...",
  "Counting the electrons...",
  "Negotiating with the firewall...",
  "Optimizing the link-state database...",
  "Pinging the space station...",
  "Untangling the Ethernet cables...",
  "Teaching the packets to swim...",
  "Decoding the carrier pigeons...",
  "Recalibrating the Wi‑Fi feng shui...",
  "Convincing the ISP gnomes to cooperate...",
  "Coaching packets to use indoor voices...",
  "Politely asking the NAT to share...",
  "Dusting off the ARP cache...",
  "Aligning the lasers on the fiber...",
  "Checking if Schrödinger's port is open...",
  "Threatening the switch with a reboot (gently)..."
];
const loadingSubMessages = [
  "If it takes too long, blame DNS.",
  "Packets walk faster when no one watches.",
  "Some ports bite. Handle with care.",
  "QoS negotiations may include snacks.",
  "Spinning up extra electrons...",
  "Routing table is doing stretches.",
  "Firewall promised to behave this time.",
  "Teaching ICMP to knock politely.",
  "Tracing cables, finding hope."
];

let loadingInterval = null;
let firstLoad = true;
let lastGoodTs = null;
let lastKnownDevices = [];
let lastDiscoveredDevices = [];
let lastGatewayIp = null;
let lastShowTsIndicator = false;
let knownSortKey = 'name';
let knownSortDir = 'asc';
let discSortKey = 'name';
let discSortDir = 'asc';

function toggleLoading(show) {
  const overlay = document.getElementById('loadingOverlay');
  const msg = document.getElementById('loadingMsg');
  const sub = document.getElementById('loadingSub');
  overlay.style.display = show ? 'flex' : 'none';
  
  if (show) {
    msg.textContent = loadingMessages[Math.floor(Math.random() * loadingMessages.length)];
    sub.textContent = loadingSubMessages[Math.floor(Math.random() * loadingSubMessages.length)];
    loadingInterval = setInterval(() => {
      msg.className = 'loading-msg'; // reset animation
      void msg.offsetWidth; // trigger reflow
      msg.textContent = loadingMessages[Math.floor(Math.random() * loadingMessages.length)];
      sub.textContent = loadingSubMessages[Math.floor(Math.random() * loadingSubMessages.length)];
    }, 2500);
  } else {
    clearInterval(loadingInterval);
  }
}

async function refresh(options = {}) {
  const showLoading = firstLoad || options.force;
  if (showLoading) toggleLoading(true);
  try {
    const data = await fetchStatus({ force: options.force, timeoutMs: 60000 });
    lastGoodTs = Date.now();

    document.getElementById('hostLine').textContent = `Host: ${data.host?.hostname || "unknown"} • ${data.host?.os || "unknown"}`;
    document.getElementById('dotHost').style.background = "rgba(45,212,191,0.5)";

    const dt = new Date((data.ts || 0) * 1000);
    document.getElementById('lastUpdated').textContent = "Last updated: " + dt.toLocaleString();
    document.getElementById('dotTime').style.background = "rgba(45,212,191,0.5)";

    const meta = data.meta || {};
    const cacheOn = meta.cache_enabled !== false;
    const snapOn = meta.neighbor_snapshot_enabled !== false;
    const forced = meta.cache_forced === true;
    const cacheBits = `Cache: ${cacheOn ? "On" : "Off"}${forced ? " (forced)" : ""}`;
    const stale = meta.discovery_stale ? " • Refreshing…" : "";
    document.getElementById('cacheMeta').textContent = `${cacheBits}${stale} • Snapshot: ${snapOn ? "On" : "Off"}`;
    document.getElementById('dotCache').style.background = cacheOn ? "rgba(45,212,191,0.5)" : "rgba(251,113,133,0.5)";

    const nd = data.nextdns || {};
    const ndOk = nd.using_nextdns === true;
    setBadge(document.getElementById('nextdnsUsing'), ndOk ? "Using NextDNS: Yes" : "Using NextDNS: No", ndOk ? "ok" : "bad");
    setBadge(document.getElementById('nextdnsStatus'), (nd.status ?? "unknown"), nd.reachable ? "ok" : "bad");
    document.getElementById('nextdnsDetails').textContent = nd.reachable ? (ndOk ? "Detected." : "Not detected.") : ("Error: " + (nd.error || "unknown"));

    const ts = data.tailscale || {};
    const installed = ts.installed === true;
    const running = ts.running_local === true;

    setBadge(document.getElementById('tsInstalled'), installed ? "Yes" : "No", installed ? "ok" : "bad");
    setBadge(document.getElementById('tsRunning'), running ? "Running" : "Not running", running ? "ok" : (installed ? "warn" : "bad"));
    document.getElementById('tsBackend').textContent = ts.backend_state ?? "-";
    document.getElementById('tsDetails').textContent = ts.error ? ts.error : "-";

    const exitNode = ts.current_exit_node || null;
    if (exitNode) {
      const st = exitNode.online ? `<span class="badge bOk">Active</span>` : `<span class="badge bBad">Offline</span>`;
      const label = `${exitNode.name}${exitNode.ip ? " • " + exitNode.ip : ""}${exitNode.mullvad ? " • Mullvad" : ""}`;
      document.getElementById('tsExitNode').innerHTML = `${st} <span class="mono" style="margin-left:10px;">${label}</span>`;
    } else {
      document.getElementById('tsExitNode').innerHTML = `<span class="badge">None</span>`;
    }

    const peers = ts.peers_display || [];
    if (!installed) {
      document.getElementById('tsPeers').innerHTML = `<div class="muted">Tailscale CLI not installed or not on PATH.</div>`;
    } else if (!peers.length) {
      document.getElementById('tsPeers').innerHTML = `<div class="muted">No devices to display.</div>`;
    } else {
      document.getElementById('tsPeers').innerHTML = peers.map(peerRow).join("");
    }

    const disc = data.discovery || {};
    const dMeta = disc.meta || {};
    const metaParts = [
      `Networks: ${(disc.networks || []).join(", ") || "unknown"}`,
      `Mode: ${disc.mode || "unknown"}`,
      `Neighbors: ${disc.neighbors_count ?? "?"}`
    ].filter(Boolean);
    document.getElementById('discMeta').textContent = metaParts.join(" • ");

    // Display warnings (only show actionable issues)
    const warnings = [];
    if (dMeta.transparent_proxy_detected) {
      warnings.push("Transparent proxy detected - VPN or captive portal may be intercepting traffic. Some devices may not be discovered.");
    }
    // Note: subnet_mismatches not shown as warning - devices on other networks show as "missing" which is sufficient

    const warningsCard = document.getElementById('warningsCard');
    const warningsList = document.getElementById('warningsList');
    if (warnings.length > 0) {
      warningsCard.style.display = 'block';
      warningsList.innerHTML = warnings.map(w => `<div style="margin: 4px 0;">⚠ ${w}</div>`).join("");
    } else {
      warningsCard.style.display = 'none';
    }
    const statParts = [
      dMeta.neighbor_source ? `Neighbors from: ${dMeta.neighbor_source}` : null,
      (dMeta.ping_hits ?? null) !== null ? `Ping hits: ${dMeta.ping_hits}` : null,
      (dMeta.port_only_hits ?? null) !== null ? `Port hits: ${dMeta.port_only_hits}` : null,
      dMeta.duration_ms ? `Duration: ${dMeta.duration_ms} ms` : null
    ].filter(Boolean);
    document.getElementById('discStats').textContent = statParts.join(" • ") || "—";
    if (dMeta.completed_at) {
      const dtRun = new Date((dMeta.completed_at || 0) * 1000);
      document.getElementById('discLastRun').textContent = `Last run: ${dtRun.toLocaleString()}`;
    } else {
      document.getElementById('discLastRun').textContent = "Last run: —";
    }
    document.getElementById('statDuration').textContent = dMeta.duration_ms ? `Last sweep: ${dMeta.duration_ms} ms` : "Last sweep: —";
    document.getElementById('statNeighbor').textContent = dMeta.neighbor_source ? `Neighbors: ${dMeta.neighbor_source}` : "Neighbors: unknown";

    const known = disc.known_devices || [];
    const discovered = disc.discovered_devices || [];
    const knownUp = known.filter(d => d.up).length;
    const discUp = discovered.filter(d => d.up).length;

    // Store for filtering
    lastKnownDevices = known;
    lastDiscoveredDevices = discovered;
    lastShowTsIndicator = ts.installed === true || ts.running_local === true;
    lastGatewayIp = dMeta.gateway_ip || null;

    document.getElementById('knownCount').textContent = `${known.length} tracked`;
    applyFilters(); // Render with current filters

    document.getElementById('statKnown').textContent = `Known: ${known.length}`;
    document.getElementById('statKnownUp').textContent = `Known up: ${knownUp}`;
    document.getElementById('statDisc').textContent = `Discovered: ${discovered.length}`;
    document.getElementById('statDiscUp').textContent = `Discovered up: ${discUp}`;

    setGraphData(data);

  } catch (err) {
    console.error("Refresh failed:", err);
    const tooltip = document.getElementById('tooltip');
    tooltip.innerHTML = `<strong>Refresh failed:</strong> ${err.message}`;
    tooltip.style.display = 'block';
    tooltip.style.left = (window.innerWidth - 240) + 'px';
    tooltip.style.top = '10px';
    setTimeout(() => { tooltip.style.display = 'none'; }, 2000);
  } finally {
    firstLoad = false;
    if (showLoading) toggleLoading(false);
  }
}

window.copyText = copyText;

function applyFilters() {
  const searchTerm = (document.getElementById('deviceSearch')?.value || '').toLowerCase();
  const showOffline = document.getElementById('filterShowOffline')?.checked !== false;
  const showMissing = document.getElementById('filterShowMissing')?.checked !== false;

  const filterDevice = (d) => {
    // Filter by search term
    if (searchTerm) {
      const name = (d.name || '').toLowerCase();
      const mac = (d.mac || '').toLowerCase();
      const ips = (d.interfaces || []).map(i => i.ip || '').join(' ').toLowerCase();
      if (!name.includes(searchTerm) && !mac.includes(searchTerm) && !ips.includes(searchTerm)) {
        return false;
      }
    }
    // Filter by status
    if (!showOffline && !d.up) return false;
    if (!showMissing && d.missing) return false;
    return true;
  };

  let filteredKnown = lastKnownDevices.filter(filterDevice);
  let filteredDiscovered = lastDiscoveredDevices.filter(filterDevice);

  // Apply sorting
  filteredKnown = sortDevices(filteredKnown, knownSortKey, knownSortDir);
  filteredDiscovered = sortDevices(filteredDiscovered, discSortKey, discSortDir);

  document.getElementById('knownDevices').innerHTML = deviceTable(filteredKnown, {
    showTailscale: lastShowTsIndicator,
    gatewayIp: lastGatewayIp,
    tableId: 'knownTable',
    sortKey: knownSortKey,
    sortDir: knownSortDir
  });
  document.getElementById('discoveredDevices').innerHTML = deviceTable(filteredDiscovered, {
    gatewayIp: lastGatewayIp,
    tableId: 'discTable',
    sortKey: discSortKey,
    sortDir: discSortDir
  });

  // Add click handlers for sorting
  document.querySelectorAll('#knownTable th[data-sort]').forEach(th => {
    th.onclick = () => {
      const key = th.dataset.sort;
      if (knownSortKey === key) {
        knownSortDir = knownSortDir === 'asc' ? 'desc' : 'asc';
      } else {
        knownSortKey = key;
        knownSortDir = 'asc';
      }
      applyFilters();
    };
  });
  document.querySelectorAll('#discTable th[data-sort]').forEach(th => {
    th.onclick = () => {
      const key = th.dataset.sort;
      if (discSortKey === key) {
        discSortDir = discSortDir === 'asc' ? 'desc' : 'asc';
      } else {
        discSortKey = key;
        discSortDir = 'asc';
      }
      applyFilters();
    };
  });

  // Update counts to show filtered
  const knownEl = document.getElementById('knownCount');
  if (filteredKnown.length !== lastKnownDevices.length) {
    knownEl.textContent = `${filteredKnown.length}/${lastKnownDevices.length} shown`;
  } else {
    knownEl.textContent = `${lastKnownDevices.length} tracked`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initGraph('netGraph', document.getElementById('tooltip'));
  document.getElementById('btnRefresh').addEventListener('click', () => refresh());
  document.getElementById('btnForceRefresh').addEventListener('click', () => refresh({ force: true }));
  document.getElementById('btnResetView').addEventListener('click', () => resetGraphView());

  // Filter event listeners
  document.getElementById('deviceSearch')?.addEventListener('input', applyFilters);
  document.getElementById('filterShowOffline')?.addEventListener('change', applyFilters);
  document.getElementById('filterShowMissing')?.addEventListener('change', applyFilters);

  // Keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    // Ctrl+R or Cmd+R for refresh (prevent browser refresh)
    if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
      e.preventDefault();
      refresh({ force: e.shiftKey }); // Shift+Ctrl+R for force refresh
    }
    // Escape to clear search
    if (e.key === 'Escape') {
      const search = document.getElementById('deviceSearch');
      if (search && search.value) {
        search.value = '';
        applyFilters();
      }
    }
    // / to focus search
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
      e.preventDefault();
      document.getElementById('deviceSearch')?.focus();
    }
  });

  refresh();
  setInterval(refresh, 30000);
});
