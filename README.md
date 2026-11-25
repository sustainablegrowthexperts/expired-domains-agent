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
> Rotate `AHREFS_KEY` and `OPENROUTER_API_KEY` at their providers and update
> your local `.env` before using the tooling.

> **Agent note (AI assistants).** This repository ships `.env.example` with
> empty placeholders, and `.env` is gitignored. After cloning, do not run any
> retrieval command until `.env` exists with real values. **Ask the user for
> the required keys** (`AHREFS_KEY` is needed for any Ahrefs call) and
> **preferably create or update `.env` for them** — copy `.env.example` if the
> file is missing. Never invent or guess a key, never print or log secret
> values, and never commit `.env` or real values to version control. If the
> user prefers to paste the keys in themselves, create the file, give them the
> path, and wait.

## Usage

```bash
uv run python -m expired_domains --help
uv run python -m expired_domains --version
uv run python -m expired_domains enrich example.com
uv run python -m expired_domains enrich example.com --force
uv run python -m expired_domains enrich example.com --db /tmp/index.sqlite3
```

`enrich` makes the nine live Ahrefs requests for one domain and prints a
per-request summary followed by `run_id`, `raw_dir`, coverage, and unit totals.
It spends Ahrefs units and has no offline or dry-run mode.

- `--force` — fetch a new run even when a run already exists for today.
- `--db PATH` — write the SQLite index somewhere other than the configured path.

## Where results are written

A run writes to two local, gitignored locations. Both default under
`<project_root>/data/`:

```text
data/
├── index.sqlite3                       # SQLite index (queryable rows)
└── raw/
    └── ahrefs/
        └── gardenofdelights.com/
            └── gardenofdelights.com_20261125T103541Z_ahrefs/
                ├── manifest.json       # run metadata + per-request manifest
                ├── stats.p0.json       # untouched provider payloads
                ├── stats.p0.headers.json
                └── ...                 # one .json + .headers.json per request
```

**Raw payloads** — `<raw_root>/<domain>/<run_id>/`, one directory per run.
Every request's response is stored verbatim alongside its headers; nothing is
compressed or rewritten, so runs can be re-parsed later without spending units.
Runs are never overwritten — each one gets its own timestamped directory.

**SQLite index** — `<data_root>/index.sqlite3`. Every table is keyed by
`run_id` so multiple runs and domains can coexist:

| Table | Contents |
|---|---|
| `runs` | One row per run: domain, mode, timestamps, drop date/source, config, coverage, units. |
| `requests` | One audit row per HTTP call: status, params, row count, units, raw path. |
| `stats` | Live and all-time link/referring-domain snapshots. |
| `history` | Monthly referring-domain and organic-traffic series. |
| `backlinks` | Deduplicated backlink rows (URLs, anchor, rel, rating, raw JSON). |
| `anchors` | Top and lexicon-matched anchors with local classifications. |

Each successful run prints its `run_id` and absolute `raw_dir`, so results can
always be located without guessing. Override the defaults with
`EXPIRED_DOMAINS_DATA_ROOT`, `EXPIRED_DOMAINS_RAW_ROOT`, or
`EXPIRED_DOMAINS_DB_PATH`; `--db` overrides the index path for one run.

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
