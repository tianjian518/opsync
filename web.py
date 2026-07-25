"""opsync 的 Web 管理界面 + 后台调度器（多路线）。

流程：填 OpenList 账号密码 → 保存并连接测试 → 测试通过后浏览选择源/目标目录 →
建立多条搬运路线 → 调度器按各自设置自动跑。容器监听 $PORT（抱脸注入，默认 7860）。
配置保存在 /data/config.toml（持久化）。
"""

import datetime
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, request
from confighelper import load_config, save_path
from openlist_client import OpenListClient
from sync import SyncEngine

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 7860))

_state = {"running": False, "last_run": None, "last_error": None}
_route_results = {}
_running = set()
_running_lock = threading.Lock()
_next = {}
_exec = ThreadPoolExecutor(max_workers=4)
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


def run_route_safe(ol: dict, r: dict) -> None:
    name = r.get("name") or "?"
    with _running_lock:
        if name in _running:
            return
        _running.add(name)
    try:
        stats = SyncEngine(ol).run_route(r)
        _route_results[name] = {"last_run": _now_str(), "stats": stats, "error": None}
    except Exception:
        _log.exception("路线[%s] 执行失败", name)
        _route_results[name] = {"last_run": _now_str(), "stats": None, "error": "执行出错，详见日志"}
    finally:
        with _running_lock:
            _running.discard(name)
        with _running_lock:
            _state["running"] = bool(_running)


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
            ol = cfg.get("openlist")
            if not is_configured(ol):
                _log.info("OpenList 连接未配置（请在网页填写并测试），调度暂停。")
                time.sleep(30)
                continue
            now = datetime.datetime.now()
            for r in cfg.get("routes", []):
                if not r.get("enabled", True):
                    continue
                name = r.get("name") or id(r)
                nxt = _next.get(name)
                due = (nxt is None) or (now >= nxt)
                with _running_lock:
                    busy = name in _running
                if due and not busy:
                    _exec.submit(run_route_safe, ol, r)
                    _next[name] = compute_next(r, now)
            time.sleep(15)
        except Exception:
            _log.exception("调度异常")
            time.sleep(30)


# ------------------------------------------------------------------ 路由
@app.route("/")
def index():
    return HTML


@app.route("/api/connection-test", methods=["POST"])
def api_connection_test():
    d = request.get_json(force=True, silent=True) or {}
    try:
        c = OpenListClient(d["url"], d["username"], d["password"])
        c.login()
        c.list_files("/")  # 顺带验证能列根目录
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/browse")
def api_browse():
    path = request.args.get("path", "/")
    try:
        cfg, _ = load_config()
        ol = cfg.get("openlist", {})
        if not is_configured(ol):
            return jsonify({"ok": False, "error": "请先配置并保存 OpenList 连接"})
        client = OpenListClient(ol["url"], ol["username"], ol["password"])
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
    return jsonify({
        "openlist": cfg.get("openlist", {}),
        "routes": cfg.get("routes", []),
        "config_path": save_path(),
    })


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True, silent=True) or {}
    ol = data.get("openlist", {})
    if not ol.get("url") or not ol.get("username") or not ol.get("password"):
        return jsonify({"ok": False, "error": "OpenList 需要 url / username / password"}), 400
    routes = data.get("routes", [])
    path = save_path()
    write_toml(path, {"openlist": ol, "routes": routes})
    return jsonify({"ok": True, "path": path})


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    try:
        cfg, _ = load_config()
        ol = cfg.get("openlist")
        if not is_configured(ol):
            return jsonify({"ok": False, "error": "未配置 OpenList 连接"})
        targets = [r for r in cfg.get("routes", [])
                   if r.get("enabled", True) and (name is None or r.get("name") == name)]
        started = 0
        for r in targets:
            with _running_lock:
                busy = r.get("name") in _running
            if not busy:
                _exec.submit(run_route_safe, ol, r)
                started += 1
        return jsonify({"ok": True, "started": started})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/status")
def api_status():
    with _running_lock:
        running = bool(_running)
    return jsonify({"running": running, "last_run": _state["last_run"],
                    "last_error": _state["last_error"], "routes": _route_results})


@app.route("/api/logs")
def api_logs():
    n = int(request.args.get("n", 300))
    with _log_lock:
        lines = _log_lines[-n:]
    return jsonify({"logs": "\n".join(lines)})


# ------------------------------------------------------------------ TOML 序列化
def _toml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace('"', '\\"') + '"'


def _toml_kv(k, v):
    return f"{k} = {_toml_scalar(v)}"


def write_toml(path: str, cfg: dict) -> None:
    out = []
    for key, val in cfg.items():
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    out.append(f"[[{key}]]")
                    for k, v in item.items():
                        out.append(_toml_kv(k, v))
                    out.append("")
                else:
                    out.append(f"{key} = {_toml_scalar(item)}")
        elif isinstance(val, dict):
            out.append(f"[{key}]")
            for k, v in val.items():
                out.append(_toml_kv(k, v))
            out.append("")
        else:
            out.append(_toml_kv(key, val))
    out.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>opsync · 网盘定时搬运</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --fg:#e2e8f0; --mut:#94a3b8; --acc:#38bdf8; --ok:#34d399; --err:#f87171; --bd:#334155; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui,Segoe UI,Roboto,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:1000px; margin:0 auto; padding:24px 16px 60px; }
  h1 { margin:0 0 4px; font-size:22px; }
  .sub { color:var(--mut); margin:0 0 20px; }
  section { background:var(--card); border-radius:12px; padding:16px; margin-bottom:16px; }
  h2 { margin:0 0 12px; font-size:15px; color:var(--acc); }
  label { display:block; margin:8px 0 4px; color:var(--mut); font-size:12px; }
  input, select { width:100%; padding:8px 10px; border-radius:8px; border:1px solid var(--bd); background:#0b1220; color:var(--fg); }
  .row { display:flex; gap:10px; }
  .row > div { flex:1; }
  button { cursor:pointer; border:0; border-radius:8px; padding:9px 14px; font-weight:600; background:var(--acc); color:#06283d; }
  button.ghost { background:#0b1220; color:var(--fg); border:1px solid var(--bd); }
  button.ok { background:var(--ok); color:#053b2c; }
  button.danger { background:#3f1d1d; color:#fca5a5; border:1px solid #7f1d1d; }
  .actions { margin:14px 0; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  #msg { color:var(--mut); }
  .route { border:1px solid var(--bd); border-radius:10px; padding:12px; margin-bottom:12px; background:#172033; }
  .route h3 { margin:0 0 10px; font-size:14px; display:flex; justify-content:space-between; align-items:center; }
  .status { background:var(--card); border-radius:10px; padding:12px 14px; margin-bottom:14px; font-size:13px; }
  pre { background:#0b1220; border:1px solid var(--bd); border-radius:10px; padding:14px; max-height:340px; overflow:auto; white-space:pre-wrap; word-break:break-all; color:#cbd5e1; }
  .hint { color:var(--mut); font-size:12px; }
  /* 目录选择器 */
  #picker { position:fixed; inset:0; background:rgba(2,6,23,.7); display:none; align-items:center; justify-content:center; z-index:50; }
  #picker .box { width:min(560px,92vw); background:var(--card); border-radius:12px; padding:16px; }
  #pickerPath { color:var(--acc); margin:8px 0; word-break:break-all; }
  #pickerList { max-height:320px; overflow:auto; }
  #pickerList div { padding:8px 10px; border-radius:8px; cursor:pointer; }
  #pickerList div:hover { background:#0b1220; }
</style>
</head>
<body>
<div class="wrap">
  <h1>opsync</h1>
  <p class="sub">同一个 OpenList 里，把网盘 A 定时搬运到网盘 B · 可建多条路线</p>

  <section>
    <h2>1. OpenList 连接</h2>
    <label>OpenList 地址</label><input id="url" placeholder="http://127.0.0.1:5244">
    <div class="row">
      <div><label>账号</label><input id="username" placeholder="admin"></div>
      <div><label>密码</label><input id="password" type="password" placeholder="密码"></div>
    </div>
    <div class="actions">
      <button id="saveConn">保存并连接测试</button>
      <button class="ghost" id="testConn">仅测试连接</button>
      <span id="connMsg"></span>
    </div>
  </section>

  <section id="routesSection" style="display:none">
    <h2>2. 搬运路线</h2>
    <div id="routes"></div>
    <div class="actions">
      <button class="ghost" id="addRoute">+ 添加路线</button>
      <button id="saveRoutes">保存路线</button>
      <button class="ok" id="runAll">立即运行全部</button>
      <span id="msg"></span>
    </div>
  </section>

  <div class="status" id="status">加载中…</div>
  <h2>日志</h2>
  <pre id="logs"></pre>
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

let openlist = {};
let routes = [];
let picker = { idx:0, field:"src_path", path:"/" };

async function load(){
  try{
    const c = await (await fetch('/api/config')).json();
    openlist = c.openlist || {};
    routes = c.routes || [];
    setv('url', openlist.url); setv('username', openlist.username); setv('password', openlist.password);
    updateConnUI();
    renderRoutes();
  }catch(e){ msg('加载配置失败'); }
}
function updateConnUI(){
  const ok = !!(openlist && openlist.url && openlist.password && openlist.password!=='your_password' && !openlist.url.includes('127.0.0.1'));
  $('routesSection').style.display = ok ? 'block' : 'none';
  $('connMsg').textContent = ok ? '✅ 已配置' : '⚠ 请先填写并保存 OpenList 连接';
}
function msg(t){ $('msg').textContent = t; }
function connMsg(t){ $('connMsg').textContent = t; }

async function doTest(data){
  const r = await fetch('/api/connection-test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  return await r.json();
}
$('testConn').onclick = async () => {
  connMsg('测试中…');
  const j = await doTest({url:gv('url'),username:gv('username'),password:gv('password')});
  connMsg(j.ok ? '✅ 连接成功' : ('❌ '+j.error));
};
$('saveConn').onclick = async () => {
  connMsg('测试中…');
  const data = {url:gv('url'),username:gv('username'),password:gv('password')};
  const j = await doTest(data);
  if(!j.ok){ connMsg('❌ 连接失败：'+j.error); return; }
  openlist = data;
  const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({openlist:data, routes:routes})});
  const c = await r.json();
  connMsg(c.ok ? '✅ 连接成功并已保存' : ('❌ 保存失败：'+c.error));
  updateConnUI();
};

function addRoute(){
  routes.push({name:'路线'+(routes.length+1), src_path:'', dst_path:'', mode:'copy', enabled:true, overwrite:false, concurrency:3, delete_empty_dirs:false, schedule_type:'interval', interval_minutes:30, run_at:'03:00'});
  renderRoutes();
}
$('addRoute').onclick = addRoute;

function delRoute(i){ routes.splice(i,1); renderRoutes(); }
function runOne(i){ const r=routes[i]; if(!r||!r.name) return; fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:r.name})}); msg('已触发：'+r.name); poll(); }

function renderRoutes(){
  const w = $('routes'); w.innerHTML='';
  routes.forEach((r,i)=>{
    const d = document.createElement('div'); d.className='route';
    const en = r.enabled?'checked':'';
    d.innerHTML =
      '<h3><span>路线 '+(i+1)+'</span>'+
      '<span><button class="ghost" data-act="run">运行</button> <button class="danger" data-act="del">删除</button></span></h3>'+
      '<label>名称</label><input data-f="name" value="'+(r.name||'')+'">'+
      '<div class="row"><div><label>源目录</label><input data-f="src_path" value="'+(r.src_path||'')+'"></div>'+
      '<div><button class="ghost" data-act="pick_src">浏览选择</button></div></div>'+
      '<div class="row"><div><label>目标目录</label><input data-f="dst_path" value="'+(r.dst_path||'')+'"></div>'+
      '<div><button class="ghost" data-act="pick_dst">浏览选择</button></div></div>'+
      '<div class="row"><div><label>模式</label><select data-f="mode"><option value="copy"'+(r.mode==='copy'?' selected':'')+'>copy 复制保留源</option><option value="move"'+(r.mode==='move'?' selected':'')+'>move 搬完删源</option></select></div>'+
      '<div><label>调度</label><select data-f="schedule_type"><option value="interval"'+(r.schedule_type==='interval'?' selected':'')+'>interval 每N分钟</option><option value="daily"'+(r.schedule_type==='daily'?' selected':'')+'>daily 每天</option><option value="once"'+(r.schedule_type==='once'?' selected':'')+'>once 仅手动</option></select></div></div>'+
      '<div class="row"><div><label>间隔(分钟)</label><input data-f="interval_minutes" value="'+(r.interval_minutes||30)+'" type="number" min="1"></div>'+
      '<div><label>daily 时间</label><input data-f="run_at" value="'+(r.run_at||'03:00')+'"></div></div>'+
      '<label class="hint"><input type="checkbox" data-f="enabled" '+en+'> 启用　'+
      '<input type="checkbox" data-f="overwrite" '+(r.overwrite?'checked':'')+'> 强制覆盖　'+
      '<input type="checkbox" data-f="delete_empty_dirs" '+(r.delete_empty_dirs?'checked':'')+'> move删空目录</label>';
    d.querySelectorAll('[data-f]').forEach(el=>{
      el.addEventListener('input', ()=>{ r[el.dataset.f] = el.type==='checkbox' ? el.checked : el.value; });
      el.addEventListener('change', ()=>{ r[el.dataset.f] = el.type==='checkbox' ? el.checked : el.value; });
    });
    d.querySelector('[data-act="del"]').onclick = ()=>delRoute(i);
    d.querySelector('[data-act="run"]').onclick = ()=>runOne(i);
    d.querySelector('[data-act="pick_src"]').onclick = ()=>openPicker(i,'src_path');
    d.querySelector('[data-act="pick_dst"]').onclick = ()=>openPicker(i,'dst_path');
    w.appendChild(d);
  });
}
$('saveRoutes').onclick = async () => {
  const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({openlist:openlist, routes:routes})});
  const c = await r.json();
  msg(c.ok ? '路线已保存' : '保存失败：'+c.error);
};
$('runAll').onclick = async () => {
  const r = await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  const c = await r.json();
  msg(c.ok ? ('已触发 '+c.started+' 条路线') : '触发失败：'+c.error);
  poll();
};

// ---- 目录选择器 ----
function openPicker(i, field){ picker={idx:i, field:field, path:'/'}; $('picker').style.display='flex'; renderPicker(); }
async function renderPicker(){
  $('pickerPath').textContent = picker.path;
  const r = await (await fetch('/api/browse?path='+encodeURIComponent(picker.path))).json();
  const list = $('pickerList'); list.innerHTML='';
  if(!r.ok){ list.innerHTML='<div>'+r.error+'</div>'; return; }
  if(r.dirs.length===0){ list.innerHTML='<div class="hint">（此目录没有子目录）</div>'; return; }
  r.dirs.forEach(name=>{
    const el = document.createElement('div'); el.textContent='📁 '+name;
    el.onclick = ()=>{ picker.path = (picker.path==='/'?'/':'/'+picker.path.replace(/^\//,'')) ; picker.path = picker.path.replace(/\/$/,'')+'/'+name; renderPicker(); };
    list.appendChild(el);
  });
}
$('pickerUp').onclick = ()=>{ if(picker.path==='/') return; const p=picker.path.replace(/\/$/,''); picker.path = p.includes('/')? p.slice(0,p.lastIndexOf('/'))||'/' : '/'; renderPicker(); };
$('pickerCancel').onclick = ()=>{ $('picker').style.display='none'; };
$('pickerOk').onclick = ()=>{ const r=routes[picker.idx]; if(r){ r[picker.field]=picker.path; } $('picker').style.display='none'; renderRoutes(); };

// ---- 状态/日志轮询 ----
async function poll(){
  try{
    const s = await (await fetch('/api/status')).json();
    let txt = (s.running?'● 有路线运行中…':'○ 空闲') + '　全局上次：' + (s.last_run||'-');
    const names = Object.keys(s.routes||{});
    if(names.length){ txt += '\\n各路线：'; names.forEach(n=>{ const x=s.routes[n]; txt += '\\n  · '+n+'：'+(x.last_run||'-')+(x.error?' ⚠'+x.error:(x.stats?(' 已搬'+x.stats.copied+'/跳'+x.stats.skipped+'/失败'+x.stats.failed):'')); }); }
    $('status').textContent = txt;
    const l = await (await fetch('/api/logs?n=300')).json();
    $('logs').textContent = l.logs || '(暂无日志)';
  }catch(e){}
}
setInterval(poll, 3000);
load(); poll();
</script>
</body>
</html>
"""


def run() -> None:
    threading.Thread(target=scheduler_loop, daemon=True).start()
    _log.info("opsync Web 已启动，监听端口 %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
