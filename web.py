"""opsync 的 Web 管理界面 + 后台调度器。

- 容器监听 $PORT（抱脸会注入，默认 7860），提供浏览器配置/触发/看日志。
- 后台线程按 schedule 定时跑；配置未填好时安静待命，绝不抛异常崩容器。
- 配置持久化到 /data/config.toml（抱脸持久化目录），否则写本地 config.toml。
"""

import datetime
import logging
import os
import threading
import time

from flask import Flask, jsonify, request
from confighelper import load_config, save_path
from sync import SyncEngine

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 7860))

_state = {"running": False, "last_run": None, "last_stats": None, "last_error": None}
_state_lock = threading.Lock()
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


def is_configured(cfg: dict) -> bool:
    """配置是否已填好（避免拿着占位值去连 127.0.0.1 导致崩溃）。"""
    for side in ("source", "target"):
        s = cfg.get(side, {})
        if not s.get("url") or not s.get("path"):
            return False
        if s.get("password") in (None, "", "your_password"):
            return False
        if "127.0.0.1" in str(s.get("url", "")):
            return False
    return True


def do_run() -> bool:
    """执行一次同步（带锁，保证同一时间只有一次在跑）。返回是否成功启动。"""
    with _state_lock:
        if _state["running"]:
            return False
        _state["running"] = True
    try:
        cfg, _ = load_config()
        stats = SyncEngine(cfg).run_once()
        _state["last_stats"] = stats
        _state["last_error"] = None
    except Exception:
        _log.exception("同步失败")
        _state["last_error"] = "同步出错，详见日志"
    finally:
        _state["last_run"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _state_lock:
            _state["running"] = False
    return True


def scheduler_loop() -> None:
    while True:
        try:
            cfg, _ = load_config()
            if not is_configured(cfg):
                _log.info("配置未完成（请在网页填写 OpenList 地址/账号/目录），调度暂停。")
                time.sleep(60)
                continue
            sched = cfg.get("schedule", {})
            mode = sched.get("type", "interval")
            if mode == "once":
                do_run()
                while True:
                    time.sleep(3600)
            elif mode == "daily":
                run_at = str(sched.get("run_at", "03:00"))
                hh, mm = (int(x) for x in run_at.split(":"))
                now = datetime.datetime.now()
                nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if nxt <= now:
                    nxt += datetime.timedelta(days=1)
                wait = (nxt - now).total_seconds()
                _log.info("下次执行：%s", nxt.strftime("%Y-%m-%d %H:%M:%S"))
                time.sleep(wait)
                do_run()
            else:
                do_run()
                time.sleep(float(sched.get("interval_minutes", 30)) * 60)
        except Exception:
            _log.exception("调度异常")
            time.sleep(60)


# ------------------------------------------------------------------ 路由
@app.route("/")
def index():
    return HTML


@app.route("/api/config", methods=["GET"])
def api_config_get():
    try:
        cfg, _ = load_config()
    except Exception:
        cfg = {}
    return jsonify({
        "source": cfg.get("source", {}),
        "target": cfg.get("target", {}),
        "transfer": cfg.get("transfer", {}),
        "schedule": cfg.get("schedule", {}),
        "config_path": save_path(),
    })


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True, silent=True) or {}
    cfg = {
        "source": data.get("source", {}),
        "target": data.get("target", {}),
        "transfer": data.get("transfer", {}),
        "schedule": data.get("schedule", {}),
        "logging": {"level": "INFO", "file": ""},
    }
    for side in ("source", "target"):
        if not cfg[side].get("url") or not cfg[side].get("path"):
            return jsonify({"ok": False, "error": f"{side} 需要填写 url 与 path"}), 400
    path = save_path()
    write_toml(path, cfg)
    return jsonify({"ok": True, "path": path})


@app.route("/api/run", methods=["POST"])
def api_run():
    if do_run():
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "正在运行中，请稍候"})


@app.route("/api/status")
def api_status():
    return jsonify(_state)


@app.route("/api/logs")
def api_logs():
    n = int(request.args.get("n", 300))
    with _log_lock:
        lines = _log_lines[-n:]
    return jsonify({"logs": "\n".join(lines)})


# ------------------------------------------------------------------ 工具
def write_toml(path: str, cfg: dict) -> None:
    out = []
    for sec, kv in cfg.items():
        if not isinstance(kv, dict):
            continue
        out.append(f"[{sec}]")
        for k, v in kv.items():
            if isinstance(v, bool):
                out.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                out.append(f"{k} = {v}")
            else:
                out.append(f'{k} = "{str(v).replace(chr(34), chr(92) + chr(34))}"')
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
  :root { --bg:#0f172a; --card:#1e293b; --fg:#e2e8f0; --mut:#94a3b8; --acc:#38bdf8; --ok:#34d399; --err:#f87171; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui,Segoe UI,Roboto,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:960px; margin:0 auto; padding:24px 16px 60px; }
  h1 { margin:0 0 4px; font-size:22px; }
  .sub { color:var(--mut); margin:0 0 20px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:720px){ .grid { grid-template-columns:1fr; } }
  section { background:var(--card); border-radius:12px; padding:16px; }
  h2 { margin:0 0 12px; font-size:15px; color:var(--acc); }
  label { display:block; margin:8px 0 4px; color:var(--mut); font-size:12px; }
  input, select { width:100%; padding:8px 10px; border-radius:8px; border:1px solid #334155; background:#0b1220; color:var(--fg); }
  .row { display:flex; gap:10px; }
  .row > div { flex:1; }
  .actions { margin:18px 0; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  button { cursor:pointer; border:0; border-radius:8px; padding:10px 16px; font-weight:600; }
  #save { background:var(--acc); color:#06283d; }
  #run { background:var(--ok); color:#053b2c; }
  #msg { color:var(--mut); }
  .status { background:var(--card); border-radius:10px; padding:12px 14px; margin-bottom:14px; }
  pre { background:#0b1220; border:1px solid #334155; border-radius:10px; padding:14px; max-height:360px; overflow:auto; white-space:pre-wrap; word-break:break-all; color:#cbd5e1; }
  .hint { color:var(--mut); font-size:12px; margin-top:6px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>opsync</h1>
  <p class="sub">通过 OpenList 把网盘 A 定时搬运到网盘 B</p>

  <div class="grid">
    <section>
      <h2>源 OpenList</h2>
      <label>地址 URL</label><input id="source_url" placeholder="http://127.0.0.1:5244">
      <label>账号</label><input id="source_username" placeholder="admin">
      <label>密码</label><input id="source_password" type="password" placeholder="密码">
      <label>目录路径</label><input id="source_path" placeholder="/阿里云盘/照片">
    </section>
    <section>
      <h2>目标 OpenList</h2>
      <label>地址 URL</label><input id="target_url" placeholder="http://127.0.0.1:5244">
      <label>账号</label><input id="target_username" placeholder="admin">
      <label>密码</label><input id="target_password" type="password" placeholder="密码">
      <label>目录路径</label><input id="target_path" placeholder="/OneDrive/备份/照片">
    </section>
    <section>
      <h2>搬运设置</h2>
      <label>模式</label>
      <select id="mode"><option value="copy">copy（复制，保留源）</option><option value="move">move（搬完删源）</option></select>
      <div class="row">
        <div><label>并发数</label><input id="concurrency" type="number" value="3" min="1"></div>
        <div><label>调度间隔(分钟)</label><input id="interval_minutes" type="number" value="30" min="1"></div>
      </div>
      <label class="hint"><input type="checkbox" id="overwrite"> 强制覆盖（同名且大小一致也重传）</label>
      <label class="hint"><input type="checkbox" id="delete_empty_dirs"> move 后删除已搬空的源目录</label>
    </section>
    <section>
      <h2>调度</h2>
      <label>类型</label>
      <select id="schedule_type">
        <option value="interval">interval（每 N 分钟）</option>
        <option value="daily">daily（每天定时）</option>
        <option value="once">once（仅手动/启动时一次）</option>
      </select>
      <label>daily 执行时间</label><input id="run_at" value="03:00" placeholder="HH:MM">
      <p class="hint">保存后调度器按此设置自动运行；也可点“立即运行一次”手动触发。</p>
    </section>
  </div>

  <div class="actions">
    <button id="save">保存配置</button>
    <button id="run">立即运行一次</button>
    <span id="msg"></span>
  </div>

  <div class="status" id="status">加载中…</div>

  <h2>日志</h2>
  <pre id="logs"></pre>
</div>

<script>
const $ = id => document.getElementById(id);
const gv = id => $(id).value;
function setv(id,v){ if(v!==undefined&&v!==null) $(id).value=v; }
function setc(id,v){ $(id).checked = !!v; }

async function load(){
  try{
    const c = await (await fetch('/api/config')).json();
    setv('source_url',c.source?.url); setv('source_username',c.source?.username);
    setv('source_password',c.source?.password); setv('source_path',c.source?.path);
    setv('target_url',c.target?.url); setv('target_username',c.target?.username);
    setv('target_password',c.target?.password); setv('target_path',c.target?.path);
    setv('mode',c.transfer?.mode||'copy'); setv('concurrency',c.transfer?.concurrency??3);
    setc('overwrite',c.transfer?.overwrite); setc('delete_empty_dirs',c.transfer?.delete_empty_dirs);
    setv('schedule_type',c.schedule?.type||'interval'); setv('interval_minutes',c.schedule?.interval_minutes??30);
    setv('run_at',c.schedule?.run_at||'03:00');
  }catch(e){ msg('加载配置失败'); }
}
function msg(t){ $('msg').textContent=t; }

async function save(){
  const data = {
    source:{url:gv('source_url'),username:gv('source_username'),password:gv('source_password'),path:gv('source_path')},
    target:{url:gv('target_url'),username:gv('target_username'),password:gv('target_password'),path:gv('target_path')},
    transfer:{mode:$('mode').value, overwrite:$('overwrite').checked, concurrency:Number(gv('concurrency')), delete_empty_dirs:$('delete_empty_dirs').checked},
    schedule:{type:$('schedule_type').value, interval_minutes:Number(gv('interval_minutes')), run_at:gv('run_at')}
  };
  const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const j = await r.json();
  msg(j.ok ? ('已保存：'+j.path) : ('保存失败：'+j.error));
}
async function runnow(){
  const r = await fetch('/api/run',{method:'POST'});
  const j = await r.json();
  msg(j.ok ? '已开始同步' : '忙碌中：'+j.error);
  poll();
}
async function poll(){
  try{
    const s = await (await fetch('/api/status')).json();
    $('status').textContent = (s.running?'● 运行中…':'○ 空闲') + '　上次运行：' + (s.last_run||'-') + (s.last_error?('　⚠ '+s.last_error):'');
    const l = await (await fetch('/api/logs?n=300')).json();
    $('logs').textContent = l.logs || '(暂无日志)';
  }catch(e){}
}

$('save').onclick = save;
$('run').onclick = runnow;
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
