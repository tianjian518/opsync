#!/usr/bin/env bash
# opsync 一键部署：推到 Docker Hub + GitHub，并把镜像部署进抱脸 Space（只改 Space 的 Dockerfile+README）
#
# 使用前请先登录：
#   docker login
#   gh auth login
#   hf auth login
#
# 然后这样跑（三个都是公开标识，不是密钥）：
#   DH_USER=你的DockerHub用户名 \
#   GH_REPO=你的GitHub用户名/仓库名 \
#   HF_SPACE=你的HF用户名/space名 \
#   ./deploy.sh
set -euo pipefail

DH_USER="${DH_USER:?请设置 Docker Hub 用户名 (DH_USER)}"
GH_REPO="${GH_REPO:?请设置 GitHub 仓库, 如 user/repo (GH_REPO)}"
HF_SPACE="${HF_SPACE:-}"   # 可选：不填则跳过抱脸部署

TAG="docker.io/$DH_USER/opsync:latest"

echo ">>> [1/3] 推送镜像到 Docker Hub: $TAG"
docker tag opsync:latest "$TAG"
docker push "$TAG"

echo ">>> [2/3] 推送源码到 GitHub: $GH_REPO"
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/$GH_REPO.git"
git branch -M main
git push -u origin main

if [ -n "$HF_SPACE" ]; then
  echo ">>> [3/3] 部署到抱脸 Space: $HF_SPACE (只改 Dockerfile + README)"
  TMP="$(mktemp -d)"
  git clone "https://huggingface.co/spaces/$HF_SPACE" "$TMP/space"
  cp hf-space/Dockerfile "$TMP/space/Dockerfile"
  cp hf-space/README.md   "$TMP/space/README.md"
  # 把占位用户名换成真实的 Docker Hub 用户名
  sed -i "s|<你的DockerHub用户名>|$DH_USER|g" "$TMP/space/Dockerfile" "$TMP/space/README.md"
  ( cd "$TMP/space" && git add -A && git commit -m "deploy opsync" && git push )
  rm -rf "$TMP"
else
  echo ">>> [3/3] 跳过抱脸部署（未设置 HF_SPACE）"
fi

echo "完成。抱脸 Space 的 Dockerfile 现在只有一行: FROM $TAG"
