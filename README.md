# opsync

通过 **OpenList（AList v3 兼容）** 把网盘 A 里的东西**定时搬运**到网盘 B 的极简工具，带一个**浏览器管理界面**。

设计取舍（只做核心功能，没有花哨东西）：

- 支持**多个 OpenList 账号**：顶部标签页，每个账号一个子页面，可随时添加/切换/删除
- 每个账号的登录信息**持久化**到 `/data/config.toml`（抱脸等平台的持久化目录），刷新页面不丢失
- **同实例**（源/目标在同一个 OpenList）= 走服务端 `/api/fs/copy` 或 `/api/fs/move`，不占本地带宽
- 递归遍历源目录、自动建目录、按「文件名 + 大小」跳过已同步的文件
- 两条模式：`copy`（保留源）/ `move`（搬完删源，可选清理空目录）
- 极简调度：`interval`（每 N 分钟）/ `daily`（每天几点）/ `once`（仅手动）
- 各账号的搬运任务由全局线程池**并发执行**，互不影响
- 依赖：`requests` + `flask`

> 一个轻量的网盘定时搬运工具：填连接、浏览选目录、建多条路线、看日志，全在网页里完成。

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

1. 点右上角 **+ 添加账号**，填展示名 + OpenList 地址 + 账号 + 密码。
2. 点 **保存连接**（登录信息立即落盘，刷新不丢）；再点 **保存并连接测试**（连不通不允许建路线）。
3. 测试通过后，该账号子页面出现「搬运路线」区：点 **+ 添加路线**，每条点「浏览选择」在目录树里点出**源目录**和**目标目录**（不用手敲路径），再选模式与调度、是否启用。
4. 点 **保存路线**后调度器按各自设置自动跑；也可单条「运行」或「立即运行全部（本账号）」手动触发。
5. 可添加多个账号，每个账号独立运行；页面下方「日志」实时显示进度与每条路线最近结果。

配置保存在 `/data/config.toml`（持久化目录，重启不丢）；本地没 `/data` 时落在 `config.toml`。

命令行（本地调试用）：

```bash
python main.py --check         # 仅测试各账号连接并列出根目录
python main.py --once          # 立即执行所有已配置账号的启用路线一次
python main.py --config x.toml # 指定配置文件
```

## 配置示例（config.toml）

```toml
[[openlists]]
id = "conn-1"
name = "账号1"
url = "https://a.hf.space"
username = "admin"
password = "密码"
tested_ok = true

  [[openlists.routes]]
  name = "照片备份"
  src_path = "/阿里云盘/照片"
  dst_path = "/OneDrive/备份/照片"
  mode = "copy"
  enabled = true
  overwrite = false
  delete_empty_dirs = false
  schedule_type = "interval"
  interval_minutes = 30
  run_at = "03:00"

[[openlists]]
id = "conn-2"
name = "账号2"
url = "https://b.hf.space"
username = "admin"
password = "密码"
tested_ok = true

  [[openlists.routes]]
  name = "文档同步"
  src_path = "/115/文档"
  dst_path = "/天翼云/文档"
  mode = "move"
  enabled = true
```

也可通过环境变量覆盖**第一个**账号的连接（`OPSYNC_URL` / `OPSYNC_USERNAME` / `OPSYNC_PASSWORD`，优先级高于网页保存的配置）。路线建议在网页里建。

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
| `web.py` | Web 管理界面（多账号子页面 + 目录浏览器）+ 后台并发调度器 |
| `confighelper.py` | 配置加载/序列化（多账号模型 + 递归 TOML 写入）/保存路径 |
| `main.py` | 命令行入口，默认起 Web |
| `config.toml` | 配置示例 |
| `test_sync.py` | 逻辑自测（`python -m unittest test_sync`） |

## Docker（可选）

```bash
docker build -t opsync .
docker run -d -p 7860:7860 -v $(pwd)/data:/data opsync
```

镜像已发布在 `ghcr.io/tianjian518/opsync:latest`（**多架构：amd64 + arm64**，由 GitHub Actions 自动构建），可直接 `FROM` 使用。

## 说明

- 接口路径/字段严格对齐 AList v3 / OpenList 官方文档（`/api/fs/list`、`/api/fs/get`、
  `/api/fs/put`、`/api/fs/copy`、`/api/fs/move`、`/api/fs/mkdir`、`/api/fs/remove`、
  `/api/auth/login`）。
- 跨实例下载会自动处理 302 跳转，且**不会把你的 token 发给第三方域名**。
- 网盘路径以 OpenList 里显示的虚拟路径为准（一般形如 `/挂载名/子目录`）。
