FROM node:24-bookworm-slim AS frontend

WORKDIR /workspace
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./frontend
RUN npm run build

FROM python:3.14-slim

ENV FLEETWATCH_CONTAINER=1 \
    FLEETWATCH_DB_PATH=/data/fleetwatch.db \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /srv/fleetwatch
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
COPY --from=frontend /workspace/app/static ./app/static
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home fleetwatch \
    && install -d -o fleetwatch -g fleetwatch /data

USER fleetwatch
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
