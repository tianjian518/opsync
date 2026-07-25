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
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, request
from confighelper import load_config, save_path, write_toml
from openlist_client import OpenListClient
from sync import SyncEngine

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 7860))

_state = {"running": False}
_route_results = {}
_running = set()
_running_lock = threading.Lock()
_next = {}
_exec = ThreadPoolExecutor(max_workers=6)
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
    with _running_lock:
        if key in _running:
            return
        _running.add(key)
    try:
        stats = SyncEngine(ol).run_route(r)
        _route_results[key] = {"last_run": _now_str(), "stats": stats, "error": None}
    except Exception:
        _log.exception("账号[%s] 路线[%s] 执行失败", conn_id, r.get("name"))
        _route_results[key] = {"last_run": _now_str(), "stats": None, "error": "执行出错，详见日志"}
    finally:
        with _running_lock:
            _running.discard(key)
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
    return HTML


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


@app.route("/api/status")
def api_status():
    with _running_lock:
        running = bool(_running)
    return jsonify({"running": running, "routes": _route_results})


@app.route("/api/logs")
def api_logs():
    n = int(request.args.get("n", 300))
    with _log_lock:
        lines = _log_lines[-n:]
    return jsonify({"logs": "\n".join(lines)})


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>opsync · 多网盘定时搬运</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --fg:#e2e8f0; --mut:#94a3b8; --acc:#38bdf8; --ok:#34d399; --err:#f87171; --bd:#334155; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui,Segoe UI,Roboto,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:1040px; margin:0 auto; padding:20px 16px 60px; }
  h1 { margin:0 0 4px; font-size:22px; }
  .sub { color:var(--mut); margin:0 0 16px; }
  .tabs { display:flex; gap:6px; overflow-x:auto; padding-bottom:8px; border-bottom:1px solid var(--bd); margin-bottom:16px; }
  .tab { flex:0 0 auto; padding:8px 14px; border-radius:8px 8px 0 0; background:#0b1220; color:var(--mut); cursor:pointer; border:1px solid var(--bd); border-bottom:0; white-space:nowrap; }
  .tab.active { background:var(--card); color:var(--fg); }
  .tab .x { margin-left:8px; color:var(--err); }
  .tab.add { color:var(--acc); }
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
  .status { background:var(--card); border-radius:10px; padding:12px 14px; margin-bottom:14px; font-size:13px; white-space:pre-wrap; }
  pre { background:#0b1220; border:1px solid var(--bd); border-radius:10px; padding:14px; max-height:320px; overflow:auto; white-space:pre-wrap; word-break:break-all; color:#cbd5e1; }
  .hint { color:var(--mut); font-size:12px; }
  #empty { color:var(--mut); text-align:center; padding:40px 0; }
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
  <p class="sub">多个 OpenList 账号各自定时搬运网盘 · 每个账号一个子页面 · 登录信息持久化</p>

  <div class="tabs" id="tabs"></div>
  <div id="panel"></div>
  <div id="empty" style="display:none">还没有 OpenList 账号，点右上角「+ 添加账号」开始。</div>

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

let conns = [];
let activeId = null;
let picker = { connId:null, routeIdx:0, field:"src_path", path:"/" };

async function load(){
  try{
    const c = await (await fetch('/api/config')).json();
    conns = c.openlists || [];
    if(conns.length) activeId = conns[0].id; else activeId=null;
    renderTabs(); renderPanel();
  }catch(e){ msg('加载配置失败'); }
}
function connById(id){ return conns.find(c=>c.id===id); }
function persist(){ return fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({openlists:conns})}); }

function renderTabs(){
  const t = $('tabs'); t.innerHTML='';
  conns.forEach(c=>{
    const el=document.createElement('div'); el.className='tab'+(c.id===activeId?' active':'');
    el.innerHTML = (c.name||'未命名') + ' <span class="x" data-del="'+c.id+'">✕</span>';
    el.onclick = (e)=>{ if(e.target.dataset.del){ delConn(e.target.dataset.del); } else { activeId=c.id; renderTabs(); renderPanel(); } };
    t.appendChild(el);
  });
  const add=document.createElement('div'); add.className='tab add'; add.textContent='+ 添加账号';
  add.onclick=addConn; t.appendChild(add);
}
function renderPanel(){
  const p=$('panel');
  if(!activeId){ $('empty').style.display = conns.length?'none':'block'; p.innerHTML=''; return; }
  $('empty').style.display='none';
  const c=connById(activeId);
  const tested = !!(c && c.tested_ok);
  p.innerHTML =
    '<section><h2>OpenList 连接：'+(c.name||'未命名')+'</h2>'+
    '<label>展示名</label><input id="c_name" value="'+(c.name||'')+'">'+
    '<label>OpenList 地址</label><input id="c_url" placeholder="https://xxx.hf.space" value="'+(c.url||'')+'">'+
    '<div class="row"><div><label>账号</label><input id="c_username" placeholder="admin" value="'+(c.username||'')+'"></div>'+
    '<div><label>密码</label><input id="c_password" type="password" placeholder="密码" value="'+(c.password||'')+'"></div></div>'+
    '<div class="actions"><button id="saveConn">保存连接</button>'+
    '<button class="ghost" id="testConn">仅测试连接</button>'+
    '<button class="ok" id="saveTest">保存并连接测试</button>'+
    '<span id="connMsg"></span></div></section>';
  $('saveConn').onclick = async ()=>{ syncForm(c); const r=await persist(); const j=await r.json(); $('connMsg').textContent=j.ok?'已保存（持久化）':'失败：'+j.error; renderTabs(); };
  $('testConn').onclick = async ()=>{ syncForm(c); $('connMsg').textContent='测试中…'; const r=await fetch('/api/connection-test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:c.url,username:c.username,password:c.password})}); const j=await r.json(); c.tested_ok=j.ok; $('connMsg').textContent=j.ok?'连接成功':'失败：'+j.error; };
  $('saveTest').onclick = async ()=>{ syncForm(c); $('connMsg').textContent='测试中…'; const r=await fetch('/api/connection-test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:c.url,username:c.username,password:c.password})}); const j=await r.json(); c.tested_ok=j.ok; if(!j.ok){ $('connMsg').textContent='连接失败：'+j.error; return; } const rp=await persist(); const rpj=await rp.json(); $('connMsg').textContent=rpj.ok?'连接成功并已保存':'保存失败：'+rpj.error; renderTabs(); renderPanel(); };

  if(tested){
    const rs=(c.routes||[]).map((r,i)=>routeHTML(c,r,i)).join('');
    const sec=document.createElement('section'); sec.innerHTML='<h2>搬运路线（账号：'+(c.name||'')+'）</h2>'+rs+
      '<div class="actions"><button class="ghost" id="addRoute">+ 添加路线</button>'+
      '<button id="saveRoutes">保存路线</button>'+
      '<button class="ok" id="runAll">立即运行全部（本账号）</button>'+
      '<span id="msg"></span></div>';
    p.appendChild(sec);
    bindRoute(c);
  } else {
    const w=document.createElement('section'); w.innerHTML='<h2>搬运路线</h2><p class="hint">请先「保存并连接测试」通过，再在此添加搬运路线。</p>'; p.appendChild(w);
  }
}
function syncForm(c){ c.name=gv('c_name'); c.url=gv('c_url'); c.username=gv('c_username'); c.password=gv('c_password'); }
function routeHTML(c,r,i){
  const en=r.enabled?'checked':'';
  return '<div class="route" data-i="'+i+'">'+
    '<h3><span>路线 '+(i+1)+'</span><span><button class="ghost" data-act="run">运行</button> <button class="danger" data-act="del">删除</button></span></h3>'+
    '<label>名称</label><input data-f="name" value="'+(r.name||'')+'">'+
    '<div class="row"><div><label>源目录</label><input data-f="src_path" value="'+(r.src_path||'')+'"></div><div><button class="ghost" data-act="pick_src">浏览选择</button></div></div>'+
    '<div class="row"><div><label>目标目录</label><input data-f="dst_path" value="'+(r.dst_path||'')+'"></div><div><button class="ghost" data-act="pick_dst">浏览选择</button></div></div>'+
    '<div class="row"><div><label>模式</label><select data-f="mode"><option value="copy"'+(r.mode==='copy'?' selected':'')+'>copy 复制保留源</option><option value="move"'+(r.mode==='move'?' selected':'')+'>move 搬完删源</option></select></div>'+
    '<div><label>调度</label><select data-f="schedule_type"><option value="interval"'+(r.schedule_type==='interval'?' selected':'')+'>interval 每N分钟</option><option value="daily"'+(r.schedule_type==='daily'?' selected':'')+'>daily 每天</option><option value="once"'+(r.schedule_type==='once'?' selected':'')+'>once 仅手动</option></select></div></div>'+
    '<div class="row"><div><label>间隔(分钟)</label><input data-f="interval_minutes" value="'+(r.interval_minutes||30)+'" type="number" min="1"></div>'+
    '<div><label>daily 时间</label><input data-f="run_at" value="'+(r.run_at||'03:00')+'"></div></div>'+
    '<label class="hint"><input type="checkbox" data-f="enabled" '+en+'> 启用　<input type="checkbox" data-f="overwrite" '+(r.overwrite?'checked':'')+'> 强制覆盖　<input type="checkbox" data-f="delete_empty_dirs" '+(r.delete_empty_dirs?'checked':'')+'> move删空目录</label>'+
    '</div>';
}
function bindRoute(c){
  const routes=c.routes||[];
  routes.forEach((r,i)=>{
    const el=document.querySelector('.route[data-i="'+i+'"]');
    el.querySelectorAll('[data-f]').forEach(inp=>{
      const h=()=>{ r[inp.dataset.f]= inp.type==='checkbox'?inp.checked:inp.value; };
      inp.addEventListener('input',h); inp.addEventListener('change',h);
    });
    el.querySelector('[data-act="del"]').onclick=()=>{ routes.splice(i,1); renderPanel(); };
    el.querySelector('[data-act="run"]').onclick=()=>{ fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conn_id:c.id,name:r.name})}); msg('已触发：'+r.name); poll(); };
    el.querySelector('[data-act="pick_src"]').onclick=()=>openPicker(c.id,i,'src_path');
    el.querySelector('[data-act="pick_dst"]').onclick=()=>openPicker(c.id,i,'dst_path');
  });
  $('addRoute').onclick=()=>{ routes.push({name:'路线'+(routes.length+1),src_path:'',dst_path:'',mode:'copy',enabled:true,overwrite:false,delete_empty_dirs:false,schedule_type:'interval',interval_minutes:30,run_at:'03:00'}); renderPanel(); };
  $('saveRoutes').onclick=async()=>{ const r=await persist(); const j=await r.json(); msg(j.ok?'路线已保存':'保存失败：'+j.error); };
  $('runAll').onclick=async()=>{ const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conn_id:c.id})}); const j=await r.json(); msg(j.ok?('已触发 '+j.started+' 条'):'触发失败：'+j.error); poll(); };
}
function msg(t){ const m=$('msg'); if(m) m.textContent=t; }
function addConn(){ const id='conn-'+Date.now(); conns.push({id:id,name:'新账号',url:'',username:'',password:'',tested_ok:false,routes:[]}); activeId=id; renderTabs(); renderPanel(); }
function delConn(id){ const c=connById(id); if(c&&c.tested_ok && !confirm('确定删除账号「'+(c.name||'')+'」及其所有路线？')) return; conns=conns.filter(x=>x.id!==id); if(activeId===id) activeId=conns[0]?conns[0].id:null; persist(); renderTabs(); renderPanel(); }

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
$('pickerOk').onclick=()=>{ const c=connById(picker.connId); if(c&&c.routes[picker.routeIdx]) c.routes[picker.routeIdx][picker.field]=picker.path; $('picker').style.display='none'; renderPanel(); };

async function poll(){
  try{
    const s=await (await fetch('/api/status')).json();
    let txt=(s.running?'有任务运行中…':'空闲');
    const keys=Object.keys(s.routes||{});
    if(keys.length){ txt+='\n各路线最近结果：'; keys.forEach(k=>{ const x=s.routes[k]; txt+='\n  · '+k+'：'+(x.last_run||'-')+(x.error?' 失败:'+x.error:(x.stats?(' 已搬'+x.stats.copied+'/跳'+x.stats.skipped+'/失败'+x.stats.failed):'')); }); }
    $('status').textContent=txt;
    const l=await (await fetch('/api/logs?n=300')).json();
    $('logs').textContent=l.logs||'(暂无日志)';
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
    _log.info("opsync Web 已启动，监听端口 %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
