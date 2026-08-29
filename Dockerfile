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

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# No VOLUME declaration for /data, deliberately. Railway rejects a Dockerfile
# that has one -- persistence there is a Railway Volume attached to the service,
# not something the image can ask for. It would earn nothing elsewhere either:
# compose declares its own named volume and `docker run -v` overrides it, so all
# a VOLUME line adds is an anonymous volume per container when you forget one.
# The entrypoint creates $DATAQ_DATA_DIR itself, so the path exists regardless
# of what is (or is not) mounted over it.

# Documentation only: the process binds $PORT, which the platform injects.
EXPOSE 8000

# Probes the port the server is actually on, not a hardcoded one.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/api/health\")"

# The entrypoint restores a pending data bundle before starting uvicorn, and
# binds $PORT. It must be shell form for the variable to expand.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
