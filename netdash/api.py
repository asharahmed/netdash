import argparse
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request

from netdash.config import APP_TITLE, cache_disabled, get_config_warnings, load_config, neighbor_snapshot_disabled
from netdash.discovery import (
    build_known_stub,
    discover,
    get_cached_discovery,
    is_discovery_rate_limited,
    kick_discovery,
)
from netdash.nextdns import nextdns_status
from netdash.tailscale import tailscale_status

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ASSET_VERSION = int((BASE_DIR / "static" / "app.js").stat().st_mtime)

app = FastAPI(title=APP_TITLE)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "ts": int(__import__("time").time()), "cache_enabled": not cache_disabled()}


@app.get("/api/status")
async def api_status(request: Request) -> JSONResponse:
    now = __import__("time").time()
    cfg = load_config()
    test_url = (cfg.get("nextdns", {}) or {}).get("test_url", "https://test.nextdns.io")
    ts_cfg = cfg.get("tailscale", {}) or {}
    ts_exe = ts_cfg.get("exe", None)
    force_refresh = request.query_params.get("fresh", "").lower() in ("1", "true", "yes", "on")
    light = request.query_params.get("light", "").lower() in ("1", "true", "yes", "on")

    disc_result = {}
    discovery_stale = False
    if not light:
        if force_refresh:
            disc_result = await discover(cfg, force_refresh=True)
        else:
            disc_result = get_cached_discovery()
            discovery_stale = not is_discovery_rate_limited(30)
            kick_discovery(cfg, force_refresh=False, min_interval_s=30, fast=True)
            if not disc_result.get("known_devices"):
                disc_result = {
                    "networks": [],
                    "neighbors_count": 0,
                    "mode": (cfg.get("discovery", {}) or {}).get("mode", "bounded_sweep"),
                    "known_devices": build_known_stub(cfg),
                    "discovered_devices": [],
                    "meta": {"completed_at": 0},
                }

    results = await __import__("asyncio").gather(
        __import__("asyncio").to_thread(nextdns_status, test_url),
        tailscale_status(ts_exe),
    )

    payload = {
        "ts": int(now),
        "discovery": disc_result or {},
        "nextdns": results[0],
        "tailscale": results[1],
        "meta": {
            "cache_enabled": not cache_disabled(),
            "neighbor_snapshot_enabled": not neighbor_snapshot_disabled(),
            "cache_forced": force_refresh,
            "discovery_timeout": disc_result.get("meta", {}).get("error") == "timeout",
            "discovery_stale": discovery_stale,
            "light": light,
            "config_warnings": get_config_warnings() or None,
        },
        "host": {"os": __import__("platform").platform(), "hostname": __import__("socket").gethostname()},
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/", response_class=templates.TemplateResponse)
def dashboard(request: Request) -> Any:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": APP_TITLE,
            "asset_version": ASSET_VERSION,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Home Network Dashboard")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable in-process API/discovery caching for this run",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Override listen host (else uses dashboard.listen_host or 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override listen port (else uses dashboard.listen_port or 8080)",
    )
    args = parser.parse_args()

    if args.no_cache:
        os.environ["NETDASH_DISABLE_CACHE"] = "1"

    cfg = load_config()
    dcfg = cfg.get("dashboard", {}) or {}
    host = args.host or dcfg.get("listen_host", "0.0.0.0")
    port = int(args.port or dcfg.get("listen_port", 8080))

    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
