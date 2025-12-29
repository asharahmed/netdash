import os
from typing import Any, Dict

import yaml

CONFIG_PATH = os.environ.get("DASH_CONFIG", "./config.yaml")
APP_TITLE = "Home Network Dashboard"


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cache_disabled() -> bool:
    flag = os.environ.get("NETDASH_DISABLE_CACHE", "")
    return flag.lower() in ("1", "true", "yes", "on")


def neighbor_snapshot_disabled() -> bool:
    flag = os.environ.get("NETDASH_DISABLE_NEIGHBOR_SNAPSHOT", "")
    return flag.lower() in ("1", "true", "yes", "on")


def concurrency_from_env(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        parsed = int(val)
        if parsed <= 0:
            return default
        return parsed
    except ValueError:
        return default
