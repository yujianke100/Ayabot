# ──────────────────────────────────────────────
#  ayabot — B 站弹幕机器人（多阶段构建）
# ──────────────────────────────────────────────

# ── Stage 1: 依赖安装 ──
FROM python:3.13-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: 运行镜像 ──
FROM python:3.13-slim

LABEL org.opencontainers.image.title="ayabot"
LABEL org.opencontainers.image.description="轻量 B 站直播间弹幕机器人"
LABEL org.opencontainers.image.source="https://github.com/yujianke100/ayabot"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# 从 builder 拷贝已安装的包
COPY --from=builder /usr/local/lib/python3.13/site-packages/ /usr/local/lib/python3.13/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# 拷贝应用代码
COPY . .

# 默认端口（Web 管理后台）
EXPOSE 19810

# 数据卷（配置文件 + 房间数据 + 账号凭证）
VOLUME ["/app/data", "/app/rooms", "/app/accounts"]

# 启动入口：运行 WebUI（Bot 子进程由 ProcessManager 自动管理）
CMD ["python", "-m", "uvicorn", "app.web.server:app", "--host", "0.0.0.0", "--port", "19810"]
