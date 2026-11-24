---
id: task-004
title: "[Slice] Retrieve anchor slices with lexicon classification and truncation flag"
status: completed
depends_on: ["task-003"]
target_files:
  - "config/lexicon/v1.json"
  - "src/expired_domains/ahrefs/lexicon.py"
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

**Objective:** A single `enrich <domain>` run now also retrieves the top-50 live anchors and the batched all-time lexicon search, classifies each returned anchor against the versioned lexicon locally without dropping rows, stores both sets in the `anchors` table, and reports `lexicon_complete=true` at 165 rows for `smoke-fire.com` and `false` at 250 for `123eft.com`.

### Layer-by-Layer Changes

#### Data / Schema

- Create `config/lexicon/v1.json` with exactly the §3.10 content: `version: "v1"` and five categories — `gambling` (14 terms), `pharma` (7), `adult` (6), `link_selling_seo` (6), `evasions` (3) — for 36 unique terms in total. Preserve the multilingual terms exactly (`카지노`, `바카라`, `토토`, `казино`).
- Add the `anchors` table to `src/expired_domains/storage/schema.sql` exactly per spec §3.4: `run_id`, `source_query`, `anchor_raw`, `anchor_norm`, `links_to_target`, `dofollow_links`, `refdomains`, `first_seen`, `last_seen`, `lexicon_terms` (JSON), `lexicon_categories` (JSON), `raw_json`, PK `(run_id, source_query, anchor_norm)`, with `run_id` referencing `runs(run_id)`.
- In `src/expired_domains/storage/db.py`, add `upsert_anchors(conn, run_id, rows)`. On PK conflict update the metrics and JSON columns. Persist `lexicon_terms`/`lexicon_categories` as compact JSON arrays; `refdomains`/`last_seen` remain SQL `NULL` when the query's `select` did not request them.
- Do not add pagination or offset columns; truncation is expressed only through coverage.

#### Core Logic / Engine

- Create `src/expired_domains/ahrefs/lexicon.py`:
  - `load_lexicon(path: Path | None = None) -> Lexicon` — load `config/lexicon/v1.json` once per process (module-level cache or `functools.lru_cache`) and expose `version`, ordered categories, and a flat unique term list.
  - `build_where(lexicon) -> str` — return the compact JSON string for **one** `or` over all 36 terms: `{"or":[{"field":"anchor","is":["isubstring","<term>"]}, ...]}`. Include every category, including `link_selling_seo`.
  - `classify(anchor_norm: str | None, lexicon) -> tuple[list[str], list[str]]` — case-insensitive substring matching over `anchor_norm`, returning matched terms and the sorted unique categories. Return empty arrays for `None`/empty anchors; never filter rows.
- Extend `build_run_queries(domain, run_date)` with two specs after `latest_backlinks`:
  - `lexicon_anchors` → `/site-explorer/anchors` with `target`, `mode=domain`, `history=all_time`, `limit=250`, `select=anchor,links_to_target,dofollow_links,refdomains,first_seen,last_seen`, and `where=<build_where output>`.
  - `top_anchors` → `/site-explorer/anchors` with `target`, `mode=domain`, `history=live`, `limit=50`, `order_by=links_to_target:desc`, `select=anchor,links_to_target,dofollow_links,first_seen` (no `refdomains`, no `last_seen`).
  - Never add `offset`; the endpoint hard-caps at 250.
- In `src/expired_domains/storage/parsers.py` add `parse_anchors(payload, source_query, lexicon) -> list[AnchorRow]`:
  - Normalise each anchor with the shared `normalize_anchor` helper; keep `anchor_raw` untouched.
  - Classify locally and store both matched terms and categories.
  - For `top_anchors`, set `refdomains` and `last_seen` to `None`; for `lexicon_anchors`, preserve returned values including `None`.
  - Never drop a returned row, even if no lexicon term matches locally.
- In `src/expired_domains/enrich.py`, execute the two anchor queries, insert their rows with their `source_query`, and set:
  - `coverage.top_anchors` = number of `top_anchors` rows returned.
  - `coverage.lexicon_complete = (lexicon_anchors returned rows < 250)`.
  - `runs.config_json["lexicon_version"] = "v1"` (and optionally the lexicon path) from the loaded lexicon.
- Keep `runs.coverage_json` otherwise unchanged from task 003.

#### CLI / Interface

- Include both anchor queries in the per-request output with status, rows, and units.
- Extend the summary with `top_anchors` and `lexicon_complete`.

#### Test Harness

- Reuse `tests/conftest.py`. Reference counts for the corrected fixture and peers: `smoke-fire.com` 165 lexicon / 50 top, `123eft.com` 250 / 50, `kanuksheepskinproducts.com` 250 / 50, `longbeachcoffee.com` 188 / 50, `abipr.com` 168 / 50.
- Use synthetic payload rows to assert null `last_seen`/`refdomains` for `top_anchors` and local classification behaviour (for example `Seoul` matches `seo`, `C4SINO` matches `c4sino` after normalisation).
- Keep DB/raw writes under `tmp_path`.

### TDD Execution Plan

#### Step 1: RED Phase (Write Failing Test)

- **Test files to create/update:**
  - `tests/test_queries.py` — `config/lexicon/v1.json` parses with version `v1`, five categories, and 36 unique terms; `build_where` yields one `or` with 36 entries, each shaped `{"field": "anchor", "is": ["isubstring", term]}`, and includes `link_selling_seo` terms; `lexicon_anchors` params are exactly `history=all_time`, `limit=250`, the full select list, and the `where` string; `top_anchors` params are exactly `history=live`, `limit=50`, `order_by=links_to_target:desc`, and the shorter select; neither includes `offset`.
  - `tests/test_parsers.py` — parsing the corrected `lexicon_anchors.p0.json` yields 165 rows with `source_query="lexicon_anchors"`; `top_anchors.p0.json` yields 50 rows with `refdomains is None` and `last_seen is None`; `123eft.com` yields 250 lexicon rows; normalisation unescapes HTML entities and lowercases; `classify("Seoul")` returns the `seo` term/category, `classify("C4SINO")` returns `c4sino`/`evasions`, and a non-matching anchor returns empty arrays; `lexicon_complete` is true at 165 and false at 250.
  - `tests/test_storage.py` — `anchors` table has PK `(run_id, source_query, anchor_norm)`; re-inserting the same rows is idempotent; JSON term/category columns round-trip; both source queries coexist for one run; `runs.config_json` contains `lexicon_version="v1"`.
- **Test execution command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_parsers.py tests/test_storage.py -v
  ```
- **Expected result:** Tests MUST fail because the lexicon file/loader, anchor queries, parser, table, and coverage flag do not exist. Confirm failures are assertion/`FileNotFoundError`/`NotImplementedError`/missing-table failures caused by missing functionality.

#### Step 2: GREEN Phase (Implementation)

- **Minimum application code to write/modify:**
  - `config/lexicon/v1.json` — exact §3.10 terms.
  - `src/expired_domains/ahrefs/lexicon.py` — load, `where` builder, classifier.
  - `src/expired_domains/ahrefs/queries.py` — two anchor specs.
  - `src/expired_domains/storage/schema.sql` — `anchors`.
  - `src/expired_domains/storage/db.py` — `upsert_anchors`.
  - `src/expired_domains/storage/parsers.py` — `parse_anchors` and lexicon-aware coverage.
  - `src/expired_domains/enrich.py` — execute, insert, record lexicon version.
  - `src/expired_domains/cli.py` — report counts and completion flag.
- **Test re-run command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_parsers.py tests/test_storage.py -v
  ```
- **Expected result:** Tests MUST pass cleanly, including 165/250 truncation flags and NULL `last_seen`/`refdomains` for `top_anchors`, with no network access.

#### Step 3: REFACTOR Phase

- **Cleanup tasks:**
  - Ensure one anchor-normalisation implementation is shared with the backlink code from task 003.
  - Build the `where` JSON with `json.dumps(..., separators=(",", ":"), ensure_ascii=False)` so multilingual terms survive unchanged.
  - Confirm the lexicon is read once and never from a live `.env` or network source.
  - Remove any test-only term lists from production code.
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
- [ ] `config/lexicon/v1.json` contains the version and all five categories with 36 unique terms
- [ ] The lexicon request uses a single `or` over all 36 terms and `limit=250`; no `offset` is sent
- [ ] Stored counts are 165/50 for `smoke-fire.com`, 250/50 for `123eft.com`, 250/50 for `kanuksheepskinproducts.com`, 188/50 for `longbeachcoffee.com`, and 168/50 for `abipr.com`
- [ ] `lexicon_complete` is `true` below 250 lexicon rows and `false` at exactly 250
- [ ] `top_anchors` rows store NULL `refdomains`/`last_seen`; local classification never drops a returned row; `runs.config_json` records `lexicon_version="v1"`
