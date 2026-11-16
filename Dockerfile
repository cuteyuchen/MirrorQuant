# Stage 1: Build Vue 3 Frontend
FROM node:22-alpine AS frontend-builder
WORKDIR /build/frontend
# 根路径部署的构建产物：VITE_BASE_PATH 由 CI 传入 /；本地 compose 默认 /mq/。
ARG VITE_BASE_PATH=/mq/
ENV VITE_BASE_PATH=$VITE_BASE_PATH
COPY VERSION /build/VERSION
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2: Python 3.12 Backend + Static Assets
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MQ_DATA_DIR=/app/data \
    MQ_HOST=0.0.0.0 \
    MQ_PORT=8000

WORKDIR /app/backend

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY VERSION /app/VERSION
COPY --from=frontend-builder /build/frontend/dist ./static

# 固定低权限 UID，宿主机绑定目录需授予该 UID 写权限。
RUN groupadd --gid 10001 mirrorquant \
    && useradd --uid 10001 --gid mirrorquant --no-create-home --shell /usr/sbin/nologin mirrorquant \
    && mkdir -p /app/data \
    && chown mirrorquant:mirrorquant /app/data \
    && chmod 700 /app/data

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
