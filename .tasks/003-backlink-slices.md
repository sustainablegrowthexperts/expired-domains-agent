---
id: task-003
title: "[Slice] Retrieve backlink slices with normalisation, hashing, and dedupe"
status: completed
depends_on: ["task-002"]
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

**Objective:** A single `enrich <domain>` run now also retrieves the top-70-by-DR and latest-30 live backlinks, normalises both URLs, derives PSL root domains, hashes each link, merges cross-query duplicates into one `backlinks` row with both source query IDs, and reports the deduplicated `rows.unique` count (98 for the corrected `smoke-fire.com` fixture).

### Layer-by-Layer Changes

#### Data / Schema

- Add the `backlinks` table to `src/expired_domains/storage/schema.sql` exactly per spec §3.4: `run_id`, `link_hash`, `source_queries` (JSON array text), `url_from`, `url_to`, `domain_from`, `root_domain_from`, `anchor_raw`, `anchor_norm`, `is_dofollow`, `domain_rating`, `first_seen_link`, `era` (always `NULL`), `raw_json`, PK `(run_id, link_hash)`, with `run_id` referencing `runs(run_id)`.
- In `src/expired_domains/storage/db.py`, add `upsert_backlinks(conn, run_id, rows)`. On conflict, merge `source_queries` as the sorted union of existing and incoming query IDs, and keep the existing row's canonical values. Never create a second row for the same `link_hash`.
- Store booleans as SQLite integers, JSON arrays as compact JSON text, and `era` as `NULL` for every row.

#### Core Logic / Engine

- Extend `build_run_queries(domain, run_date)` in `src/expired_domains/ahrefs/queries.py` with two specs after `metrics_history`:
  - `top_dr_backlinks` → `/site-explorer/all-backlinks` with `target`, `mode=domain`, `history=live`, `aggregation=1_per_domain`, `limit=70`, `order_by=domain_rating_source:desc`, `select=url_from,url_to,anchor,first_seen_link,is_dofollow,domain_rating_source`.
  - `latest_backlinks` → the same endpoint with `limit=30` and `order_by=first_seen_link:desc`; every other parameter identical.
  - No pagination parameters; one page each.
- In `src/expired_domains/storage/parsers.py` add:
  - `normalize_url(value) -> str | None` — lowercase scheme and host, strip only default ports `80`/`443`, preserve path and query casing, preserve the fragment if present. On a parse failure return the original raw value and leave derived fields `None` (see below).
  - `normalize_anchor(value) -> str | None` — return `None` for `None`, otherwise HTML-unescape, collapse internal whitespace runs to one space, trim, and lowercase. `anchor_raw` stays untouched.
  - `root_domain(host) -> str | None` using a module-level `tldextract.TLDExtract(suffix_list_urls=())` (bundled snapshot, no network). Return the registrable domain (for example `example.co.uk`).
  - `link_hash(url_from, url_to, anchor_norm, first_seen_link) -> str` implementing exactly `sha256("ahrefs\n" + norm_url_from + "\n" + norm_url_to + "\n" + anchor_norm + "\n" + first_seen_link)`; use an empty string when `first_seen_link` is `None`. When a normalised value is `None`, use an empty string in the hash input.
  - `parse_backlinks(payload, source_query) -> list[BacklinkRow]` mapping `payload["backlinks"]` per §3.5: normalise both URLs, set `domain_from` to the normalised `url_from` host, derive `root_domain_from` via PSL, preserve null anchors and null `first_seen_link`, keep `domain_rating` from `domain_rating_source`, and embed `raw_json` for the original row.
  - URL parse failures keep the raw `url_from`/`url_to` strings and set `domain_from`/`root_domain_from` to `None` instead of dropping the row.
- In `src/expired_domains/enrich.py`, execute the two backlink queries in registry order, accumulate their parsed rows, merge duplicates by `link_hash` while appending the source query ID, and insert once per run.
- Update `compute_coverage` so `rows.top_dr` is the `top_dr_backlinks` returned-row count, `rows.latest` is the `latest_backlinks` count, and `rows.unique` is the number of distinct `link_hash` values stored for the run. Ignore dedupe effects when counting `top_dr`/`latest`.

#### CLI / Interface

- Include both backlink queries in the per-request output with status, returned rows, and units.
- Extend the summary with `top_dr`, `latest`, and `unique` counts.

#### Test Harness

- Reuse `tests/conftest.py`; point tests at the corrected `smoke-fire.com` run for the 70/30 counts and use synthetic rows (constructed in-test) for null anchors, default ports, mixed-case hosts, invalid URLs, and cross-query duplicates.
- Keep every DB and raw write under `tmp_path`; fixture files are read-only.

### TDD Execution Plan

#### Step 1: RED Phase (Write Failing Test)

- **Test files to create/update:**
  - `tests/test_queries.py` — both backlink specs are present after `metrics_history`; exact `limit` (`70`/`30`), `order_by` (`domain_rating_source:desc` / `first_seen_link:desc`), `history=live`, `aggregation=1_per_domain`, and the exact `select` list; no `offset`.
  - `tests/test_parsers.py` — normalisation cases: `HTTPS://Example.COM:443/Path?Q=1` → `https://example.com/Path?Q=1`; `http://example.com:80/` → `http://example.com/`; non-default port retained; invalid URL keeps the raw value and returns `None` for derived host/root. Anchor normalisation: `&amp;` unescaped, tabs/newlines collapsed, lowercased, `None` preserved. Root extraction: `blog.example.co.uk` → `example.co.uk` with no network. `link_hash` is stable across calls, changes when any input changes, and matches a hard-coded expected digest for one constructed row. Parsing the corrected fixture yields 70 and 30 rows. A synthetic duplicate pair produces one merged row with sorted `source_queries`.
  - `tests/test_storage.py` — `backlinks` table PK is `(run_id, link_hash)`; inserting the same hash twice with `top_dr_backlinks` then `latest_backlinks` leaves one row and `["latest_backlinks", "top_dr_backlinks"]` (or the chosen deterministic order); `era IS NULL`; persisted `raw_json` parses back to the original row; the corrected fixture inserts 98 distinct hashes from 100 returned rows; `rows.unique == 98`, `rows.top_dr == 70`, `rows.latest == 30`.
- **Test execution command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_parsers.py tests/test_storage.py -v
  ```
- **Expected result:** Tests MUST fail because the backlink queries, normalisation helpers, hashing, dedupe upsert, and table do not exist. Confirm failures are assertion/`NotImplementedError`/missing-table failures caused by missing functionality.

#### Step 2: GREEN Phase (Implementation)

- **Minimum application code to write/modify:**
  - `src/expired_domains/ahrefs/queries.py` — two specs.
  - `src/expired_domains/storage/schema.sql` — `backlinks`.
  - `src/expired_domains/storage/db.py` — `upsert_backlinks` with `source_queries` union.
  - `src/expired_domains/storage/parsers.py` — URL/anchor normalisation, PSL root, `link_hash`, `parse_backlinks`, coverage counters.
  - `src/expired_domains/enrich.py` — execute, dedupe, insert, update coverage.
  - `src/expired_domains/cli.py` — report counts.
- **Test re-run command:**
  ```bash
  uv run pytest tests/test_queries.py tests/test_parsers.py tests/test_storage.py -v
  ```
- **Expected result:** Tests MUST pass cleanly, including 70 + 30 returned rows and 98 unique hashes for the corrected fixture, with no network access by `tldextract`.

#### Step 3: REFACTOR Phase

- **Cleanup tasks:**
  - Extract the hash-input assembly into one function used by both parsing and tests.
  - Ensure `tldextract` is constructed once at module import and never fetches the suffix list.
  - Confirm the dedupe merge is a single SQL upsert rather than read-modify-write across transactions.
  - Remove fixture-specific constants from production code.
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
- [ ] A mocked `enrich` run stores 70 top-DR rows, 30 latest rows, and 98 unique `backlinks` rows for the corrected `smoke-fire.com` fixture; overlapping rows carry both query IDs
- [ ] URL normalisation lowercases scheme/host, strips only default ports, and preserves path/query casing; invalid URLs keep raw values with NULL derived fields
- [ ] `root_domain_from` is derived offline via PSL; `first_seen_link` and null anchors are preserved; `era` is NULL
- [ ] Coverage reports `rows.top_dr`, `rows.latest`, and `rows.unique` with the correct values
