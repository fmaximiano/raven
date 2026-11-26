# RAVEN — imagem única (interface web + CLI). Portável: Railway, VPS ou qualquer host Docker.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    RAVEN_DATA_DIR=/data \
    RAVEN_CONFIG_DIR=/app/config

WORKDIR /app

# Dependências primeiro (camada reaproveitada enquanto uv.lock não mudar).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev

# O processo roda como o usuário sem privilégios "raven". O entrypoint começa como root só para
# ajustar a posse do volume montado em /data (o Railway monta volumes como root) e então troca
# de usuário com setpriv.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh \
    && useradd --create-home --uid 10001 raven \
    && mkdir -p /data && chown raven:raven /data
ENTRYPOINT ["docker-entrypoint.sh"]

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)"

CMD ["raven", "web"]
