#!/usr/bin/env python3
"""生成本地自包含的 Space Dockerfile：把工具源码内联进镜像。

这样部署到 Hugging Face Space 时，只需改写 Space 的 Dockerfile + README 两个文件，
不需要 Docker Hub / GitHub 等外部仓库。源码从这里（本地）读取后写进 Dockerfile 的 heredoc。
"""
import pathlib

SRC = pathlib.Path("/workspace/opsync")
OUT = SRC / "hf-space" / "Dockerfile"

# 运行时需要的文件（顺序无关）
files = ["requirements.txt", "openlist_client.py", "sync.py", "main.py", "config.toml"]

parts = [
    "FROM python:3.11-slim",
    "WORKDIR /app",
    "",
    "# 安装唯一的第三方依赖",
    "RUN pip install --no-cache-dir requests",
    "",
    "# 以下源码由本地 gen_space_dockerfile.py 内联进镜像，",
    "# 使 Space 只需改 Dockerfile + README 即可运行，无需外部仓库。",
]

for f in files:
    content = (SRC / f).read_text(encoding="utf-8").rstrip("\n")
    parts.append(f"# ---- {f} ----")
    parts.append("RUN cat > /app/%s <<'TAOSYNC_EOF'" % f)
    parts.append(content)
    parts.append("TAOSYNC_EOF")
    parts.append("")

parts += [
    "# 用非 root 用户运行",
    "RUN useradd -m appuser && chown -R appuser:appuser /app",
    "USER appuser",
    "",
    "# 若 /data/config.toml 存在则优先使用，否则用内置 config.toml；",
    "# docker run 的额外参数会正确追加到 python main.py 之后。",
    'ENTRYPOINT ["python", "main.py"]',
]

OUT.write_text("\n".join(parts) + "\n", encoding="utf-8")
print("已生成:", OUT, "大小:", OUT.stat().st_size, "字节")
