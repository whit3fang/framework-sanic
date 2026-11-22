FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --extra gcp --group dev

COPY src ./src
COPY tests ./tests
COPY tools ./tools
RUN uv sync --frozen --extra gcp --group dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["framework-sanic", "serve", "--host", "0.0.0.0", "--port", "8000"]
