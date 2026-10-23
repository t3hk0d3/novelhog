# syntax=docker/dockerfile:1

# SPA build: web/dist
FROM node:26.12-slim AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Caddy binary only; the official image is the supported source of a static build.
FROM caddy:2.11 AS caddy

# Python environment
FROM python:3.14-slim AS python
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md* ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Runtime
FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /home/novelhog novelhog \
    && mkdir -p /data/db /data/static /var/lib/novelhog/scraper /srv/web \
    && chown -R novelhog:novelhog /data /var/lib/novelhog /home/novelhog
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
COPY --from=python /app/.venv /app/.venv
COPY --from=web /build/web/dist /srv/web
COPY docker/Caddyfile /etc/caddy/Caddyfile
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    NOVELHOG_DATA_DIR=/data \
    NOVELHOG_CADDYFILE=/etc/caddy/Caddyfile \
    XDG_CONFIG_HOME=/home/novelhog/.config \
    XDG_DATA_HOME=/home/novelhog/.local/share
USER 10001
WORKDIR /home/novelhog
EXPOSE 8080
ENTRYPOINT ["tini", "--", "novelhog"]
CMD ["web"]
