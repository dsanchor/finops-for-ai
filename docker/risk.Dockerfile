FROM ghcr.io/astral-sh/uv:0.13.4 AS uv
FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" RISK_PORT=8001
WORKDIR /app
RUN useradd --uid 10001 --create-home agent
COPY --from=builder /app/.venv /app/.venv
COPY agents /app/agents
USER agent
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('RISK_PORT','8001')+'/healthz',timeout=3)"
CMD ["python", "-m", "agents.risk"]
