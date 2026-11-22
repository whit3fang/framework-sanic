# syntax=docker/dockerfile:1
#
# Images:
#   --target base   the framework runtime image: production extras (gcp, redis), non-root, no dev
#                   tools, prebuilt UI inside.
#                   Integrations build FROM it with docker/integration.Dockerfile.
#   --target dev    the whole workspace + dev tools (docker compose: app, fake Dialpad, contract tests)
#
#   docker build --target base -t framework-sanic-base .
#   docker build -f docker/integration.Dockerfile --build-arg INTEGRATION=integrations/hubspot \
#       --build-arg BASE_IMAGE=framework-sanic-base -t integration-hubspot .

ARG PYTHON=python:3.12-slim
ARG UV=ghcr.io/astral-sh/uv:0.13.9

# --- UI: built into the Python package (src/framework_sanic/ui_dist) ----------------------------
FROM node:22-alpine AS ui
RUN corepack enable
WORKDIR /src
COPY ui/ ui/
RUN mkdir -p src/framework_sanic && cd ui \
    && if [ -f pnpm-lock.yaml ]; then pnpm install --frozen-lockfile; else pnpm install; fi \
    && pnpm --filter @ifw/ui-shell build

FROM ${UV} AS uv

# --- builder: the framework as a regular (non-editable) package in /app/.venv -----------------
FROM ${PYTHON} AS builder
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra gcp --extra redis
COPY src ./src
COPY --from=ui /src/src/framework_sanic/ui_dist ./src/framework_sanic/ui_dist
RUN uv sync --frozen --no-dev --no-editable --extra gcp --extra redis

# --- base: what runs in production -------------------------------------------------------------
FROM ${PYTHON} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY --from=uv /uv /usr/local/bin/uv
RUN useradd --create-home --uid 10001 app
COPY --from=builder /app/.venv /app/.venv
WORKDIR /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"]
CMD ["framework-sanic", "serve", "--host", "0.0.0.0", "--port", "8000"]

# --- dev: the whole workspace (framework + integrations/*) with dev tools ---------------------
FROM ${PYTHON} AS dev
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY . .
COPY --from=ui /src/src/framework_sanic/ui_dist ./src/framework_sanic/ui_dist
RUN uv sync --frozen --all-extras --all-packages
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["framework-sanic", "serve", "--host", "0.0.0.0", "--port", "8000"]
