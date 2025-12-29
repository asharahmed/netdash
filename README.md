# Home Network Dashboard

A FastAPI-powered dashboard that discovers devices on your LAN/tailnet, runs bounded subnet sweeps with port checks, detects NextDNS usage, and surfaces Tailscale status in a single-page UI.

## Quickstart
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py                      # uses dashboard.listen_host/port from config.yaml
# or live reload:
uvicorn netdash.api:app --reload --host 0.0.0.0 --port 8080
```
Optional flags on `python app.py`: `--no-cache` (disable in-process caching), `--host`, `--port`.

## Configuration
- Default config: `config.yaml` (override via `DASH_CONFIG=/path/to/config.yaml`).
- Key sections:
  - `dashboard`: `listen_host`, `listen_port`, `refresh_seconds`
  - `discovery`: `mode` (`bounded_sweep`), `max_sweep_hosts`, `ping_timeout_ms`, `default_ports`
  - `tailscale`: `exe` (path or name on `PATH`)
  - `devices`: known devices with `match.ip`/`match.mac`, `ports`, `notes`
- To disable caching globally, set `NETDASH_DISABLE_CACHE=1` (affects discovery and `/api/status` payload cache).

## Endpoints
- `/` - dashboard UI (HTML/JS).
- `/api/status` - discovery, NextDNS, Tailscale, host info.
- `/api/health` - basic probe `{ok, ts, cache_enabled}`.

## Notes
- ARP/neighbor cache + bounded ping sweep drive discovery; if ICMP is blocked, ARP responders are still included after sweep refresh.
- Tailscale peers are filtered to hide Mullvad catalog unless actively used as exit.
- UI auto-refreshes every 30s; use "Refresh now" for manual updates.
