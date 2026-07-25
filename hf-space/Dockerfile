# opsync 部署到 Hugging Face Docker Space
# Web 管理界面监听 $PORT（默认 7860），配置 / 触发 / 日志都在浏览器完成。
# 只改这一行即可：基于已推送到 GHCR 的预构建多架构镜像（amd64 + arm64，pin 到 digest 确保拉到新构建）。
# 镜像由 GitHub Actions 自动构建并推送（含 arm64，Apple Silicon / ARM 服务器 / 部分 HF Space 均可运行）。
FROM ghcr.io/tianjian518/opsync:latest@sha256:0ce9551662774ffe6d30111876dabf1ce3319947e2bafec9c699776103723dca
