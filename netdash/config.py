import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

CONFIG_PATH = os.environ.get("DASH_CONFIG", "./config.yaml")
APP_TITLE = "Home Network Dashboard"

_config_cache: Optional[Dict[str, Any]] = None
_config_warnings: List[str] = []


def _validate_config(cfg: Dict[str, Any]) -> List[str]:
    """Validate config and return list of warnings."""
    warnings = []

    # Check for required sections
    if not cfg.get("devices"):
        warnings.append("No 'devices' section in config - no known devices will be tracked")

    # Validate devices
    devices = cfg.get("devices", []) or []
    for i, dev in enumerate(devices):
        if not isinstance(dev, dict):
            warnings.append(f"Device {i} is not a valid object")
            continue
        if not dev.get("name"):
            warnings.append(f"Device {i} has no 'name' field")
        match = dev.get("match", {}) or {}
        if not match.get("ip") and not match.get("mac"):
            name = dev.get("name", f"device {i}")
            warnings.append(f"Device '{name}' has no IP or MAC to match against")

    # Validate discovery settings
    discovery = cfg.get("discovery", {}) or {}
    max_sweep = discovery.get("max_sweep_hosts", 256)
    if isinstance(max_sweep, int) and max_sweep > 1024:
        warnings.append(f"max_sweep_hosts={max_sweep} is very high - may cause slow discovery")

    return warnings


def load_config() -> Dict[str, Any]:
    """Load and validate config from YAML file."""
    global _config_cache, _config_warnings

    if _config_cache is not None:
        return _config_cache

    config_path = Path(CONFIG_PATH)

    if not config_path.exists():
        print(f"Warning: Config file not found at {CONFIG_PATH}, using defaults", file=sys.stderr)
        _config_cache = {}
        return _config_cache

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        print(f"Error: Invalid YAML in config file: {e}", file=sys.stderr)
        cfg = {}
    except Exception as e:
        print(f"Error: Could not read config file: {e}", file=sys.stderr)
        cfg = {}

    # Validate and store warnings
    _config_warnings = _validate_config(cfg)
    for warning in _config_warnings:
        print(f"Config warning: {warning}", file=sys.stderr)

    _config_cache = cfg
    return cfg


def get_config_warnings() -> List[str]:
    """Return any warnings from config validation."""
    return _config_warnings.copy()


def reload_config() -> Dict[str, Any]:
    """Force reload of config file."""
    global _config_cache, _config_warnings
    _config_cache = None
    _config_warnings = []
    return load_config()


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
