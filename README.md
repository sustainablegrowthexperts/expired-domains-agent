# Expired Domains Agent

Tooling for evaluating expired domains using Ahrefs data. This repository is a
Python 3.12 `src/`-layout project managed with [uv](https://docs.astral.sh/uv/).

## Layout

```text
.
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── opencode.json
├── task_runner.py
├── docs/
│   ├── reference/          # authoritative requirements and evaluation reports
│   └── archive/            # superseded requirement documents
├── reference/
│   ├── legacy/             # read-only endpoint reference; do not import
│   └── output/             # archived workbooks (gitignored)
├── specs/                  # change specifications
├── src/
│   └── expired_domains/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       └── config.py
├── tests/
│   ├── fixtures/
│   │   └── runs/           # local-only Ahrefs corpus (gitignored)
│   └── test_smoke.py
└── data/                   # runtime payloads and SQLite index (gitignored)
```

## Setup

```bash
uv sync
cp .env.example .env   # then fill in real values
```

`uv sync` creates `.venv/` and the `uv.lock` lockfile.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `AHREFS_KEY` | Yes, for Ahrefs retrieval | Ahrefs API key. |
| `EXPIRED_DOMAINS_DATA_ROOT` | No | Base data directory. Default: `<project_root>/data`. |
| `EXPIRED_DOMAINS_DB_PATH` | No | SQLite index path. Default: `<data_root>/index.sqlite3`. |
| `EXPIRED_DOMAINS_RAW_ROOT` | No | Raw Ahrefs payload directory. Default: `<data_root>/raw/ahrefs`. |

Real environment variables take precedence over values in `.env`. `.env` is
gitignored; only `.env.example` is committed. Configuration values are never
printed or logged.

> **Rotate exposed keys.** API keys have previously been shared in plaintext.
> Rotate `AHREFS_KEY`, `DATAFORSEO_API_PASSWORD`, and `OPENROUTER_API_KEY` at
> their providers and update your local `.env` before using the tooling.

## Usage

```bash
uv run python -m expired_domains --help
uv run python -m expired_domains --version
```

Only `--help` and `--version` exist in the current scaffold.

## Reference material

- `docs/reference/` holds the authoritative requirements document and the
  evaluation reports that informed it.
- `docs/archive/` keeps older requirement versions for historical context.
- `reference/legacy/` contains the original exploratory scripts. They document
  Ahrefs endpoints only, are read-only, and are **not** part of the package.
- `tests/fixtures/runs/` holds the zero-cost Ahrefs test corpus (5 domains, 7
  runs). It is local-only and gitignored; tests that need it skip when absent.

## Testing and linting

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
```

## Spec → task workflow

1. Discuss and write a change specification under `specs/`.
2. Break the spec into vertical-slice task files under `.tasks/`.
3. Run tasks with `task_runner.py`.
4. Verify with `uv run pytest` and update the spec/task status as work lands.
