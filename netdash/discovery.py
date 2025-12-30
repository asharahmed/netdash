import asyncio
import copy
import hashlib
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
from .utils import _run, _run_async, normalize_mac


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


def _is_tunnel_iface(name: str) -> bool:
    name_l = (name or "").lower()
    if not name_l:
        return False
    tunnel_prefixes = ("utun", "tun", "tap", "wg", "tailscale", "ts", "vpn", "ppp")
    link_prefixes = ("awdl", "llw", "p2p", "bridge")
    return name_l.startswith(tunnel_prefixes) or name_l.startswith(link_prefixes)


def local_ipv4_networks(active_only: bool = False, exclude_tunnel: bool = False) -> List[ipaddress.IPv4Network]:
    nets: List[ipaddress.IPv4Network] = []
    iface_stats = psutil.net_if_stats() if active_only else {}
    for iface, addrs in psutil.net_if_addrs().items():
        if active_only:
            stats = iface_stats.get(iface)
            if not stats or not stats.isup:
                continue
        if exclude_tunnel and _is_tunnel_iface(iface):
            continue
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


def _preferred_sweep_network(
    networks: List[ipaddress.IPv4Network],
    gateway_ip: Optional[str],
    host_ips: List[str],
) -> Optional[ipaddress.IPv4Network]:
    def _tighten(net: ipaddress.IPv4Network, ip_str: str) -> ipaddress.IPv4Network:
        if net.prefixlen >= 24:
            return net
        return ipaddress.IPv4Network(f"{ip_str}/24", strict=False)

    def _host24(ip_str: str) -> Optional[ipaddress.IPv4Network]:
        try:
            return ipaddress.IPv4Network(f"{ip_str}/24", strict=False)
        except Exception:
            return None

    for ip_str in host_ips:
        try:
            host_ip = ipaddress.IPv4Address(ip_str)
        except Exception:
            continue
        for net in networks:
            if host_ip in net:
                return _tighten(net, ip_str)
        return ipaddress.IPv4Network(f"{ip_str}/24", strict=False)

    if gateway_ip:
        try:
            gw = ipaddress.IPv4Address(gateway_ip)
        except Exception:
            gw = None
        if gw:
            for net in networks:
                if gw in net:
                    return _tighten(net, gateway_ip)
            return ipaddress.IPv4Network(f"{gateway_ip}/24", strict=False)

    if networks:
        return sorted(networks, key=lambda n: n.num_addresses)[0]
    return None


def _default_gateway_ip() -> Optional[str]:
    system = platform.system().lower()
    if "darwin" in system:
        # First try netstat to find non-VPN gateway (prefer en0/physical interface)
        rc, out, _ = _run(["netstat", "-rn"], timeout=2)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[0] == "default":
                    gateway = parts[1]
                    iface = parts[3] if len(parts) > 3 else ""
                    # Skip tunnel/VPN interfaces, prefer physical interfaces
                    if not _is_tunnel_iface(iface) and "." in gateway:
                        return gateway
        # Fallback to route command
        rc, out, err = _run(["route", "-n", "get", "default"], timeout=2)
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("gateway:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return parts[1]
        elif err:
            print(f"DEBUG: route get default failed (rc={rc}): {err.strip()}")
    elif "linux" in system:
        rc, out, err = _run(["ip", "route", "show", "default"], timeout=2)
        if rc == 0:
            for line in out.splitlines():
                m = re.search(r"\bdefault via (\S+)", line)
                if m:
                    return m.group(1)
        elif err:
            print(f"DEBUG: ip route default failed (rc={rc}): {err.strip()}")
    return None


def _gateway_mac(gateway_ip: str) -> Optional[str]:
    if not gateway_ip:
        return None
    system = platform.system().lower()
    if "windows" in system:
        rc, out, _ = _run(["arp", "-a", gateway_ip], timeout=2)
        if rc == 0:
            m = re.search(rf"{re.escape(gateway_ip)}\s+([0-9a-fA-F\-]{{17}})", out)
            if m:
                return normalize_mac(m.group(1))
        return None
    rc, out, _ = _run(["arp", "-n", gateway_ip], timeout=2)
    if rc == 0:
        m = re.search(r"([0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5})", out)
        if m:
            return normalize_mac(m.group(1))
    return None


def _wifi_identity() -> Optional[str]:
    system = platform.system().lower()
    if "darwin" in system:
        airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Resources/airport"
        rc, out, _ = _run([airport, "-I"], timeout=2)
        if rc == 0:
            ssid = None
            bssid = None
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    ssid = line.split(":", 1)[1].strip()
                elif line.startswith("BSSID:"):
                    bssid = line.split(":", 1)[1].strip()
            if ssid and bssid:
                return f"{ssid}|{bssid}"
            if ssid:
                return ssid
    elif "linux" in system:
        rc, out, _ = _run(["iwgetid", "-r"], timeout=2)
        if rc == 0 and out.strip():
            return out.strip()
        rc, out, _ = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"], timeout=2)
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("yes:"):
                    return line.split(":", 1)[1].strip()
    elif "windows" in system:
        rc, out, _ = _run(["netsh", "wlan", "show", "interfaces"], timeout=2)
        if rc == 0:
            ssid = None
            bssid = None
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("SSID") and "BSSID" not in line:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        ssid = parts[1].strip()
                elif line.startswith("BSSID"):
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        bssid = parts[1].strip()
            if ssid and bssid:
                return f"{ssid}|{bssid}"
            if ssid:
                return ssid
    return None


def _network_fingerprint() -> str:
    nets = sorted(str(n) for n in local_ipv4_networks(active_only=True, exclude_tunnel=True))
    addrs: List[str] = []
    try:
        iface_stats = psutil.net_if_stats()
        for iface, addrs_list in psutil.net_if_addrs().items():
            stats = iface_stats.get(iface)
            if not stats or not stats.isup:
                continue
            if _is_tunnel_iface(iface):
                continue
            for addr in addrs_list:
                if addr.family == socket.AF_INET and addr.address:
                    if addr.address.startswith("127."):
                        continue
                    mask = addr.netmask or ""
                    addrs.append(f"{iface}:{addr.address}/{mask}")
    except Exception:
        pass
    gateway_ip = _default_gateway_ip() or ""
    gateway_mac = _gateway_mac(gateway_ip) or ""
    wifi_id = _wifi_identity() or ""
    payload = {
        "nets": nets,
        "addrs": sorted(addrs),
        "gateway": gateway_ip,
        "gateway_mac": gateway_mac,
        "wifi": wifi_id,
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()


def _base_name(name: str) -> str:
    name = re.sub(r"\.(local|lan|home|directory|tail[a-f0-9]{5}\.ts\.net)$", "", name, flags=re.I)
    name = re.sub(r"\s*[\(\[].*?[\)\]]", "", name)
    name = re.sub(r"[\s\-–—]*\b(tailscale|ts)\b[\s\-–—]*", " ", name, flags=re.I)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name


def _active_host_ips() -> List[str]:
    out: List[str] = []
    try:
        iface_stats = psutil.net_if_stats()
        for iface, addrs in psutil.net_if_addrs().items():
            stats = iface_stats.get(iface)
            if not stats or not stats.isup:
                continue
            if _is_tunnel_iface(iface):
                continue
            for addr in addrs:
                if addr.family != socket.AF_INET or not addr.address:
                    continue
                ip = addr.address
                if ip.startswith("127.") or ip.startswith("100."):
                    continue
                out.append(ip)
    except Exception:
        pass
    return out


def _is_valid_host_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    if addr.is_multicast or addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_reserved:
        return False
    if ip in ("0.0.0.0", "255.255.255.255"):
        return False
    return addr.is_private or addr.is_global


def _cap_concurrency(value: int, mac_limit: int) -> int:
    try:
        system = platform.system().lower()
    except Exception:
        system = ""
    if "darwin" in system:
        return min(value, mac_limit)
    return value


def _file_limit() -> Optional[int]:
    try:
        import resource
    except Exception:
        return None
    try:
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        return None


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
        name, _, _ = await asyncio.wait_for(
            loop.run_in_executor(None, socket.gethostbyaddr, ip),
            timeout=0.5,
        )
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
_last_network_fingerprint: str = ""
_last_neighbors_fingerprint: str = ""
_discovery_task: Optional[asyncio.Task] = None


def is_discovery_rate_limited(min_interval_s: int) -> bool:
    if _last_disc_time <= 0:
        return False
    return (time.time() - _last_disc_time) < min_interval_s


def get_cached_discovery() -> Dict[str, Any]:
    return copy.deepcopy(_last_disc_result)


def build_known_stub(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    known = parse_known_devices(cfg)
    host_addrs = load_host_addrs()
    host_ips_all = host_addrs["ips"]
    combined: Dict[str, Dict[str, Any]] = {}
    for kd in known:
        bn = _base_name(kd.name)
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
                "has_tailscale": False,
            }
        entry = combined[bn]
        if kd.match_mac and not entry["mac"]:
            entry["mac"] = normalize_mac(kd.match_mac)
        if kd.notes and not entry["notes"]:
            entry["notes"] = kd.notes
        if kd.match_ip:
            iface_type = "Tailscale" if kd.match_ip.startswith("100.") else "Local"
            if iface_type == "Tailscale":
                entry["has_tailscale"] = True
            # If this IP belongs to the host, mark it as up (host can always reach its own IPs)
            is_host_ip = kd.match_ip in host_ips_all
            if is_host_ip:
                entry["up"] = True
                entry["missing"] = False
                entry["is_host"] = True
            entry["interfaces"].append(
                {
                    "ip": kd.match_ip,
                    "mac": entry["mac"],
                    "ping": is_host_ip,
                    "ports": {p: False for p in kd.ports},
                    "up": is_host_ip,
                    "type": iface_type,
                    "original_name": kd.name,
                    "conn_type": "Wired",
                    "iface": None,
                }
            )
    return sorted(combined.values(), key=lambda x: x["name"].lower())


def kick_discovery(
    cfg: Dict[str, Any],
    force_refresh: bool = False,
    min_interval_s: int = 30,
    fast: bool = False,
) -> None:
    global _discovery_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _discovery_task and not _discovery_task.done():
        return
    if not force_refresh and is_discovery_rate_limited(min_interval_s):
        return
    async def _run():
        started = time.time()
        try:
            print(f"Background discovery started (fast={fast})")
            await discover(cfg, force_refresh=force_refresh, fast=fast)
            print(f"Background discovery finished in {int((time.time() - started) * 1000)} ms")
        except Exception as e:
            print(f"Background discovery failed: {e}")
    _discovery_task = loop.create_task(_run())


async def discover(cfg: Dict[str, Any], force_refresh: bool = False, fast: bool = False) -> Dict[str, Any]:
    global _last_disc_result, _last_disc_time, _last_neighbors_snapshot, _last_neighbors_ts
    global _last_network_fingerprint, _last_neighbors_fingerprint
    async with _discovery_lock:
        started_at = time.time()
        now = time.time()
        network_fp = _network_fingerprint()
        if _last_network_fingerprint and network_fp != _last_network_fingerprint:
            print("Network fingerprint changed; clearing discovery cache")
            _last_disc_result = {}
            _last_disc_time = 0
            _last_neighbors_snapshot = []
            _last_neighbors_ts = 0.0
            _last_neighbors_fingerprint = ""

        if (
            not force_refresh
            and not cache_disabled()
            and _last_disc_result
            and (now - _last_disc_time < 60)
            and network_fp == _last_network_fingerprint
        ):
            up_k = sum(1 for d in _last_disc_result.get("known_devices", []) if d.get("up"))
            up_d = sum(1 for d in _last_disc_result.get("discovered_devices", []) if d.get("up"))
            print(f"Returning cached discovery result (Up: {up_k} known, {up_d} disc)")
            return copy.deepcopy(_last_disc_result)

        disc_cfg = cfg.get("discovery", {}) or {}
        mode = disc_cfg.get("mode", "bounded_sweep")
        max_hosts = int(disc_cfg.get("max_sweep_hosts", 256))
        ping_timeout_ms = int(disc_cfg.get("ping_timeout_ms", 900))
        default_ports = list(disc_cfg.get("default_ports", [22, 80, 443, 3389]))
        if fast:
            max_hosts = min(max_hosts, 64)
            default_ports = []

        known = parse_known_devices(cfg)
        networks = local_ipv4_networks(active_only=True, exclude_tunnel=True)
        known_ips = {kd.match_ip for kd in known if kd.match_ip}
        gateway_ip = _default_gateway_ip()
        if gateway_ip:
            await ping(gateway_ip, min(ping_timeout_ms, 400))
        host_ips_active = _active_host_ips()
        if not host_ips_active:
            host_addrs = load_host_addrs()
            host_ips_active = [
                ip
                for ip in host_addrs["ips"]
                if not ip.startswith("127.") and not ip.startswith("100.")
            ]

        sweep_net = _preferred_sweep_network(networks, gateway_ip, host_ips_active)
        neighbors = await get_neighbors()
        system = platform.system().lower()
        snapshot_allowed = not neighbor_snapshot_disabled() and not force_refresh
        neighbor_source = "fresh"
        if neighbors:
            if snapshot_allowed:
                _last_neighbors_snapshot = copy.deepcopy(neighbors)
                _last_neighbors_ts = now
                _last_neighbors_fingerprint = network_fp
        else:
            # Nudge ARP/neighbor cache by pinging likely seeds (gateways + known IPs), then retry once.
            seeds = set()
            if gateway_ip:
                seeds.add(gateway_ip)
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
                seed_sem = asyncio.Semaphore(_cap_concurrency(16, 8))

                async def ping_seed(ip: str):
                    async with seed_sem:
                        await ping(ip, min(ping_timeout_ms, 600))

                await asyncio.gather(*[ping_seed(ip) for ip in seeds])
                neighbors = await get_neighbors()
                if neighbors:
                    neighbor_source = "seed-repopulated"
                    print(f"Neighbor repopulated after seed pings ({len(neighbors)} entries)")
            if (
                not neighbors
                and snapshot_allowed
                and _last_neighbors_snapshot
                and (now - _last_neighbors_ts < 120)
                and _last_neighbors_fingerprint == network_fp
            ):
                print("Neighbor scan returned empty; reusing last known snapshot")
                neighbors = copy.deepcopy(_last_neighbors_snapshot)
                neighbor_source = "snapshot"
            elif not neighbors:
                print("Neighbor scan returned empty" + (" and snapshot reuse disabled" if not snapshot_allowed else " and no snapshot available"))
                neighbor_source = "empty"
        if neighbors and not fast:
            validate_pool = []
            for n in neighbors:
                ip = n.get("ip")
                if not ip or not _is_valid_host_ip(ip) or ip in known_ips:
                    continue
                validate_pool.append(ip)
            validate_pool = validate_pool[:8]
            if validate_pool:
                validate_sem = asyncio.Semaphore(_cap_concurrency(8, 6))
                ping_ok: Dict[str, bool] = {}

                async def validate_neighbor(ip: str):
                    async with validate_sem:
                        ping_ok[ip] = await ping(ip, min(ping_timeout_ms, 600))

                await asyncio.gather(*[validate_neighbor(ip) for ip in validate_pool])
                if any(ping_ok.values()):
                    before = len(neighbors)
                    neighbors = [
                        n
                        for n in neighbors
                        if n.get("ip") not in ping_ok or ping_ok.get(n.get("ip"), True)
                    ]
                    dropped = before - len(neighbors)
                    if dropped:
                        print(f"Dropped {dropped} stale neighbor entries after validation")
                else:
                    print("Neighbor validation saw no ping responses; keeping all neighbors")

        # Detect and filter proxy ARP: if many IPs share the same MAC (especially gateway MAC),
        # those are likely proxy ARP responses, not real devices.
        gateway_mac = _gateway_mac(gateway_ip) if gateway_ip else None
        mac_ip_counts: Dict[str, List[str]] = {}
        for n in neighbors:
            mac = n.get("mac")
            ip = n.get("ip")
            if mac and ip:
                mac_ip_counts.setdefault(mac, []).append(ip)

        proxy_arp_macs: set = set()
        PROXY_ARP_THRESHOLD = 10  # If a MAC has 10+ IPs, it's likely proxy ARP
        for mac, ips in mac_ip_counts.items():
            if len(ips) >= PROXY_ARP_THRESHOLD:
                proxy_arp_macs.add(mac)
                print(f"Detected proxy ARP: MAC {mac} has {len(ips)} IPs (filtering)")

        # Filter neighbors to remove proxy ARP entries (keep gateway IP itself)
        if proxy_arp_macs:
            before_count = len(neighbors)
            neighbors = [
                n for n in neighbors
                if n.get("mac") not in proxy_arp_macs or n.get("ip") == gateway_ip
            ]
            filtered = before_count - len(neighbors)
            if filtered:
                print(f"Filtered {filtered} proxy ARP neighbor entries")

        candidate_ips = set()
        mac_by_ip: Dict[str, str] = {}
        iface_by_ip: Dict[str, str] = {}

        # Always include gateway in candidates (even if outside sweep network)
        if gateway_ip and _is_valid_host_ip(gateway_ip):
            candidate_ips.add(gateway_ip)
            gw_mac = _gateway_mac(gateway_ip)
            if gw_mac:
                mac_by_ip[gateway_ip] = gw_mac

        for n in neighbors:
            ip = n.get("ip")
            if not ip or not _is_valid_host_ip(ip):
                continue
            # Allow gateway IP regardless of sweep network
            if sweep_net and ip != gateway_ip:
                try:
                    if ipaddress.IPv4Address(ip) not in sweep_net:
                        continue
                except Exception:
                    continue
            mac = n.get("mac")
            iface = n.get("iface")
            state = n.get("state", "").upper()
            if "windows" not in system and not mac:
                continue
            if iface and _is_tunnel_iface(iface):
                continue
            if state not in ("FAILED", "INCOMPLETE"):
                candidate_ips.add(ip)
            if mac:
                mac_by_ip[ip] = mac
            if iface:
                iface_by_ip[ip] = iface

        sweep_ips: List[str] = []
        sweep_network = str(sweep_net) if sweep_net else None
        if mode == "bounded_sweep" and networks:
            net = sweep_net
            count = 0
            if net:
                for host in net.hosts():
                    h_str = str(host)
                    if h_str not in candidate_ips:
                        sweep_ips.append(h_str)
                        count += 1
                    if count >= max_hosts:
                        break

        ping_results: Dict[str, bool] = {}
        ping_concurrency = _cap_concurrency(concurrency_from_env("NETDASH_PING_CONCURRENCY", 96), 32)
        ping_hits = 0
        pre_sweep_count = len(candidate_ips)
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
            new_active = max(0, len(candidate_ips) - pre_sweep_count)
            print(f"Sweep complete. Found {new_active} new active IPs.")
        elif not networks:
            print("No local networks detected; skipping sweep.")

        try:
            refreshed_neighbors = await get_neighbors()
            for n in refreshed_neighbors:
                ip = n.get("ip")
                if not ip or not _is_valid_host_ip(ip):
                    continue
                if sweep_net:
                    try:
                        if ipaddress.IPv4Address(ip) not in sweep_net:
                            continue
                    except Exception:
                        continue
                mac = n.get("mac")
                # Skip proxy ARP MACs (except gateway IP)
                if mac and mac in proxy_arp_macs and ip != gateway_ip:
                    continue
                if "windows" not in system and not mac:
                    continue
                state = (n.get("state", "") or "").upper()
                if state in ("FAILED", "INCOMPLETE"):
                    continue
                candidate_ips.add(ip)
                iface = n.get("iface")
                if iface and _is_tunnel_iface(iface):
                    continue
                if mac:
                    mac_by_ip[ip] = mac
                if iface:
                    iface_by_ip[ip] = iface
        except Exception as e:
            print(f"DEBUG: refresh neighbors after sweep failed: {e}")

        # Probe sweep IPs that did not respond to ping to catch TCP-only responders
        non_ping_ips = [ip for ip in sweep_ips if ip not in candidate_ips]
        port_probe_conc = _cap_concurrency(concurrency_from_env("NETDASH_PORT_PROBE_CONCURRENCY", 64), 32)
        port_conn_conc = _cap_concurrency(concurrency_from_env("NETDASH_PORT_CONN_CONCURRENCY", 128), 64)
        port_only_hits = 0
        transparent_proxy_detected = False

        if non_ping_ips and not fast and default_ports:
            sem3 = asyncio.Semaphore(port_probe_conc)
            port_sem = asyncio.Semaphore(port_conn_conc)

            async def probe_ports(ip: str) -> bool:
                async with sem3:
                    status = await check_ports(ip, default_ports, sem=port_sem)
                    if any(status.values()):
                        candidate_ips.add(ip)
                        mac_by_ip.setdefault(ip, None)
                        iface_by_ip.setdefault(ip, None)
                        return True
                    return False

            # Early detection: sample 20 evenly-distributed IPs first
            SAMPLE_SIZE = 20
            if len(non_ping_ips) > SAMPLE_SIZE * 2:
                step = len(non_ping_ips) // SAMPLE_SIZE
                sample_ips = [non_ping_ips[i * step] for i in range(SAMPLE_SIZE)]
                sample_results = await asyncio.gather(*[probe_ports(ip) for ip in sample_ips])
                sample_hits = sum(1 for r in sample_results if r)

                # If >80% of sample responds, likely transparent proxy - skip full scan
                if sample_hits > SAMPLE_SIZE * 0.8:
                    print(f"Early proxy detection: {sample_hits}/{SAMPLE_SIZE} sample IPs responded (skipping full scan)")
                    transparent_proxy_detected = True
                    port_only_hits = sample_hits
                else:
                    # Continue with remaining IPs
                    remaining_ips = [ip for ip in non_ping_ips if ip not in sample_ips]
                    remaining_results = await asyncio.gather(*[probe_ports(ip) for ip in remaining_ips])
                    port_only_hits = sample_hits + sum(1 for r in remaining_results if r)
            else:
                # Small sweep, just probe all
                results = await asyncio.gather(*[probe_ports(ip) for ip in non_ping_ips])
                port_only_hits = sum(1 for r in results if r)

            if port_only_hits:
                print(f"Port-only discovery found {port_only_hits} hosts")
            elif not candidate_ips:
                print("Port-only probes found no additional hosts.")

            # Detect transparent proxy: if >50% of sweep IPs respond port-only with no MAC,
            # it's likely a VPN/captive portal intercepting traffic. Filter them out.
            PORT_PROXY_THRESHOLD = 0.5  # 50% of sweep
            if port_only_hits > len(sweep_ips) * PORT_PROXY_THRESHOLD and port_only_hits > 20:
                transparent_proxy_detected = True

            if transparent_proxy_detected:
                # Count how many IPs have no MAC and didn't respond to ping (port-only)
                no_mac_port_only = sum(1 for ip in candidate_ips if mac_by_ip.get(ip) is None and not ping_results.get(ip, False))
                if no_mac_port_only > port_only_hits * 0.8:  # >80% have no MAC
                    print(f"Detected transparent proxy: {port_only_hits} port-only hits with no MAC (filtering)")
                    # Keep only IPs that have MAC or responded to ping
                    before = len(candidate_ips)
                    candidate_ips = {ip for ip in candidate_ips if mac_by_ip.get(ip) or ping_results.get(ip)}
                    # Always keep gateway
                    if gateway_ip:
                        candidate_ips.add(gateway_ip)
                    filtered = before - len(candidate_ips)
                    print(f"Filtered {filtered} transparent proxy entries")
                    port_only_hits = 0  # Reset since we filtered them
        else:
            port_only_hits = 0

        if not candidate_ips and sweep_ips:
            # If neighbor cache is empty and no pings responded, limit port checks to avoid long stalls.
            fallback_limit = min(64, len(sweep_ips))
            candidate_ips.update(sweep_ips[:fallback_limit])
            print(f"No neighbors/ping hits; falling back to port-check {len(candidate_ips)} sweep IPs.")

        host_addrs = load_host_addrs()
        host_ips_all = host_addrs["ips"]
        host_macs = host_addrs["macs"]

        discovered: Dict[str, Dict[str, Any]] = {}
        sem2 = asyncio.Semaphore(_cap_concurrency(concurrency_from_env("NETDASH_ENRICH_CONCURRENCY", 96), 48))
        port_sem = asyncio.Semaphore(_cap_concurrency(concurrency_from_env("NETDASH_PORT_CONN_CONCURRENCY", 128), 64))
        dns_sem = asyncio.Semaphore(_cap_concurrency(concurrency_from_env("NETDASH_DNS_CONCURRENCY", 16), 8))

        async def enrich(ip: str):
            async with sem2:
                mac = mac_by_ip.get(ip)
                kd = match_known(known, ip, mac)

                dns_name = None
                if not fast:
                    async with dns_sem:
                        dns_name = await reverse_dns_async(ip)
                name = kd.name if kd else (dns_name or ip)
                ports = kd.ports if (kd and kd.ports) else default_ports

                if fast:
                    port_status = {}
                    ping_up = ip in ping_results
                    is_up = ping_up
                else:
                    if ports:
                        port_status = await check_ports(ip, ports, sem=port_sem)
                    else:
                        port_status = {}

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

                if gateway_ip and ip == gateway_ip:
                    name = f"Gateway ({ip})"

                is_host = ip in host_ips_all or ip == "127.0.0.1"

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

        combined: Dict[str, Dict[str, Any]] = {}
        known_mac_to_group: Dict[str, str] = {}
        known_name_to_group: Dict[str, str] = {}
        known_ip_to_group: Dict[str, str] = {}

        for kd in known:
            bn = _base_name(kd.name)
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
                    "has_tailscale": False,
                }

            mac = normalize_mac(kd.match_mac) if kd.match_mac else None
            if (kd.match_ip and kd.match_ip in host_ips_all) or (mac and any(normalize_mac(m) == mac for m in host_macs)):
                combined[bn]["is_host"] = True

            if mac:
                known_mac_to_group[mac] = bn
            known_name_to_group[bn] = bn
            if kd.match_ip:
                known_ip_to_group[kd.match_ip] = bn

            if kd.match_ip:
                iface_type = "Tailscale" if kd.match_ip.startswith("100.") else "Local"
                if iface_type == "Tailscale":
                    combined[bn]["has_tailscale"] = True
                # If this IP belongs to the host, mark it as up (host can always reach its own IPs)
                is_host_ip = kd.match_ip in host_ips_all
                if is_host_ip:
                    combined[bn]["up"] = True
                    combined[bn]["missing"] = False
                combined[bn]["interfaces"].append(
                    {
                        "ip": kd.match_ip,
                        "mac": mac,
                        "ping": is_host_ip,
                        "ports": {p: False for p in kd.ports},
                        "up": is_host_ip,
                        "type": iface_type,
                        "original_name": kd.name,
                    }
                )

        host_group_name = next((bn for bn, g in combined.items() if g.get("is_host")), "Host machine")

        # Check ports for host IP interfaces (they weren't in candidate_ips so weren't checked)
        async def check_host_iface_ports(iface: Dict[str, Any], ports: List[int]):
            if not ports:
                return
            port_status = await check_ports(iface["ip"], ports, sem=port_sem)
            iface["ports"] = port_status
            if any(port_status.values()):
                iface["up"] = True

        host_port_tasks = []
        for grp in combined.values():
            if not grp.get("is_host"):
                continue
            for iface in grp.get("interfaces", []):
                ip = iface.get("ip", "")
                if ip in host_ips_all:
                    ports_to_check = [p for p in iface.get("ports", {}).keys()]
                    if ports_to_check:
                        host_port_tasks.append(check_host_iface_ports(iface, ports_to_check))

        if host_port_tasks:
            await asyncio.gather(*host_port_tasks)

        for dev in discovered.values():
            mac = normalize_mac(dev.get("mac")) if dev.get("mac") else None
            ip = dev.get("ip") or "Unknown IP"
            name = dev.get("name", "")
            is_gateway = (gateway_ip and ip == gateway_ip)
            # Preserve gateway naming with IP, otherwise use base_name
            base_name = name if is_gateway else _base_name(name)
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
                    "has_tailscale": False,
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
            if iface_payload["type"] == "Tailscale":
                grp["has_tailscale"] = True
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

        def merge_known_groups(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            merged: Dict[str, Dict[str, Any]] = {}
            for g in groups:
                key = _base_name(g.get("name", "")).lower()
                if key not in merged:
                    merged[key] = g
                    continue
                target = merged[key]
                target["up"] = target.get("up", False) or g.get("up", False)
                target["missing"] = target.get("missing", False) and g.get("missing", False)
                target["is_host"] = target.get("is_host", False) or g.get("is_host", False)
                target["known"] = True
                target["mac"] = target.get("mac") or g.get("mac")
                target["notes"] = target.get("notes") or g.get("notes")
                target["has_tailscale"] = target.get("has_tailscale", False) or g.get("has_tailscale", False)
                iface_by_ip = {i["ip"]: i for i in target.get("interfaces", []) if i.get("ip")}
                for iface in g.get("interfaces", []):
                    ip = iface.get("ip")
                    if not ip:
                        continue
                    if ip in iface_by_ip:
                        continue
                    iface_by_ip[ip] = iface
                target["interfaces"] = list(iface_by_ip.values())
            return list(merged.values())

        final_known = merge_known_groups(final_known)

        # Tighten networks to /24 for display (large /8 or /16 masks are misleading)
        display_networks: List[str] = []
        for net in networks:
            if net.prefixlen < 24:
                # Find host IP in this network to create a tightened /24
                for hip in host_ips_active:
                    try:
                        if ipaddress.IPv4Address(hip) in net:
                            tightened = ipaddress.IPv4Network(f"{hip}/24", strict=False)
                            display_networks.append(str(tightened))
                            break
                    except Exception:
                        continue
                else:
                    # Fallback: use network address with /24
                    display_networks.append(str(ipaddress.IPv4Network(f"{net.network_address}/24", strict=False)))
            else:
                display_networks.append(str(net))

        # Check for subnet mismatches in known devices
        subnet_mismatches: List[str] = []
        if sweep_net:
            for kd in known:
                if kd.match_ip and not kd.match_ip.startswith("100."):  # Skip Tailscale IPs
                    try:
                        if ipaddress.IPv4Address(kd.match_ip) not in sweep_net:
                            subnet_mismatches.append(f"{kd.name}: {kd.match_ip}")
                    except Exception:
                        pass

        # Check if gateway is outside sweep network
        gateway_outside_sweep = False
        if gateway_ip and sweep_net:
            try:
                gateway_outside_sweep = ipaddress.IPv4Address(gateway_ip) not in sweep_net
            except Exception:
                pass

        res = {
            "networks": display_networks,
            "neighbors_count": len(neighbors),
            "mode": mode,
            "known_devices": sorted(final_known, key=lambda x: x["name"].lower()),
            "discovered_devices": sorted(final_discovered, key=lambda x: (not x["up"], x["name"].lower())),
            "meta": {
                "neighbor_source": neighbor_source,
                "sweep_size": len(sweep_ips),
                "ping_hits": ping_hits,
                "port_only_hits": port_only_hits,
                "transparent_proxy_detected": transparent_proxy_detected,
                "duration_ms": int((time.time() - started_at) * 1000),
                "completed_at": int(time.time()),
                "gateway_ip": gateway_ip,
                "gateway_mac": mac_by_ip.get(gateway_ip) if gateway_ip else None,
                "gateway_outside_sweep": gateway_outside_sweep,
                "sweep_network": sweep_network,
                "host_ips": sorted(host_ips_active),
                "subnet_mismatches": subnet_mismatches if subnet_mismatches else None,
            },
        }
        print(f"Discovery complete: {len(final_known)} known, {len(final_discovered)} discovered")
        if not cache_disabled():
            _last_disc_result = res
            _last_disc_time = time.time()
            _last_network_fingerprint = network_fp
        return res
