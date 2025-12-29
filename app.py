"""Application entry point for the Home Network Dashboard."""

import argparse
import os

from netdash.api import app  # noqa: F401
from netdash.config import load_config


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
