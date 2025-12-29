import asyncio
import copy
import ipaddress
import json
import platform
import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import psutil

from .config import cache_disabled, concurrency_from_env, neighbor_snapshot_disabled
from .utils import _run_async, normalize_mac


@dataclass
class KnownDevice:
    name: str
    match_ip: Optional[str]
    match_mac: Optional[str]
    ports: List[int]
    notes: str


def load_host_addrs() -> Dict[str, Any]:
    ips = set()
    macs: List[str] = []
    try:
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ips.add(addr.address)
                elif addr.family == psutil.AF_LINK:
                    macs.append(addr.address)
    except Exception:
        pass
    ips.add("127.0.0.1")
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except Exception:
        pass
    return {"ips": ips, "macs": macs}


def parse_known_devices(cfg: Dict[str, Any]) -> List[KnownDevice]:
    out: List[KnownDevice] = []
    for d in cfg.get("devices", []):
        match = d.get("match", {}) or {}
        out.append(
            KnownDevice(
                name=d.get("name", "Unnamed"),
                match_ip=((match.get("ip") or "").strip() or None),
                match_mac=((match.get("mac") or "").strip() or None),
                ports=list(d.get("ports", [])),
                notes=(d.get("notes", "") or ""),
            )
        )
    return out


def local_ipv4_networks() -> List[ipaddress.IPv4Network]:
    nets: List[ipaddress.IPv4Network] = []
    for _iface, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and a.address:
                ip = ipaddress.IPv4Address(a.address)
                if ip.is_loopback:
                    continue
                mask = a.netmask or "255.255.255.0"
                try:
                    nets.append(ipaddress.IPv4Network(f"{a.address}/{mask}", strict=False))
                except Exception:
                    continue
    uniq, seen = [], set()
    for n in nets:
        if str(n) not in seen:
            uniq.append(n)
            seen.add(str(n))
    return uniq


async def ping(host: str, timeout_ms: int = 900) -> bool:
    system = platform.system().lower()
    try:
        if "windows" in system:
            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
            rc, _, _ = await _run_async(cmd, timeout=max(2, int(timeout_ms / 1000) + 2))
            return rc == 0
        elif "darwin" in system:
            cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_ms / 1000))), host]
            rc, _, _ = await _run_async(cmd, timeout=2)
            return rc == 0
        else:
            cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_ms / 1000))), host]
            rc, _, _ = await _run_async(cmd, timeout=2)
            return rc == 0
    except Exception:
        return False


async def tcp_check(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def reverse_dns_async(ip: str) -> Optional[str]:
    try:
        loop = asyncio.get_running_loop()
        name, _, _ = await loop.run_in_executor(None, socket.gethostbyaddr, ip)
        return name
    except Exception:
        return None


async def get_neighbors_windows() -> List[Dict[str, str]]:
    neighbors: List[Dict[str, str]] = []

    ps_cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-NetNeighbor -AddressFamily IPv4 | Select-Object IPAddress,LinkLayerAddress,State | ConvertTo-Json",
    ]
    rc, out, _ = await _run_async(ps_cmd, timeout=7)
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            for row in data:
                ip = (row.get("IPAddress") or "").strip()
                mac = (row.get("LinkLayerAddress") or "").strip()
                state = (row.get("State") or "").strip()
                if ip and mac and mac != "00-00-00-00-00-00":
                    neighbors.append({"ip": ip, "mac": normalize_mac(mac), "state": state})
        except Exception:
            pass

    if neighbors:
        return list({n["ip"]: n for n in neighbors}.values())

    rc, out, _ = await _run_async(["arp", "-an"], timeout=5)
    if rc == 0 and out.strip():
        for line in out.splitlines():
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F\-]{17})\s+(\w+)", line)
            if m:
                ip, mac, state = m.group(1), m.group(2), m.group(3)
                neighbors.append({"ip": ip, "mac": normalize_mac(mac), "state": state})

    return list({n["ip"]: n for n in neighbors}.values())


async def get_neighbors_unix() -> List[Dict[str, str]]:
    neighbors: List[Dict[str, str]] = []
    system = platform.system().lower()

    if "linux" in system:
        rc, out, _ = await _run_async(["ip", "neigh"], timeout=5)
        if rc == 0 and out.strip():
            for line in out.splitlines():
                m = re.search(
                    r"(\d+\.\d+\.\d+\.\d+).*dev\s+(\w+).*lladdr\s+([0-9a-f:]{17}).*(REACHABLE|STALE|DELAY|PROBE|FAILED|INCOMPLETE)?",
                    line,
                    re.I,
                )
                if m:
                    ip, iface, mac = m.group(1), m.group(2), m.group(3)
                    state = (m.group(4) or "").upper()
                    if state not in ("FAILED", "INCOMPLETE"):
                        neighbors.append({"ip": ip, "mac": normalize_mac(mac), "state": state, "iface": iface})
            if neighbors:
                return list({n["ip"]: n for n in neighbors}.values())

    rc, out, _ = await _run_async(["arp", "-an"], timeout=5)
    if rc == 0 and out.strip():
        for line in out.splitlines():
            m = re.search(
                r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-f:]{1,17}|[0-9a-f\.]{1,17})(?:\s+on\s+(\w+))?",
                line,
                re.I,
            )
            if m:
                ip = m.group(1)
                mac_raw = m.group(2).replace(".", ":")
                iface = m.group(3)
                if mac_raw == "incomplete" or ":" not in mac_raw:
                    continue
                parts = [p.zfill(2) for p in mac_raw.split(":")]
                mac = normalize_mac(":".join(parts))
                neighbors.append({"ip": ip, "mac": mac, "state": "", "iface": iface})

    return list({n["ip"]: n for n in neighbors}.values())


async def get_neighbors() -> List[Dict[str, str]]:
    system = platform.system().lower()
    if "windows" in system:
        return await get_neighbors_windows()
    return await get_neighbors_unix()


def match_known(known: List[KnownDevice], ip: str, mac: Optional[str]) -> Optional[KnownDevice]:
    mac_n = normalize_mac(mac) if mac else None
    for k in known:
        if k.match_ip and k.match_ip == ip:
            return k
        if k.match_mac and mac_n and normalize_mac(k.match_mac) == mac_n:
            return k
    return None


async def check_ports(ip: str, ports: List[int], sem: Optional[asyncio.Semaphore] = None) -> Dict[int, bool]:
    async def guarded_tcp(port: int):
        if sem:
            async with sem:
                return await tcp_check(ip, port)
        return await tcp_check(ip, port)

    results = await asyncio.gather(*[guarded_tcp(p) for p in ports], return_exceptions=True)
    return {p: (r is True) for p, r in zip(ports, results)}


_discovery_lock = asyncio.Lock()
_last_disc_result: Dict[str, Any] = {}
_last_disc_time: float = 0
_last_neighbors_snapshot: List[Dict[str, Any]] = []
_last_neighbors_ts: float = 0.0


async def discover(cfg: Dict[str, Any]) -> Dict[str, Any]:
    global _last_disc_result, _last_disc_time, _last_neighbors_snapshot, _last_neighbors_ts
    async with _discovery_lock:
        started_at = time.time()
        now = time.time()
        if not cache_disabled() and _last_disc_result and (now - _last_disc_time < 60):
            up_k = sum(1 for d in _last_disc_result.get("known_devices", []) if d.get("up"))
            up_d = sum(1 for d in _last_disc_result.get("discovered_devices", []) if d.get("up"))
            print(f"Returning cached discovery result (Up: {up_k} known, {up_d} disc)")
            return copy.deepcopy(_last_disc_result)

        disc_cfg = cfg.get("discovery", {}) or {}
        mode = disc_cfg.get("mode", "bounded_sweep")
        max_hosts = int(disc_cfg.get("max_sweep_hosts", 256))
        ping_timeout_ms = int(disc_cfg.get("ping_timeout_ms", 900))
        default_ports = list(disc_cfg.get("default_ports", [22, 80, 443, 3389]))

        known = parse_known_devices(cfg)
        networks = local_ipv4_networks()

        neighbors = await get_neighbors()
        snapshot_allowed = not neighbor_snapshot_disabled()
        neighbor_source = "fresh"
        if neighbors:
            if snapshot_allowed:
                _last_neighbors_snapshot = copy.deepcopy(neighbors)
                _last_neighbors_ts = now
        elif not neighbors:
            # Nudge ARP/neighbor cache by pinging likely seeds (gateways + known IPs), then retry once.
            seeds = set()
            for net in networks:
                try:
                    seeds.add(str(next(net.hosts())))
                except StopIteration:
                    pass
            for kd in known:
                if kd.match_ip:
                    seeds.add(kd.match_ip)
            seeds = list(seeds)[:12]  # bound to avoid bursts
            if seeds:
                print(f"Neighbor scan empty; pinging {len(seeds)} seeds to repopulate ARP")
                seed_sem = asyncio.Semaphore(16)

                async def ping_seed(ip: str):
                    async with seed_sem:
                        await ping(ip, min(ping_timeout_ms, 600))

                await asyncio.gather(*[ping_seed(ip) for ip in seeds])
                neighbors = await get_neighbors()
                if neighbors:
                    neighbor_source = "seed-repopulated"
                    print(f"Neighbor repopulated after seed pings ({len(neighbors)} entries)")
            if not neighbors and snapshot_allowed and _last_neighbors_snapshot and (now - _last_neighbors_ts < 120):
                print("Neighbor scan returned empty; reusing last known snapshot")
                neighbors = copy.deepcopy(_last_neighbors_snapshot)
                neighbor_source = "snapshot"
            elif not neighbors:
                print("Neighbor scan returned empty" + (" and snapshot reuse disabled" if not snapshot_allowed else " and no snapshot available"))
                neighbor_source = "empty"
        elif snapshot_allowed and _last_neighbors_snapshot and (now - _last_neighbors_ts < 120):
            print("Neighbor scan returned empty; reusing last known snapshot")
            neighbors = copy.deepcopy(_last_neighbors_snapshot)
            neighbor_source = "snapshot"
        else:
            print("Neighbor scan returned empty" + (" and snapshot reuse disabled" if not snapshot_allowed else " and no snapshot available"))
            neighbor_source = "empty"

        candidate_ips = set()
        mac_by_ip: Dict[str, str] = {}
        iface_by_ip: Dict[str, str] = {}
        for n in neighbors:
            ip = n.get("ip")
            mac = n.get("mac")
            iface = n.get("iface")
            state = n.get("state", "").upper()
            if ip:
                if state not in ("FAILED", "INCOMPLETE"):
                    candidate_ips.add(ip)
                if mac:
                    mac_by_ip[ip] = mac
                if iface:
                    iface_by_ip[ip] = iface

        networks = local_ipv4_networks()

        sweep_ips: List[str] = []
        if mode == "bounded_sweep" and networks:
            net = sorted(networks, key=lambda n: n.num_addresses)[0]
            count = 0
            for host in net.hosts():
                h_str = str(host)
                if h_str not in candidate_ips:
                    sweep_ips.append(h_str)
                    count += 1
                if count >= max_hosts:
                    break

        ping_results: Dict[str, bool] = {}
        ping_concurrency = concurrency_from_env("NETDASH_PING_CONCURRENCY", 96)
        ping_hits = 0
        if sweep_ips:
            sem = asyncio.Semaphore(ping_concurrency)

            async def ping_one(ip: str):
                nonlocal ping_hits
                async with sem:
                    ok = await ping(ip, ping_timeout_ms)
                    ping_results[ip] = ok
                    if ok:
                        ping_hits += 1
                        candidate_ips.add(ip)

            print(f"Starting sweep of {len(sweep_ips)} IPs...")
            await asyncio.gather(*[ping_one(ip) for ip in sweep_ips])
            print(f"Sweep complete. Found {len(candidate_ips) - len(neighbors)} new active IPs.")
        elif not networks:
            print("No local networks detected; skipping sweep.")

        try:
            refreshed_neighbors = await get_neighbors()
            for n in refreshed_neighbors:
                ip = n.get("ip")
                if not ip:
                    continue
                state = (n.get("state", "") or "").upper()
                if state in ("FAILED", "INCOMPLETE"):
                    continue
                candidate_ips.add(ip)
                mac = n.get("mac")
                iface = n.get("iface")
                if mac:
                    mac_by_ip[ip] = mac
                if iface:
                    iface_by_ip[ip] = iface
        except Exception as e:
            print(f"DEBUG: refresh neighbors after sweep failed: {e}")

        # Probe sweep IPs that did not respond to ping to catch TCP-only responders
        non_ping_ips = [ip for ip in sweep_ips if ip not in candidate_ips]
        port_probe_conc = concurrency_from_env("NETDASH_PORT_PROBE_CONCURRENCY", 64)
        port_conn_conc = concurrency_from_env("NETDASH_PORT_CONN_CONCURRENCY", 128)
        port_only_hits = 0
        if non_ping_ips:
            sem3 = asyncio.Semaphore(port_probe_conc)
            port_sem = asyncio.Semaphore(port_conn_conc)

            async def probe_ports(ip: str):
                nonlocal port_only_hits
                async with sem3:
                    status = await check_ports(ip, default_ports, sem=port_sem)
                    if any(status.values()):
                        candidate_ips.add(ip)
                        mac_by_ip.setdefault(ip, None)
                        iface_by_ip.setdefault(ip, None)
                        open_ports = [p for p, ok in status.items() if ok]
                        print(f"Port-only discovery hit {ip} (open: {open_ports})")
                        port_only_hits += 1

            await asyncio.gather(*[probe_ports(ip) for ip in non_ping_ips])
            if not candidate_ips:
                print("Port-only probes found no additional hosts.")
        else:
            port_only_hits = 0

        if not candidate_ips and sweep_ips:
            # If neighbor cache is empty and no pings responded, still probe sweep IPs for open ports.
            candidate_ips.update(sweep_ips)
            print(f"No neighbors/ping hits; falling back to port-check {len(candidate_ips)} sweep IPs.")

        host_addrs = load_host_addrs()
        host_ips = host_addrs["ips"]
        host_macs = host_addrs["macs"]

        discovered: Dict[str, Dict[str, Any]] = {}
        sem2 = asyncio.Semaphore(concurrency_from_env("NETDASH_ENRICH_CONCURRENCY", 96))
        port_sem = asyncio.Semaphore(concurrency_from_env("NETDASH_PORT_CONN_CONCURRENCY", 128))

        async def enrich(ip: str):
            async with sem2:
                mac = mac_by_ip.get(ip)
                kd = match_known(known, ip, mac)

                dns_name = await reverse_dns_async(ip)
                name = kd.name if kd else (dns_name or ip)
                ports = kd.ports if (kd and kd.ports) else default_ports

                port_status = await check_ports(ip, ports, sem=port_sem)

                if ip in ping_results:
                    ping_up = ping_results[ip]
                else:
                    ping_up = await ping(ip, ping_timeout_ms)

                is_up = ping_up or any(port_status.values())

                iface = iface_by_ip.get(ip)
                conn_type = "Wired"
                if iface:
                    iface_l = iface.lower()
                    if iface_l.startswith("w") or "air" in iface_l or "wireless" in iface_l:
                        conn_type = "Wireless"
                    elif iface_l == "en0" and platform.system().lower() == "darwin":
                        conn_type = "Wireless"

                if ip == "10.0.0.1":
                    name = f"Gateway ({ip})"

                is_host = ip in host_ips or ip == "127.0.0.1"

                discovered[ip] = {
                    "ip": ip,
                    "name": name,
                    "mac": mac,
                    "known": bool(kd),
                    "notes": kd.notes if kd else "",
                    "ping": ping_up,
                    "ports": port_status,
                    "up": is_up,
                    "iface": iface,
                    "conn_type": conn_type,
                    "is_host": is_host,
                }

        await asyncio.gather(*[enrich(ip) for ip in sorted(candidate_ips)])

        def get_base_name(name: str) -> str:
            name = re.sub(r"\.(local|lan|home|directory|tail[a-f0-9]{5}\.ts\.net)$", "", name, flags=re.I)
            name = re.sub(r"\s*[\(\[].*?[\)\]]", "", name).strip()
            return name

        combined: Dict[str, Dict[str, Any]] = {}
        known_mac_to_group: Dict[str, str] = {}
        known_name_to_group: Dict[str, str] = {}
        known_ip_to_group: Dict[str, str] = {}

        for kd in known:
            bn = get_base_name(kd.name)
            if bn not in combined:
                combined[bn] = {
                    "name": bn,
                    "mac": normalize_mac(kd.match_mac) if kd.match_mac else None,
                    "known": True,
                    "notes": kd.notes or "",
                    "interfaces": [],
                    "up": False,
                    "missing": True,
                    "is_host": False,
                }

            mac = normalize_mac(kd.match_mac) if kd.match_mac else None
            if (kd.match_ip and kd.match_ip in host_ips) or (mac and any(normalize_mac(m) == mac for m in host_macs)):
                combined[bn]["is_host"] = True

            if mac:
                known_mac_to_group[mac] = bn
            known_name_to_group[bn] = bn
            if kd.match_ip:
                known_ip_to_group[kd.match_ip] = bn

            if kd.match_ip:
                combined[bn]["interfaces"].append(
                    {
                        "ip": kd.match_ip,
                        "mac": mac,
                        "ping": False,
                        "ports": {p: False for p in kd.ports},
                        "up": False,
                        "type": "Tailscale" if kd.match_ip.startswith("100.") else "Local",
                        "original_name": kd.name,
                    }
                )

        host_group_name = next((bn for bn, g in combined.items() if g.get("is_host")), "Host machine")

        for dev in discovered.values():
            mac = normalize_mac(dev.get("mac")) if dev.get("mac") else None
            ip = dev.get("ip") or "Unknown IP"
            name = dev.get("name", "")
            base_name = get_base_name(name)
            is_host = dev.get("is_host", False)

            target_group = None
            if is_host:
                target_group = host_group_name
            elif mac and mac in known_mac_to_group:
                target_group = known_mac_to_group[mac]
            elif base_name in known_name_to_group:
                target_group = known_name_to_group[base_name]
            elif ip != "Unknown IP" and ip in known_ip_to_group:
                target_group = known_ip_to_group[ip]

            if not target_group:
                target_group = mac if mac else (base_name if not re.match(r"^\d+\.\d+\.\d+\.\d+$", base_name) else ip)

            if target_group not in combined:
                combined[target_group] = {
                    "name": base_name if target_group != "Host machine" else socket.gethostname(),
                    "mac": mac,
                    "known": False,
                    "notes": dev.get("notes", ""),
                    "interfaces": [],
                    "up": False,
                    "missing": False,
                    "is_host": (target_group == "Host machine" or is_host),
                }

            grp = combined[target_group]
            if is_host:
                grp["is_host"] = True
            grp["up"] = grp["up"] or dev.get("up", False)
            if dev.get("known"):
                grp["known"] = True
            if not dev.get("missing"):
                grp["missing"] = False
            if dev.get("notes") and not grp["notes"]:
                grp["notes"] = dev.get("notes")
            if not grp["mac"] and mac:
                grp["mac"] = mac
            if dev.get("is_host"):
                grp["is_host"] = True

            iface = next((i for i in grp["interfaces"] if i["ip"] == ip), None)
            iface_payload = {
                "ip": ip,
                "mac": dev.get("mac"),
                "ping": dev.get("ping"),
                "ports": dev.get("ports", {}),
                "up": dev.get("up", False),
                "type": "Tailscale" if ip.startswith("100.") else "Local",
                "original_name": name,
                "conn_type": dev.get("conn_type", "Wired"),
                "iface": dev.get("iface"),
            }
            if iface:
                iface.update(iface_payload)
            else:
                grp["interfaces"].append(iface_payload)

        final_known = []
        final_discovered = []
        for c in combined.values():
            if c["known"]:
                final_known.append(c)
            elif c["interfaces"]:
                final_discovered.append(c)

        res = {
            "networks": [str(n) for n in networks],
            "neighbors_count": len(neighbors),
            "mode": mode,
            "known_devices": sorted(final_known, key=lambda x: x["name"].lower()),
            "discovered_devices": sorted(final_discovered, key=lambda x: (not x["up"], x["name"].lower())),
            "meta": {
                "neighbor_source": neighbor_source,
                "sweep_size": len(sweep_ips),
                "ping_hits": ping_hits,
                "port_only_hits": port_only_hits,
                "duration_ms": int((time.time() - started_at) * 1000),
                "completed_at": int(time.time()),
            },
        }
        print(f"Discovery complete: {len(final_known)} known, {len(final_discovered)} discovered")
        if not cache_disabled():
            _last_disc_result = res
            _last_disc_time = time.time()
        return res
