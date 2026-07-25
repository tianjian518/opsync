#!/usr/bin/env python3
"""opsync —— 多个 OpenList 账号，各自把网盘 A 定时搬到网盘 B（每条账号下可建多条路线）。

默认以 Web 模式运行（浏览器管理界面，每个账号一个子页面，监听 $PORT）。
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


def _configured(ol: dict) -> bool:
    return bool(ol.get("url") and ol.get("username") and ol.get("password")
                and ol.get("password") != "your_password"
                and "127.0.0.1" not in str(ol.get("url", ""))
                and ol.get("tested_ok", False))


def cmd_check(cfg: dict) -> None:
    for ol in cfg.get("openlists", []):
        try:
            client = OpenListClient(ol["url"], ol["username"], ol["password"])
            client.login()
            logger.info("OpenList[%s] 连接成功：%s", ol.get("name"), ol["url"])
            for e in client.list_files("/"):
                logger.info("  - %s  (%s)", e["name"], "目录" if e["is_dir"] else f"{e['size']} B")
        except Exception as exc:
            logger.error("OpenList[%s] 连接失败：%s", ol.get("name"), exc)


def cmd_once(cfg: dict) -> None:
    for ol in cfg.get("openlists", []):
        if not _configured(ol):
            logger.warning("账号[%s] 未配置或未通过测试，跳过。", ol.get("name"))
            continue
        engine = SyncEngine(ol)
        for r in ol.get("routes", []):
            if r.get("enabled", True):
                engine.run_route(r)


def main() -> None:
    parser = argparse.ArgumentParser(description="通过 OpenList 定时搬运网盘的极简工具（多账号）")
    parser.add_argument("--config", default=None, help="指定配置文件路径")
    parser.add_argument("--once", action="store_true", help="立即执行所有已配置账号的启用路线一次")
    parser.add_argument("--check", action="store_true", help="仅测试各 OpenList 连接并列出根目录")
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
