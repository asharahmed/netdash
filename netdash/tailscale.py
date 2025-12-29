import json
import os
import shutil
from typing import Any, Dict, List, Optional

from .utils import _run_async


def resolve_exe(exe: Optional[str]) -> Optional[str]:
    exe = (exe or "").strip()
    if not exe:
        exe = "tailscale"
    if "/" in exe or "\\" in exe:
        return exe if os.path.exists(exe) else None
    return shutil.which(exe)


def _strip_prefix(ip_or_prefix: str) -> str:
    return (ip_or_prefix or "").split("/")[0].strip()


def _is_mullvad_node(peer: Dict[str, Any]) -> bool:
    name = (peer.get("name") or "").lower()
    dns_name = (peer.get("dns_name") or "").lower()
    return (
        ("mullvad" in name)
        or name.endswith(".mullvad.ts.net")
        or ("mullvad" in dns_name)
        or dns_name.endswith(".mullvad.ts.net")
    )


async def tailscale_status(ts_exe: Optional[str]) -> Dict[str, Any]:
    resolved = resolve_exe(ts_exe)
    if not resolved:
        return {"installed": False, "running_local": False, "error": "tailscale CLI not found on PATH"}

    rc, out, err = await _run_async([resolved, "status", "--json"], timeout=7)
    if rc != 0:
        if rc == -11:
            details = "Tailscale service (tailscaled) not responding (segfault)"
        else:
            details = (err or out or f"tailscale status failed (rc={rc})").strip()
            if "failed to connect to local Tailscale service" in details:
                details = "Tailscale service not running"

        return {"installed": True, "running_local": False, "error": details}

    try:
        data = json.loads(out)
    except Exception:
        return {"installed": True, "running_local": False, "error": "tailscale returned non-JSON output"}

    backend_state = data.get("BackendState") or (data.get("Self", {}) or {}).get("BackendState")
    running_local = str(backend_state).lower() == "running"

    exit_status = data.get("ExitNodeStatus") or (data.get("Self", {}) or {}).get("ExitNodeStatus") or None
    exit_ips = set()
    exit_online = None
    if isinstance(exit_status, dict):
        exit_online = exit_status.get("Online")
        for pfx in (exit_status.get("TailscaleIPs") or []):
            exit_ips.add(_strip_prefix(str(pfx)))

    peers_out: List[Dict[str, Any]] = []
    peers = data.get("Peer", {}) or data.get("Peers", {}) or {}
    if isinstance(peers, dict):
        for _k, p in peers.items():
            dns_name = p.get("DNSName") or p.get("Name") or ""
            name = p.get("HostName") or dns_name or "unknown"
            online = bool(p.get("Online", False))
            os_name = p.get("OS") or p.get("Platform") or ""
            addrs = p.get("TailscaleIPs") or p.get("Addrs") or []
            addrs_norm = [_strip_prefix(str(a)) for a in addrs]
            peers_out.append(
                {
                    "name": name,
                    "dns_name": dns_name,
                    "online": online,
                    "os": os_name,
                    "tailscale_ips": addrs_norm,
                }
            )

    current_exit_peer = None
    if exit_ips:
        for p in peers_out:
            if any(ip in exit_ips for ip in (p.get("tailscale_ips") or [])):
                current_exit_peer = p
                break

    peers_display: List[Dict[str, Any]] = []
    for p in peers_out:
        if not _is_mullvad_node(p):
            peers_display.append(p)
        elif current_exit_peer and p.get("name") == current_exit_peer.get("name"):
            peers_display.append(p)

    peers_display = sorted(peers_display, key=lambda x: (not x["online"], x["name"].lower()))

    current_exit_node = None
    if current_exit_peer:
        ip0 = (current_exit_peer.get("tailscale_ips") or [None])[0]
        current_exit_node = {
            "name": current_exit_peer.get("name"),
            "ip": ip0,
            "online": current_exit_peer.get("online") if exit_online is None else bool(exit_online),
            "mullvad": _is_mullvad_node(current_exit_peer),
        }

    return {
        "installed": True,
        "running_local": running_local,
        "backend_state": backend_state,
        "current_exit_node": current_exit_node,
        "peers_display": peers_display,
        "warning": data.get("Warning") or None,
    }
