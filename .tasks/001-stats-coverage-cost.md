---
id: task-001
title: "[Slice] Retrieve stats end to end with coverage and unit-cost accounting"
status: completed
depends_on: []
target_files:
  - "pyproject.toml"
  - ".env.example"
  - "src/expired_domains/config.py"
  - "src/expired_domains/models.py"
  - "src/expired_domains/ahrefs/__init__.py"
  - "src/expired_domains/ahrefs/client.py"
  - "src/expired_domains/ahrefs/queries.py"
  - "src/expired_domains/storage/__init__.py"
  - "src/expired_domains/storage/schema.sql"
  - "src/expired_domains/storage/db.py"
  - "src/expired_domains/storage/raw.py"
  - "src/expired_domains/storage/parsers.py"
  - "src/expired_domains/enrich.py"
  - "src/expired_domains/cli.py"
  - "tests/conftest.py"
  - "tests/test_client.py"
  - "tests/test_queries.py"
  - "tests/test_parsers.py"
  - "tests/test_storage.py"
  - "tests/test_cli.py"
---

### Objective

**Objective:** `enrich <domain>` validates the Ahrefs key, executes `limits_before` → `stats` → `limits_after` through an offline-testable HTTP client, writes each raw payload/header pair, persists `runs`/`requests`/`stats` rows with a coverage object and actual/billed unit totals, and prints the per-query results plus the fixture's 1159/5979/992/1599 stat counts.

### Layer-by-Layer Changes

#### Data / Schema

- Create `src/expired_domains/storage/schema.sql` with **only** the `runs`, `requests`, and `stats` tables from spec §3.4. Use `CREATE TABLE IF NOT EXISTS` and the exact column names and primary keys (`runs.run_id`; `requests` PK `(run_id, query_id, page)`; `stats` PK `(run_id, snapshot)`). Do **not** create `refdomains`; the remaining tables are added by later slices.
- In `src/expired_domains/storage/db.py`, implement:
  - `connect(db_path: Path) -> sqlite3.Connection` — create parent directories, set `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON`, and return a connection with row access by column name.
  - `init_db(db_path: Path) -> None` — execute `schema.sql` idempotently.
  - `insert_run`, `upsert_request`, `upsert_stats` — parameterised `INSERT ... ON CONFLICT ... DO UPDATE` helpers; callers pass already-validated values from the models.
  - A transaction/context-manager helper so one run's index writes commit atomically.
- Store `requests.raw_path` as a POSIX path relative to `PROJECT_ROOT`. Use `os.path.relpath` so tests running against a `tmp_path` raw root still produce a relative path (it may start with `..`); never store an absolute path.
- Keep `data/`, `*.sqlite3`, and `tests/fixtures/runs/` untracked; tasks must never write to the production `data/index.sqlite3`.

#### Core Logic / Engine

- Create `src/expired_domains/models.py` with dataclasses for this slice:
  - `QuerySpec` — `query_id`, `endpoint`, `params: dict[str, str]`, `page: int = 0`.
  - `RequestRecord` — the fields in spec §3.3 (`query_id`, `endpoint`, `page`, `params`, `url`, `status`, `started_at`, `finished_at`, `attempts`, `error`, `parse_error`, `rows`, `units_cost_total`, `units_cost_total_actual`, `units_cost_row`, `cache`, `headers`) plus the parsed `payload`.
  - `Coverage` — `mode`, `stats` (four nullable counts), `rows` (`top_dr`, `latest`, `unique`), `top_anchors`, `lexicon_complete`, `history_months`, `drop_date_source`, `prefilter`.
  - `Units` — `actual`, `billed`.
  - `RunSummary` — `run_id`, `domain`, `raw_dir`, `requests: list[RequestRecord]`, `coverage: Coverage`, `units: Units`, `error: dict[str, str] | None`, `exit_code: int`.
  - Prefer frozen dataclasses and explicit `asdict`-friendly shapes; no evaluation/scoring fields.
- In `src/expired_domains/storage/parsers.py`:
  - `parse_stats(payload: dict) -> list[StatsRow]` mapping `payload["metrics"]` to `live = ("live", live_refdomains)` and `all_time = ("all_time", all_time_refdomains)` per spec §3.5.
  - `extract_units(headers: Mapping[str, str]) -> Units` returning `int(x-api-units-cost-total-actual or 0)` and `int(x-api-units-cost-total or 0)`, tolerating missing/invalid values as `0`.
  - `compute_coverage(stats_rows, backlink_rows=(), anchor_rows=()) -> Coverage` producing exactly the §3.8 key shape: `mode="top_n"`, stats counts (all four `None` if the stats call failed), zero row counters for this slice, `history_months=48`, `drop_date_source="unknown"`, `prefilter=None`.
  - Preserve payload `null` values as `None`; never coerce them to zero or `""`.
- In `src/expired_domains/enrich.py`, implement `run_enrich(domain, *, settings=None, db_path=None, client=None, now=None) -> RunSummary` that:
  - Loads settings and calls the key-validation helper before any network work.
  - Derives `run_id`/directory from the UTC timestamp and executes the three `QuerySpec`s returned by the query registry in order.
  - Initialises the DB, inserts the `runs` row (`mode="domain"`, `tier="standard"`, `prefilter_batch=NULL`, `drop_date=NULL`, `drop_date_source="unknown"`, `fetched_at` UTC ISO-8601, `config_json`, placeholder coverage), then inserts each request and the stats rows.
  - Parses stats only when the request succeeded; a failed stats call records the request and leaves stats counts `None`.
  - Returns the typed `RunSummary`; do not print from this module.
- Record `config_json` with at least `{"spec": "ahrefs-ingestion-spec-v3.1", "history_months": 48, "top_dr_rows": 70, "latest_rows": 30, "top_anchor_rows": 50}`. The lexicon version is added in task 004.

#### Provider / Client

- Add runtime dependencies `httpx>=0.27` and `tldextract>=5.1` to `pyproject.toml`, then run `uv sync` to refresh `uv.lock`. Do not add the `live` marker yet.
- Update `.env.example` so the `AHREFS_KEY` comment reads as required for this milestone. Keep the value empty. Never create or modify `.env`.
- In `src/expired_domains/config.py`:
  - Add `api_base: str` to `Settings` with default `https://api.ahrefs.com/v3`.
  - Add `ConfigError` and a validation helper (`Settings.require_ahrefs_key()`) that returns the key or raises `ConfigError("AHREFS_KEY is required for Ahrefs retrieval")`. Never include the key value in the message.
- Create `src/expired_domains/ahrefs/queries.py` with `build_run_queries(domain: str, run_date: date) -> list[QuerySpec]` returning, in order:
  1. `limits_before` → `/subscription-info/limits-and-usage`, no params.
  2. `stats` → `/site-explorer/backlinks-stats`, params exactly `target`, `mode=domain`, `date=<run_date as YYYY-MM-DD>`; no `history` parameter.
  3. `limits_after` → `/subscription-info/limits-and-usage`, no params.
- Create `src/expired_domains/ahrefs/client.py` with a synchronous `AhrefsClient`:
  - Constructor accepts `settings`, optional `httpx` transport (for `MockTransport`), and an injectable `sleep` callable defaulting to `time.sleep`.
  - Sends `Authorization: Bearer <key>` and uses `base_url=settings.api_base`, `timeout=120.0`.
  - Retries 429, 5xx, `httpx.TransportError`, and timeouts with exponential backoff (0.5 s doubling, max 6 attempts total). Do not retry 401/403/404 or other 4xx.
  - Returns a `RequestRecord` for every outcome — HTTP errors, exhausted retries, JSON decode failures — instead of raising. Populate `status`, `attempts`, `error`, `parse_error`, `cache` (`x-api-cache`), and unit values.
  - On a successful JSON response, write `{query_id}.p0.json` with the §3.3 shape (`{"request": {...}, "payload": {...}, "raw_body": null}`) and `{query_id}.p0.headers.json` with the full header mapping. Raw writes happen through `storage/raw.py`.
- Create `src/expired_domains/storage/raw.py`:
  - `make_run_id(domain, now_utc) -> str` producing `"{domain}:{YYYYmmddTHHMMSSZ}:ahrefs"` and `run_dir_name(run_id)` replacing `:` with `_`.
  - `create_run_dir(raw_root, domain, run_id) -> Path` under `{raw_root}/{domain}/{run_dir_name}/`, refusing to reuse an existing directory.
  - `write_request_files(run_dir, record) -> Path` writing the payload pair and returning the relative payload path.
  - No network access, no overwrites.

#### CLI / Interface

- Extend `src/expired_domains/cli.py` with an `enrich` subcommand: `python -m expired_domains enrich <domain> [--db PATH]`.
- Print one line per request in execution order: query ID, status, row count, actual units (and billed units when different).
- Print a final summary: run ID, raw directory, coverage flags (`stats`, `rows`, `history_months`, `drop_date_source`), and actual/billed unit totals.
- Missing/invalid `AHREFS_KEY` prints a clear message naming the variable to stderr and exits `2`. Successful runs exit `0`.
- Keep `--help` and `--version` working. Do not add an import/replay command and do not add `--force` in this slice (task 005 owns it).

#### Test Harness

- Create `tests/conftest.py` with:
  - `FIXTURES_RUNS = PROJECT_ROOT / "tests" / "fixtures" / "runs"`.
  - A `reference_run_dir(name: str)` helper that returns a run directory containing `manifest.json` or calls `pytest.skip("Ahrefs fixture corpus not available")`.
  - `tmp_db` and `tmp_raw_root` fixtures rooted in `tmp_path` so no test touches `data/`.
  - An autouse fixture that removes `AHREFS_KEY` and the `EXPIRED_DOMAINS_*` variables unless a test opts in; tests must set a dummy key with `monkeypatch`.
  - Because the developer machine may have a local `.env`, never let tests depend on it: call `load_settings(env_file=<tmp_path>/missing.env)` or construct/patch an explicit `Settings` value. Environment cleanup alone is not sufficient.
- Fixture payloads for the corrected `smoke-fire.com` run live at `tests/fixtures/runs/smoke-fire.com/smoke-fire.com_20261124T084340Z_ahrefs/raw/`. Tests may read them; never rewrite them.

### TDD Execution Plan

#### Step 1: RED Phase (Write Failing Test)

- **Test files to create/update:**
  - `tests/conftest.py` — fixtures above.
  - `tests/test_queries.py` — fixed `run_date=date(2026, 11, 24)`: three specs in the required order; stats params exactly `target`, `mode=domain`, `date=2026-11-24`; `"history" not in stats.params`; limits calls carry no params; URL path for stats is `/site-explorer/backlinks-stats`.
  - `tests/test_client.py` — using `httpx.MockTransport`: a 200 response captures headers/units and writes both raw files; a 429-then-200 sequence yields `attempts == 2` with the injected sleeper recording one call; six consecutive 500s yield `status == 500`, `attempts == 6`, and a non-null `error` without raising; a 401 is attempted once; malformed JSON sets `parse_error`; a cache-hit response (`x-api-cache: hit`, actual `0`, billed `50`) yields `Units(actual=0, billed=50)`.
  - `tests/test_parsers.py` — parsing the corrected fixture `stats.p0.json` yields live `(1159, 992)` and all-time `(5979, 1599)`; `extract_units` handles missing/blank/invalid headers as `0`; coverage object has exactly the §3.8 keys and `stats` all `None` when stats parsing is skipped.
  - `tests/test_storage.py` — `init_db` creates the three tables with the required PKs; `PRAGMA foreign_keys` is on and journal mode is WAL; re-running `init_db` is a no-op; repeated `upsert_request`/`upsert_stats`/`insert_run` do not duplicate rows; coverage and `config_json` round-trip through `runs`.
  - `tests/test_cli.py` — `--help` lists `enrich`; `--version` still exits 0; `enrich example.com --db <tmp>` with an explicitly injected settings object whose `ahrefs_key` is `None` exits `2` and names `AHREFS_KEY` on stderr without making a request (do not rely on unsetting the env var, since a local `.env` may exist); a monkeypatched `run_enrich` returning a fixed `RunSummary` prints the query line and summary and exits `0`.
- **Test execution command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_client.py tests/test_parsers.py tests/test_storage.py tests/test_cli.py -v
  ```
- **Expected result:** Tests MUST fail. For this greenfield slice, initial collection may fail on missing modules; if so, add signature-only stubs (no logic) for the new modules and re-run so every failure is an assertion/`NotImplementedError` failure caused by missing functionality, not a syntax or import error. Fixture-dependent tests MUST run locally, not skip, when the corpus is present.

#### Step 2: GREEN Phase (Implementation)

- **Minimum application code to write/modify:**
  - `pyproject.toml` — add `httpx`/`tldextract`; run `uv sync`.
  - `.env.example` — clarify the `AHREFS_KEY` requirement.
  - `src/expired_domains/config.py` — `api_base`, `ConfigError`, `Settings.require_ahrefs_key()`.
  - `src/expired_domains/models.py` — `QuerySpec`, `RequestRecord`, `Coverage`, `Units`, `RunSummary`.
  - `src/expired_domains/ahrefs/__init__.py`, `client.py`, `queries.py`.
  - `src/expired_domains/storage/__init__.py`, `schema.sql`, `db.py`, `raw.py`, `parsers.py`.
  - `src/expired_domains/enrich.py`, `src/expired_domains/cli.py`.
- **Test re-run command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_client.py tests/test_parsers.py tests/test_storage.py tests/test_cli.py -v
  ```
- **Expected result:** Tests MUST pass cleanly, including the 1159/5979/992/1599 stats assertions and the cache-hit `0 actual / 50 billed` assertion. No live network calls.

#### Step 3: REFACTOR Phase

- **Cleanup tasks:**
  - Consolidate repeated header/unit parsing and timestamp formatting into small helpers.
  - Confirm public names match the spec (`run_enrich`, `build_run_queries`, `compute_coverage`, `extract_units`) and every module uses `from __future__ import annotations` consistently with the existing package.
  - Remove debug prints, sleeps, and any hard-coded fixture paths outside `tests/conftest.py`.
  - Confirm no secret value can reach logs, raw files, or error messages.
- **Refactor validation commands:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_client.py tests/test_parsers.py tests/test_storage.py tests/test_cli.py -v
  uv run ruff check src tests
  ```
- **Expected result:** All previously passing tests still pass and ruff reports no findings.

### Definition of Done

- [ ] Failing test written and executed first — failure was observed and confirmed to be due to missing functionality (not a syntax/import error)
- [ ] Implementation completed and tests re-run — all tests pass cleanly
- [ ] Entire test suite passes without regressions (`uv run pytest`)
- [ ] `uv run ruff check src tests` passes
- [ ] A mocked run of `enrich smoke-fire.com` writes `stats.p0.json` + `stats.p0.headers.json` plus the two limits pairs, and the printed summary contains 1159/5979/992/1599
- [ ] `runs.coverage_json` has exactly the §3.8 keys with zero row counters, `history_months=48`, `drop_date_source="unknown"`, `prefilter=null`
- [ ] Cache-hit accounting is proven: stats stores `units_actual=0` and billed `50`; a miss stores actual equal to billed
- [ ] No test reads or creates `.env`, touches the production `data/` directory, or makes a network call
