FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
FROM python:3.13.18-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev \
 && useradd --system --uid 10001 jarvis \
 && mkdir /data && chown jarvis /data
USER jarvis
ENV PATH=/app/.venv/bin:$PATH
EXPOSE 8700
CMD ["uvicorn", "jarvis_hub.main:app", "--host", "0.0.0.0", "--port", "8700", "--no-server-header"]
