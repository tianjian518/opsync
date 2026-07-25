#!/usr/bin/env python3
"""opsync —— 同一个 OpenList 里把网盘 A 定时搬到网盘 B（支持多条路线）。

默认以 Web 模式运行（浏览器管理界面，监听 $PORT）。
也可加 --once / --check 走纯命令行（本地调试用）。
"""

import argparse
import logging
import os
import sys

from confighelper import load_config
from openlist_client import OpenListClient
from sync import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("opsync")


def cmd_check(cfg: dict) -> None:
    ol = cfg["openlist"]
    client = OpenListClient(ol["url"], ol["username"], ol["password"])
    client.login()
    logger.info("OpenList 连接成功：%s", ol["url"])
    entries = client.list_files("/")
    logger.info("根目录共 %d 个条目：", len(entries))
    for e in entries:
        logger.info("  - %s  (%s)", e["name"], "目录" if e["is_dir"] else f"{e['size']} B")


def cmd_once(cfg: dict) -> None:
    ol = cfg["openlist"]
    engine = SyncEngine(ol)
    for r in cfg.get("routes", []):
        if r.get("enabled", True):
            engine.run_route(r)


def main() -> None:
    parser = argparse.ArgumentParser(description="通过 OpenList 定时搬运网盘的极简工具")
    parser.add_argument("--config", default=None, help="指定配置文件路径")
    parser.add_argument("--once", action="store_true", help="立即执行所有启用的路线一次")
    parser.add_argument("--check", action="store_true", help="仅测试 OpenList 连接并列出根目录")
    args = parser.parse_args()

    if args.config:
        os.environ["CONFIG_PATH"] = args.config

    if args.check:
        cmd_check(load_config()[0])
        return
    if args.once:
        cmd_once(load_config()[0])
        return

    from web import run

    run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("已手动停止。")
        sys.exit(0)
