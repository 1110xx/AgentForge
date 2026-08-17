ARG PYTHON_BASE_IMAGE=python:3.13-slim-bookworm
ARG UV_VERSION=0.12.4

FROM ${PYTHON_BASE_IMAGE} AS builder
ARG UV_VERSION=0.12.4
ARG UV_INDEX_URL=
ARG INSTALL_DEV=false
ENV UV_DEFAULT_INDEX=${UV_INDEX_URL:-https://pypi.org/simple}
ENV UV_PROJECT_ENVIRONMENT=/app/backend/.venv
WORKDIR /app/backend
RUN if [ -n "${UV_INDEX_URL}" ]; then \
        python -m pip install --no-cache-dir --index-url "${UV_INDEX_URL}" "uv==${UV_VERSION}"; \
    else \
        python -m pip install --no-cache-dir "uv==${UV_VERSION}"; \
    fi
COPY backend/pyproject.toml backend/uv.lock /app/backend/
RUN if [ "${INSTALL_DEV}" = "true" ]; then \
        uv sync --frozen --no-install-project; \
    else \
        uv sync --frozen --no-install-project --no-dev; \
    fi
COPY backend/src /app/backend/src
COPY backend/alembic.ini /app/backend/alembic.ini
COPY backend/alembic /app/backend/alembic
RUN if [ "${INSTALL_DEV}" = "true" ]; then \
        uv sync --frozen; \
    else \
        uv sync --frozen --no-dev; \
    fi

FROM ${PYTHON_BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/backend/.venv/bin:${PATH}
WORKDIR /app/backend
COPY --from=builder /app/backend /app/backend
RUN chown -R 65532:65532 /app
USER 65532:65532
CMD ["/app/backend/.venv/bin/python", "-m", "enterprise_agent_platform.platform.entrypoint", "api"]
