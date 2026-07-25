"""搬运引擎：在同一个 OpenList 实例里，把一条路线的源目录定时搬运到目标目录。

设计要点：
  - 单一 OpenList 连接（同一实例内不同网盘之间搬运），用服务端 /api/fs/copy 或 /api/fs/move，不占本地带宽。
  - 按「文件名 + 大小」判断是否需要搬运，已同步的自动跳过。
  - 递归遍历、自动建目录、move 模式可清理已搬空的源目录。
"""

import logging

from openlist_client import OpenListClient

logger = logging.getLogger("opsync")


class SyncEngine:
    def __init__(self, openlist_cfg: dict):
        self.client = OpenListClient(
            openlist_cfg["url"], openlist_cfg["username"], openlist_cfg["password"]
        )
        self.client.login()

    def run_route(self, route: dict) -> dict:
        src = str(route["src_path"]).rstrip("/")
        dst = str(route["dst_path"]).rstrip("/")
        mode = route.get("mode", "copy")            # copy | move
        overwrite = route.get("overwrite", False)
        delete_empty = route.get("delete_empty_dirs", False)
        stats = {"copied": 0, "skipped": 0, "failed": 0, "removed": 0}

        logger.info("路线[%s] 开始：%s -> %s（模式=%s）",
                    route.get("name", "?"), src, dst, mode)
        self._walk(src, dst, mode, overwrite, delete_empty, stats, is_root=True)
        logger.info("路线[%s] 完成：已搬运=%d，跳过=%d，失败=%d，删除源=%d",
                    route.get("name", "?"), stats["copied"], stats["skipped"],
                    stats["failed"], stats["removed"])
        return stats

    # ------------------------------------------------------------------ 递归
    def _walk(self, src_dir, dst_dir, mode, overwrite, delete_empty, stats, is_root=False):
        self.client.ensure_dir(dst_dir)
        entries = self.client.list_files(src_dir)
        files = [e for e in entries if not e["is_dir"]]
        dirs = [e for e in entries if e["is_dir"]]

        dst_index = {e["name"]: e for e in self.client.list_files(dst_dir)}

        pending = []
        for e in files:
            target = dst_index.get(e["name"])
            if target and not overwrite and target.get("size") == e.get("size"):
                stats["skipped"] += 1
                continue
            pending.append(e)

        if pending:
            names = [e["name"] for e in pending]
            try:
                if mode == "move":
                    self.client.move(src_dir, dst_dir, names)
                    stats["removed"] += len(names)
                else:
                    self.client.copy(src_dir, dst_dir, names)
                stats["copied"] += len(names)
                logger.info("%s %d 个条目：%s -> %s",
                            "移动" if mode == "move" else "复制", len(names), src_dir, dst_dir)
            except Exception as exc:  # noqa: BLE001
                logger.error("搬运失败 %s -> %s: %s", src_dir, dst_dir, exc)
                stats["failed"] += len(names)

        for d in dirs:
            self._walk(f"{src_dir}/{d['name']}", f"{dst_dir}/{d['name']}",
                       mode, overwrite, delete_empty, stats, is_root=False)

        if mode == "move" and not is_root and delete_empty:
            if not self.client.list_files(src_dir):
                parent = src_dir.rsplit("/", 1)[0] or "/"
                name = src_dir.rsplit("/", 1)[1]
                self.client.remove(parent, [name])
                stats["removed"] += 1
                logger.info("已删除空目录：%s", src_dir)
