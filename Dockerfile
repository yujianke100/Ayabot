# ──────────────────────────────────────────────
#  bilibili-live-robot — 多阶段构建
# ──────────────────────────────────────────────

# ── Stage 1: 依赖安装 ──
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: 运行镜像 ──
FROM python:3.11-slim

LABEL org.opencontainers.image.title="bilibili-live-robot"
LABEL org.opencontainers.image.description="轻量 B 站直播间弹幕机器人"
LABEL org.opencontainers.image.source="https://github.com/yujianke100/bilibili-live-robot"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# 从 builder 拷贝已安装的包
COPY --from=builder /usr/local/lib/python3.11/site-packages/ /usr/local/lib/python3.11/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# 拷贝应用代码
COPY . .

# 默认端口（Web 管理后台）
EXPOSE 8000

# 数据卷（config.yaml + SQLite + credential）
VOLUME ["/app/data", "/app/config.yaml"]

# 启动入口
CMD ["python", "-m", "app.main"]
