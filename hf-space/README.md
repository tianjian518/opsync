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
带一个**浏览器管理界面**：多账号、填连接、浏览选目录、建多条路线、手动触发、看日志全在网页里完成。

本 Space 只改了 `Dockerfile`（一行 `FROM` 镜像）和这个 `README`，源码与镜像都在
[docker.io/tianjian518/opsync](https://hub.docker.com/r/tianjian518/opsync)，
Space 里无需放任何代码。容器启动后监听 `$PORT`（默认 7860），打开 Space 页面即是管理界面。

## 工作原理

- **同实例**（源/目标在同一个 OpenList）= 服务端复制/移动，不占本地带宽
- 按「文件名 + 大小」跳过已同步的文件；`copy`（保留源）/ `move`（搬完删源，可清理空目录）
- **多个 OpenList 账号**：顶部标签页，每个账号一个子页面，可随时添加/切换/删除
- 每个账号的登录信息**持久化**到 `/data/config.toml`，刷新页面不丢失
- 每个账号下可建**多条搬运路线**，每条独立设置源/目标目录、模式、调度
- 各账号任务由全局线程池**并发执行**，互不影响
- 极简调度：`interval`（每 N 分钟）/ `daily`（每天几点）/ `once`（仅手动）

## 使用（全在网页里）

1. 打开 Space 页面（即 Web 管理界面）。
2. 点右上角 **+ 添加账号**，填展示名 + OpenList 地址 + 账号 + 密码，点「保存连接」（立即落盘）。
3. 点「保存并连接测试」（连不通不允许建路线）。测试通过后该账号出现「搬运路线」区。
4. 点「+ 添加路线」，每条点「浏览选择」在目录树里点出**源目录**和**目标目录**（不用手敲路径），
   再选模式（copy/move）和调度（interval/daily/once），勾选是否启用。
5. 点「保存路线」后调度器自动跑；也可单条「运行」或「立即运行全部（本账号）」手动触发。
6. 页面下方「日志」实时显示同步进度与每条路线最近结果。可再加别的账号，各自独立运行。

> 配置保存在 Space 的持久化目录 `/data/config.toml`，重启不丢。
> 也可通过 Space 的 **Variables** 注入 `OPSYNC_URL` / `OPSYNC_USERNAME` / `OPSYNC_PASSWORD`
> 覆盖**第一个**账号的连接（优先级高于网页保存的配置）；路线建议在网页里建。

## 本地调试

```bash
docker run --rm -p 7860:7860 \
  -e OPSYNC_URL=https://你的openlist \
  -e OPSYNC_USERNAME=admin -e OPSYNC_PASSWORD=你的密码 \
  docker.io/tianjian518/opsync:latest
# 然后浏览器打开 http://127.0.0.1:7860
```

## 排错：连接测试 / 搬运时「连不上」

- **Docker Space 必须开启联网**：在 Space 的 **Settings → 勾选 "Internet access"**（默认可能是关的）。
  否则容器**完全无法访问外网**，连你自己的 OpenList 也会全部失败（包括换了好几个都不行）。
  这是「换了好几个都连不上」最常见的原因——不是地址/账号问题，而是 Space 出不去网。
- **目标 OpenList 是个休眠的 HF Space**：首个请求会被返回一段 HTML（warming 页面），
  于是客户端报「返回非 JSON」。现在客户端会自动重试唤醒，稍等几秒再测即可；
  若仍失败，错误信息会明确提示「响应为 HTML，大概率是休眠的 HF Space warming 页面」。
- **看错误原文**：连接测试 / 浏览目录 / 运行时若失败，界面会直接显示**带 HTTP 状态码和响应片段**的报错，
  不再是难懂的 `Expecting value`。例如 `... 返回非 JSON（HTTP 200）。响应为 HTML ...`，
  据此判断是「休眠 warming 页」「代理拦截」还是「地址/端口错误」。
- 确认 OpenList 地址以 `http(s)://` 开头、端口正确，且账号密码无误（登录接口 `code != 200` 会显示具体原因）。

源码见 GitHub：`https://github.com/tianjian518/opsync`
