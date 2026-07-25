---
title: opsync
emoji: 📦
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---

# opsync

通过 **OpenList（AList v3 兼容）** 把网盘 A 里的东西**定时搬运**到网盘 B 的极简工具。
本 Space 只改了 `Dockerfile`（一行 `FROM` 镜像）和这个 `README`，源码与镜像都在
[docker.io/tianjian518/opsync](https://hub.docker.com/r/tianjian518/opsync)，
Space 里无需放任何代码。

## 工作原理

- **同实例**（源/目标在同一个 OpenList）= 服务端复制/移动，不占本地带宽
- **跨实例**（两个不同 OpenList）= 边下载边上传流式搬运
- 按「文件名 + 大小」跳过已同步的文件；支持 `copy`（保留源）/ `move`（搬完删源）
- 极简调度：`interval`（每 N 分钟）/ `daily`（每天几点）

## 配置（在 Space 设置里填 Variables 即可，无需改文件）

| 变量 | 说明 | 示例 |
|------|------|------|
| `TAOSYNC_SOURCE_URL` | 源 OpenList 地址 | `http://127.0.0.1:5244` |
| `TAOSYNC_SOURCE_USERNAME` | 源账号 | `admin` |
| `TAOSYNC_SOURCE_PASSWORD` | 源密码 | `****` |
| `TAOSYNC_SOURCE_PATH` | 源目录 | `/阿里云盘/照片` |
| `TAOSYNC_TARGET_URL` | 目标 OpenList 地址 | `http://127.0.0.1:5244` |
| `TAOSYNC_TARGET_USERNAME` | 目标账号 | `admin` |
| `TAOSYNC_TARGET_PASSWORD` | 目标密码 | `****` |
| `TAOSYNC_TARGET_PATH` | 目标目录 | `/OneDrive/备份/照片` |
| `TAOSYNC_MODE` | `copy` 或 `move` | `copy` |
| `TAOSYNC_OVERWRITE` | 是否强制覆盖（true/false） | `false` |
| `TAOSYNC_CONCURRENCY` | 跨实例并发数 | `3` |
| `TAOSYNC_SCHEDULE_TYPE` | `interval` / `daily` / `once` | `interval` |
| `TAOSYNC_INTERVAL_MINUTES` | interval 模式间隔（分钟） | `30` |
| `TAOSYNC_RUN_AT` | daily 模式执行时间 | `03:00` |

> 密码类变量建议用 **Secrets**。嫌麻烦也可把 `config.toml` 放到 Space 的 `/data/config.toml`
> （优先于 Variables 和内置配置），但最省事还是上面这组 Variables。

## 使用

1. 确保镜像已推到 Docker Hub：`docker.io/tianjian518/opsync:latest`
2. 本 Space 的 `Dockerfile` 已是 `FROM docker.io/tianjian518/opsync:latest`
3. 在 Space 设置 → **Variables** 填好上面的变量
4. 重启 Space，日志里能看到「开始同步 / 下次执行时间」

## 本地调试

```bash
docker run --rm \
  -e TAOSYNC_SOURCE_URL=... -e TAOSYNC_SOURCE_USERNAME=... -e TAOSYNC_SOURCE_PASSWORD=... -e TAOSYNC_SOURCE_PATH=/a \
  -e TAOSYNC_TARGET_URL=... -e TAOSYNC_TARGET_USERNAME=... -e TAOSYNC_TARGET_PASSWORD=... -e TAOSYNC_TARGET_PATH=/b \
  -e TAOSYNC_SCHEDULE_TYPE=once \
  docker.io/tianjian518/opsync:latest --check
```

源码见 GitHub：`https://github.com/tianjian518/opsync`
