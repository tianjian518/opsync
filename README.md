# opsync

通过 **OpenList（AList v3 兼容）** 把网盘 A 里的东西**定时搬运**到网盘 B 的极简工具，带一个**浏览器管理界面**。

设计取舍（只做核心功能，没有花哨东西）：

- 在**同一个 OpenList 实例**里挂了多个网盘，把 A 定时搬到 B
- **同实例**搬运走服务端 `/api/fs/copy` 或 `/api/fs/move`，不占本地带宽
- 递归遍历源目录、自动建目录、按「文件名 + 大小」跳过已同步的文件
- 两条模式：`copy`（保留源）/ `move`（搬完删源，可选清理空目录）
- 极简调度：`interval`（每 N 分钟）/ `daily`（每天几点）/ `once`（仅手动）
- Web 管理界面：填 OpenList 账号密码 → 连接测试 → 浏览选择源/目标目录 → 建多条路线
- 依赖：`requests` + `flask`

> 相比原版 taosync，本工具砍掉了前端框架、SQLite、作业管理、通知、排除规则等，
> 只保留「定时把网盘 A 搬到网盘 B」这一件事，并用一个轻量 Web 界面来管理。

## 安装

```bash
pip install -r requirements.txt
```

需要 Python 3.11+（用到内置 `tomllib` 解析配置）。

## 使用（全在网页里）

```bash
python main.py                 # 起 Web 管理界面（监听 $PORT，默认 7860）
```

浏览器打开 `http://<host>:<PORT>`，按这个顺序操作：

1. **填 OpenList 连接**：地址 + 账号 + 密码，点「保存并连接测试」（会自动连接测试，通过才允许下一步）。
2. **建搬运路线**（可建多条）：每条路线选 源目录 / 目标目录（点「浏览选择」在目录树里点选，**不用手敲路径**）、选模式与调度、是否启用。
3. **保存路线**后调度器按各自设置自动跑；也可点「运行」单条触发、「立即运行全部」一次跑完。
4. 页面下方「日志」实时显示同步进度与每条路线的上次结果。

配置保存在 `/data/config.toml`（持久化目录，Space 重启不丢）；本地没 `/data` 时落在 `config.toml`。

命令行（本地调试用）：

```bash
python main.py --check         # 仅测试连接并列出根目录
python main.py --once          # 立即执行所有启用的路线一次
python main.py --config x.toml # 指定配置文件
```

## 配置示例（config.toml）

```toml
[openlist]
url = "http://127.0.0.1:5244"   # OpenList 地址
username = "admin"
password = "your_password"      # 占位，请用网页填写并连接测试

[[routes]]
name = "路线1"
src_path = "/阿里云盘/照片"          # 源目录
dst_path = "/OneDrive/备份/照片"     # 目标目录
mode = "copy"                       # copy / move
enabled = true
overwrite = false
delete_empty_dirs = false
schedule_type = "interval"          # interval / daily / once
interval_minutes = 30
run_at = "03:00"
```

也可通过环境变量覆盖 OpenList 连接（`TAOSYNC_URL` / `TAOSYNC_USERNAME` / `TAOSYNC_PASSWORD`，优先级高于网页保存的配置）。路线仍建议在网页里建。

## 本地自测

```bash
python -m unittest test_sync -v
```

用内存假客户端验证递归遍历、跳过判断、copy/move 行为，不依赖真实 OpenList。

## 文件结构

| 文件 | 作用 |
|------|------|
| `openlist_client.py` | OpenList/AList v3 API 客户端（登录、列目录、复制/移动、下载上传、删除） |
| `sync.py` | 搬运引擎：`SyncEngine` 单连接、按路线 `run_route` 递归遍历 |
| `web.py` | Web 管理界面 + 后台调度器（连接测试、目录浏览、多路线管理） |
| `confighelper.py` | 配置加载/合并/保存（CLI 与 Web 共用） |
| `main.py` | 命令行入口，默认起 Web |
| `config.toml` | 配置示例 |
| `test_sync.py` | 逻辑自测（`python -m unittest test_sync`） |

## Docker（可选）

```bash
docker build -t opsync .
docker run -d -p 7860:7860 -v $(pwd)/data:/data opsync
```

镜像已发布在 `docker.io/tianjian518/opsync:latest`，可直接 `FROM` 使用。

## 说明

- 接口路径/字段严格对齐 AList v3 / OpenList 官方文档（`/api/fs/list`、`/api/fs/get`、
  `/api/fs/put`、`/api/fs/copy`、`/api/fs/move`、`/api/fs/mkdir`、`/api/fs/remove`、
  `/api/auth/login`）。
- 跨实例下载会自动处理 302 跳转，且**不会把你的 token 发给第三方域名**。
- 网盘路径以 OpenList 里显示的虚拟路径为准（一般形如 `/挂载名/子目录`）。
