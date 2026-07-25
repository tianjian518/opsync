FROM python:3.11-slim
WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

# 用非 root 用户运行，更安全
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# 若 /data/config.toml 存在则优先使用（抱脸等平台的持久化目录），否则用内置 config.toml
# 用 ENTRYPOINT，使 `docker run 镜像 --once` 等参数能正确追加到 python main.py 之后
ENTRYPOINT ["python", "main.py"]
