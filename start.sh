#!/usr/bin/env sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

if [ "${FLEETWATCH_CONTAINER:-}" = "1" ]; then
  exec python -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
fi

PYTHON="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  python3 -m venv "$PROJECT_DIR/.venv"
fi
if ! "$PYTHON" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  "$PYTHON" -m pip install --quiet --editable "$PROJECT_DIR"
fi

exec "$PYTHON" -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
