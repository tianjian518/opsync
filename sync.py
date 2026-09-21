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
import threading

from openlist_client import OpenListClient

logger = logging.getLogger("opsync")


class RouteCancelled(Exception):
    """路线被用户强制终止时抛出。

    刻意不在搬运途中打断 —— 只在「一个目录处理完、准备进入下一个动作」的安全点
    检查。这样不会留下半截的复制请求，最多是少搬几个条目，可重复执行补上。
    """


class CancelToken:
    """协作式取消令牌。

    - cancel() 由 Web 层调用（可能来自另一个线程），置位后：
        - 下一次 check() 抛 RouteCancelled
        - 若正在等 HTTP 响应，wait() 会被立即唤醒，sleep_until() 会提前返回，
          从而不必等当前请求超时就能退出
    - 线程安全：内部用 threading.Event
    """

    def __init__(self):
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def check(self) -> None:
        """到达安全点时调用；已取消则抛出 RouteCancelled。"""
        if self._ev.is_set():
            raise RouteCancelled("已被用户强制终止")

    def wait(self, timeout: float) -> bool:
        """可中断地等待 timeout 秒。

        返回 True 表示「被取消」，调用方应尽快退出。
        """
        return self._ev.wait(timeout)

    def sleep(self, seconds: float) -> None:
        """可中断 sleep：被取消时立即返回（由调用方的 check() 抛出）。"""
        self._ev.wait(seconds)

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


def _valid_entries(entries) -> list:
    """过滤掉接口返回的残缺条目（None、非 dict、没有 name 的），避免遍历时崩掉。"""
    if not entries:
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("name")]


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
    def __init__(self, openlist_cfg: dict, token: "CancelToken | None" = None):
        self.client = OpenListClient(
            openlist_cfg["url"], openlist_cfg["username"], openlist_cfg["password"]
        )
        # 取消令牌：接上后才能被「强制终止」。为 None 时退化为不可取消（行为同旧版）。
        self.token = token or CancelToken()
        # 把令牌挂到 HTTP 客户端上，让正在等待的请求也能被立刻唤醒
        self.client.cancel_token = self.token
        self.client.login()

    def run_route(self, route: dict) -> dict:
        src = str(route["src_path"]).rstrip("/")
        dst = str(route["dst_path"]).rstrip("/")
        mode = route.get("mode", "copy")            # copy | move
        overwrite = route.get("overwrite", False)
        delete_empty = route.get("delete_empty_dirs", False)
        lo, hi = size_range(route)
        stats = {"copied": 0, "skipped": 0, "failed": 0, "removed": 0,
                 "out_of_range": 0, "cancelled": False}

        logger.info("路线[%s] 开始：%s -> %s（模式=%s，大小范围=%s）",
                    route.get("name", "?"), src, dst, mode, describe_range(route))
        try:
            self._walk(src, dst, mode, overwrite, delete_empty, stats, lo, hi, is_root=True)
        except RouteCancelled:
            # 被强制终止不是错误：把已完成的部分如实返回，并在结果里标记出来
            stats["cancelled"] = True
            logger.warning("路线[%s] 已被强制终止（已搬运=%d，跳过=%d，超出范围=%d，失败=%d）",
                           route.get("name", "?"), stats["copied"], stats["skipped"],
                           stats["out_of_range"], stats["failed"])
            return stats
        if self.token.cancelled:
            stats["cancelled"] = True
            logger.warning("路线[%s] 已被强制终止（已搬运=%d）", route.get("name", "?"), stats["copied"])
            return stats
        logger.info("路线[%s] 完成：已搬运=%d，跳过=%d，超出范围=%d，失败=%d，删除源=%d",
                    route.get("name", "?"), stats["copied"], stats["skipped"],
                    stats["out_of_range"], stats["failed"], stats["removed"])
        return stats

    # ------------------------------------------------------------------ 递归
    def _walk(self, src_dir, dst_dir, mode, overwrite, delete_empty, stats, lo, hi, is_root=False):
        # 每进入一个目录先检查一次：已在搬运途中被打断的动作不追回，
        # 但可以保证「每搬完一个目录就有一个安全退出点」，不会继续往下扩散。
        self.token.check()
        self.client.ensure_dir(dst_dir)
        entries = _valid_entries(self.client.list_files(src_dir))
        files = [e for e in entries if not e["is_dir"]]
        dirs = [e for e in entries if e["is_dir"]]

        # 目标目录刚建好，这里再列一次拿「已有哪些文件」用于跳过判断；
        # list_files 对不存在的目录返回 []，所以不会因为目标侧滞后而炸掉。
        dst_index = {e["name"]: e for e in _valid_entries(self.client.list_files(dst_dir))}
        logger.debug("扫描 %s：文件 %d 个，子目录 %d 个（目标已有 %d 项）",
                     src_dir, len(files), len(dirs), len(dst_index))

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

        # 准备发起复制/移动前再检查一次：宁可不搬，也不要在被终止后还发出新请求
        if pending:
            self.token.check()

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
            # 单个子目录出错不能拖垮整条路线：否则第一个失败的子目录会让
            # 后面所有兄弟目录都不再被处理（表现就是「只搬了根目录的散装文件，
            # 子目录全没反应」）。这里兜住异常、计入 failed，继续下一个。
            try:
                self._walk(f"{src_dir}/{d['name']}", f"{dst_dir}/{d['name']}",
                           mode, overwrite, delete_empty, stats, lo, hi, is_root=False)
            except RouteCancelled:
                # 强制终止必须一路向上传出去，不能被这里的容错吃掉 ——
                # 否则会「记一次 failed 然后继续搬下一个目录」，终止就失效了。
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("子目录处理失败 %s -> %s/%s: %s",
                             f"{src_dir}/{d['name']}", dst_dir, d["name"], exc)
                stats["failed"] += 1

        if mode == "move" and not is_root and delete_empty:
            # 源目录里若还有因「超出大小范围」而留下的文件，则不能删（否则等于误删用户数据）
            if not self.client.list_files(src_dir):
                parent = src_dir.rsplit("/", 1)[0] or "/"
                name = src_dir.rsplit("/", 1)[1]
                self.client.remove(parent, [name])
                stats["removed"] += 1
                logger.info("已删除空目录：%s", src_dir)
