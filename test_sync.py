"""不依赖真实 OpenList 的逻辑自测（单连接 + 路线模型）。

用内存版假客户端模拟 OpenList 文件系统，覆盖：
  - 递归遍历子目录
  - 已同步文件被跳过
  - copy 模式保留源
  - move 模式删除源并清理空目录
  - 按单个文件大小范围筛选（范围外文件留在源目录，copy/move 都不动）
  - 大小字符串解析（GB/MB/KB/B、裸数字按 MB）
"""

import unittest
from unittest.mock import patch

import sync


class FakeClient:
    """内存版 OpenList 客户端，所有实例共享同一棵「文件系统」。"""

    store: dict = {}

    def __init__(self, base_url, username, password):
        self.base_url = base_url
        self.username = username

    def login(self):
        return "fake-token"

    def list_files(self, path):
        path = path.rstrip("/") or "/"
        out = []
        for p in sorted(self.store):
            if p == path or not p.startswith(path + "/"):
                continue
            rest = p[len(path) + 1:]
            if "/" in rest:
                continue
            entry = self.store[p]
            out.append({"name": rest, "is_dir": entry["is_dir"], "size": entry.get("size", 0), "path": p})
        return out

    def get_file_info(self, path):
        path = path.rstrip("/") or "/"
        return self.store.get(path)

    def mkdir(self, path):
        if path not in self.store:
            self.store[path] = {"is_dir": True, "size": 0}

    def ensure_dir(self, full_dir):
        parts = [p for p in full_dir.split("/") if p]
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}"
            self.mkdir(cur)

    def copy(self, src_dir, dst_dir, names):
        self.ensure_dir(dst_dir)
        for n in names:
            sp, dp = f"{src_dir}/{n}", f"{dst_dir}/{n}"
            self.store[dp] = dict(self.store[sp])

    def move(self, src_dir, dst_dir, names):
        self.copy(src_dir, dst_dir, names)
        for n in names:
            self.store.pop(f"{src_dir}/{n}", None)

    def remove(self, dir_path, names):
        for n in names:
            self.store.pop(f"{dir_path}/{n}", None)


OL = {"url": "http://x", "username": "u", "password": "p"}


def build_route(name, mode, src="/photos", dst="/backup", delete_empty=False,
                min_size=None, max_size=None):
    route = {
        "name": name, "src_path": src, "dst_path": dst, "mode": mode,
        "enabled": True, "overwrite": False, "delete_empty_dirs": delete_empty,
        "schedule_type": "interval", "interval_minutes": 30,
    }
    if min_size is not None:
        route["min_size"] = min_size
    if max_size is not None:
        route["max_size"] = max_size
    return route


def seed():
    FakeClient.store = {
        "/photos": {"is_dir": True, "size": 0},
        "/photos/a.jpg": {"is_dir": False, "size": 100},
        "/photos/b.jpg": {"is_dir": False, "size": 200},
        "/photos/2023": {"is_dir": True, "size": 0},
        "/photos/2023/c.jpg": {"is_dir": False, "size": 300},
        "/photos/2023/d.jpg": {"is_dir": False, "size": 400},
    }


class TestSync(unittest.TestCase):
    def setUp(self):
        seed()

    @patch("sync.OpenListClient", FakeClient)
    def test_copy_recursive_and_skip(self):
        engine = sync.SyncEngine(OL)
        stats = engine.run_route(build_route("r1", "copy"))
        self.assertEqual(stats["copied"], 4)
        self.assertEqual(stats["skipped"], 0)
        # 源应保留
        self.assertIn("/photos/a.jpg", FakeClient.store)
        # 再跑一次：全部命中，应被跳过
        stats2 = engine.run_route(build_route("r1", "copy"))
        self.assertEqual(stats2["copied"], 0)
        self.assertEqual(stats2["skipped"], 4)

    @patch("sync.OpenListClient", FakeClient)
    def test_move_removes_source_and_empty_dirs(self):
        engine = sync.SyncEngine(OL)
        stats = engine.run_route(build_route("r2", "move", delete_empty=True))
        self.assertEqual(stats["copied"], 4)
        self.assertEqual(stats["removed"], 4 + 1)  # 4 文件 + 1 个空目录
        self.assertNotIn("/photos/a.jpg", FakeClient.store)
        self.assertNotIn("/photos/2023", FakeClient.store)
        self.assertIn("/backup/a.jpg", FakeClient.store)
        self.assertIn("/backup/2023/c.jpg", FakeClient.store)


class TestSizeFilter(unittest.TestCase):
    """大小筛选测试：用 MB 量级的假文件，避免和真实单位混淆。"""

    MB = 1024 * 1024

    def setUp(self):
        # a=100MB  b=200MB  c=300MB  d=400MB
        FakeClient.store = {
            "/photos": {"is_dir": True, "size": 0},
            "/photos/a.mkv": {"is_dir": False, "size": 100 * self.MB},
            "/photos/b.mkv": {"is_dir": False, "size": 200 * self.MB},
            "/photos/2023": {"is_dir": True, "size": 0},
            "/photos/2023/c.mkv": {"is_dir": False, "size": 300 * self.MB},
            "/photos/2023/d.mkv": {"is_dir": False, "size": 400 * self.MB},
        }

    @patch("sync.OpenListClient", FakeClient)
    def test_max_size_keeps_small_only(self):
        """只搬 <=250MB 的文件：a(100)/b(200) 搬走，c(300)/d(400) 留在源目录。"""
        engine = sync.SyncEngine(OL)
        stats = engine.run_route(build_route("r", "copy", max_size="250MB"))
        self.assertEqual(stats["copied"], 2)
        self.assertEqual(stats["out_of_range"], 2)
        self.assertIn("/photos/a.mkv", FakeClient.store)
        self.assertIn("/photos/b.mkv", FakeClient.store)
        self.assertIn("/backup/a.mkv", FakeClient.store)
        self.assertIn("/backup/b.mkv", FakeClient.store)
        # 超范围文件既不复制也不删除
        self.assertIn("/photos/2023/c.mkv", FakeClient.store)
        self.assertIn("/photos/2023/d.mkv", FakeClient.store)
        self.assertNotIn("/backup/2023/c.mkv", FakeClient.store)

    @patch("sync.OpenListClient", FakeClient)
    def test_min_size_keeps_large_only(self):
        engine = sync.SyncEngine(OL)
        stats = engine.run_route(build_route("r", "copy", min_size="250MB"))
        self.assertEqual(stats["copied"], 2)
        self.assertEqual(stats["out_of_range"], 2)
        self.assertNotIn("/backup/a.mkv", FakeClient.store)
        self.assertIn("/backup/2023/c.mkv", FakeClient.store)

    @patch("sync.OpenListClient", FakeClient)
    def test_both_bounds(self):
        """区间 150MB~350MB：只有 b(200)/c(300) 符合。"""
        engine = sync.SyncEngine(OL)
        stats = engine.run_route(build_route("r", "copy", min_size="150MB", max_size="350MB"))
        self.assertEqual(stats["copied"], 2)
        self.assertEqual(stats["out_of_range"], 2)
        self.assertIn("/backup/b.mkv", FakeClient.store)
        self.assertIn("/backup/2023/c.mkv", FakeClient.store)
        self.assertNotIn("/backup/a.mkv", FakeClient.store)
        self.assertNotIn("/backup/2023/d.mkv", FakeClient.store)

    @patch("sync.OpenListClient", FakeClient)
    def test_move_mode_never_deletes_out_of_range(self):
        """move 模式下超范围的文件必须留在源目录，不能被删。"""
        engine = sync.SyncEngine(OL)
        stats = engine.run_route(build_route("r", "move", max_size="250MB"))
        self.assertEqual(stats["copied"], 2)
        self.assertEqual(stats["removed"], 2)            # 只删了 2 个真正搬走的
        self.assertIn("/photos/2023/c.mkv", FakeClient.store)
        self.assertIn("/photos/2023/d.mkv", FakeClient.store)
        self.assertIn("/backup/a.mkv", FakeClient.store)
        self.assertNotIn("/photos/a.mkv", FakeClient.store)

    @patch("sync.OpenListClient", FakeClient)
    def test_move_does_not_delete_nonempty_dir(self):
        """源目录里还有未搬运的文件时应保留该目录，即使开了 delete_empty_dirs。"""
        engine = sync.SyncEngine(OL)
        engine.run_route(build_route("r", "move", max_size="250MB", delete_empty=True))
        self.assertIn("/photos/2023", FakeClient.store)   # 里面还有 c/d，不能删
        self.assertIn("/photos/2023/c.mkv", FakeClient.store)
        self.assertIn("/photos/2023/d.mkv", FakeClient.store)

    @patch("sync.OpenListClient", FakeClient)
    def test_empty_range_behaves_like_before(self):
        engine = sync.SyncEngine(OL)
        stats = engine.run_route(build_route("r", "copy", min_size="", max_size=""))
        self.assertEqual(stats["copied"], 4)
        self.assertEqual(stats["out_of_range"], 0)

    @patch("sync.OpenListClient", FakeClient)
    def test_no_size_fields_is_unlimited(self):
        """老配置里没有 min_size/max_size 字段时，必须等同「不限制」，不能回归。"""
        engine = sync.SyncEngine(OL)
        route = build_route("r", "copy")
        route.pop("min_size", None)
        route.pop("max_size", None)
        stats = engine.run_route(route)
        self.assertEqual(stats["copied"], 4)
        self.assertEqual(stats["out_of_range"], 0)


class TestParseSize(unittest.TestCase):
    def test_units(self):
        self.assertEqual(sync.parse_size("1GB"), 1024 ** 3)
        self.assertEqual(sync.parse_size("500MB"), 500 * 1024 ** 2)
        self.assertEqual(sync.parse_size("2tb"), 2 * 1024 ** 4)
        self.assertEqual(sync.parse_size("1024"), 1024 * sync.MB)  # 裸数字按 MB
        self.assertEqual(sync.parse_size("250B"), 250)             # 带 B 按字节
        self.assertEqual(sync.parse_size("1.5g"), int(1.5 * 1024 ** 3))
        self.assertEqual(sync.parse_size("1KB"), 1024)
        self.assertEqual(sync.parse_size("1KiB"), 1024)
        self.assertEqual(sync.parse_size(""), 0)
        self.assertEqual(sync.parse_size(None), 0)
        self.assertEqual(sync.parse_size("乱填"), 0)

    def test_size_range_variants(self):
        self.assertEqual(sync.size_range({"min_size": "1GB", "max_size": "5GB"}),
                         (1024 ** 3, 5 * 1024 ** 3))
        self.assertEqual(sync.size_range({"min_size_mb": 100, "max_size_mb": 200}),
                         (100 * 1024 ** 2, 200 * 1024 ** 2))

    def test_in_range(self):
        self.assertTrue(sync.in_range(10, 0, 0))
        self.assertTrue(sync.in_range(10, 0, 100))
        self.assertFalse(sync.in_range(200, 0, 100))
        self.assertTrue(sync.in_range(200, 100, 0))
        self.assertFalse(sync.in_range(50, 100, 0))
        self.assertIn("不限", sync.describe_range({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
