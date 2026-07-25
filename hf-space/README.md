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
带一个**浏览器管理界面**：填连接、浏览选目录、建多条路线、手动触发、看日志全在网页里完成。

本 Space 只改了 `Dockerfile`（一行 `FROM` 镜像）和这个 `README`，源码与镜像都在
[docker.io/tianjian518/opsync](https://hub.docker.com/r/tianjian518/opsync)，
Space 里无需放任何代码。容器启动后监听 `$PORT`（默认 7860），打开 Space 页面即是管理界面。

## 工作原理

- **同实例**（源/目标在同一个 OpenList）= 服务端复制/移动，不占本地带宽
- 按「文件名 + 大小」跳过已同步的文件；支持 `copy`（保留源）/ `move`（搬完删源，可清理空目录）
- 可同时建**多条搬运路线**，每条路线独立设置源目录 / 目标目录 / 模式 / 调度
- 极简调度：`interval`（每 N 分钟）/ `daily`（每天几点）/ `once`（仅手动）

## 使用（全在网页里）

1. 打开 Space 页面（即 Web 管理界面）。
2. **填 OpenList 连接**：地址 + 账号 + 密码，点「保存并连接测试」（连不通不让下一步）。
3. **建搬运路线**（可多条）：每条点「浏览选择」在目录树里点出**源目录**和**目标目录**（不用手敲路径），
   再选模式（copy/move）和调度（interval/daily/once），勾选是否启用。
4. 点「保存路线」后调度器自动跑；也可单条「运行」或「立即运行全部」手动触发。
5. 页面下方「日志」实时显示同步进度与每条路线的最近结果。

> 配置保存在 Space 的持久化目录 `/data/config.toml`，重启不丢。
> 也可通过 Space 的 **Variables** 注入 `TAOSYNC_URL` / `TAOSYNC_USERNAME` / `TAOSYNC_PASSWORD`
> 覆盖 OpenList 连接（优先级高于网页保存的配置）；路线建议在网页里建。

## 本地调试

```bash
docker run --rm -p 7860:7860 \
  -e TAOSYNC_URL=http://127.0.0.1:5244 \
  -e TAOSYNC_USERNAME=admin -e TAOSYNC_PASSWORD=你的密码 \
  docker.io/tianjian518/opsync:latest
# 然后浏览器打开 http://127.0.0.1:7860
```

源码见 GitHub：`https://github.com/tianjian518/opsync`
