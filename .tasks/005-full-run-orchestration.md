---
id: task-005
title: "[Slice] Orchestrate the full nine-request run with guards and failure reporting"
status: completed
depends_on: ["task-004"]
target_files:
  - "src/expired_domains/ahrefs/client.py"
  - "src/expired_domains/models.py"
  - "src/expired_domains/storage/raw.py"
  - "src/expired_domains/storage/db.py"
  - "src/expired_domains/enrich.py"
  - "src/expired_domains/cli.py"
  - "tests/test_client.py"
  - "tests/test_queries.py"
  - "tests/test_storage.py"
  - "tests/test_cli.py"
---

### Objective

**Objective:** `enrich <domain>` executes all nine §3.1 requests in the mandated order, refuses a same-day re-fetch unless `--force` is given, completes the run after a single request failure while recording it in `runs.error`, writes a final `manifest.json`, and returns exit code `0` on full success, `1` on refusal or any failed request, and `2` on configuration errors.

### Layer-by-Layer Changes

#### Data / Schema

- In `src/expired_domains/storage/db.py`, add `has_run_on_date(conn, domain, date) -> bool` using the UTC `substr(fetched_at, 1, 10)` date of `runs.fetched_at`. Provide `latest_run_id_for_date(...)` so the refusal message can name the existing run.
- Finalise `runs` at the end of every run: update `coverage_json`, `units_actual`, and `error`.
- `runs.error` is SQL `NULL` when every request succeeded; otherwise a compact JSON object mapping each failed `query_id` to its message, for example `{"stats": "HTTP 500 after 6 attempts"}`. A parse error also counts as a failed request.
- Keep partial runs: `runs`, `requests`, `stats`, `history`, `backlinks`, and `anchors` rows written before a failure remain committed.

#### Core Logic / Engine

- Extend `src/expired_domains/enrich.py` so the execution order is exactly: `limits_before`, `stats`, `refdomains_history`, `metrics_history`, `top_dr_backlinks`, `latest_backlinks`, `lexicon_anchors`, `top_anchors`, `limits_after`.
- Before any network call, check the same-day guard: if a run for the domain has a `fetched_at` UTC date equal to the current run date and `force` is false, return a `RunSummary` with a clear message, no requests executed, no new run directory, and exit code `1`.
- `--force` bypasses the guard and creates a new immutable run; never delete or overwrite a previous run directory or its index rows.
- Continue after a failed request: record `status`/`error`/attempts in `requests`, keep executing remaining queries, and skip parsing that query's payload. Never abort the run because one request failed.
- `limits_before`/`limits_after` failures do not change `runs.units_actual`, but are still recorded as failed requests and set the exit code to `1`.
- Compute `runs.units_actual` as the sum of `units_cost_total_actual` over non-`limits_*` requests. Compute the billed total as the sum of `units_cost_total` over non-`limits_*` requests. For the corrected fixture these MUST be `2500` actual and `2844` billed.
- Finalise coverage for the whole run: stats counts from the `stats` table (or all `None` if the stats request failed), `rows.top_dr`/`rows.latest` returned counts, `rows.unique` distinct hashes, `top_anchors` count, `lexicon_complete`, `history_months=48`, `drop_date_source="unknown"`, `prefilter=None`.
- Write `manifest.json` into the run directory at completion. Include at least `run_id`, `domain`, `provider="ahrefs"`, `mode`, `tier`, `drop_date` (NULL), `drop_date_source`, `fetched_at`, `api_date`, `config` (including `lexicon_version`), `requests` (one summary per query with query ID, endpoint, status, rows, units actual/billed, cache, error), `coverage`, `units_actual_total`, `units_billed_total`, and `error`.
- Ensure the raw directory contains one `{query_id}.p0.json` + `{query_id}.p0.headers.json` pair for **every** attempted query, including failures. Failed attempts write the §3.3 request record with `"payload": null` and the recorded error; never fabricate a payload.
- Return the typed `RunSummary` with the correct `exit_code`.

#### CLI / Interface

- Add `--force` to `enrich DOMAIN [--force] [--db PATH]`; `--db` overrides the configured SQLite path for that invocation only.
- Print the same-day refusal as a clear single message naming the existing run ID and suggesting `--force`; do not start requests.
- Print all nine per-request lines and the final summary (run ID, raw directory, coverage flags, actual/billed units, and a failure list when present).
- Exit codes: `0` when every request succeeded; `1` for a same-day refusal or at least one failed/parse-errored request; `2` for configuration errors such as a missing `AHREFS_KEY`.
- Keep `--help`/`--version` working. Do not add an import command.

#### Provider / Client

- Extend `src/expired_domains/ahrefs/client.py` so failures also write the raw request/header pair (payload `null`) with the final status, attempt count, and error message. Success behaviour from task 001 is unchanged.
- Retries remain capped at 6 total attempts with exponential backoff; a failure after the cap is recorded, not raised.

#### Test Harness

- Use `httpx.MockTransport` backed by the corrected fixture payloads for a full-success test; return them in query order and assert the nine-request sequence.
- Use a transport that returns `500` for exactly one query (for example `metrics_history`) and success for the rest to test partial failure.
- Use `tmp_path` for raw root and DB; seed an existing run with today's `fetched_at` to test the guard. Tests must never hit the network or read `.env`.

### TDD Execution Plan

#### Step 1: RED Phase (Write Failing Test)

- **Test files to create/update:**
  - `tests/test_queries.py` — the full registry returns exactly nine specs in the mandated order with the expected query IDs.
  - `tests/test_storage.py` — `has_run_on_date` is true for a run fetched today (UTC) and false for yesterday; updating a failed run stores the error object and `units_actual`; re-reading returns the JSON round-trip.
  - `tests/test_client.py` — a 500 response still writes `{query_id}.p0.json` with `payload: null`, the error text, and the header pair.
  - `tests/test_cli.py` — full mocked success prints nine lines, the summary with `2500`/`2844`, and exits `0`; a same-day existing run without `--force` exits `1`, makes zero requests, and suggests `--force`; the same case with `--force` runs and exits `0`; one failed query exits `1` while the other eight queries are recorded and the `runs.error` JSON names only the failed ID; missing `AHREFS_KEY` exits `2`.
- **Test execution command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_storage.py tests/test_client.py tests/test_cli.py -v
  ```
- **Expected result:** Tests MUST fail because the guard, partial-failure handling, manifest, `--force`, and exit-code policy do not exist. Confirm failures are assertion-level and not syntax/import errors.

#### Step 2: GREEN Phase (Implementation)

- **Minimum application code to write/modify:**
  - `src/expired_domains/ahrefs/client.py` — persist failed request records.
  - `src/expired_domains/models.py` — any fields needed to carry force/refusal/error details.
  - `src/expired_domains/storage/raw.py` — run-dir creation guard and manifest writing helper.
  - `src/expired_domains/storage/db.py` — same-day lookup and final run update.
  - `src/expired_domains/enrich.py` — full orchestration, guard, totals, manifest, exit code.
  - `src/expired_domains/cli.py` — `--force`, refusal message, exit codes.
- **Test re-run command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_storage.py tests/test_client.py tests/test_cli.py -v
  ```
- **Expected result:** Tests MUST pass cleanly, including `2500` actual / `2844` billed and exit codes `0`/`1`/`2`.

#### Step 3: REFACTOR Phase

- **Cleanup tasks:**
  - Extract the per-request outcome classification (`succeeded`, `failed`) into one helper used for coverage, totals, and exit codes.
  - Keep manifest serialisation deterministic (sorted keys, ISO-8601 timestamps, no secret values).
  - Ensure the guard and force flag are evaluated before run-directory creation and before any HTTP call.
  - Remove duplicate summary formatting between `enrich.py` and `cli.py`.
- **Refactor validation commands:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_storage.py tests/test_client.py tests/test_cli.py -v
  uv run ruff check src tests
  ```
- **Expected result:** All previously passing tests still pass and ruff reports no findings.

### Definition of Done

- [ ] Failing test written and executed first — failure was observed and confirmed to be due to missing functionality (not a syntax/import error)
- [ ] Implementation completed and tests re-run — all tests pass cleanly
- [ ] Entire test suite passes without regressions (`uv run pytest`)
- [ ] `uv run ruff check src tests` passes
- [ ] The nine queries execute in the mandated order against a mocked client; a full successful run exits `0` and reports `2500` actual / `2844` billed units with `rows.unique=98`, `lexicon_complete=true`, and stats 1159/5979/992/1599
- [ ] Same-day re-fetch is refused with a clear message and exit `1` unless `--force`; forced runs create a new immutable run directory and keep the previous run
- [ ] One failed query is recorded with status/error, the remaining queries still run, `runs.error` names only that query ID, and the process exits `1`
- [ ] Every attempted query has a raw request/header pair, and `manifest.json` contains coverage, both unit totals, and per-request summaries
- [ ] Missing `AHREFS_KEY` exits `2` before any network call; tests never read `.env` or touch production `data/`
