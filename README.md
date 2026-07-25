# opsync

通过 **OpenList（AList v3 兼容）** 把一个网盘里的东西**定时搬运**到另一个网盘的极简工具。

只做核心功能，没有 Web 界面、没有数据库、没有花哨配置：

- 递归遍历源目录，自动在目标端建目录
- 按「文件名 + 大小」判断是否需要搬运，已同步的自动跳过
- **同实例**（源/目标在同一个 OpenList）= 走服务端 `/api/fs/copy` 或 `/api/fs/move`，不占本地带宽
- **跨实例**（两个不同 OpenList）= 边下载边上传流式搬运
- 两种模式：`copy`（保留源）/ `move`（搬完删源）
- 极简调度：`interval`（每 N 分钟）或 `daily`（每天几点），或 `once`（只跑一次）
- 唯一依赖：`requests`

> 相比原版 taosync，本工具砍掉了前端、SQLite、作业管理、通知、排除规则等，
> 只保留「定时把网盘 A 搬到网盘 B」这一件事。如果你需要那些能力，直接用原版即可。

## 安装

```bash
pip install -r requirements.txt
```

需要 Python 3.11+（用到内置 `tomllib` 解析配置）。

## 配置

编辑 `config.toml`：

```toml
[source]
url = "http://127.0.0.1:5244"   # OpenList 地址
username = "admin"
password = "your_password"
path = "/阿里云盘/照片"          # 源目录（OpenList 虚拟路径）

[target]
url = "http://127.0.0.1:5244"   # 目标实例；与 source 相同即「同实例」
username = "admin"
password = "your_password"
path = "/OneDrive/备份/照片"

[transfer]
mode = "copy"               # copy=复制保留源；move=搬完删源
overwrite = false           # 同名且大小一致就跳过；true=强制覆盖
concurrency = 3             # 跨实例搬运并发数

[schedule]
type = "interval"           # interval / daily / once
interval_minutes = 30       # 每 30 分钟跑一次
# run_at = "03:00"          # daily 模式专用
```

最常见的用法：一个 OpenList 里同时挂了两个网盘（比如阿里云盘 + OneDrive），
把 A 定时备份到 B —— 此时 `source` 和 `target` 填同一个 OpenList 地址即可。

## 运行

```bash
python main.py                 # 默认 Web 模式：起浏览器管理界面（监听 $PORT，默认 7860）
python main.py --once          # 只跑一次（先拿这个验证配置对不对）
python main.py --check         # 只测试连接、列出源目录内容
python main.py --config x.toml # 指定配置文件
```

Web 模式下打开 `http://<host>:<PORT>` 即可在页面里填写源/目标 OpenList、选择模式与调度、
手动触发并查看实时日志；配置保存在 `/data/config.toml`（持久化）。

建议第一次先用 `--check` 确认能连上、路径写对，再用 `--once` 跑一次看效果，
最后再去掉参数常驻运行。

## 文件结构

| 文件 | 作用 |
|------|------|
| `openlist_client.py` | OpenList/AList v3 API 客户端（登录、列目录、复制/移动、流式上下传） |
| `sync.py` | 搬运引擎：递归遍历、跳过判断、按同/跨实例选择策略 |
| `main.py` | 命令行入口 + 极简调度 |
| `config.toml` | 配置示例 |
| `test_sync.py` | 内存假客户端的逻辑自测（`python -m unittest test_sync`） |

## Docker（可选）

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

```bash
docker build -t opsync .
docker run -d -v $(pwd)/config.toml:/app/config.toml opsync
```

## 说明

- 接口路径/字段严格对齐 AList v3 / OpenList 官方文档（`/api/fs/list`、`/api/fs/get`、
  `/api/fs/put`、`/api/fs/copy`、`/api/fs/move`、`/api/fs/mkdir`、`/api/fs/remove`、
  `/api/auth/login`）。
- 跨实例下载会自动处理 302 跳转，且**不会把你的 token 发给第三方域名**。
- 网盘路径以 OpenList 里显示的虚拟路径为准（一般形如 `/挂载名/子目录`）。
