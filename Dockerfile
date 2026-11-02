# Core API for the VPS hub: `tidetable serve`. The todo files and registry are
# a mounted volume, never baked into the image.
FROM python:3.12-slim
# Trust store hardening: install the current `ca-certificates` so the image
# does not trust the compromised eMudhra roots ("emSign ECC Root CA - G3" /
# "AAA Certificate Services") -- eMudhra intermediate CA compromise, 2026-10-20.
# See https://emudhra.we-have-a-problem.com
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY tidetable ./tidetable
COPY taskerkeeper ./taskerkeeper
RUN pip install . && rm -rf /src

RUN useradd --system --uid 10001 --no-create-home tidetable \
 && mkdir -p /app/projects && chown tidetable /app/projects
USER tidetable
WORKDIR /app

EXPOSE 8471
# The API is bearer-only, so health is "the port accepts a connection".
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s   CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8471), 3).close()"

# serve exits non-zero on bad args; the registry lives on the volume.
ENTRYPOINT ["tidetable"]
CMD ["serve", "--bind", "0.0.0.0", "--port", "8471", "--registry", "/app/projects/registry.json"]
