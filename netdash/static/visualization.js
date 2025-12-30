// HTML escape helper to prevent XSS
function escapeHtml(str) {
  if (!str) return str;
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

class NetdashGraph {
  constructor(containerId, tooltipEl) {
    this.container = document.getElementById(containerId);
    this.tooltip = tooltipEl;
    this.canvas = document.createElement("canvas");
    this.ctx = this.canvas.getContext("2d");
    this.container.appendChild(this.canvas);
    this.nodes = [];
    this.edges = [];
    this.pan = { x: 0, y: 0 };
    this.scale = 1;
    this.dragging = null;
    this.isPanning = false;
    this.last = { x: 0, y: 0 };
    this._bind();
    this._resize();
    window.addEventListener("resize", () => this._resize());
  }

  _bind() {
    this.container.addEventListener("mousedown", (e) => {
      const pos = this._toWorld(e);
      const hit = this._hitNode(pos.x, pos.y);
      if (hit) {
        this.dragging = hit;
        this.container.style.cursor = "grabbing";
      } else {
        this.isPanning = true;
        this.last = { x: e.clientX, y: e.clientY };
        this.container.style.cursor = "grabbing";
      }
    });

    window.addEventListener("mouseup", () => {
      this.dragging = null;
      this.isPanning = false;
      this.container.style.cursor = "default";
      if (this.tooltip) this.tooltip.style.display = "none";
    });

    this.container.addEventListener("mousemove", (e) => {
      const pos = this._toWorld(e);
      if (this.dragging) {
        this.dragging.x = pos.x;
        this.dragging.y = pos.y;
        this.draw();
      } else if (this.isPanning) {
        const dx = e.clientX - this.last.x;
        const dy = e.clientY - this.last.y;
        this.pan.x += dx;
        this.pan.y += dy;
        this.last = { x: e.clientX, y: e.clientY };
        this.draw();
      } else {
        const hit = this._hitNode(pos.x, pos.y);
        if (hit && this.tooltip) {
          this.tooltip.innerHTML = `
            <strong>${escapeHtml(hit.name)}</strong><br/>
            IPs: ${escapeHtml((hit.ips || []).join(", ")) || "N/A"}<br/>
            MAC: ${escapeHtml(hit.mac) || "N/A"}<br/>
            Connection: ${escapeHtml(hit.connType) || "Wired"}
          `;
          this.tooltip.style.display = "block";
          this.tooltip.style.left = (e.clientX + 10) + "px";
          this.tooltip.style.top = (e.clientY + 10) + "px";
        } else if (this.tooltip) {
          this.tooltip.style.display = "none";
        }
      }
    });

    this.container.addEventListener("wheel", (e) => {
      e.preventDefault();
      const delta = e.deltaY > 0 ? -0.1 : 0.1;
      const newScale = Math.min(2.5, Math.max(0.5, this.scale + delta));
      const rect = this.container.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      this.pan.x = cx - (cx - this.pan.x) * (newScale / this.scale);
      this.pan.y = cy - (cy - this.pan.y) * (newScale / this.scale);
      this.scale = newScale;
      this.draw();
    }, { passive: false });
  }

  _resize() {
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = this.container.clientWidth * dpr;
    this.canvas.height = this.container.clientHeight * dpr;
    this.canvas.style.width = this.container.clientWidth + "px";
    this.canvas.style.height = this.container.clientHeight + "px";
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.draw();
  }

  _toWorld(e) {
    const rect = this.container.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left - this.pan.x) / this.scale,
      y: (e.clientY - rect.top - this.pan.y) / this.scale
    };
  }

  _hitNode(x, y) {
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const n = this.nodes[i];
      const r = n.radius || 10;
      const dx = x - n.x;
      const dy = y - n.y;
      if (dx * dx + dy * dy <= r * r + 4) return n;
    }
    return null;
  }

  setData(data) {
    const known = (data.discovery?.known_devices || []);
    const disc = (data.discovery?.discovered_devices || []);
    const nodes = [];
    const edges = [];

    const addNode = (id, name, type, opts = {}) => {
      nodes.push({
        id,
        name,
        type,
        up: opts.up ?? true,
        ips: opts.ips || [],
        mac: opts.mac || "",
        connType: opts.connType || "Wired",
        isGateway: opts.isGateway || false,
        isHost: opts.isHost || false,
        radius: opts.radius || (type === "host" ? 14 : type === "internet" ? 16 : 10),
      });
    };

    addNode("internet", "Internet", "internet", { up: true, radius: 16 });

    const gwIp = data.discovery?.meta?.gateway_ip || null;
    known.forEach((d, idx) => {
      addNode(d.name, d.name, d.is_host ? "host" : "known", {
        up: d.up,
        ips: d.interfaces.map(i => i.ip),
        mac: d.mac,
        connType: d.interfaces.find(i => i.conn_type === "Wireless") ? "Wireless" : "Wired",
        isGateway: gwIp ? d.interfaces.some(i => i.ip === gwIp) : false,
        isHost: d.is_host
      });
    });

    disc.forEach((d, idx) => {
      addNode(`disc-${idx}-${d.name}`, d.name, d.is_host ? "host" : "disc", {
        up: d.up,
        ips: d.interfaces.map(i => i.ip),
        mac: d.mac,
        connType: d.interfaces.find(i => i.conn_type === "Wireless") ? "Wireless" : "Wired",
        isGateway: gwIp ? d.interfaces.some(i => i.ip === gwIp) : false,
        isHost: d.is_host
      });
    });

    const hostNode = nodes.find(n => n.isHost) || nodes.find(n => n.type === "host") || null;
    const gateway = nodes.find(n => n.isGateway);
    if (!hostNode) {
      addNode("host", data.host?.hostname || "This Host", "host", { isHost: true });
    }

    nodes.forEach(n => {
      if (n.id === "internet") return;
      if (n.isGateway) edges.push(["internet", n.id]);
      else if (n.isHost) edges.push([gateway?.id || "internet", n.id]);
      else edges.push([hostNode?.id || "host", n.id]);
    });

    this.nodes = nodes;
    this.edges = edges;
    this._layout();
    this.draw();
  }

  _layout() {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    const centerX = w / 2;
    const centerY = h * 0.5;

    const host = this.nodes.find(n => n.isHost) || this.nodes.find(n => n.id === "host");
    const internet = this.nodes.find(n => n.id === "internet");
    const gateways = this.nodes.filter(n => n.isGateway);
    const known = this.nodes.filter(n => n.type === "known");
    const disc = this.nodes.filter(n => n.type === "disc");

    if (internet) { internet.x = centerX; internet.y = h * 0.14; }
    gateways.forEach((g, idx) => { g.x = centerX + idx * 24 - (gateways.length - 1) * 12; g.y = h * 0.26; });
    if (host) { host.x = centerX; host.y = centerY; }

    const placeRing = (arr, radius, offset = 0) => {
      if (!arr.length) return;
      const step = (Math.PI * 2) / arr.length;
      const minStep = 10 * Math.PI / 180;
      const useStep = Math.max(step, minStep);
      const span = useStep * (arr.length - 1);
      const start = offset - span / 2;
      arr.forEach((n, idx) => {
        const ang = start + useStep * idx;
        n.x = centerX + radius * Math.cos(ang);
        n.y = centerY + radius * Math.sin(ang);
      });
    };

    const baseRadius = Math.min(w, h) * 0.28;
    placeRing(known, baseRadius, Math.PI / 8);
    placeRing(disc, baseRadius + Math.min(w, h) * 0.12, -Math.PI / 8);
  }

  draw() {
    const ctx = this.ctx;
    const { width, height } = this.canvas;
    ctx.clearRect(0, 0, width, height);
    ctx.save();
    ctx.translate(this.pan.x, this.pan.y);
    ctx.scale(this.scale, this.scale);

    // Bands
    const cw = this.container.clientWidth;
    const ch = this.container.clientHeight;
    ctx.fillStyle = "rgba(255,255,255,0.03)";
    ctx.fillRect(0, 0, cw, ch * 0.2);
    ctx.fillStyle = "rgba(255,255,255,0.02)";
    ctx.fillRect(0, ch * 0.4, cw, ch * 0.2);
    ctx.fillStyle = "rgba(255,255,255,0.01)";
    ctx.fillRect(0, ch * 0.6, cw, ch * 0.4);

    // Edges
    this.edges.forEach(([from, to]) => {
      const a = this.nodes.find(n => n.id === from);
      const b = this.nodes.find(n => n.id === to);
      if (!a || !b) return;
      ctx.strokeStyle = "rgba(255,255,255,0.2)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });

    // Nodes
    this.nodes.forEach(n => {
      ctx.beginPath();
      ctx.fillStyle = n.type === "internet" ? "#38bdf8" : n.type === "host" ? "#818cf8" : n.type === "known" ? "#7dd3fc" : "#cbd5e1";
      ctx.globalAlpha = n.up ? 1 : 0.4;
      ctx.arc(n.x, n.y, n.radius || 10, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.fillStyle = "#fff";
      ctx.font = "11px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(n.name, n.x, n.y + (n.radius || 10) + 14);
    });

    ctx.restore();
  }
}

export function initGraph(containerId, tooltipEl) {
  window.netdashGraph = new NetdashGraph(containerId, tooltipEl);
}

export function setGraphData(data) {
  if (window.netdashGraph) {
    window.netdashGraph.setData(data);
  }
}

export function resetGraphView() {
  if (window.netdashGraph) {
    window.netdashGraph.pan = { x: 0, y: 0 };
    window.netdashGraph.scale = 1;
    window.netdashGraph.draw();
  }
}
