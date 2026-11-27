#!/usr/bin/env bash
# Convenience launcher for the Domain Hunter FastAPI dev server.
#
# Usage:
#   scripts/serve.sh [options]
#
# By default this binds to a random unused port on 127.0.0.1 and enables
# uvicorn's auto-reload. The chosen URL is printed before the server starts.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

HOST="127.0.0.1"
PORT=""
DB=""
RELOAD=1

usage() {
  cat <<'EOF'
Usage: scripts/serve.sh [options]

Start the Domain Hunter FastAPI dev server on a random unused port.

Options:
  --host HOST     Bind address (default: 127.0.0.1).
  --port PORT     Use a specific port instead of picking a free one.
  --db PATH       SQLite index to read (default: configured index, usually data/index.sqlite3).
  --no-reload     Disable uvicorn auto-reload.
  -h, --help      Show this message.

Examples:
  scripts/serve.sh
  scripts/serve.sh --db /tmp/index.sqlite3
  scripts/serve.sh --host 0.0.0.0 --port 8000
EOF
}

while [[ $# -gt 0 ]]; do
  case "${1}" in
    -h|--help)
      usage
      exit 0
      ;;
    --host)
      HOST="${2:?--host requires a value}"
      shift 2
      ;;
    --port)
      PORT="${2:?--port requires a value}"
      shift 2
      ;;
    --db)
      DB="${2:?--db requires a value}"
      shift 2
      ;;
    --no-reload)
      RELOAD=0
      shift
      ;;
    *)
      echo "error: unknown option: ${1}" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  echo "error: 'uv' is not installed or not on PATH." >&2
  echo "       Install it from https://docs.astral.sh/uv/ and retry." >&2
  exit 1
fi

cd -- "${PROJECT_ROOT}"

if [[ -z "${PORT}" ]]; then
  PORT="$(
    uv run python - "${HOST}" <<'PY'
import socket
import sys

host = sys.argv[1]
family = socket.AF_INET6 if ":" in host else socket.AF_INET
with socket.socket(family, socket.SOCK_STREAM) as sock:
    sock.bind((host, 0))
    print(sock.getsockname()[1])
PY
  )"
fi

if [[ -n "${DB}" ]]; then
  export EXPIRED_DOMAINS_DB_PATH="${DB}"
fi

echo "Domain Hunter dev server: http://${HOST}:${PORT}/"
if [[ -n "${DB}" ]]; then
  echo "Index: ${DB}"
fi

ARGS=(expired_domains.web.app:app --host "${HOST}" --port "${PORT}")
if [[ "${RELOAD}" -eq 1 ]]; then
  ARGS+=(--reload)
fi

exec uv run uvicorn "${ARGS[@]}"
