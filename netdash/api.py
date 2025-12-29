import argparse
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request

from netdash.config import APP_TITLE, cache_disabled, load_config, neighbor_snapshot_disabled
from netdash.discovery import discover
from netdash.nextdns import nextdns_status
from netdash.tailscale import tailscale_status

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title=APP_TITLE)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "ts": int(__import__("time").time()), "cache_enabled": not cache_disabled()}


@app.get("/api/status")
async def api_status() -> JSONResponse:
    now = __import__("time").time()
    cfg = load_config()
    test_url = (cfg.get("nextdns", {}) or {}).get("test_url", "https://test.nextdns.io")
    ts_cfg = cfg.get("tailscale", {}) or {}
    ts_exe = ts_cfg.get("exe", None)

    results = await __import__("asyncio").gather(
        discover(cfg),
        __import__("asyncio").to_thread(nextdns_status, test_url),
        tailscale_status(ts_exe),
    )

    payload = {
        "ts": int(now),
        "discovery": results[0],
        "nextdns": results[1],
        "tailscale": results[2],
        "meta": {
            "cache_enabled": not cache_disabled(),
            "neighbor_snapshot_enabled": not neighbor_snapshot_disabled(),
        },
        "host": {"os": __import__("platform").platform(), "hostname": __import__("socket").gethostname()},
    }
    return JSONResponse(payload)


@app.get("/", response_class=templates.TemplateResponse)
def dashboard(request: Request) -> Any:
    return templates.TemplateResponse("index.html", {"request": request, "title": APP_TITLE})


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
