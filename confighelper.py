"""配置加载与合并（CLI 与 Web 共用）。

新模型：
  [openlist]  单一 OpenList 连接（同一实例里不同网盘之间搬运）
  [[routes]]  多条搬运路线，每条有 src_path / dst_path / mode / schedule
"""

import os
import tomllib


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def apply_env(cfg: dict) -> None:
    """用 TAOSYNC_* 环境变量覆盖 OpenList 连接（路线仍建议用网页配置）。"""
    m = {
        "TAOSYNC_URL": ("openlist", "url"),
        "TAOSYNC_USERNAME": ("openlist", "username"),
        "TAOSYNC_PASSWORD": ("openlist", "password"),
    }
    for env, (sec, key) in m.items():
        if env not in os.environ:
            continue
        cfg.setdefault(sec, {})[key] = os.environ[env]


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
    if "openlist" not in cfg:
        raise SystemExit("配置文件缺少必填段：[openlist]")
    apply_env(cfg)
    return cfg, path
