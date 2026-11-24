---
id: task-002
title: "[Slice] Retrieve referring-domain and organic-traffic history series"
status: completed
depends_on: ["task-001"]
target_files:
  - "src/expired_domains/ahrefs/queries.py"
  - "src/expired_domains/storage/schema.sql"
  - "src/expired_domains/storage/db.py"
  - "src/expired_domains/storage/parsers.py"
  - "src/expired_domains/enrich.py"
  - "src/expired_domains/cli.py"
  - "tests/test_queries.py"
  - "tests/test_parsers.py"
  - "tests/test_storage.py"
---

### Objective

**Objective:** A single `enrich <domain>` run now also executes `refdomains_history` and `metrics_history` for the 48-month window and persists monthly `history` rows (49 refdomain months for the reference runs, zero traffic rows for `abipr.com`) that are queryable from the SQLite index and counted in the CLI summary.

### Layer-by-Layer Changes

#### Data / Schema

- Add the `history` table to `src/expired_domains/storage/schema.sql` exactly as specified: columns `run_id`, `series` (`refdomains`/`org_traffic`), `date`, `value`, PK `(run_id, series, date)`, with `run_id` referencing `runs(run_id)`.
- In `src/expired_domains/storage/db.py`, add `upsert_history_rows(conn, run_id, rows)`. Inserts must be idempotent on the PK, and an empty row list must be a valid no-op.
- Store `date` normalised to `YYYY-MM-DD`; preserve `value` verbatim (including `None` when the API returns null). Do not infer zero traffic when the series is empty.

#### Core Logic / Engine

- Extend `build_run_queries(domain, run_date)` in `src/expired_domains/ahrefs/queries.py` so the registry is, in order: `limits_before`, `stats`, `refdomains_history`, `metrics_history`, `limits_after`.
  - `refdomains_history` → `/site-explorer/refdomains-history` with params `target`, `mode=domain`, `date_from=<first day of the month, 48 months before run_date>`, `history_grouping=monthly`. No `select`.
  - `metrics_history` → `/site-explorer/metrics-history` with the same window and `history_grouping=monthly`, plus `select=date,org_traffic` exactly.
  - Implement the window as a pure helper (for example `history_date_from(run_date) -> date`) so tests can pin it. For `run_date=2026-11-24` the result MUST be `2022-11-01`. Do not send `date_to`.
- In `src/expired_domains/storage/parsers.py`:
  - `parse_refdomains_history(payload) -> list[HistoryRow]` mapping every entry of `payload["refdomains"]` to `series="refdomains"`, `date` normalised to `YYYY-MM-DD`, `value=refdomains`.
  - `parse_metrics_history(payload) -> list[HistoryRow]` mapping every entry of `payload["metrics"]` to `series="org_traffic"`, `value=org_traffic`. An empty or missing list returns `[]` and is a valid success.
  - Ignore extra fields returned by the API; never synthesise missing months.
- In `src/expired_domains/enrich.py`:
  - Execute the two new queries in registry order and insert their parsed rows under the current `run_id`.
  - Record a failed history request in `requests` and continue with the other history query; do not fail the whole run because one series is unavailable.
  - Keep `coverage.history_months = 48` and leave the row counters for backlinks/anchors unchanged in this slice.

#### CLI / Interface

- Extend the per-request output so the history queries appear with query ID, status, row count, and units.
- Extend the final summary with `refdomains_history` and `metrics_history` row counts so a zero-row traffic series is visible as `metrics_history=0` rather than an error.
- No new flags or commands.

#### Test Harness

- Reuse `tests/conftest.py` from task 001. Add a helper that yields a specific reference run directory (for example `reference_run_dir("smoke-fire.com_20261124T084340Z_ahrefs")`) and skips with a clear message when the fixture corpus is absent.
- Tests may use both the corrected fixture (`refdomains_history.p0.json`, `metrics_history.p0.json`) and real zero-row payloads from `abipr.com`.
- All DB and raw writes stay under `tmp_path`.

### TDD Execution Plan

#### Step 1: RED Phase (Write Failing Test)

- **Test files to create/update:**
  - `tests/test_queries.py` — the full registry order; `history_date_from(date(2026, 11, 24)) == date(2022, 11, 1)` and the string `2022-11-01` in params; `history_grouping=monthly` on both; `select=date,org_traffic` on metrics only; no `date_to`; `mode=domain` and `target` on both.
  - `tests/test_parsers.py` — the corrected `smoke-fire.com` refdomains payload yields 49 rows of `series="refdomains"` with `YYYY-MM-DD` dates and integer values; its metrics payload yields 1 `org_traffic` row; `123eft.com` yields 36 traffic rows; the `abipr.com` metrics payload yields `[]` without raising; a null `org_traffic` value stays `None`.
  - `tests/test_storage.py` — `history` table exists with PK `(run_id, series, date)`; upserting the same rows twice leaves 49/1 counts; an empty list commits without error; querying by `(run_id, series)` returns rows ordered by date.
- **Test execution command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_parsers.py tests/test_storage.py -v
  ```
- **Expected result:** Tests MUST fail because the history queries, parsers, and table do not exist yet. Confirm failures are assertion/`NotImplementedError`/`sqlite3.OperationalError: no such table` failures caused by missing functionality, not syntax or import errors.

#### Step 2: GREEN Phase (Implementation)

- **Minimum application code to write/modify:**
  - `src/expired_domains/ahrefs/queries.py` — two new specs plus the window helper.
  - `src/expired_domains/storage/schema.sql` — `history`.
  - `src/expired_domains/storage/db.py` — `upsert_history_rows`.
  - `src/expired_domains/storage/parsers.py` — the two parsers.
  - `src/expired_domains/enrich.py` — execute, insert, and tolerate a failed series.
  - `src/expired_domains/cli.py` — report the two queries and row counts.
- **Test re-run command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_parsers.py tests/test_storage.py -v
  ```
- **Expected result:** Tests MUST pass cleanly, including 49 refdomain rows, 36 rows for `123eft.com`, and zero stored traffic rows for `abipr.com` with no exception.

#### Step 3: REFACTOR Phase

- **Cleanup tasks:**
  - Share one date-normalisation helper between the two parsers.
  - Keep the query registry declarative; avoid conditionals that special-case endpoints.
  - Confirm the CLI summary stays one line per request plus one summary block.
  - Remove any fixture-specific branching from production code.
- **Refactor validation commands:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_parsers.py tests/test_storage.py -v
  uv run ruff check src tests
  ```
- **Expected result:** All previously passing tests still pass and ruff reports no findings.

### Definition of Done

- [ ] Failing test written and executed first — failure was observed and confirmed to be due to missing functionality (not a syntax/import error)
- [ ] Implementation completed and tests re-run — all tests pass cleanly
- [ ] Entire test suite passes without regressions (`uv run pytest`)
- [ ] `uv run ruff check src tests` passes
- [ ] A mocked `enrich` run stores 49 `refdomains` rows and 1 `org_traffic` row for the corrected `smoke-fire.com` fixture, and 0 traffic rows for `abipr.com` without marking the run failed
- [ ] `date_from` is the first day of the month 48 months before the run date (`2022-11-01` for a 2026-11-24 run) and the request includes `history_grouping=monthly`
- [ ] The CLI shows history row counts, including a visible zero for `metrics_history`
