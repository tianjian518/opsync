"""配置加载与序列化（CLI 与 Web 共用）。

新模型：支持「多个 OpenList 账号」，每个账号下挂若干搬运路线。

    [[openlists]]
    id = "conn-xxxx"            # 唯一标识
    name = "账号1"              # 展示名
    url = "https://x.hf.space"  # OpenList 地址
    username = "admin"
    password = "12345"
    tested_ok = true            # 最近一次连接测试是否通过

        [[openlists.routes]]
        name = "路线1"
        src_path = "/源目录"
        dst_path = "/目标目录"
        mode = "copy"           # copy / move
        enabled = true
        overwrite = false
        delete_empty_dirs = false
        schedule_type = "interval"   # interval / daily / once
        interval_minutes = 30
        run_at = "03:00"
        min_size = ""                # 文件大小下限，带单位如 "1GB"；留空 = 不限
        max_size = "5GB"             # 文件大小上限，带单位如 "5GB"；留空 = 不限

大小筛选的粒度是「单个文件」；范围外的文件一律留在源目录不动（copy 不复制、move 不搬也不删）。

登录信息会持久化到 /data/config.toml（抱脸等平台的持久化目录），刷新不丢失。
"""

import os
import tomllib


def apply_env(cfg: dict) -> None:
    """用 OPSYNC_* 环境变量覆盖「第一个」OpenList 账号（多账号时仅作用于首个）。"""
    m = {
        "OPSYNC_URL": "url",
        "OPSYNC_USERNAME": "username",
        "OPSYNC_PASSWORD": "password",
    }
    kv = {key: m[env] for env in m if env in os.environ}
    if not kv:
        return
    cfg.setdefault("openlists", [])
    if cfg["openlists"]:
        cfg["openlists"][0].update(kv)
    else:
        cfg["openlists"].append({"id": "env", "name": "env", **kv})


def resolve_config_path() -> str:
    if os.environ.get("CONFIG_PATH"):
        return os.environ["CONFIG_PATH"]
    if os.path.exists("/data/config.toml"):
        return "/data/config.toml"
    return "config.toml"


def save_path() -> str:
    """配置保存位置：优先 /data/config.toml（抱脸等持久化目录），否则本地。"""
    if os.path.exists("/data") or os.path.exists("/data/config.toml"):
        return "/data/config.toml"
    return "config.toml"


def load_config():
    """返回 (config, path)。配置缺失时返回空结构而非崩溃。"""
    path = resolve_config_path()
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        cfg = {}
    cfg.setdefault("openlists", [])
    # 每个账号都补上 routes 数组：TOML 里没法表达「空表数组」（_emit_table 会跳过空列表），
    # 所以账号还没有路线时读出来就是没有 routes 键。这里统一补 []，
    # 前端拿到后就能直接 push 新路线（否则界面「+ 添加路线」会点了没反应）。
    for ol in cfg["openlists"]:
        if not isinstance(ol.get("routes"), list):
            ol["routes"] = []
    apply_env(cfg)
    return cfg, path


# ------------------------------------------------------------------ TOML 序列化
def _toml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = "" if v is None else str(v)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _emit_table(out: list, path: str, d: dict) -> None:
    # 1) 标量（非表、非表数组）先写；空列表/表数组均跳过，留待第 3 步或省略
    for k, v in d.items():
        if isinstance(v, (dict, list)):
            continue
        out.append(f"{k} = {_toml_scalar(v)}")
    # 2) 子表
    for k, v in d.items():
        if isinstance(v, dict) and v:
            sub = f"{path}.{k}" if path else k
            out.append(f"[{sub}]")
            _emit_table(out, sub, v)
            out.append("")
    # 3) 表数组（含嵌套，如 openlists.routes）
    for k, v in d.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            sub = f"{path}.{k}" if path else k
            for item in v:
                out.append(f"[[{sub}]]")
                _emit_table(out, sub, item)
                out.append("")


def write_toml(path: str, cfg: dict) -> None:
    out: list[str] = []
    _emit_table(out, "", cfg)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
