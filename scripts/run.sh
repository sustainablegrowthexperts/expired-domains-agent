#!/usr/bin/env bash
# Convenience launcher for the expired-domains CLI.
#
# Usage:
#   scripts/run.sh <domain> [enrich options]
#
# This is not a long-running server: the script performs one enrichment run
# and exits.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/run.sh <domain> [enrich options]

Runs the standard-tier Ahrefs retrieval for <domain> and writes the raw
payloads plus SQLite rows under data/ (see "Where results are written" in
README.md).

Options (passed through to `enrich`):
  --force        Fetch a new run even when one already exists for today.
  --db PATH      Write the SQLite index to PATH instead of the configured one.
  -h, --help     Show this message.

Examples:
  scripts/run.sh example.com
  scripts/run.sh example.com --force
  scripts/run.sh example.com --db /tmp/index.sqlite3

Requires a local .env containing a real AHREFS_KEY. If .env is missing:
  cp .env.example .env
EOF
}

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 1
fi

case "${1}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

cd -- "${PROJECT_ROOT}"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: 'uv' is not installed or not on PATH." >&2
  echo "       Install it from https://docs.astral.sh/uv/ and retry." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "error: .env not found at ${PROJECT_ROOT}/.env" >&2
  echo "       cp .env.example .env   # then set AHREFS_KEY" >&2
  exit 1
fi

# Real environment variables take precedence; only inspect .env when unset.
if [[ -z "${AHREFS_KEY:-}" ]] && ! grep -q '^AHREFS_KEY=.\+' .env; then
  echo "error: AHREFS_KEY is empty in ${PROJECT_ROOT}/.env and not set in the environment." >&2
  echo "       Add the key to .env before running retrieval." >&2
  exit 1
fi

exec uv run python -m expired_domains enrich "$@"
