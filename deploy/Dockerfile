FROM node:24-bookworm-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir uv==0.8.22
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ src/
# Export exact versions and hashes so an optional mirror cannot change dependencies.
RUN uv export --locked --no-dev --no-emit-project -o /tmp/requirements.txt \
    && uv venv .venv \
    && uv pip sync --require-hashes --index-url "$PIP_INDEX_URL" /tmp/requirements.txt \
    && uv pip install --no-deps --index-url "$PIP_INDEX_URL" . \
    && rm /tmp/requirements.txt \
    && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
COPY --from=web /web/dist /app/web/dist
ENV KR_DATABASE=/data/relay.sqlite3 KR_WEB_DIR=/app/web/dist PYTHONUNBUFFERED=1
USER app
EXPOSE 8765
ENTRYPOINT ["/app/.venv/bin/kitty-remote"]
CMD ["relay", "--host", "0.0.0.0", "--port", "8765"]
