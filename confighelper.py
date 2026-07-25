"""配置加载与合并（供 CLI 与 Web 共用）。"""

import os
import tomllib


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def apply_env(cfg: dict) -> None:
    """用 TAOSYNC_* 环境变量覆盖配置。"""
    m = {
        "TAOSYNC_SOURCE_URL": ("source", "url"),
        "TAOSYNC_SOURCE_USERNAME": ("source", "username"),
        "TAOSYNC_SOURCE_PASSWORD": ("source", "password"),
        "TAOSYNC_SOURCE_PATH": ("source", "path"),
        "TAOSYNC_TARGET_URL": ("target", "url"),
        "TAOSYNC_TARGET_USERNAME": ("target", "username"),
        "TAOSYNC_TARGET_PASSWORD": ("target", "password"),
        "TAOSYNC_TARGET_PATH": ("target", "path"),
        "TAOSYNC_MODE": ("transfer", "mode"),
        "TAOSYNC_OVERWRITE": ("transfer", "overwrite"),
        "TAOSYNC_CONCURRENCY": ("transfer", "concurrency"),
        "TAOSYNC_DELETE_EMPTY_DIRS": ("transfer", "delete_empty_dirs"),
        "TAOSYNC_SCHEDULE_TYPE": ("schedule", "type"),
        "TAOSYNC_INTERVAL_MINUTES": ("schedule", "interval_minutes"),
        "TAOSYNC_RUN_AT": ("schedule", "run_at"),
        "TAOSYNC_LOG_LEVEL": ("logging", "level"),
        "TAOSYNC_LOG_FILE": ("logging", "file"),
    }
    for env, (sec, key) in m.items():
        if env not in os.environ:
            continue
        val = os.environ[env]
        if key in ("overwrite", "delete_empty_dirs"):
            val = _as_bool(val)
        elif key in ("concurrency", "interval_minutes"):
            try:
                val = float(val) if key == "interval_minutes" else int(val)
            except ValueError:
                continue
        cfg.setdefault(sec, {})[key] = val


def resolve_config_path() -> str:
    if os.environ.get("CONFIG_PATH"):
        return os.environ["CONFIG_PATH"]
    if os.path.exists("/data/config.toml"):
        return "/data/config.toml"
    return "config.toml"


def save_path() -> str:
    """配置保存位置：优先 /data/config.toml（抱脸等持久化目录），否则本地。"""
    if os.path.exists("/data") or os.path.exists("/data/config.toml"):
        return "/data/config.toml"
    return "config.toml"


def load_config():
    """返回 (config, path)。"""
    path = resolve_config_path()
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for key in ("source", "target"):
        if key not in cfg:
            raise SystemExit(f"配置文件缺少必填段：[{key}]")
    if "path" not in cfg["source"] or "path" not in cfg["target"]:
        raise SystemExit("source / target 都必须包含 path 字段")
    apply_env(cfg)
    return cfg, path
