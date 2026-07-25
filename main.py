#!/usr/bin/env python3
"""opsync —— 通过 OpenList 把网盘 A 定时搬到网盘 B 的极简工具。

用法：
  python main.py                 # 按配置持续运行（schedule）
  python main.py --once          # 只跑一次同步（常用于测试）
  python main.py --check         # 仅测试连接并列出源目录内容
  python main.py --config x.toml # 指定配置文件

配置优先级：命令行 > 环境变量(TAOSYNC_*) > config.toml > 默认值。
这样在 Hugging Face Space 里只需设置 Variables，无需改动任何文件。
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import tomllib

from sync import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("opsync")


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for key in ("source", "target"):
        if key not in cfg:
            raise SystemExit(f"配置文件缺少必填段：[{key}]")
    if "path" not in cfg["source"] or "path" not in cfg["target"]:
        raise SystemExit("source / target 都必须包含 path 字段")
    return cfg


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def apply_env(cfg: dict) -> None:
    """用 TAOSYNC_* 环境变量覆盖配置（用于抱脸 Space Variables 等场景）。"""
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


def setup_file_logging(cfg: dict) -> None:
    log_file = cfg.get("logging", {}).get("file")
    level = cfg.get("logging", {}).get("level", "INFO")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)


def cmd_check(engine: SyncEngine) -> None:
    engine.src.login()
    engine.dst.login()
    logger.info("源连接成功：%s", engine.src_root)
    logger.info("目标连接成功：%s", engine.dst_root)
    entries = engine.src.list_files(engine.src_root)
    logger.info("源目录 %s 下共 %d 个条目：", engine.src_root, len(entries))
    for e in entries:
        kind = "目录" if e["is_dir"] else f"{e['size']} B"
        logger.info("  - %s  (%s)", e["name"], kind)


def run_schedule(cfg: dict, engine: SyncEngine) -> None:
    sched = cfg.get("schedule", {})
    mode = sched.get("type", "interval")

    if mode == "once":
        engine.run_once()
        return

    if mode == "daily":
        run_at = sched.get("run_at", "03:00")
        hh, mm = (int(x) for x in run_at.split(":"))
        while True:
            engine.run_once()
            now = datetime.now()
            nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            wait = (nxt - now).total_seconds()
            logger.info("下次执行时间：%s（%.0f 秒后）", nxt.strftime("%Y-%m-%d %H:%M:%S"), wait)
            time.sleep(wait)
        return

    # 默认 interval
    minutes = float(sched.get("interval_minutes", 30))
    while True:
        engine.run_once()
        logger.info("等待 %s 分钟后继续……", minutes)
        time.sleep(minutes * 60)


def main() -> None:
    # 抱脸等平台把持久化配置放在 /data；存在则优先使用
    default_config = "/data/config.toml" if os.path.exists("/data/config.toml") else "config.toml"
    parser = argparse.ArgumentParser(description="通过 OpenList 定时搬运网盘的极简工具")
    parser.add_argument("--config", default=default_config, help="配置文件路径（默认 config.toml 或 /data/config.toml）")
    parser.add_argument("--once", action="store_true", help="只执行一次同步")
    parser.add_argument("--check", action="store_true", help="仅测试连接并列出源目录")
    args = parser.parse_args()

    if not Path(args.config).exists():
        raise SystemExit(f"找不到配置文件：{args.config}")
    cfg = load_config(args.config)
    apply_env(cfg)
    setup_file_logging(cfg)

    engine = SyncEngine(cfg)

    if args.check:
        cmd_check(engine)
        return
    if args.once:
        engine.run_once()
        return
    run_schedule(cfg, engine)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("已手动停止。")
        sys.exit(0)
