"""搬运引擎：把一个 OpenList 挂载点里的东西定时搬到另一个挂载点。

设计要点：
  - 同实例（源/目标指向同一个 OpenList）= 走服务端 /api/fs/copy 或 /api/fs/move，不占本地带宽。
  - 跨实例（两个不同 OpenList）= 边下载边上传流式搬运。
  - 按「文件名 + 大小」判断是否已同步，命中则跳过。
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from openlist_client import OpenListClient

logger = logging.getLogger("taosync")


class SyncEngine:
    def __init__(self, config: dict):
        src_cfg = config["source"]
        dst_cfg = config["target"]
        self.src = OpenListClient(src_cfg["url"], src_cfg["username"], src_cfg["password"])
        self.dst = OpenListClient(dst_cfg["url"], dst_cfg["username"], dst_cfg["password"])

        # 同源（同一地址 + 同一账号）走服务端复制/移动，更高效
        self.same_instance = (
            src_cfg["url"].rstrip("/").lower() == dst_cfg["url"].rstrip("/").lower()
            and src_cfg["username"] == dst_cfg["username"]
        )

        transfer = config.get("transfer", {})
        self.src_root = src_cfg["path"].rstrip("/")
        self.dst_root = dst_cfg["path"].rstrip("/")
        self.mode = transfer.get("mode", "copy")          # copy | move
        self.overwrite = transfer.get("overwrite", False)  # True=强制覆盖已存在文件
        self.concurrency = max(1, int(transfer.get("concurrency", 3)))
        self.delete_empty_dirs = transfer.get("delete_empty_dirs", False)

        # 统计
        self.stats = {"copied": 0, "skipped": 0, "failed": 0, "removed": 0}

    # ------------------------------------------------------------------ 对外
    def run_once(self) -> dict:
        logger.info("开始同步：%s -> %s（模式=%s，同实例=%s）",
                    self.src_root, self.dst_root, self.mode, self.same_instance)
        self.src.login()
        self.dst.login()
        self.stats = {"copied": 0, "skipped": 0, "failed": 0, "removed": 0}

        self._walk(self.src_root, self.dst_root, is_root=True)

        logger.info("本次完成：已搬运=%d，跳过=%d，失败=%d，删除源=%d",
                    self.stats["copied"], self.stats["skipped"],
                    self.stats["failed"], self.stats["removed"])
        return dict(self.stats)

    # ------------------------------------------------------------------ 递归
    def _walk(self, src_dir: str, dst_dir: str, is_root: bool = False) -> None:
        self.dst.ensure_dir(dst_dir)
        entries = self.src.list_files(src_dir)
        files = [e for e in entries if not e["is_dir"]]
        dirs = [e for e in entries if e["is_dir"]]

        # 目标目录快照，用于跳过已同步的文件
        dst_index = {e["name"]: e for e in self.dst.list_files(dst_dir)}

        # 1) 处理当前目录下的文件
        pending = []
        for e in files:
            target = dst_index.get(e["name"])
            if target and not self.overwrite and target.get("size") == e.get("size"):
                self.stats["skipped"] += 1
                continue
            pending.append(e)

        if pending:
            self._transfer_files(src_dir, dst_dir, pending)

        # 2) 递归子目录
        for d in dirs:
            self._walk(f"{src_dir}/{d['name']}", f"{dst_dir}/{d['name']}", is_root=False)

        # 3) move 模式：源目录搬空后清理（不删根目录）
        if self.mode == "move" and not is_root and self.delete_empty_dirs:
            if not self.src.list_files(src_dir):
                parent = src_dir.rsplit("/", 1)[0] or "/"
                name = src_dir.rsplit("/", 1)[1]
                self.src.remove(parent, [name])
                self.stats["removed"] += 1
                logger.info("已删除空目录：%s", src_dir)

    # ----------------------------------------------------- 搬运一批文件
    def _transfer_files(self, src_dir: str, dst_dir: str, entries: list[dict]) -> None:
        if self.same_instance:
            names = [e["name"] for e in entries]
            try:
                if self.mode == "move":
                    self.src.move(src_dir, dst_dir, names)
                    self.stats["removed"] += len(names)
                else:
                    self.src.copy(src_dir, dst_dir, names)
                verb = "移动" if self.mode == "move" else "复制"
                logger.info("%s %d 个条目：%s -> %s", verb, len(names), src_dir, dst_dir)
                self.stats["copied"] += len(names)
            except Exception as exc:  # noqa: BLE001
                logger.error("服务端%s失败 %s: %s", self.mode, src_dir, exc)
                self.stats["failed"] += len(names)
        else:
            # 跨实例：并发流式下载 + 上传
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                list(pool.map(lambda e: self._relay_one(src_dir, dst_dir, e), entries))

    def _relay_one(self, src_dir: str, dst_dir: str, entry: dict) -> None:
        name = entry["name"]
        full_src = f"{src_dir}/{name}"
        try:
            resp = self.src.open_download(full_src)
            self.dst.upload(dst_dir, name, resp.iter_content(1024 * 1024), int(entry.get("size", 0)))
            logger.info("已搬运（跨实例）：%s -> %s/%s", full_src, dst_dir, name)
            self.stats["copied"] += 1
            if self.mode == "move":
                self.src.remove(src_dir, [name])
                self.stats["removed"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("搬运失败 %s: %s", full_src, exc)
            self.stats["failed"] += 1
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
