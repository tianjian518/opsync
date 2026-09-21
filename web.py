"""opsync 的 Web 管理界面 + 后台调度器（多 OpenList 账号，各账号并发搬运）。

流程：
  - 顶部标签页，每个 OpenList 账号一个子页面（可添加多个）。
  - 每个子页面：填账号密码 → 保存（持久化，刷新不丢）→ 连接测试 →
    测试通过后浏览选择源/目标目录 → 建多条搬运路线。
  - 调度器遍历所有账号的所有路线，用全局线程池并发执行（各账号互不影响）。
容器监听 $PORT（抱脸注入，默认 7860）。配置保存在 /data/config.toml（持久化）。
"""

import datetime
import logging
import os
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, make_response, request
from confighelper import load_config, save_path, write_toml
from openlist_client import OpenListClient
from sync import CancelToken, SyncEngine

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 7860))

_state = {"running": False}
_route_results = {}
_running = set()
_running_lock = threading.Lock()
_next = {}
_exec = ThreadPoolExecutor(max_workers=6)
# 正在执行的路线 -> 它的取消令牌。用于「强制终止」。
_cancels: dict[str, CancelToken] = {}
# 已记录过「用户点了终止」的 key，避免终止瞬间的结果被随后覆盖成普通失败
_cancelled_keys: set[str] = set()
_log_lock = threading.Lock()
_log_lines: list[str] = []


class _BufferHandler(logging.Handler):
    def emit(self, record):
        line = self.format(record)
        with _log_lock:
            _log_lines.append(line)
            if len(_log_lines) > 2000:
                del _log_lines[: len(_log_lines) - 2000]


_bh = _BufferHandler()
_bh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_bh)
_log = logging.getLogger("opsync")


def is_configured(ol: dict) -> bool:
    if not isinstance(ol, dict):
        return False
    if not ol.get("url") or not ol.get("username"):
        return False
    if ol.get("password") in (None, "", "your_password"):
        return False
    if "127.0.0.1" in str(ol.get("url", "")):
        return False
    return True


def _now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _key(conn_id: str, r: dict) -> str:
    return f"{conn_id}::{r.get('name') or '?'}"


def run_route_safe(ol: dict, r: dict, conn_id: str) -> None:
    key = _key(conn_id, r)
    token = CancelToken()
    with _running_lock:
        if key in _running:
            return
        _running.add(key)
        _cancels[key] = token
        _cancelled_keys.discard(key)
        _state["running"] = True
    try:
        stats = SyncEngine(ol, token=token).run_route(r)
        with _running_lock:
            was_cancelled = key in _cancelled_keys
        if stats.get("cancelled") or was_cancelled:
            _route_results[key] = {"last_run": _now_str(), "stats": stats,
                                   "error": None, "cancelled": True}
        else:
            _route_results[key] = {"last_run": _now_str(), "stats": stats, "error": None}
    except Exception as exc:  # noqa: BLE001
        _log.exception("账号[%s] 路线[%s] 执行失败", conn_id, r.get("name"))
        _route_results[key] = {"last_run": _now_str(), "stats": None,
                               "error": f"执行出错：{exc}"}
    finally:
        with _running_lock:
            _running.discard(key)
            _cancels.pop(key, None)
            _state["running"] = bool(_running)
            cancelled = key in _cancelled_keys
        if cancelled:
            _log.info("账号[%s] 路线[%s] 已停止", conn_id, r.get("name"))


def compute_next(r: dict, now: datetime.datetime):
    st = r.get("schedule_type", "interval")
    if st == "once":
        return now + datetime.timedelta(days=365)
    if st == "daily":
        run_at = str(r.get("run_at", "03:00"))
        hh, mm = (int(x) for x in run_at.split(":"))
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += datetime.timedelta(days=1)
        return nxt
    mins = float(r.get("interval_minutes", 30))
    return now + datetime.timedelta(minutes=mins)


def scheduler_loop() -> None:
    while True:
        try:
            cfg, _ = load_config()
            now = datetime.datetime.now()
            for ol in cfg.get("openlists", []):
                if not (is_configured(ol) and ol.get("tested_ok", False)):
                    continue
                cid = ol.get("id") or ol.get("name") or "?"
                for r in ol.get("routes", []):
                    if not r.get("enabled", True):
                        continue
                    key = _key(cid, r)
                    nxt = _next.get(key)
                    due = (nxt is None) or (now >= nxt)
                    with _running_lock:
                        busy = key in _running
                    if due and not busy:
                        _exec.submit(run_route_safe, ol, r, cid)
                        _next[key] = compute_next(r, now)
            time.sleep(15)
        except Exception:
            _log.exception("调度异常")
            time.sleep(30)


# ------------------------------------------------------------------ 路由
@app.route("/")
def index():
    # 页面里的 JS 是内嵌在 HTML 里的，浏览器一旦按启发式规则缓存了旧页面，
    # 就会出现「服务端已经修好了、用户浏览器还在跑旧 JS」的诡异现象。
    # 这里显式禁用缓存，保证每次刷新都拿到最新界面。
    resp = make_response(HTML.replace("__BUILD__", BUILD_TAG))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/healthz")
def healthz():
    """轻量健康检查端点，供 HF Space 等平台的存活探测使用。"""
    return jsonify({"ok": True, "arch": platform.machine()})


@app.route("/api/connection-test", methods=["POST"])
def api_connection_test():
    d = request.get_json(force=True, silent=True) or {}
    try:
        c = OpenListClient(d["url"], d["username"], d["password"], timeout=90)
        c.login()
        c.list_files("/")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/browse")
def api_browse():
    conn_id = request.args.get("id", "")
    path = request.args.get("path", "/")
    try:
        cfg, _ = load_config()
        ol = next((o for o in cfg.get("openlists", []) if o.get("id") == conn_id), None)
        if ol is None or not is_configured(ol):
            return jsonify({"ok": False, "error": "找不到该账号或连接未配置"})
        client = OpenListClient(ol["url"], ol["username"], ol["password"], timeout=90)
        client.login()
        entries = client.list_files(path)
        dirs = sorted(e["name"] for e in entries if e["is_dir"])
        return jsonify({"ok": True, "path": path, "dirs": dirs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    try:
        cfg, _ = load_config()
    except Exception:
        cfg = {}
    return jsonify({"openlists": cfg.get("openlists", []), "config_path": save_path()})


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True, silent=True) or {}
    openlists = data.get("openlists", [])
    for ol in openlists:
        if not ol.get("url") or not ol.get("username") or not ol.get("password"):
            return jsonify({"ok": False, "error": "每个账号都需要 url / username / password"}), 400
        ol.setdefault("id", "conn-" + str(int(time.time() * 1000)))
        ol.setdefault("routes", [])
    path = save_path()
    write_toml(path, {"openlists": openlists})
    return jsonify({"ok": True, "path": path})


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True, silent=True) or {}
    conn_id = data.get("conn_id")
    name = data.get("name")
    try:
        cfg, _ = load_config()
        started = 0
        for ol in cfg.get("openlists", []):
            if ol.get("id") != conn_id:
                continue
            if not (is_configured(ol) and ol.get("tested_ok", False)):
                return jsonify({"ok": False, "error": "该账号未配置或未通过连接测试"})
            for r in ol.get("routes", []):
                if not r.get("enabled", True):
                    continue
                if name is not None and r.get("name") != name:
                    continue
                key = _key(ol.get("id"), r)
                with _running_lock:
                    busy = key in _running
                if not busy:
                    _exec.submit(run_route_safe, ol, r, ol.get("id"))
                    started += 1
        return jsonify({"ok": True, "started": started})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """强制终止正在执行的路线。

    传 name 只停这一条；不传则停该账号下全部正在跑的路线。
    采用协作式取消：立即置位令牌，正在进行的单个复制/移动请求会跑完，
    之后不再发起新动作，因此不会留下半截的搬运或误删。
    """
    data = request.get_json(force=True, silent=True) or {}
    conn_id = data.get("conn_id")
    name = data.get("name")
    stopped, not_running = [], []
    with _running_lock:
        for key in list(_cancels.keys()):
            k_conn, _, k_name = key.partition("::")
            if conn_id is not None and k_conn != conn_id:
                continue
            if name is not None and k_name != name:
                continue
            _cancels[key].cancel()
            _cancelled_keys.add(key)
            stopped.append(k_name)
        # 该账号（或全部）下当前在跑的 key，用于提示「有没有在跑」
        running_keys = [
            k for k in _running
            if (conn_id is None or k.partition("::")[0] == conn_id)
            and (name is None or k.partition("::")[2] == name)
        ]
    not_running = [k.partition("::")[2] for k in running_keys if k.partition("::")[2] not in stopped]
    if stopped:
        _log.warning("收到强制终止请求：停止 %s", "、".join(stopped))
    return jsonify({"ok": True, "stopped": stopped, "not_running": not_running})


@app.route("/api/status")
def api_status():
    with _running_lock:
        running = bool(_running)
        # 必须带上所属账号：不同账号可以有同名路线，
        # 只返回名字会让「另一个账号正在跑同名路线」被误判成这条也在跑。
        running_keys = sorted(_running)
    return jsonify({"running": running, "routes": _route_results,
                    "running_keys": running_keys})


@app.route("/api/logs")
def api_logs():
    n = int(request.args.get("n", 300))
    with _log_lock:
        lines = _log_lines[-n:]
    return jsonify({"logs": "\n".join(lines)})


# 每次进程启动生成一个构建标记，渲染到页面标题旁。
# 用途：页面显示的 build 与容器启动时间不一致时，就说明浏览器缓存了旧页面
# （服务端已禁用缓存，但用户手上可能有更早打开的标签页）。
BUILD_TAG = datetime.datetime.now().strftime("%m-%d %H:%M:%S")

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>opsync · 多网盘定时搬运</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --side:#0b1220; --fg:#e2e8f0; --mut:#94a3b8;
          --acc:#38bdf8; --ok:#34d399; --err:#f87171; --bd:#334155; --run:#e8a33d;
          --sidew:250px; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 system-ui,Segoe UI,Roboto,"PingFang SC","Microsoft YaHei",sans-serif; }

  /* ---------------- 布局：左栏固定 + 右侧内容区滚动 ---------------- */
  .layout { display:flex; min-height:100vh; }
  .sidebar { width:var(--sidew); flex:0 0 var(--sidew); background:var(--side); border-right:1px solid var(--bd);
             display:flex; flex-direction:column; position:sticky; top:0; height:100vh; }
  .brand { padding:18px 18px 14px; border-bottom:1px solid var(--bd); }
  .brand h1 { margin:0; font-size:17px; letter-spacing:.3px; }
  .brand .sub { color:var(--mut); font-size:11px; margin-top:3px; }
  .nav { flex:1; overflow-y:auto; padding:8px 0 14px; }
  .nav-group { padding:12px 18px 4px; font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.6px; }
  .nav-item { display:flex; align-items:center; gap:9px; padding:9px 18px; color:var(--fg);
              cursor:pointer; border-left:3px solid transparent; font-size:13px; }
  .nav-item:hover { background:#111c33; }
  .nav-item.active { background:#132441; border-left-color:var(--acc); color:var(--acc); font-weight:600; }
  .nav-item .ic { width:16px; text-align:center; opacity:.9; }
  .nav-item .cnt { margin-left:auto; font-size:11px; color:var(--mut); }
  .nav-item .dot { width:6px; height:6px; border-radius:50%; background:var(--run); display:none; }
  .nav-item.running .dot { display:block; }
  .nav-empty { padding:8px 18px; color:var(--mut); font-size:12px; }
  .nav-add { margin:8px 14px 0; padding:9px; text-align:center; border:1px dashed var(--bd); border-radius:8px;
             color:var(--acc); cursor:pointer; font-size:12px; }
  .nav-add:hover { background:#111c33; }
  .nav-foot { padding:10px 14px; border-top:1px solid var(--bd); font-size:11px; color:var(--mut); }
  .nav-foot .runline { display:flex; align-items:center; gap:7px; margin-bottom:4px; }

  .content { flex:1; min-width:0; padding:24px 26px 70px; }
  .content > .view { display:none; }
  .content > .view.active { display:block; }
  .page-head { margin:0 0 18px; }
  .page-head h2 { margin:0; font-size:19px; color:var(--fg); }
  .page-head p { margin:5px 0 0; color:var(--mut); font-size:12.5px; }

  section { background:var(--card); border-radius:12px; padding:18px; margin-bottom:16px; border:1px solid #26364f; }
  section > h2 { margin:0 0 14px; font-size:14.5px; color:var(--acc); font-weight:600; }
  label { display:block; margin:8px 0 4px; color:var(--mut); font-size:12px; }
  input, select { width:100%; padding:8px 10px; border-radius:8px; border:1px solid var(--bd); background:#0b1220; color:var(--fg); }
  input:focus, select:focus { outline:none; border-color:var(--acc); }
  .row { display:flex; gap:10px; }
  .row > div { flex:1; }
  button { cursor:pointer; border:0; border-radius:8px; padding:9px 14px; font-weight:600; background:var(--acc); color:#06283d; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.ghost { background:#0b1220; color:var(--fg); border:1px solid var(--bd); }
  button.ok { background:var(--ok); color:#053b2c; }
  button.danger { background:#3f1d1d; color:#fca5a5; border:1px solid #7f1d1d; }
  button.sm { padding:6px 11px; font-size:12px; }
  .actions { margin:14px 0 0; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .hint { color:var(--mut); font-size:12px; }
  .notice { color:var(--mut); font-size:12.5px; }
  #msg { color:var(--mut); }

  /* 路线卡片 */
  .route { border:1px solid var(--bd); border-radius:10px; margin-bottom:12px; background:#172033; overflow:hidden; }
  .route > summary { list-style:none; cursor:pointer; padding:12px 14px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .route > summary::-webkit-details-marker { display:none; }
  .route > summary:hover { background:#1b2740; }
  .route .caret { color:var(--mut); font-size:11px; transition:transform .15s; flex:0 0 auto; }
  .route[open] .caret { transform:rotate(90deg); }
  .route .rt-main { flex:1 1 220px; min-width:0; }
  .route .rt-name { font-weight:600; font-size:13.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .route .rt-path { color:var(--mut); font-size:11.5px; margin-top:3px; word-break:break-all; }
  .route .rt-meta { margin-left:auto; display:flex; align-items:center; gap:8px; flex:0 0 auto; flex-wrap:wrap; }
  .route .rt-body { padding:2px 14px 14px; border-top:1px solid var(--bd); }
  .badge { font-size:11px; padding:2px 8px; border-radius:20px; border:1px solid var(--bd); color:var(--mut); white-space:nowrap; }
  .badge.on { color:var(--ok); border-color:#1f6f52; background:#0d2b21; }
  .badge.off { color:#cbd5e1; }
  .badge.move { color:#fbbf24; border-color:#7c5a12; background:#2a1f07; }
  .badge.run { color:var(--run); border-color:#7c5a12; background:#2a1f07; }

  /* 概览统计格 */
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:12px; }
  .stat { background:#172033; border:1px solid var(--bd); border-radius:10px; padding:12px 14px; }
  .stat .k { color:var(--mut); font-size:11.5px; }
  .stat .v { font-size:20px; font-weight:700; margin-top:4px; }
  .stat .v.ok { color:var(--ok); }
  .stat .v.acc { color:var(--acc); }

  /* 运行记录 */
  .rec { border-bottom:1px solid var(--bd); padding:12px 0; }
  .rec:last-child { border-bottom:0; }
  .rec .rh { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .rec .rt { font-weight:600; font-size:13px; }
  .rec .time { color:var(--mut); font-size:11.5px; margin-left:auto; }
  .rec .nums { margin-top:7px; display:flex; gap:14px; flex-wrap:wrap; font-size:12px; color:var(--mut); }
  .rec .nums b { color:var(--fg); font-weight:600; }
  .rec .err { color:var(--err); font-size:12px; margin-top:6px; }

  pre { background:#0b1220; border:1px solid var(--bd); border-radius:10px; padding:14px; max-height:60vh;
        overflow:auto; white-space:pre-wrap; word-break:break-all; color:#cbd5e1; margin:0; font-size:12px; }
  #empty { color:var(--mut); text-align:center; padding:60px 0; }
  #empty button { margin-top:14px; }

  /* 目录选择弹窗 */
  #picker { position:fixed; inset:0; background:rgba(2,6,23,.7); display:none; align-items:center; justify-content:center; z-index:50; }
  #picker .box { width:min(560px,92vw); background:var(--card); border-radius:12px; padding:16px; border:1px solid var(--bd); }
  #pickerPath { color:var(--acc); margin:8px 0; word-break:break-all; font-size:13px; }
  #pickerList { max-height:320px; overflow:auto; }
  #pickerList div { padding:8px 10px; border-radius:8px; cursor:pointer; font-size:13px; }
  #pickerList div:hover { background:#0b1220; }
  #pickerList input { margin-bottom:8px; }

  /* ---------------- 窄屏：侧栏收成顶部横向条 ---------------- */
  @media (max-width:820px){
    .layout { flex-direction:column; }
    .sidebar { width:auto; flex:none; height:auto; position:static; border-right:0; border-bottom:1px solid var(--bd); }
    .nav { display:flex; gap:6px; overflow-x:auto; padding:10px 12px; }
    .nav-group, .nav-empty, .nav-foot { display:none; }
    .nav-item { padding:8px 13px; border-left:0; border-radius:8px; white-space:nowrap; background:#111c33; }
    .nav-item.active { border-left:0; }
    .nav-add { margin:0; flex:0 0 auto; white-space:nowrap; }
    .content { padding:16px 14px 50px; }
  }
</style>
</head>
<body>
<div class="layout">
  <aside class="sidebar">
    <div class="brand">
      <h1>opsync</h1>
      <div class="sub">多网盘定时搬运 · build __BUILD__</div>
    </div>
    <nav class="nav" id="nav"></nav>
    <div class="nav-foot">
      <div class="runline"><span class="badge" id="footState">空闲</span></div>
      <div>运行中 <span id="footRunning">0</span> 条 · 设置已自动持久化</div>
    </div>
  </aside>

  <main class="content">
    <div class="view" id="view-routes"></div>
    <div class="view" id="view-conn"></div>
    <div class="view" id="view-results"></div>
    <div class="view" id="view-logs">
      <div class="page-head"><h2>日志</h2><p>实时输出，保留最近 2000 行（每 3 秒刷新）</p></div>
      <section style="padding:12px"><pre id="logs">加载中…</pre></section>
    </div>
    <div class="view" id="view-guide">
      <div class="page-head"><h2>使用说明</h2><p>从添加账号到开始搬运的完整流程</p></div>
      <section id="guideBody"></section>
    </div>
    <div id="empty" style="display:none"></div>
  </main>
</div>

<div id="picker">
  <div class="box">
    <h2>选择目录</h2>
    <div id="pickerPath">/</div>
    <div id="pickerList"></div>
    <div class="actions">
      <button class="ghost" id="pickerUp">上一级</button>
      <button class="ghost" id="pickerCancel">取消</button>
      <button id="pickerOk">选择此目录</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const gv = id => $(id).value;
function setv(id,v){ if(v!==undefined&&v!==null) $(id).value=v; }

let conns = [];
let activeId = null;
let picker = { connId:null, routeIdx:0, field:"src_path", path:"/" };
// 当前视图：routes(路线管理) | conn(连接设置) | results(运行结果) | logs(日志) | guide(使用说明)
let view = 'routes';
const VIEWS = {
  routes: { icon:'🔀', label:'搬运路线', needConn:true },
  conn:   { icon:'🔌', label:'连接设置', needConn:true },
  results:{ icon:'📊', label:'运行结果', needConn:false },
  logs:   { icon:'📜', label:'日志',     needConn:false },
  guide:  { icon:'📖', label:'使用说明', needConn:false },
};
let lastStatus = { running:false, running_keys:[], routes:{} };
let expanded = {};      // 路线卡片展开状态：{ "账号id::路线名": true }

async function load(){
  try{
    const c = await (await fetch('/api/config')).json();
    conns = c.openlists || [];
    if(conns.length) activeId = (connById(activeId)?activeId:conns[0].id); else activeId=null;
    renderNav(); renderAllViews();
  }catch(e){ /* 首次加载失败由 poll 的提示兜住 */ }
}
function connById(id){ return conns.find(c=>c.id===id); }
function persist(){ return fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({openlists:conns})}); }

/* ============================ 左侧边栏 ============================ */
function renderNav(){
  const n = $('nav'); n.innerHTML='';
  const mk=(cls,txt,ic,cnt,run)=>{ const d=document.createElement('div'); d.className=cls;
    d.innerHTML='<span class="ic">'+(ic||'')+'</span><span>'+txt+'</span>'+
      (cnt?('<span class="cnt">'+cnt+'</span>'):'')+'<span class="dot"></span>'; return d; };

  n.appendChild(Object.assign(document.createElement('div'),{className:'nav-group',textContent:'账号'}));
  if(!conns.length){
    n.appendChild(Object.assign(document.createElement('div'),{className:'nav-empty',textContent:'还没有账号'}));
  }
  conns.forEach(c=>{
    const running = (lastStatus.running_keys||[]).some(k=>k.split('::')[0]===c.id);
    const item = mk('nav-item'+(c.id===activeId?' active':'')+(running?' running':''),
                    esc(c.name||'未命名'), '●', ((c.routes||[]).length||''), running);
    item.querySelector('.ic').style.color = c.tested_ok ? 'var(--ok)' : 'var(--mut)';
    item.querySelector('.ic').style.fontSize = '9px';
    item.title = c.tested_ok ? '已通过连接测试' : '尚未通过连接测试';
    item.onclick=()=>{ activeId=c.id; if(!VIEWS[view].needConn) view='routes'; renderNav(); renderAllViews(); };
    n.appendChild(item);
  });
  const add = Object.assign(document.createElement('div'),{className:'nav-add',textContent:'+ 添加账号'});
  add.onclick=addConn; n.appendChild(add);

  n.appendChild(Object.assign(document.createElement('div'),{className:'nav-group',textContent:'功能'}));
  Object.keys(VIEWS).forEach(k=>{
    const v=VIEWS[k];
    if(v.needConn && !activeId) return;             // 需要选中账号的视图，无账号时隐藏
    const item = mk('nav-item'+(k===view?' active':''), v.label, v.icon, '', false);
    item.onclick=()=>{ view=k; renderNav(); renderAllViews(); };
    n.appendChild(item);
  });
}

/* ---- 视图切换：只显示当前视图，其余隐藏 ---- */
function showView(k, on){
  const el=$('view-'+k); if(el) el.classList.toggle('active', !!on);
}
function renderAllViews(){
  if(!activeId){ showView('routes',false); showView('conn',false); }
  Object.keys(VIEWS).forEach(k=>{ if(k!=='routes'&&k!=='conn') showView(k, k===view); });
  if(activeId){ showView('routes', view==='routes'); showView('conn', view==='conn'); }
  $('empty').style.display = (!activeId && (view==='routes'||view==='conn')) ? 'block' : 'none';
  if(!activeId){
    $('empty').innerHTML='<h2 style="margin:0 0 8px;font-size:16px">还没有 OpenList 账号</h2>'+
      '<div class="hint">先在左侧「+ 添加账号」填入 OpenList 地址与账号密码，通过连接测试后即可建搬运路线。</div>'+
      '<button id="emptyAdd">+ 添加账号</button>';
    const b=$('emptyAdd'); if(b) b.onclick=addConn;
  }
  if(activeId){
    if(view==='routes') renderRoutes();
    if(view==='conn') renderConn();
  }
  if(view==='results') renderResults();
  if(view==='guide') renderGuide();
}

function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function escAttr(s){ return esc(s); }

/* ============================ 视图一：搬运路线 ============================ */
function renderRoutes(){
  const c=connById(activeId);
  const p=$('view-routes');
  if(!c){ p.innerHTML=''; return; }

  let html='<div class="page-head"><h2>搬运路线</h2>'+
    '<p>账号「'+esc(c.name||'未命名')+'」的定时搬运任务</p></div>';

  if(!c.tested_ok){
    p.innerHTML=html+'<section><h2>尚未就绪</h2>'+
      '<p class="notice">该账号还没通过连接测试。<br>请先到左侧「连接设置」填好地址与账号密码，点「保存并连接测试」通过后，再回来添加搬运路线。</p>'+
      '<div class="actions"><button id="goConn">前往连接设置</button></div></section>';
    const g=$('goConn'); if(g) g.onclick=()=>{ view='conn'; renderNav(); renderAllViews(); };
    return;
  }

  if(!Array.isArray(c.routes)) c.routes=[];
  const total=c.routes.length;
  const runningKeys=lastStatus.running_keys||[];
  const runningCount=c.routes.filter(r=>runningKeys.indexOf(c.id+'::'+r.name)>=0).length;
  const enabledCount=c.routes.filter(r=>r.enabled!==false).length;

  html+='<section><h2>概览</h2><div class="stats">'+
    '<div class="stat"><div class="k">路线总数</div><div class="v">'+total+'</div></div>'+
    '<div class="stat"><div class="k">已启用</div><div class="v ok">'+enabledCount+'</div></div>'+
    '<div class="stat"><div class="k">正在运行</div><div class="v '+(runningCount?'acc':'')+'">'+runningCount+'</div></div>'+
    '</div></section>';

  html+='<section><h2>路线列表</h2>'+
    (total? c.routes.map((r,i)=>routeHTML(c,r,i)).join('')
          : '<p class="notice">还没有搬运路线。点下面「+ 添加路线」，填好源目录与目标目录后保存，调度器就会按设定自动搬运。</p>')+
    '<div class="actions"><button class="ghost" id="addRoute">+ 添加路线</button>'+
    '<button id="saveRoutes">保存路线</button>'+
    '<button class="ok" id="runAll">立即运行全部</button>'+
    '<button class="danger" id="stopAll">终止全部</button>'+
    '<span id="routeMsg" class="hint"></span></div></section>';
  p.innerHTML=html;
  bindRoute(c);
}

/* ============================ 视图二：连接设置 ============================ */
function renderConn(){
  const c=connById(activeId);
  const p=$('view-conn');
  if(!c){ p.innerHTML=''; return; }
  const tested=!!c.tested_ok;
  p.innerHTML='<div class="page-head"><h2>连接设置</h2><p>OpenList 地址与登录信息，修改后需重新保存</p></div>'+
    '<section><h2>'+(tested
        ? '<span class="badge on">已通过连接测试</span> '
        : '<span class="badge off">未测试 / 未通过</span> ')+esc(c.name||'未命名')+'</h2>'+
    '<label>展示名</label><input id="c_name" value="'+escAttr(c.name||'')+'">'+
    '<label>OpenList 地址</label><input id="c_url" placeholder="https://xxx.hf.space" value="'+escAttr(c.url||'')+'">'+
    '<div class="row"><div><label>账号</label><input id="c_username" placeholder="admin" value="'+escAttr(c.username||'')+'"></div>'+
    '<div><label>密码</label><input id="c_password" type="password" placeholder="密码" value="'+escAttr(c.password||'')+'"></div></div>'+
    '<div class="actions"><button id="saveConn">保存连接</button>'+
    '<button class="ghost" id="testConn">仅测试连接</button>'+
    '<button class="ok" id="saveTest">保存并连接测试</button>'+
    '<span id="connMsg" class="hint"></span></div></section>'+
    '<section><h2>其他操作</h2>'+
    '<p class="notice">删除账号会同时删除它下面的所有搬运路线，且不可恢复。</p>'+
    '<div class="actions"><button class="danger" id="delConn">删除该账号</button></div></section>';

  const flash=(t)=>{ const m=$('connMsg'); if(m) m.textContent=t; };
  $('saveConn').onclick = async ()=>{ syncForm(c); const r=await persist(); const j=await r.json();
    flash(j.ok?'已保存（持久化）':'失败：'+j.error); renderNav(); };
  $('testConn').onclick = async ()=>{ syncForm(c); flash('测试中…');
    const r=await fetch('/api/connection-test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:c.url,username:c.username,password:c.password})});
    const j=await r.json(); c.tested_ok=j.ok; flash(j.ok?'连接成功':'失败：'+j.error);
    renderNav(); renderConn(); };
  $('saveTest').onclick = async ()=>{ syncForm(c); flash('测试中…');
    const r=await fetch('/api/connection-test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:c.url,username:c.username,password:c.password})});
    const j=await r.json(); c.tested_ok=j.ok;
    if(!j.ok){ flash('连接失败：'+j.error); return; }
    const rp=await persist(); const rpj=await rp.json();
    flash(rpj.ok?'连接成功并已保存':'保存失败：'+rpj.error);
    renderNav(); renderConn(); };
  $('delConn').onclick = ()=>delConn(c.id);
}
function syncForm(c){ c.name=gv('c_name'); c.url=gv('c_url'); c.username=gv('c_username'); c.password=gv('c_password'); }

/* ============================ 视图三：运行结果 ============================ */
function renderResults(){
  const p=$('view-results');
  const routes=lastStatus.routes||{};
  const keys=Object.keys(routes);
  let html='<div class="page-head"><h2>运行结果</h2><p>每条路线最近一次执行的结果（所有账号）</p></div>';
  if(!keys.length){
    p.innerHTML=html+'<section><p class="notice">还没有执行记录。到「搬运路线」点「立即运行全部」即可看到结果。</p></section>';
    return;
  }
  const rows=keys.map(k=>{
    const x=routes[k]||{}; const st=x.stats||{};
    const [connId,name]=[k.split('::')[0], k.split('::')[1]];
    const cn=connById(connId);
    const cancelled = st.cancelled || x.cancelled;
    const badge = x.error ? '<span class="badge" style="color:var(--err);border-color:#7f1d1d;background:#3f1d1d">失败</span>'
                : cancelled ? '<span class="badge">已终止</span>'
                : '<span class="badge on">成功</span>';
    const nums = st ? ('<span>已搬运 <b>'+st.copied+'</b></span><span>跳过 <b>'+st.skipped+'</b></span>'+
                       '<span>超出范围 <b>'+(st.out_of_range||0)+'</b></span><span>失败 <b>'+st.failed+'</b></span>'+
                       '<span>删除源 <b>'+(st.removed||0)+'</b></span>') : '';
    return '<div class="rec"><div class="rh">'+badge+
      '<span class="rt">'+esc(name||'?')+'</span>'+
      '<span class="hint">'+esc((cn&&cn.name)||connId)+'</span>'+
      '<span class="time">'+(x.last_run||'-')+'</span></div>'+
      (nums?('<div class="nums">'+nums+'</div>'):'')+
      (x.error?('<div class="err">'+esc(x.error)+'</div>'):'')+
      '</div>';
  }).join('');
  p.innerHTML=html+'<section>'+rows+'</section>';
}

/* ============================ 视图四：使用说明 ============================ */
function renderGuide(){
  const p=$('view-guide');
  p.querySelector('#guideBody').innerHTML=
    '<ol style="margin:0;padding-left:20px;line-height:2">'+
    '<li>左侧点 <b>+ 添加账号</b>，填展示名、OpenList 地址、账号、密码。</li>'+
    '<li>到 <b>连接设置</b> 点 <b>保存并连接测试</b>。只有测试通过才能建路线。</li>'+
    '<li>回到 <b>搬运路线</b>，点 <b>+ 添加路线</b>：用「浏览选择」点出源目录和目标目录（不用手敲路径）。</li>'+
    '<li>选搬运模式、文件大小范围、调度方式，勾选「启用」。</li>'+
    '<li>点 <b>保存路线</b> 后调度器会自动跑；也可随时点单条「运行」或「立即运行全部」。</li>'+
    '</ol>'+
    '<h2 style="margin-top:22px">运行与终止</h2>'+
    '<p class="notice">运行中的路线标题会出现 <span class="badge run">运行中</span>，此时「终止」按钮才会出现。'+
    '终止采用协作式取消：已发出的单个复制/移动请求会跑完（不留半截、不误删），之后不再发起新动作；'+
    '已完成的部分如实保留并计入结果，终止不计入失败，之后可再点「运行」续搬（已搬过的会自动跳过）。</p>'+
    '<h2 style="margin-top:22px">搬运文件大小范围</h2>'+
    '<p class="notice">筛选粒度是<b>单个文件</b>，范围外的文件<b>留在源目录不动</b>（move 模式也不搬、不删），'+
    '所以不会丢数据，下次放宽范围还能接着搬。单位写法：<code>250B</code>=250字节，<code>5GB</code>=5×1024³，'+
    '裸数字（如 <code>1024</code>）按 MB 算。</p>'+
    '<h2 style="margin-top:22px">配置与持久化</h2>'+
    '<p class="notice">所有设置自动保存到持久化目录，重启不丢。页面上的「保存」按钮只是把当前改动立即落盘。</p>';
}

/* ---- 搬运文件大小范围 ---- */
// 预设档位：[值, 展示文字]。值为 "min|max"（空 = 不限），"custom" = 自定义。
const SIZE_PRESETS = [
  ['|',      '全部（不限制）'],
  ['|5GB',   '小于 5GB'],
  ['|1GB',   '小于 1GB'],
  ['|500MB', '小于 500MB'],
  ['1GB|',   '大于 1GB'],
  ['5GB|',   '大于 5GB'],
  ['1GB|10GB','1GB ~ 10GB'],
  ['5GB|50GB','5GB ~ 50GB'],
  ['custom', '自定义…'],
];
function sizePreset(v){ return SIZE_PRESETS.find(p=>p[0]===v); }
function sizeOf(r){ return (r.min_size||'')+'|'+(r.max_size||''); }
function isCustomSize(r){
  const v=sizeOf(r);
  if(v==='|') return false;
  return !sizePreset(v);
}
function sizePresetOptions(r){
  const v=sizeOf(r);
  const custom=isCustomSize(r);
  return SIZE_PRESETS.map(p=>{
    const sel = (p[0]==='custom'? custom : (!custom && p[0]===v)) ? ' selected':'';
    return '<option value="'+p[0]+'"'+sel+'>'+p[1]+'</option>';
  }).join('');
}
function sizeCustomText(r){
  const lo=r.min_size||'', hi=r.max_size||'';
  if(lo&&hi) return lo+'~'+hi;
  if(hi) return '小于'+hi;
  if(lo) return '大于'+lo;
  return '';
}
// 解析自定义输入：支持 "1GB~10GB" / "小于5GB" / "大于1GB" / ">5GB" / "<5GB" / 单填一个数字
function parseCustomSize(text){
  let s=String(text||'').trim().replace(/\s+/g,'');
  if(!s) return {min_size:'',max_size:''};
  s=s.replace(/^≤|^<=/,'小于').replace(/^≥|^>=/,'大于');
  let m=s.match(/^(.+?)[~～\-—到至]+(.+)$/);
  if(m) return {min_size:m[1],max_size:m[2]};
  m=s.match(/^(小于|不超过|至多|<)(.+)$/);
  if(m) return {min_size:'',max_size:m[2]};
  m=s.match(/^(大于|不少于|至少|>)(.+)$/);
  if(m) return {min_size:m[2],max_size:''};
  return {min_size:'',max_size:s};   // 只填一个值 → 当作上限
}

function routeHTML(c,r,i){
  const en=r.enabled?'checked':'';
  const key=c.id+'::'+(r.name||'');
  const open=expanded[key]?' open':'';
  const on=r.enabled!==false;
  return '<details class="route" data-i="'+i+'" data-key="'+escAttr(key)+'"'+open+'>'+
    '<summary>'+
      '<span class="caret">▶</span>'+
      '<span class="rt-main">'+
        '<div class="rt-name">'+esc(r.name||('路线'+(i+1)))+'</div>'+
        '<div class="rt-path">'+esc(r.src_path||'(未设置源)')+' → '+esc(r.dst_path||'(未设置目标)')+'</div>'+
      '</span>'+
      '<span class="rt-meta">'+
        '<span class="badge '+(on?'on':'off')+'">'+(on?'已启用':'已停用')+'</span>'+
        '<span class="badge '+(r.mode==='move'?'move':'')+'">'+(r.mode==='move'?'move':'copy')+'</span>'+
        '<span class="badge run" data-runstate="'+i+'" style="display:none">运行中</span>'+
        '<button class="ghost sm" data-act="run">运行</button>'+
        '<button class="danger sm" data-act="stop" style="display:none">终止</button>'+
        '<button class="danger sm" data-act="del">删除</button>'+
      '</span>'+
    '</summary>'+
    '<div class="rt-body">'+
    '<label>名称</label><input data-f="name" value="'+escAttr(r.name||'')+'">'+
    '<div class="row"><div><label>源目录</label><input data-f="src_path" value="'+escAttr(r.src_path||'')+'"></div><div style="flex:0 0 auto;padding-top:22px"><button class="ghost sm" data-act="pick_src">浏览选择</button></div></div>'+
    '<div class="row"><div><label>目标目录</label><input data-f="dst_path" value="'+escAttr(r.dst_path||'')+'"></div><div style="flex:0 0 auto;padding-top:22px"><button class="ghost sm" data-act="pick_dst">浏览选择</button></div></div>'+
    '<div class="row"><div><label>模式</label><select data-f="mode"><option value="copy"'+(r.mode==='copy'?' selected':'')+'>copy 复制保留源</option><option value="move"'+(r.mode==='move'?' selected':'')+'>move 搬完删源</option></select></div>'+
    '<div><label>搬运文件大小范围</label><select data-size="preset">'+sizePresetOptions(r)+'</select></div></div>'+
    '<div class="row" data-custom="'+(isCustomSize(r)?'1':'0')+'" style="'+(isCustomSize(r)?'':'display:none')+'">'+
    '<div><label>自定义范围</label><input data-size="custom" placeholder="如 1GB~10GB / 小于5GB / 大于1GB" value="'+escAttr(sizeCustomText(r))+'"></div></div>'+
    '<div class="row"><div><label>调度</label><select data-f="schedule_type"><option value="interval"'+(r.schedule_type==='interval'?' selected':'')+'>interval 每N分钟</option><option value="daily"'+(r.schedule_type==='daily'?' selected':'')+'>daily 每天</option><option value="once"'+(r.schedule_type==='once'?' selected':'')+'>once 仅手动</option></select></div>'+
    '<div><label>间隔(分钟)</label><input data-f="interval_minutes" value="'+(r.interval_minutes||30)+'" type="number" min="1"></div>'+
    '<div><label>daily 时间</label><input data-f="run_at" value="'+escAttr(r.run_at||'03:00')+'"></div></div>'+
    '<label class="hint" style="margin-top:10px"><input type="checkbox" data-f="enabled" '+en+' style="width:auto"> 启用　<input type="checkbox" data-f="overwrite" '+(r.overwrite?'checked':'')+' style="width:auto"> 强制覆盖　<input type="checkbox" data-f="delete_empty_dirs" '+(r.delete_empty_dirs?'checked':'')+' style="width:auto"> move删空目录</label>'+
    '</div></details>';
}
function bindRoute(c){
  // 注意：必须把数组「写回」c.routes，不能只用 const routes=c.routes||[]。
  // 否则账号还没有 routes 键时（新账号 / 手写配置没写空数组），
  // routes 只是一个游离的临时数组，renderRoutes() 从 c.routes 重新渲染时看不到它，
  // 表现为「+ 添加路线」点了没反应。
  if(!Array.isArray(c.routes)) c.routes=[];
  const routes=c.routes;

  // 折叠状态要跨重渲染保留，否则一改数据（改个名字）卡片就全部收起，很难用
  document.querySelectorAll('#view-routes .route').forEach(el=>{
    el.addEventListener('toggle',()=>{ if(el.dataset.key) expanded[el.dataset.key]=el.open; });
  });

  routes.forEach((r,i)=>{
    const el=document.querySelector('#view-routes .route[data-i="'+i+'"]');
    if(!el) return;                      // 卡片没渲染出来就跳过，别让整个绑定中断
    el.querySelectorAll('[data-f]').forEach(inp=>{
      const h=()=>{ r[inp.dataset.f]= inp.type==='checkbox'?inp.checked:inp.value; };
      inp.addEventListener('input',h); inp.addEventListener('change',h);
    });
    // 汇总栏是「摘要」，改了名称/模式/启用后要立即反映，不然看着像没生效
    const refresh=()=>{
      const nm=el.querySelector('.rt-name'); if(nm) nm.textContent=r.name||('路线'+(i+1));
      const pth=el.querySelector('.rt-path');
      if(pth) pth.textContent=(r.src_path||'(未设置源)')+' → '+(r.dst_path||'(未设置目标)');
      const bd=el.querySelectorAll('.rt-meta .badge');
      if(bd[0]){ const on=r.enabled!==false; bd[0].className='badge '+(on?'on':'off'); bd[0].textContent=on?'已启用':'已停用'; }
      if(bd[1]){ bd[1].className='badge '+(r.mode==='move'?'move':''); bd[1].textContent=r.mode==='move'?'move':'copy'; }
      if(el.dataset.key) expanded[el.dataset.key]=el.open;
    };
    el.querySelectorAll('[data-f]').forEach(inp=>{
      inp.addEventListener('input',refresh); inp.addEventListener('change',refresh);
    });
    // 用「取到才绑定」的方式，避免某个元素缺失时抛错导致后面的按钮全部绑不上
    const on=(sel,fn)=>{ const n=el.querySelector(sel); if(n) n.onclick=fn; };
    on('[data-act="del"]',()=>{ routes.splice(i,1); renderRoutes(); });
    on('[data-act="run"]',()=>{ fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conn_id:c.id,name:r.name})}); msg('已触发：'+r.name); poll(); });
    on('[data-act="stop"]',async()=>{
      const btn=el.querySelector('[data-act="stop"]');
      if(btn){ btn.disabled=true; btn.textContent='终止中…'; }
      const res=await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conn_id:c.id,name:r.name})});
      const j=await res.json();
      if(j.stopped&&j.stopped.length) msg('正在终止：'+j.stopped.join('、'));
      else msg('该路线当前没有在运行');
      poll();
    });
    on('[data-act="pick_src"]',()=>openPicker(c.id,i,'src_path'));
    on('[data-act="pick_dst"]',()=>openPicker(c.id,i,'dst_path'));

    // 大小范围：下拉切档位，选到「自定义」才显示输入框
    const presetSel=el.querySelector('[data-size="preset"]');
    const customRow=el.querySelector('[data-custom]');
    const customInp=el.querySelector('[data-size="custom"]');
    if(presetSel){
      presetSel.onchange=()=>{
        const v=presetSel.value;
        if(v==='custom'){ if(customRow) customRow.style.display=''; if(customInp) customInp.focus(); return; }
        const parts=v.split('|');
        r.min_size=parts[0]; r.max_size=parts[1];
        if(customRow) customRow.style.display='none';
      };
    }
    if(customInp){
      const apply=()=>{ const p=parseCustomSize(customInp.value); r.min_size=p.min_size; r.max_size=p.max_size; };
      customInp.addEventListener('input',apply);
      customInp.addEventListener('change',apply);
    }
  });
  // 按钮统一用「取到才绑定」，任何一个缺失都不会让「+ 添加路线」失效
  const bind=(id,fn)=>{ const n=$(id); if(n) n.onclick=fn; };
  bind('addRoute',()=>{
    const name='路线'+(routes.length+1);
    routes.push({name:name,src_path:'',dst_path:'',mode:'copy',enabled:true,overwrite:false,delete_empty_dirs:false,schedule_type:'interval',interval_minutes:30,run_at:'03:00',min_size:'',max_size:''});
    expanded[c.id+'::'+name]=true;      // 新建的自动展开，方便马上填路径
    renderRoutes();
    const el=document.querySelector('#view-routes .route[data-i="'+(routes.length-1)+'"]');
    if(el){ el.scrollIntoView({block:'nearest'}); const nm=el.querySelector('[data-f="name"]'); if(nm) nm.focus(); }
  });
  bind('saveRoutes',async()=>{ const r=await persist(); const j=await r.json(); msg(j.ok?'路线已保存':'保存失败：'+j.error); });
  bind('runAll',async()=>{ const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conn_id:c.id})}); const j=await r.json(); msg(j.ok?('已触发 '+j.started+' 条'):'触发失败：'+j.error); poll(); });
  bind('stopAll',async()=>{
    const res=await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conn_id:c.id})});
    const j=await res.json();
    msg((j.stopped&&j.stopped.length)?('正在终止：'+j.stopped.join('、')):'当前没有在运行的路线');
    poll();
  });
}
function msg(t){ const m=$('routeMsg')||$('connMsg')||$('msg'); if(m) m.textContent=t; }
function addConn(){
  const id='conn-'+Date.now();
  conns.push({id:id,name:'新账号',url:'',username:'',password:'',tested_ok:false,routes:[]});
  activeId=id; view='conn';                 // 新账号先去填连接信息，路径更顺
  renderNav(); renderAllViews();
  const n=$('c_name'); if(n){ n.focus(); n.select(); }
}
function delConn(id){
  const c=connById(id);
  if(c && !confirm('确定删除账号「'+(c.name||'')+'」及其所有路线？此操作不可恢复。')) return;
  conns=conns.filter(x=>x.id!==id);
  if(activeId===id) activeId=conns[0]?conns[0].id:null;
  persist(); renderNav(); renderAllViews();
}

function openPicker(connId,idx,field){ picker={connId,routeIdx:idx,field,path:'/'}; $('picker').style.display='flex'; renderPicker(); }
async function renderPicker(){
  $('pickerPath').textContent=picker.path;
  const r=await (await fetch('/api/browse?id='+encodeURIComponent(picker.connId)+'&path='+encodeURIComponent(picker.path))).json();
  const list=$('pickerList'); list.innerHTML='';
  if(!r.ok){ list.innerHTML='<div>'+r.error+'</div>'; return; }
  if(!r.dirs.length){ list.innerHTML='<div class="hint">（此目录没有子目录）</div>'; return; }
  r.dirs.forEach(name=>{ const el=document.createElement('div'); el.textContent='📁 '+name;
    el.onclick=()=>{ let p=picker.path==='/'?'':picker.path.replace(/\/$/,''); picker.path=(p+'/'+name).replace(/\/+/g,'/'); renderPicker(); }; list.appendChild(el); });
}
$('pickerUp').onclick=()=>{ if(picker.path==='/')return; const p=picker.path.replace(/\/$/,''); picker.path= p.includes('/')? p.slice(0,p.lastIndexOf('/'))||'/' : '/'; renderPicker(); };
$('pickerCancel').onclick=()=>{ $('picker').style.display='none'; };
$('pickerOk').onclick=()=>{ const c=connById(picker.connId); if(c&&Array.isArray(c.routes)&&c.routes[picker.routeIdx]){ c.routes[picker.routeIdx][picker.field]=picker.path; expanded[c.id+'::'+(c.routes[picker.routeIdx].name||'')]=true; } $('picker').style.display='none'; renderAllViews(); };

async function poll(){
  try{
    const s=await (await fetch('/api/status')).json();
    lastStatus = s;
    const runningKeys=s.running_keys||[];

    // ---- 左侧栏：运行指示点 + 底部状态 ----
    document.querySelectorAll('#nav .nav-item').forEach(it=>{ it.classList.remove('running'); });
    conns.forEach((c,idx)=>{
      const running=runningKeys.some(k=>k.split('::')[0]===c.id);
      // 顺序固定：账号项在前（每个账号 1 项），其后是功能项
      const el=document.querySelectorAll('#nav .nav-item')[idx];
      if(el && running) el.classList.add('running');
    });
    const fs=$('footState');
    if(fs){ fs.textContent = s.running?'运行中':'空闲';
      fs.className = 'badge'+(s.running?' run':' on'); }
    const fr=$('footRunning'); if(fr) fr.textContent = runningKeys.length;

    // ---- 路线卡片：运行中状态与「运行 / 终止」按钮切换 ----
    const c=connById(activeId);
    ((c&&c.routes)||[]).forEach((r,i)=>{
      const el=document.querySelector('#view-routes .route[data-i="'+i+'"]');
      if(!el) return;
      const running = runningKeys.indexOf(activeId+'::'+r.name)>=0;
      const badge=el.querySelector('[data-runstate]');
      if(badge) badge.style.display = running?'':'none';
      const runBtn=el.querySelector('[data-act="run"]');
      const stopBtn=el.querySelector('[data-act="stop"]');
      if(runBtn){ runBtn.disabled=running; runBtn.textContent=running?'运行中…':'运行'; }
      if(stopBtn){
        stopBtn.style.display=running?'':'none';
        if(!running){ stopBtn.disabled=false; stopBtn.textContent='终止'; }
      }
      // 运行中的卡片给个左边条提示
      el.style.borderLeftColor = running?'var(--run)':'var(--bd)';
      el.style.borderLeftWidth = running?'3px':'1px';
    });

    // ---- 当前视图是「运行结果」时同步刷新 ----
    if(view==='results') renderResults();

    const l=await (await fetch('/api/logs?n=300')).json();
    const lg=$('logs'); if(lg) lg.textContent=l.logs||'(暂无日志)';
  }catch(e){}
}
setInterval(poll,3000);
load(); poll();
</script>
</body>
</html>
"""

def run() -> None:
    threading.Thread(target=scheduler_loop, daemon=True).start()
    banner = (
        f"opsync Web 已启动：监听端口 {PORT}，架构={platform.machine()}，"
        f"Python={platform.python_version()}，配置路径={save_path()}"
    )
    # 同时 print 与 log：确保 HF Space 等平台的日志里一定看得到启动信息
    print(banner, flush=True)
    _log.info(banner)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
