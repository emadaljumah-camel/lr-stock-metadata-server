from __future__ import annotations

import os
from pathlib import Path

import yaml


CONFIG_PATH = Path(os.environ.get("LR_METADATA_CONFIG", Path(__file__).with_name("config.yaml"))).resolve()


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Config file must contain a YAML mapping: {CONFIG_PATH}")
    return data


CONFIG = _load_config()


def _config_value(path: str):
    value = CONFIG
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def setting(path: str, default=None, *env_names: str):
    value = _config_value(path)
    if value not in (None, ""):
        return value
    for name in env_names:
        env_value = os.environ.get(name)
        if env_value not in (None, ""):
            return env_value
    return default


def int_setting(path: str, default: int, *env_names: str) -> int:
    try:
        return int(setting(path, default, *env_names))
    except (TypeError, ValueError):
        return default


HOST = str(setting("server.host", "127.0.0.1", "LR_METADATA_HOST"))
PORT = int_setting("server.port", 8008, "LR_METADATA_PORT")
UPLOAD_DIR = Path(str(setting("server.upload_dir", "_uploads", "LR_METADATA_UPLOAD_DIR"))).resolve()
JOBS_DIR = Path(str(setting("server.jobs_dir", "_jobs", "LR_METADATA_JOBS_DIR"))).resolve()
MAX_JOB_WORKERS = max(1, int_setting("server.max_job_workers", 1, "LR_METADATA_MAX_JOB_WORKERS"))
