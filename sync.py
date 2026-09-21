"""搬运引擎：在同一个 OpenList 实例里，把一条路线的源目录定时搬运到目标目录。

设计要点：
  - 单一 OpenList 连接（同一实例内不同网盘之间搬运），用服务端 /api/fs/copy 或 /api/fs/move，不占本地带宽。
  - 按「文件名 + 大小」判断是否需要搬运，已同步的自动跳过。
  - 递归遍历、自动建目录、move 模式可清理已搬空的源目录。
  - 支持按**单个文件大小范围**筛选：不在范围内的文件一律留在源目录不动（copy/move 都不动），
    因此不会误删任何东西；判定粒度是单个文件，目录本身不参与筛选。
"""

import logging
import re

from openlist_client import OpenListClient

logger = logging.getLogger("opsync")

MB = 1024 * 1024
GB = 1024 * MB

# 支持 B / KB / MB / GB / TB，可写单个字母（k/m/g/t）；不带单位按 MB
_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)(i?b)?$")


def parse_size(text, default: float = 0.0) -> float:
    """把 "5GB" / "500MB" / "1.5g" / "1024" / "250B" 解析成字节数；无法解析返回 default。

    规则：
      - 带单位按单位算：250B = 250 字节，500MB = 500*1024*1024 字节
      - 不带单位（裸数字）按 **MB** 处理，方便在界面上直接填 "500"
      - 单位大小写不敏感，支持 1KB / 1KiB / 1K 等写法
    """
    if text is None:
        return default
    if isinstance(text, (int, float)):
        return float(text) * MB
    s = str(text).strip().lower().replace(" ", "")
    if not s:
        return default
    m = _SIZE_RE.match(s)
    if not m:
        return default
    num = float(m.group(1))
    prefix = m.group(2)
    unit_b = m.group(3)
    if not prefix:
        if unit_b:                      # 明确写了 B：按字节，如 250B
            return num
        return num * MB                 # 裸数字：按 MB
    factors = {"k": 1024, "m": MB, "g": GB, "t": 1024 ** 4}
    return num * factors[prefix]


def size_range(route: dict):
    """取路线的文件大小范围 (min_bytes, max_bytes)，0 表示该侧不限。

    兼容两种字段写法：
      - min_size / max_size：带单位字符串，如 "5GB"
      - min_size_mb / max_size_mb：数字，单位 MB
    都没写 = 不限制，行为与加此功能前完全一致。
    """
    if route.get("min_size") not in (None, ""):
        lo = parse_size(route["min_size"], 0.0)
    else:
        lo = parse_size(route.get("min_size_mb"), 0.0)
    if route.get("max_size") not in (None, ""):
        hi = parse_size(route["max_size"], 0.0)
    else:
        hi = parse_size(route.get("max_size_mb"), 0.0)
    return lo, hi


def in_range(size, lo, hi) -> bool:
    """size 是否落在 [lo, hi] 内；0 表示该侧不限。"""
    size = size or 0
    if lo and size < lo:
        return False
    if hi and size > hi:
        return False
    return True


def describe_range(route: dict) -> str:
    """给人看的大小范围描述，用于日志。"""
    lo, hi = size_range(route)
    if not lo and not hi:
        return "不限"

    def fmt(n):
        n = float(n)
        for unit, factor in (("TB", 1024 ** 4), ("GB", GB), ("MB", MB)):
            if n >= factor:
                v = n / factor
                return f"{v:g}{unit}"
        return f"{int(n)}B"

    if lo and hi:
        return f"{fmt(lo)} ~ {fmt(hi)}"
    if hi:
        return f"≤ {fmt(hi)}"
    return f"≥ {fmt(lo)}"


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
        lo, hi = size_range(route)
        stats = {"copied": 0, "skipped": 0, "failed": 0, "removed": 0, "out_of_range": 0}

        logger.info("路线[%s] 开始：%s -> %s（模式=%s，大小范围=%s）",
                    route.get("name", "?"), src, dst, mode, describe_range(route))
        self._walk(src, dst, mode, overwrite, delete_empty, stats, lo, hi, is_root=True)
        logger.info("路线[%s] 完成：已搬运=%d，跳过=%d，超出范围=%d，失败=%d，删除源=%d",
                    route.get("name", "?"), stats["copied"], stats["skipped"],
                    stats["out_of_range"], stats["failed"], stats["removed"])
        return stats

    # ------------------------------------------------------------------ 递归
    def _walk(self, src_dir, dst_dir, mode, overwrite, delete_empty, stats, lo, hi, is_root=False):
        self.client.ensure_dir(dst_dir)
        entries = self.client.list_files(src_dir)
        files = [e for e in entries if not e["is_dir"]]
        dirs = [e for e in entries if e["is_dir"]]

        dst_index = {e["name"]: e for e in self.client.list_files(dst_dir)}

        pending = []
        for e in files:
            # 大小筛选：不在范围内的文件原地不动（copy 不复制、move 不搬也不删）
            if not in_range(e.get("size") or 0, lo, hi):
                stats["out_of_range"] += 1
                continue
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
                       mode, overwrite, delete_empty, stats, lo, hi, is_root=False)

        if mode == "move" and not is_root and delete_empty:
            # 源目录里若还有因「超出大小范围」而留下的文件，则不能删（否则等于误删用户数据）
            if not self.client.list_files(src_dir):
                parent = src_dir.rsplit("/", 1)[0] or "/"
                name = src_dir.rsplit("/", 1)[1]
                self.client.remove(parent, [name])
                stats["removed"] += 1
                logger.info("已删除空目录：%s", src_dir)
