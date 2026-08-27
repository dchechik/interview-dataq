# Stage 1: build the SPA.
FROM node:22-slim AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python runtime serving both /api and the built bundle, so the whole
# app is one container with one volume.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    DATAQ_DATA_DIR=/data

# Dependencies first, so a source-only change does not re-resolve them.
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./backend/
RUN cd backend && uv sync --frozen --no-install-project --no-dev

COPY backend/ ./backend/
RUN cd backend && uv sync --frozen --no-dev

# FastAPI serves this directory at / when it exists (see api/app.py).
COPY --from=frontend /app/dist ./static

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["uvicorn", "dataq.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend/src"]
