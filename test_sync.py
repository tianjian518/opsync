"""不依赖真实 OpenList 的逻辑自测。

用内存版假客户端模拟 OpenList 文件系统，覆盖：
  - 递归遍历子目录
  - 已同步文件被跳过
  - copy 模式保留源
  - move 模式删除源并清理空目录
"""

import unittest
from unittest.mock import patch

import sync


class FakeClient:
    """内存版 OpenList 客户端，所有实例共享同一棵「文件系统」，模拟同实例。"""

    store: dict = {}  # 全路径 -> {"is_dir": bool, "size": int}

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

    def open_download(self, path):
        raise NotImplementedError("测试未覆盖跨实例")

    def upload(self, dst_dir, name, data_iter, size):
        raise NotImplementedError("测试未覆盖跨实例")


def build_config(mode, delete_empty_dirs=False):
    return {
        "source": {"url": "http://x", "username": "u", "password": "p", "path": "/photos"},
        "target": {"url": "http://x", "username": "u", "password": "p", "path": "/backup"},
        "transfer": {"mode": mode, "overwrite": False, "delete_empty_dirs": delete_empty_dirs},
    }


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
        engine = sync.SyncEngine(build_config("copy"))
        stats = engine.run_once()
        self.assertEqual(stats["copied"], 4)
        self.assertEqual(stats["skipped"], 0)
        # 源应保留
        self.assertIn("/photos/a.jpg", FakeClient.store)

        # 再跑一次：全部命中，应被跳过
        stats2 = engine.run_once()
        self.assertEqual(stats2["copied"], 0)
        self.assertEqual(stats2["skipped"], 4)

    @patch("sync.OpenListClient", FakeClient)
    def test_move_removes_source_and_empty_dirs(self):
        engine = sync.SyncEngine(build_config("move", delete_empty_dirs=True))
        stats = engine.run_once()
        self.assertEqual(stats["copied"], 4)
        self.assertEqual(stats["removed"], 4 + 1)  # 4 文件 + 1 个空目录
        # 源子树应被清空（根 /photos 仍在，因为是根目录）
        self.assertNotIn("/photos/a.jpg", FakeClient.store)
        self.assertNotIn("/photos/2023", FakeClient.store)
        # 目标应齐全
        self.assertIn("/backup/a.jpg", FakeClient.store)
        self.assertIn("/backup/2023/c.jpg", FakeClient.store)


if __name__ == "__main__":
    unittest.main(verbosity=2)
