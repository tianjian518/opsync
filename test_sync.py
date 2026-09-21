"""不依赖真实 OpenList 的逻辑自测（单连接 + 路线模型）。

用内存版假客户端模拟 OpenList 文件系统，覆盖：
  - 递归遍历子目录
  - 已同步文件被跳过
  - copy 模式保留源
  - move 模式删除源并清理空目录
  - 按单个文件大小范围筛选（范围外文件留在源目录，copy/move 都不动）
  - 大小字符串解析（GB/MB/KB/B、裸数字按 MB）
  - 多级子目录递归搬运，以及「单个子目录出错不拖垮整条路线」
  - list_files 对「目录不存在 / content 为 null」的容错
"""

import os
import unittest
from unittest.mock import patch

import confighelper
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


class TestNestedDirsAndRobustness(unittest.TestCase):
    """回归：子目录必须被搬，且单个子目录出错不能拖垮整条路线。

    线上问题：源目录里有多个电影子目录，结果只搬了根目录的散装视频，
    子目录全没反应。原因是目标侧子目录还没建，list_files 拿到
    code=500 object not found，_api 抛错 → 整条递归在第一个子目录就中断了。
    """

    def setUp(self):
        # 根目录 2 个散装文件 + 2 个子目录，每个子目录里有自己的文件
        FakeClient.store = {
            "/photos": {"is_dir": True, "size": 0},
            "/photos/loose1.mkv": {"is_dir": False, "size": 100},
            "/photos/loose2.mkv": {"is_dir": False, "size": 200},
            "/photos/子目录A": {"is_dir": True, "size": 0},
            "/photos/子目录A/a1.mkv": {"is_dir": False, "size": 300},
            "/photos/子目录A/a2.mkv": {"is_dir": False, "size": 400},
            "/photos/子目录B": {"is_dir": True, "size": 0},
            "/photos/子目录B/b1.mkv": {"is_dir": False, "size": 500},
        }

    @patch("sync.OpenListClient", FakeClient)
    def test_all_nested_dirs_are_copied(self):
        engine = sync.SyncEngine(OL)
        stats = engine.run_route(build_route("r", "copy"))
        self.assertEqual(stats["copied"], 5)      # 2 散装 + 2 + 1
        self.assertEqual(stats["failed"], 0)
        for p in ("/backup/loose1.mkv", "/backup/loose2.mkv",
                  "/backup/子目录A/a1.mkv", "/backup/子目录A/a2.mkv",
                  "/backup/子目录B/b1.mkv"):
            self.assertIn(p, FakeClient.store)

    @patch("sync.OpenListClient", FakeClient)
    def test_nested_dirs_respect_size_filter(self):
        """大小筛选要作用到子目录里的文件，而不只是根目录。"""
        engine = sync.SyncEngine(OL)
        stats = engine.run_route(build_route("r", "copy", min_size="250B", max_size="450B"))
        self.assertEqual(stats["copied"], 2)       # a1(300) + a2(400)
        self.assertEqual(stats["out_of_range"], 3)
        self.assertIn("/backup/子目录A/a1.mkv", FakeClient.store)
        self.assertNotIn("/backup/子目录B/b1.mkv", FakeClient.store)

    @patch("sync.OpenListClient", FakeClient)
    def test_one_broken_subdir_does_not_stop_others(self):
        """某个子目录炸了，兄弟目录仍要继续搬（这是线上那个 bug 的核心）。"""
        real = FakeClient.list_files

        def flaky(self, path):
            if path.endswith("/子目录A"):
                raise RuntimeError("模拟接口抽风")
            return real(self, path)

        with patch.object(FakeClient, "list_files", flaky):
            engine = sync.SyncEngine(OL)
            stats = engine.run_route(build_route("r", "copy"))
        # 子目录B 不能因为 子目录A 失败而被跳过
        self.assertIn("/backup/子目录B/b1.mkv", FakeClient.store)
        self.assertEqual(stats["copied"], 3)       # 2 散装 + b1
        self.assertEqual(stats["failed"], 1)       # 子目录A 计入失败

    @patch("sync.OpenListClient", FakeClient)
    def test_entries_without_name_ignored(self):
        """接口返回残缺条目时不能崩。"""
        real = FakeClient.list_files

        def messy(self, path):
            return real(self, path) + [{"is_dir": False}, None]

        with patch.object(FakeClient, "list_files", messy):
            engine = sync.SyncEngine(OL)
            stats = engine.run_route(build_route("r", "copy"))
        self.assertEqual(stats["copied"], 5)
        self.assertEqual(stats["failed"], 0)


class TestListFilesTolerance(unittest.TestCase):
    """list_files 对「目录不存在 / content 为 null」必须返回 []，而不是抛错。"""

    def _client(self, responses):
        from openlist_client import OpenListClient

        c = OpenListClient("http://x", "u", "p")
        c.token = "t"
        calls = {"i": 0}

        def fake_api(method, path, **kwargs):
            r = responses[min(calls["i"], len(responses) - 1)]
            calls["i"] += 1
            if isinstance(r, Exception):
                raise r
            return r

        c._api = fake_api
        return c

    def test_not_found_returns_empty(self):
        from openlist_client import OpenListError

        c = self._client([OpenListError("接口 /api/fs/list 失败: code=500 msg=failed get objs: "
                                       "failed get dir: object not found")])
        self.assertEqual(c.list_files("/不存在"), [])

    def test_null_content_returns_empty(self):
        c = self._client([{"code": 200, "data": {"content": None}}])
        self.assertEqual(c.list_files("/空目录"), [])

    def test_null_data_returns_empty(self):
        c = self._client([{"code": 200, "data": None}])
        self.assertEqual(c.list_files("/怪目录"), [])

    def test_normal_listing(self):
        c = self._client([{"code": 200, "data": {"content": [
            {"name": "a.mkv", "is_dir": False, "size": 1},
        ]}}])
        self.assertEqual(len(c.list_files("/x")), 1)

    def test_other_errors_still_raise(self):
        from openlist_client import OpenListError

        c = self._client([OpenListError("接口 /api/fs/list 失败: code=401 msg=token expired")])
        with self.assertRaises(OpenListError):
            c.list_files("/x")

    def test_pagination(self):
        full = [{"name": f"f{i}", "is_dir": False, "size": 1} for i in range(500)]
        c = self._client([
            {"code": 200, "data": {"content": full}},
            {"code": 200, "data": {"content": [{"name": "last", "is_dir": False, "size": 1}]}},
        ])
        self.assertEqual(len(c.list_files("/many")), 501)


class TestConfigRoutesNormalized(unittest.TestCase):
    """回归：账号没有 routes 键时，读出来必须补成 []。

    背景：TOML 无法表达「空表数组」（序列化时会跳过空列表），所以一个还没有
    任何路线的账号，落盘的文件里就没有 routes 键。前端如果拿到 undefined，
    「+ 添加路线」会因为数组没写回 c.routes 而表现为点了没反应。
    """

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.toml")

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self):
        old = os.environ.get("CONFIG_PATH")
        os.environ["CONFIG_PATH"] = self.path
        try:
            return confighelper.load_config()[0]
        finally:
            if old is None:
                os.environ.pop("CONFIG_PATH", None)
            else:
                os.environ["CONFIG_PATH"] = old

    def test_missing_routes_key_becomes_empty_list(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('[[openlists]]\nid = "c1"\nname = "C1"\ntested_ok = true\n')
        cfg = self._load()
        self.assertEqual(cfg["openlists"][0]["routes"], [])

    def test_empty_routes_survive_round_trip(self):
        """空 routes 写盘再读回，仍应是 []（而不是缺键）。"""
        cfg = {"openlists": [{"id": "c1", "name": "C1", "routes": []}]}
        confighelper.write_toml(self.path, cfg)
        self.assertEqual(self._load()["openlists"][0]["routes"], [])

    def test_existing_routes_preserved(self):
        cfg = {"openlists": [{"id": "c1", "name": "C1", "routes": [
            {"name": "r1", "min_size": "1GB", "max_size": "10GB"},
        ]}]}
        confighelper.write_toml(self.path, cfg)
        loaded = self._load()
        self.assertEqual(len(loaded["openlists"][0]["routes"]), 1)
        self.assertEqual(loaded["openlists"][0]["routes"][0]["max_size"], "10GB")

    def test_no_openlists_key(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[logging]\nlevel = \"INFO\"\n")
        self.assertEqual(self._load()["openlists"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
