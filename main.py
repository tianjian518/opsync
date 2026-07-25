#!/usr/bin/env python3
"""opsync —— 通过 OpenList 把网盘 A 定时搬到网盘 B。

默认以 Web 模式运行（提供浏览器管理界面，监听 $PORT）。
也可加 --once / --check 走纯命令行（本地调试用）。
"""

import argparse
import logging
import os
import sys

from confighelper import load_config
from sync import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("opsync")


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


def main() -> None:
    parser = argparse.ArgumentParser(description="通过 OpenList 定时搬运网盘的极简工具")
    parser.add_argument("--config", default=None, help="指定配置文件路径")
    parser.add_argument("--once", action="store_true", help="只执行一次同步")
    parser.add_argument("--check", action="store_true", help="仅测试连接并列出源目录")
    parser.add_argument("--web", action="store_true", help="运行 Web 界面（默认）")
    args = parser.parse_args()

    if args.config:
        os.environ["CONFIG_PATH"] = args.config

    if args.check:
        cfg, _ = load_config()
        cmd_check(SyncEngine(cfg))
        return
    if args.once:
        cfg, _ = load_config()
        SyncEngine(cfg).run_once()
        return

    # 默认 Web 模式
    from web import run

    run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("已手动停止。")
        sys.exit(0)
