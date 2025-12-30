# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NetDash is a FastAPI-powered home network dashboard that discovers devices on LANs/Tailscale networks, runs bounded subnet sweeps with port checks, detects NextDNS usage, and displays Tailscale status in a single-page UI.

## Commands

### Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run
```bash
python app.py                                    # Uses config.yaml defaults
python app.py --no-cache --host 0.0.0.0 --port 8080
uvicorn netdash.api:app --reload --host 0.0.0.0 --port 8080  # Dev with hot reload
```

### Configuration
- Default: `config.yaml` in project root
- Override: `DASH_CONFIG=/path/to/config.yaml`
- Disable caching: `NETDASH_DISABLE_CACHE=1`
- Disable neighbor snapshot: `NETDASH_DISABLE_NEIGHBOR_SNAPSHOT=1`

### Testing
No automated test suite yet. Manual smoke test: start app and load dashboard UI. Future tests go in `tests/` using `pytest`.

## Architecture

### Entry Points
- **`app.py`**: CLI entry point, parses args, starts Uvicorn server
- **`netdash/api.py`**: FastAPI app with routes:
  - `GET /` - Dashboard UI
  - `GET /api/status` - Main API (discovery + NextDNS + Tailscale)
  - `GET /api/health` - Health check

### Core Backend Modules

**`netdash/discovery.py`** (~1,000 lines) - Network discovery engine:
- `discover()` - Main async function running bounded subnet sweep
- `get_neighbors()` - Cross-platform ARP/neighbor cache parsing (Windows/Unix/Linux)
- `ping()` - Platform-specific async ping
- `tcp_check()` - Non-blocking TCP port probe
- `local_ipv4_networks()` - Find local networks (excludes tunnels)
- Uses semaphores for concurrency limiting (reduced on macOS to avoid FD exhaustion)

**`netdash/tailscale.py`** - Executes `tailscale status --json`, filters Mullvad nodes

**`netdash/nextdns.py`** - HTTP GET to test.nextdns.io, detects NextDNS status

**`netdash/config.py`** - YAML config loading with environment variable overrides

**`netdash/utils.py`** - Async subprocess execution, posix_spawn optimization, MAC normalization

### Frontend (`netdash/static/` + `netdash/templates/`)
- **`templates/index.html`** - Jinja2 template with grid layout
- **`static/app.js`** - Main logic, fetches `/api/status` every 30s
- **`static/api.js`** - API fetch with timeout handling
- **`static/ui.js`** - UI helper functions
- **`static/visualization.js`** - Network graph visualization

### Data Flow
```
Browser (app.js) → /api/status → api.py
  → parallel: discover(), nextdns_status(), tailscale_status()
  → JSON response → Render tables, stats, visualization
```

### Caching
- Discovery results cached in-memory with network fingerprinting
- Fingerprint (interface IPs, SSID, gateway) triggers cache invalidation
- Rate limiting: 30s minimum between refreshes (unless `?fresh=1`)
- API query params: `?fresh=1` (force refresh), `?light=1` (skip discovery)

## Coding Conventions
- Python 3, PEP 8, 4-space indentation, ~100-120 char lines
- Type hints and dataclasses for device models
- Private helpers prefixed with `_`
- Shell/network functions should be small and log return codes/stderr
- Platform-specific flags noted in code comments
