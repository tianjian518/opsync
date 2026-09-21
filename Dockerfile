FROM python:3.11-slim
WORKDIR /app

# 先装依赖，利用层缓存
# PIP_NO_RICH=1：部分沙箱/ARM 环境里 pip 的 rich 进度条会尝试开线程而失败
# （can't start new thread），关掉它即可正常安装；对最终镜像无影响。
COPY requirements.txt .
RUN PIP_NO_RICH=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    pip install --no-cache-dir --progress-bar off -r requirements.txt

# 再拷代码
COPY . .

# 用非 root 用户运行，更安全
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Web 模式下监听此端口（抱脸会注入 $PORT，默认 7860）
EXPOSE 7860

# 若 /data/config.toml 存在则优先使用（抱脸等平台的持久化目录），否则用内置 config.toml
# 默认以 Web 模式运行（提供浏览器管理界面）
ENTRYPOINT ["python", "main.py"]
