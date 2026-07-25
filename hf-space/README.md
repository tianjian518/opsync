---
title: opsync
emoji: 📦
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---

# opsync

通过 **OpenList（AList v3 兼容）** 把网盘 A 里的东西**定时搬运**到网盘 B 的极简工具，
带一个**浏览器管理界面**：配置、手动触发、看日志全在网页里完成。

本 Space 只改了 `Dockerfile`（一行 `FROM` 镜像）和这个 `README`，源码与镜像都在
[docker.io/tianjian518/opsync](https://hub.docker.com/r/tianjian518/opsync)，
Space 里无需放任何代码。容器启动后监听 `$PORT`（默认 7860），打开 Space 页面即是管理界面。

## 工作原理

- **同实例**（源/目标在同一个 OpenList）= 服务端复制/移动，不占本地带宽
- **跨实例**（两个不同 OpenList）= 边下载边上传流式搬运
- 按「文件名 + 大小」跳过已同步的文件；支持 `copy`（保留源）/ `move`（搬完删源）
- 极简调度：`interval`（每 N 分钟）/ `daily`（每天几点）/ `once`（仅手动）

## 使用（全在网页里）

1. 打开 Space 页面（即 Web 管理界面）。
2. 填「源 OpenList」和「目标 OpenList」的地址、账号、密码、目录路径。
3. 选搬运模式（copy/move）、调度方式（interval/daily/once）。
4. 点 **保存配置**，再点 **立即运行一次** 验证；之后调度器会按设置自动跑。
5. 页面下方的「日志」实时显示同步进度。

> 配置保存在 Space 的持久化目录 `/data/config.toml`，重启不丢。
> 也仍可用 Space 的 **Variables** 注入 `TAOSYNC_*` 环境变量（优先级高于网页保存的配置）。

## 本地调试

```bash
docker run --rm -p 7860:7860 \
  -e TAOSYNC_SOURCE_URL=... -e TAOSYNC_SOURCE_USERNAME=... -e TAOSYNC_SOURCE_PASSWORD=... -e TAOSYNC_SOURCE_PATH=/a \
  -e TAOSYNC_TARGET_URL=... -e TAOSYNC_TARGET_USERNAME=... -e TAOSYNC_TARGET_PASSWORD=... -e TAOSYNC_TARGET_PATH=/b \
  docker.io/tianjian518/opsync:latest
# 然后浏览器打开 http://127.0.0.1:7860
```

源码见 GitHub：`https://github.com/tianjian518/opsync`
