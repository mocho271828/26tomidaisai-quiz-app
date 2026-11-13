ARG SOURCE_COMMIT=unversioned
ARG DEPLOYMENT_ENVIRONMENT=local-demo

FROM ghcr.io/astral-sh/uv:0.11.28 AS uv

FROM python:3.12.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/src ./src
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
RUN uv sync --frozen --no-dev

ARG SOURCE_COMMIT
ARG DEPLOYMENT_ENVIRONMENT

LABEL org.opencontainers.image.revision="${SOURCE_COMMIT}" \
    io.tomidaisai.deployment-environment="${DEPLOYMENT_ENVIRONMENT}"

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "quiz_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "5"]
