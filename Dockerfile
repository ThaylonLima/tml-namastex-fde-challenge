# Agente AutoSeguro (FastAPI). Build a partir da raiz do repositorio:
#   docker build -t autoseguro-agent .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /srv

# dependencias primeiro (cache de camada); sem grupo dev
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY agent ./agent

# sessoes (SQLite sanitizado) ficam em volume; usuario sem privilegios
RUN useradd --create-home --uid 10001 app && mkdir -p /srv/data && chown -R app /srv
USER app

# logs JSONL vao para stderr (docker compose logs agent); defina LOG_FILE para tambem gravar em arquivo
ENV QUOTE_API_URL=http://quote-api:8000 \
    SESSION_DB=/srv/data/sessions.sqlite

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
  CMD ["/srv/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=2).status == 200 else 1)"]
CMD ["/srv/.venv/bin/uvicorn", "agent.app:production_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
