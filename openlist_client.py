"""OpenList / AList v3 兼容的极简客户端。

只封装搬运所需的最小 API 集合：
  - 登录拿 token
  - 列目录 / 取文件信息
  - 建目录
  - 服务端复制 / 移动（同实例）
  - 流式下载 / 上传（跨实例兜底）
  - 删除

API 路径与字段名严格对齐 AList v3 / OpenList 官方文档。
"""

from urllib.parse import quote, urljoin, urlparse

import requests


class OpenListError(RuntimeError):
    """OpenList 接口返回非成功 code 时抛出。"""


class OpenListClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.token: str | None = None
        self.session = requests.Session()
        self._netloc = urlparse(self.base_url).netloc

    # ------------------------------------------------------------------ 基础
    def login(self) -> str:
        """登录并缓存 token。"""
        url = f"{self.base_url}/api/auth/login"
        resp = self.session.post(
            url,
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        data = resp.json()
        if data.get("code") != 200:
            raise OpenListError(f"登录失败: {data.get('message')}")
        self.token = data["data"]["token"]
        self.session.headers.update({"Authorization": self.token})
        return self.token

    def _api(self, method: str, path: str, **kwargs):
        """带自动登录 / 401 重登的 JSON 接口调用。"""
        if self.token is None:
            self.login()
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if resp.status_code == 401:
            self.login()
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise OpenListError(f"{path} 返回非 JSON: {resp.text[:200]}")
        if data.get("code") != 200:
            raise OpenListError(f"接口 {path} 失败: code={data.get('code')} msg={data.get('message')}")
        return data

    # ------------------------------------------------------------------ 目录
    def list_files(self, path: str, password: str = "") -> list[dict]:
        """列出某目录下的全部条目（自动翻页）。"""
        entries: list[dict] = []
        page = 1
        while True:
            data = self._api(
                "POST",
                "/api/fs/list",
                json={"path": path, "password": password, "page": page, "per_page": 500, "refresh": False},
            )
            content = data["data"]["content"]
            entries.extend(content)
            # 单页未拉满即视为最后一页（兼容无 has_more 字段的老版本）
            if len(content) < 500:
                break
            page += 1
        return entries

    def get_file_info(self, path: str) -> dict | None:
        """获取单个条目信息；不存在时返回 None。"""
        try:
            data = self._api("POST", "/api/fs/get", json={"path": path, "password": ""})
            return data["data"]
        except OpenListError:
            return None

    def mkdir(self, path: str) -> None:
        """创建单个目录（不存在才建，已存在则忽略）。"""
        if self.get_file_info(path) is not None:
            return
        self._api("POST", "/api/fs/mkdir", json={"path": path})

    def ensure_dir(self, full_dir: str) -> None:
        """逐级创建目录树。"""
        parts = [p for p in full_dir.split("/") if p]
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}"
            self.mkdir(cur)

    # ----------------------------------------------------- 同实例：复制 / 移动
    def copy(self, src_dir: str, dst_dir: str, names: list[str]) -> None:
        self._api("POST", "/api/fs/copy", json={"src_dir": src_dir, "dst_dir": dst_dir, "names": names})

    def move(self, src_dir: str, dst_dir: str, names: list[str]) -> None:
        self._api("POST", "/api/fs/move", json={"src_dir": src_dir, "dst_dir": dst_dir, "names": names})

    def remove(self, dir_path: str, names: list[str]) -> None:
        """删除 dir_path 下的若干条目（文件或目录）。"""
        self._api("POST", "/api/fs/remove", json={"dir": dir_path, "names": names})

    # --------------------------------------------------- 跨实例：流式下载 / 上传
    def open_download(self, path: str):
        """打开一个流式下载响应（自动处理 302 跳转，且不向第三方泄露 token）。"""
        data = self._api("POST", "/api/fs/get", json={"path": path, "password": ""})
        info = data["data"]
        raw = info["raw_url"]
        sign = info.get("sign", "")
        if raw.startswith("/"):
            raw = self.base_url + raw
        if sign and "sign=" not in raw:
            raw += ("&" if "?" in raw else "?") + "sign=" + quote(sign)

        resp = self._safe_get(raw)
        return resp

    def _safe_get(self, url: str):
        """GET 并处理重定向：仅当跳转目标仍是本实例时才携带 Authorization，避免 token 泄漏。"""
        resp = self._get_one(url)
        while resp.status_code in (301, 302, 307, 308):
            loc = resp.headers.get("Location")
            if not loc:
                break
            loc = urljoin(url, loc)
            resp = self._get_one(loc)
            url = loc
        resp.raise_for_status()
        return resp

    def _get_one(self, url: str):
        same_host = urlparse(url).netloc == self._netloc
        req = self.session if same_host else requests
        return req.get(url, stream=True, allow_redirects=False, timeout=self.timeout)

    def upload(self, dst_dir: str, name: str, data_iter, size: int) -> None:
        """向目标实例流式上传一个文件。"""
        full_path = f"{dst_dir.rstrip('/')}/{name}"
        filepath = quote(full_path, safe="/")
        headers = {
            "File-Path": filepath,
            "As-Task": "false",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        }
        url = f"{self.base_url}/api/fs/put"
        resp = self.session.put(url, data=data_iter, headers=headers, timeout=self.timeout)
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise OpenListError(f"上传 {full_path} 返回非 JSON: {resp.text[:200]}")
        if data.get("code") != 200:
            raise OpenListError(f"上传 {full_path} 失败: code={data.get('code')} msg={data.get('message')}")
