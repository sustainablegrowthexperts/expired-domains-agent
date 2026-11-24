---
id: task-006
title: "[Slice] Prove the corpus via replay and one live verification run"
status: pending
depends_on: ["task-005"]
target_files:
  - "pyproject.toml"
  - "tests/conftest.py"
  - "tests/replay.py"
  - "tests/test_replay_parity.py"
  - "tests/test_live.py"
---

### Objective

**Objective:** A test-only replay helper loads any reference run directory (including the two legacy `stats_live`/`stats_all_time` layouts) into a temporary SQLite index using the production parsers and reproduces the known counts, units, and `lexicon_complete` flags for all seven runs; one manual `@pytest.mark.live` check confirms a real `enrich` writes raw files, index rows, coverage, and units.

### Layer-by-Layer Changes

#### Test Harness

- Create `tests/replay.py` (not importable from the CLI, no `expired_domains` entry point) exposing:
  - `replay_run(run_dir: Path, db_path: Path) -> ReplayResult` — requires `manifest.json`; creates/initialises the DB, inserts the `runs` row from manifest metadata (`domain`, `mode`, `tier`, `fetched_at`, `drop_date`, `drop_date_source`), then walks `raw/*.p0.json` in a deterministic query order.
  - `ReplayResult` with at least `run_id`, `warnings: list[str]`, `coverage`, `units_actual`, `units_billed`, and per-table row counts.
- Query ID mapping:
  - `stats | stats_live | stats_all_time` → parse as stats. For the split legacy pair, each payload carries all four counts; populate the two `stats` rows once (upsert is idempotent), and record both IDs in `requests`.
  - `refdomains_history` → `history` (`series="refdomains"`).
  - `metrics_history` → `history` (`series="org_traffic"`).
  - `top_dr_backlinks | latest_backlinks` → `backlinks`, merging duplicates by `link_hash` and appending the source query ID.
  - `lexicon_anchors | top_anchors` → `anchors` with their `source_query`.
  - `limits_*` → `requests` only.
  - Unknown query IDs → insert a `requests` row and append a warning; never fail the replay.
- Recompute coverage from the replayed data with the production `compute_coverage`: stats from the `stats` table, `rows.top_dr`/`rows.latest` from the two request row counts, `rows.unique` from distinct backlink hashes, `top_anchors` from the request, `lexicon_complete = lexicon_row_count < 250`, `history_months=48`, `drop_date_source` from the manifest, `prefilter=None`.
- Compute actual/billed units by summing non-`limits_*` requests from the replay request records, matching the manifest totals. Do not modify the raw inputs, manifest, or production index.
- Extend `tests/conftest.py` with a `run_dirs()` helper that returns only directories containing `manifest.json` (the corpus contains 7 such runs plus the non-run `123eft.com/lexicon_probes` directory) and skips with a clear message when the corpus is absent.

#### Project Configuration

- In `pyproject.toml`:
  - Register the marker: `markers = ["live: requires AHREFS_KEY and a live Ahrefs API call"]`.
  - Keep live checks out of the default suite by updating `addopts` to `-q -m "not live"`. An explicit `-m live` on the command line overrides this.
- The live test must skip on `pytest.skip("AHREFS_KEY not set")` when `AHREFS_KEY` is absent from the process environment and must not read or require `.env` (pass `env_file=<tmp_path>/missing.env` or an explicit `Settings` so a local `.env` cannot satisfy the check). Use `EXPIRED_DOMAINS_LIVE_DOMAIN` for the target domain and skip when it is unset, so a fresh domain can be chosen at run time.

#### Test / Verification Behaviour

- The live check runs `enrich <domain>` against the real API using a `tmp_path` raw root and DB (via `EXPIRED_DOMAINS_RAW_ROOT` and `EXPIRED_DOMAINS_DB_PATH` environment overrides) and asserts:
  - exit code `0` (or `1` only if a request genuinely failed, which must be reported, not hidden),
  - nine request records exist and raw files are present,
  - `stats`, `history`, `backlinks`, and `anchors` rows exist for the run,
  - coverage has 48 history months and a boolean `lexicon_complete`,
  - actual units are positive and within the expected ~2.7k–4.1k range; record the observed value in the completion notes.
- If reusing a domain, note cache effects (`x-api-cache: hit` lowers actual units) rather than treating a lower total as an error.

### TDD Execution Plan

#### Step 1: RED Phase (Write Failing Test)

- **Test files to create/update:**
  - `tests/replay.py` — helper required by the tests (imports fail first).
  - `tests/test_replay_parity.py` — parametrised over all seven manifest-bearing run directories. Assert per run:
    - `smoke-fire.com_20261124T084340Z_ahrefs`: stats `1159/5979/992/1599`; 49 refdomain rows; 1 traffic row; 70 top-DR; 30 latest; 98 unique; 165 lexicon; 50 top anchors; `lexicon_complete is True`; units `2500/2844`.
    - `smoke-fire.com_20261124T082958Z_ahrefs`: split stats populate the same four counts; 49/1 history; 70/30; 98 unique; 165/50 anchors; units `2894/2894`.
    - `smoke-fire.com_20261124T083032Z_ahrefs`: split stats; 49/1; 70/30; 98 unique; 165/50; units `2744/2894`.
    - `123eft.com_20261124T083034Z_ahrefs`: 49 refdomain / 36 traffic rows; 70/30; 96 unique; 250 lexicon ⇒ `lexicon_complete is False`; 50 top anchors; units `4040/4090`.
    - `kanuksheepskinproducts.com_20261124T083038Z_ahrefs`: 49/37; 70/30; 99 unique; 250 ⇒ false; units `4101/4101`.
    - `longbeachcoffee.com_20261124T083044Z_ahrefs`: 49/21; 70/30; 99 unique; 188 ⇒ true; units `3255/3305`.
    - `abipr.com_20261124T083049Z_ahrefs`: 49/0; 70/30; 98 unique; 168 ⇒ true; units `2874/2924`.
  - `tests/test_replay_parity.py` also asserts an unknown query ID produces a warning and a `requests` row without raising, and that replaying a run twice into the same DB is idempotent.
  - `tests/test_live.py` — marked `@pytest.mark.live`, skips without `AHREFS_KEY`/`EXPIRED_DOMAINS_LIVE_DOMAIN`, and performs the assertions above.
- **Test execution command:**
  ```bash
  uv run pytest tests/test_replay_parity.py -v
  ```
- **Expected result:** Tests MUST fail because `tests/replay.py` and the parity wiring do not exist yet. Confirm failures are `ModuleNotFoundError`/assertion failures for missing replay functionality (the fixture corpus must be present locally so these tests run rather than skip).

#### Step 2: GREEN Phase (Implementation)

- **Minimum test/support code to write/modify:**
  - `tests/replay.py` — helper, mapping, coverage recomputation, warnings.
  - `tests/conftest.py` — manifest-bearing run discovery and skip helper.
  - `tests/test_replay_parity.py` — parity table above.
  - `pyproject.toml` — marker registration and default `-m "not live"`.
  - `tests/test_live.py` — live check.
- **Test re-run command:**
  ```bash
  uv run pytest tests/test_replay_parity.py -v
  ```
- **Expected result:** All seven parity cases MUST pass with exact counts, units, and truncation flags. `uv run pytest` MUST collect the live test but deselect it.

#### Step 3: REFACTOR Phase

- **Cleanup tasks:**
  - Keep the mapping table data-driven (one dict), not a chain of `if/elif`.
  - Ensure every fixture file is opened read-only and no temporary DB leaks between parameterised cases.
  - Deduplicate the expected-value table so each run's numbers appear once.
  - Confirm live-test skip messages are actionable and name the missing variable.
- **Refactor validation commands:**
  ```bash
  uv run pytest tests/test_replay_parity.py -v
  uv run pytest
  uv run ruff check src tests
  ```
- **Expected result:** Parity tests still pass, the default suite is green and excludes live, and ruff reports no findings.

#### Step 4: Manual Live Verification (once)

- **Command:**
  ```bash
  AHREFS_KEY=<set-in-your-shell> EXPIRED_DOMAINS_LIVE_DOMAIN=<fresh-domain> \
    uv run pytest tests/test_live.py -m live -v
  ```
- **Expected result:** The test passes, a new run directory with nine raw pairs and `manifest.json` exists under the temporary raw root, index rows exist for every table, coverage is complete, and actual units are recorded. If the domain was previously fetched, note the cache effect instead of forcing a new domain.

### Definition of Done

- [ ] Failing test written and executed first — failure was observed and confirmed to be due to missing functionality (not a syntax/import error)
- [ ] Implementation completed and tests re-run — all tests pass cleanly
- [ ] Entire test suite passes without regressions (`uv run pytest`) and excludes the live test
- [ ] `uv run ruff check src tests` passes
- [ ] All seven manifest-bearing reference runs replay into a fresh temporary DB with the exact stats, history, backlink/unique, anchor, `lexicon_complete`, and unit values listed above
- [ ] Legacy split-stats runs populate the same four `stats` counts and both stats query IDs appear in `requests`
- [ ] Unknown query IDs produce a warning plus a `requests` row and never fail the replay; raw fixtures are never modified
- [ ] The manual live check has been run once, passed, and its observed actual/billed units are recorded in the completion notes; it remains excluded from the default suite
