# syntax=docker/dockerfile:1

# ---- frontend build stage --------------------------------------------------
FROM node:24-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN useradd --create-home --uid 10001 repairdesk

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY config/ ./config/
COPY --from=frontend /build/frontend/dist ./frontend/dist

RUN mkdir -p /data && chown -R repairdesk:repairdesk /data /app
USER repairdesk

ENV APP_HOST=127.0.0.1 \
    APP_PORT=8000 \
    DATABASE_PATH=/data/repairdesk.sqlite \
    VERIFICATION_POLICY_PATH=/app/config/verification.yaml \
    STATIC_DIR=/app/frontend/dist

EXPOSE 8000
# Bind inside the container to localhost semantics via compose port mapping
# 127.0.0.1:8000:8000 — the container binds 0.0.0.0 so the host mapping can
# be restricted to loopback.
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
