# The web app is the same on every architecture, so it is built once on the build machine.
FROM --platform=$BUILDPLATFORM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH" \
    KIWIX_DATA_DIR=/data KIWIX_STATE_DIR=/state KIWIX_WEB_DIR=/app/web KIWIX_PORT=8080
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist /app/web
VOLUME ["/data", "/state"]
EXPOSE 8080
# The first start scans the whole library before the server answers, hence the long grace.
HEALTHCHECK --interval=30s --timeout=10s --start-period=2m --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=8)"]
CMD ["zim-pro"]
