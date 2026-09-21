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
import time


class OpenListError(RuntimeError):
    """OpenList 接口返回非成功 code 时抛出。"""


# 各存储后端在「目录/文件不存在」时给的说法不太统一，这里统一识别
_NOT_FOUND_HINTS = (
    "object not found",
    "not found",
    "no such file",
    "does not exist",
    "not exist",
    "object_not_found",
    "failed get objs",
)


def _is_not_found(exc: Exception) -> bool:
    """判断异常是否表示「目标不存在」。"""
    msg = str(exc).lower()
    return any(h in msg for h in _NOT_FOUND_HINTS)


class OpenListClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.token: str | None = None
        self.session = requests.Session()
        self._netloc = urlparse(self.base_url).netloc
        self._html_prefixes = ("<!doctype", "<html", "<!DOCTYPE", "<?xml")

    # ------------------------------------------------------------------ 唤醒
    def _looks_html(self, text: str) -> bool:
        head = (text or "").lstrip()[:20].lower()
        return any(head.startswith(p) for p in self._html_prefixes)

    def _wake_once(self) -> None:
        """GET 根路径以触发休眠中的 HF Space 唤醒（忽略结果）。

        休眠的 HF Space 在首个请求时会返回 HTML warming 页，反复访问根路径
        可促使其实例被拉起，之后真正的 API 调用才会返回 JSON。
        """
        try:
            self.session.get(self.base_url + "/", timeout=min(self.timeout, 30), allow_redirects=True)
        except requests.RequestException:
            pass

    def _with_wake_retry(self, do_request, url: str, max_retries: int = 3):
        """执行请求并解析 JSON，带「唤醒 + 重试」逻辑。

        - 网络层异常（休眠实例连接被拒 / 超时）会触发唤醒后重试；
        - 返回非 JSON（HTML warming 页 / 代理拦截页）同样触发唤醒后重试；
        - 最终失败抛出含 HTTP 状态与响应片段的清晰错误，便于排查。
        """
        last_err: str = "未知错误"
        for attempt in range(max_retries + 1):
            try:
                resp = do_request()
            except requests.RequestException as exc:
                last_err = f"网络层异常: {exc}"
                self._wake_once()
                if attempt < max_retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise OpenListError(
                    f"请求 {url} 失败（{last_err}）。\n"
                    f"若目标是 HF Space，请确认：① 该 Space 已启动；② 已在 Settings 开启 Internet access；③ URL 正确。"
                )
            try:
                return resp.json()
            except ValueError:
                last_err = f"返回非 JSON（HTTP {resp.status_code}）"
                # 休眠实例常返回 HTML warming 页，唤醒后重试
                self._wake_once()
                if attempt < max_retries:
                    time.sleep(3 * (attempt + 1))
                    continue
                snippet = (resp.text or "").strip()[:200]
                hint = "响应为 HTML，大概率是休眠的 HF Space 返回的 warming 页面或代理拦截页。" \
                    if self._looks_html(resp.text or "") else ""
                raise OpenListError(
                    f"{url} {last_err}。{hint}\n"
                    f"若目标是 HF Space，请确认：① 实例已唤醒/启动；② 已开启 Internet access；③ URL/端口正确。\n"
                    f"响应片段: {snippet!r}"
                )
        # 理论上不会到达
        raise OpenListError(f"请求 {url} 异常: {last_err}")

    # ------------------------------------------------------------------ 基础
    def login(self) -> str:
        """登录并缓存 token。"""
        url = f"{self.base_url}/api/auth/login"
        payload = {"username": self.username, "password": self.password}

        def do():
            return self.session.post(url, json=payload, timeout=self.timeout)

        data = self._with_wake_retry(do, url)
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

        def do():
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code == 401:
                self.login()
                return self.session.request(method, url, timeout=self.timeout, **kwargs)
            return resp

        data = self._with_wake_retry(do, url)
        if data.get("code") != 200:
            raise OpenListError(f"接口 {path} 失败: code={data.get('code')} msg={data.get('message')}")
        return data

    # ------------------------------------------------------------------ 目录
    def list_files(self, path: str, password: str = "") -> list[dict]:
        """列出某目录下的全部条目（自动翻页）。

        目录不存在时返回空列表（而不是抛错）。这一点很关键：
        搬运时目标侧的目录是「边遍历边创建」的，父目录刚建好、子目录还没建，
        此时去列它就会拿到 code=500 object not found —— 若直接抛错，
        整条路线的递归会在第一个子目录处中断。
        """
        entries: list[dict] = []
        page = 1
        while True:
            try:
                data = self._api(
                    "POST",
                    "/api/fs/list",
                    json={"path": path, "password": password, "page": page, "per_page": 500, "refresh": False},
                )
            except OpenListError as exc:
                if _is_not_found(exc):
                    return []          # 目录还不存在 → 视为空目录
                raise
            # 某些存储后端会返回 data=null 或 content=null，都不能当成可迭代对象
            payload = data.get("data") or {}
            content = payload.get("content") or []
            if not isinstance(content, list):
                content = []
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
        """创建单个目录（已存在则直接返回，不报错）。"""
        try:
            if self.get_file_info(path) is not None:
                return
        except OpenListError:
            pass          # 查不到信息就当作不存在，直接尝试创建
        try:
            self._api("POST", "/api/fs/mkdir", json={"path": path})
        except OpenListError as exc:
            if _is_not_found(exc):
                return    # 并发/竞态下别人已建好，视为成功
            raise

    def ensure_dir(self, full_dir: str) -> None:
        """逐级创建目录树。

        建完再复查一遍：网盘（尤其刚被写入过父目录时）对 list/get 可能有
        短暂的缓存不一致，导致 mkdir 明明成功了、紧接着列目录却仍报 not found。
        复查不过就重建一次，避免把「目录没建出来」带到后面的 copy 里。
        """
        parts = [p for p in full_dir.split("/") if p]
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}"
            self.mkdir(cur)
        # 复查：确认最深一级确实存在（目录树已逐级创建，只需查最后一级）
        try:
            if self.get_file_info(full_dir) is None:
                self._api("POST", "/api/fs/mkdir", json={"path": full_dir})
        except OpenListError:
            pass

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
            snippet = (resp.text or "").strip()[:200]
            hint = "响应为 HTML，大概率是休眠的 HF Space warming 页或代理拦截页。" \
                if self._looks_html(resp.text or "") else ""
            raise OpenListError(
                f"上传 {full_path} 返回非 JSON（HTTP {resp.status_code}）。{hint}\n"
                f"若目标是 HF Space，请确认已唤醒、已开启 Internet access、URL 正确。\n"
                f"响应片段: {snippet!r}"
            )
        if data.get("code") != 200:
            raise OpenListError(f"上传 {full_path} 失败: code={data.get('code')} msg={data.get('message')}")
