# Change Summary Specification — Evaluation Data Retrieval & Storage

**Date:** 2026-11-24
**Slug:** `evaluation-data-retrieval`
**Depends on:** `specs/2026-11-24-project-scaffolding.md`
**Requirement source:** `docs/reference/ahrefs-ingestion-spec-v3.md` §2, §4, §6, §7, §8, §10

---

## 1. Objective & Overview

Given a bare domain, retrieve the v3.1 §4 standard-tier dataset from Ahrefs, persist every raw payload and request record, and load a queryable structured index — recording coverage, truncation flags, and API-unit cost — with no evaluation, scoring, or judgement of the domain. The output is the trustworthy evidence base that a later evaluation agent (and any future UI) will consume.

---

## 2. Scope Boundaries

### In Scope

| Item | Detail |
|---|---|
| Candidate input | A single bare domain string. `drop_date` is always unavailable; `drop_date_source = "unknown"`. |
| Provider | Ahrefs API v3 only. |
| Retrieval contract | Exactly the §4 standard-tier queries listed in §3.1, executed in order, one page each. |
| Raw persistence | Payload, response headers, request params, status, attempts, and errors per request, saved under `data/raw/ahrefs/{domain}/{run_id}/`. |
| Structured index | SQLite tables `runs`, `requests`, `stats`, `history`, `backlinks`, `anchors` per §7. |
| Normalisation | URL/domain normalisation with public-suffix root extraction; anchor HTML-unescape/whitespace-collapse/lowercase for matching; `null` preserved. |
| Coverage object | §8 standard-tier shape, including `lexicon_complete`. |
| Cost accounting | `x-api-units-cost-total-actual` per request, totals per run, limits before/after. |
| Run identity | Immutable run per fetch; same-day re-fetch refused unless `--force`. |
| CLI | `enrich DOMAIN` with `--force` / `--db`; per-query progress and a final summary. |
| Fixture replay (test-only) | A test helper loads an existing run directory into a temporary DB without network, so the seven reference runs act as a zero-cost regression corpus. No user-facing import command. |
| Lexicon | Versioned `config/lexicon/v1.json` containing the v3.1 §4.6 starter terms. |
| Testing | Offline pytest suite driven by the seven reference runs; one optional live smoke check. |

### Out of Scope

| Item | Reason |
|---|---|
| §3 pre-filter (name, free DR, live check, `batch-analysis`, Wayback) | Deferred with evaluation; no candidate is rejected or filtered. |
| §5 finalist tier and `refdomains` table population | Deferred; `refdomains` table is not created. |
| Evaluation, rubric, scoring, verdicts, buy/review/reject | Explicitly deferred by the user. |
| Web UI, scheduling, auth, multi-user | Not needed for this milestone. |
| LangGraph, agents, LLM calls | Explicitly dropped for this milestone. |
| DataForSEO integration or cross-checks | Not part of this milestone. |
| Evaluation-report §7 recommendations: lexicon split into tiers, removing/qualifying `seo`, tri-state `traffic_history`, complete-lexicon splitting | v3.1 is authoritative; the report is diagnostic only. |
| Pagination/offset of anchors beyond 250 rows | §4.6 says record truncation, do not split. |
| Workbook generation (`xlsx`) | Legacy scripts archived; not part of the new pipeline. |
| User-facing `import-run` command or migrating legacy runs into the live index | Not needed; existing runs are test fixtures only. |
| Rotating API keys | Manual user action. |
| Backfilling pre-filter or finalist data into fixtures | Not retrievable from the existing runs. |

---

## 3. Technical Architecture & Decisions

### 3.1 Retrieval Contract (one run = these requests, in order)

| # | `query_id` | Endpoint | Parameters |
|---|---|---|---|
| 0a | `limits_before` | `GET /subscription-info/limits-and-usage` | none |
| 1 | `stats` | `GET /site-explorer/backlinks-stats` | `target`, `mode=domain`, `date=<run date>` |
| 2 | `refdomains_history` | `GET /site-explorer/refdomains-history` | `target`, `mode=domain`, `date_from=<today − 48 months, first of month>`, `history_grouping=monthly` |
| 3 | `metrics_history` | `GET /site-explorer/metrics-history` | same date window + `history_grouping=monthly`, `select=date,org_traffic` |
| 4 | `top_dr_backlinks` | `GET /site-explorer/all-backlinks` | `history=live`, `aggregation=1_per_domain`, `limit=70`, `order_by=domain_rating_source:desc`, `select=url_from,url_to,anchor,first_seen_link,is_dofollow,domain_rating_source` |
| 5 | `latest_backlinks` | `GET /site-explorer/all-backlinks` | same as #4 with `limit=30`, `order_by=first_seen_link:desc` |
| 6 | `lexicon_anchors` | `GET /site-explorer/anchors` | `history=all_time`, `limit=250`, `select=anchor,links_to_target,dofollow_links,refdomains,first_seen,last_seen`, `where` = one `or` over every lexicon term (`isubstring`) |
| 7 | `top_anchors` | `GET /site-explorer/anchors` | `history=live`, `limit=50`, `order_by=links_to_target:desc`, `select=anchor,links_to_target,dofollow_links,first_seen` |
| 0b | `limits_after` | `GET /subscription-info/limits-and-usage` | none |

`mode=domain` on every target-scoped call. No `history` parameter on `backlinks-stats`. `select` is always explicit.

### 3.2 Run Identity & Lifecycle

- `run_id = "{domain}:{YYYYmmddTHHMMSSZ}:ahrefs"` (UTC). Directory name uses `_` in place of `:`.
- Every execution creates a new immutable run; nothing is overwritten.
- If a run already exists for the domain with a `fetched_at` date equal to today (UTC), `enrich` exits with a clear message unless `--force` is passed.
- Ordering: `limits_before` → queries 1–7 → `limits_after`.
- Partial failure: a failed request is recorded (`status`, `error`) and the run continues; remaining queries still execute. `runs.error` is `null` when every request succeeded, otherwise a JSON object listing failed query IDs and messages. CLI exit code is `1` if any request failed, `0` otherwise; `2` for configuration errors (e.g. missing `AHREFS_KEY`).
- Cache headers are honoured as delivered: the run reports both actual and billed units, but does not decide freshness from `x-api-cache`.

### 3.3 Raw Storage Layout

```text
data/raw/ahrefs/{domain}/{run_id_dir}/
  stats.p0.json
  stats.p0.headers.json
  refdomains_history.p0.json
  ... (one pair per request; page is always 0 in this milestone)
```

Each `.json` file uses the existing run format:

```json
{"request": {query_id, endpoint, page, params, url, status, started_at, finished_at,
             attempts, error, parse_error, rows, units_cost_total,
             units_cost_total_actual, units_cost_row, cache, headers},
 "payload": { ... },
 "raw_body": null}
```

`.headers.json` holds the full response headers. `requests.raw_path` stores the JSON path **relative to the project root**.

### 3.4 Structured Index (SQLite)

Default `data/index.sqlite3`; `PRAGMA journal_mode=WAL`, `foreign_keys=ON`. Schema from §7:

| Table | Columns |
|---|---|
| `runs` | `run_id` PK, `domain`, `mode`, `tier`, `prefilter_batch` (NULL), `drop_date` (NULL), `drop_date_source` (`unknown`), `fetched_at`, `config_json`, `coverage_json`, `units_actual`, `error` |
| `requests` | `run_id`, `query_id`, `page`, `endpoint`, `params_json`, `row_count`, `units_actual`, `raw_path`, `status`, `error`, PK (`run_id`,`query_id`,`page`) |
| `stats` | `run_id`, `snapshot` (`live`/`all_time`), `links`, `refdomains`, PK (`run_id`,`snapshot`) |
| `history` | `run_id`, `series` (`refdomains`/`org_traffic`), `date`, `value`, PK (`run_id`,`series`,`date`) |
| `backlinks` | `run_id`, `link_hash`, `source_queries` (JSON array), `url_from`, `url_to`, `domain_from`, `root_domain_from`, `anchor_raw`, `anchor_norm`, `is_dofollow`, `domain_rating`, `first_seen_link`, `era` (NULL), `raw_json`, PK (`run_id`,`link_hash`) |
| `anchors` | `run_id`, `source_query`, `anchor_raw`, `anchor_norm`, `links_to_target`, `dofollow_links`, `refdomains`, `first_seen`, `last_seen`, `lexicon_terms` (JSON), `lexicon_categories` (JSON), `raw_json`, PK (`run_id`,`source_query`,`anchor_norm`) |

`refdomains` (finalist-only) is **not** created. `era` is always `NULL` because drop date is unknown. `prefilter_batch` is always `NULL`.

### 3.5 Parsing Rules

| Payload | Mapping |
|---|---|
| `backlinks-stats` → `metrics` | Two `stats` rows from one response: `live` = (`live`, `live_refdomains`), `all_time` = (`all_time`, `all_time_refdomains`). |
| `refdomains-history` → `refdomains[]` | One `history` row per entry, `series='refdomains'`, `value=refdomains`; `date` normalised to `YYYY-MM-DD`. |
| `metrics-history` → `metrics[]` | One row per entry, `series='org_traffic'`, `value=org_traffic`; zero rows is a valid result. |
| `all-backlinks` → `backlinks[]` | Normalise both URLs, derive `domain_from` from the normalised `url_from` host, `root_domain_from` via PSL; `link_hash` per §3.7; merge duplicate hashes across queries by appending the query ID to `source_queries`. |
| `anchors` → `anchors[]` | `source_query` = `top_anchors` or `lexicon_anchors`; normalise anchor; classify against the lexicon locally; `last_seen`/`refdomains` are NULL when the select did not request them. |
| `limits-and-usage` | Stored as a request only; the payload is available in raw form for budget reporting. |

### 3.6 Normalisation

- URL: lowercase scheme and host, strip default ports (`80`/`443`), preserve path/query casing; parse failures keep the raw value and leave derived fields NULL.
- Anchor: HTML-unescape → collapse whitespace → lowercase for `anchor_norm`; `anchor_raw` untouched.
- Root domain: `tldextract` with `suffix_list_urls=()` (bundled offline snapshot; no network).
- `null` in the payload stays `null` in the index.

### 3.7 Link Hash

```text
link_hash = sha256("ahrefs\n" + norm_url_from + "\n" + norm_url_to + "\n" + anchor_norm + "\n" + first_seen_link)
```

`first_seen_link` is used verbatim (or empty string when null).

### 3.8 Coverage Object (`runs.coverage_json`)

```json
{
  "mode": "top_n",
  "stats": {"live": 0, "all_time": 0, "live_refdomains": 0, "all_time_refdomains": 0},
  "rows": {"top_dr": 0, "latest": 0, "unique": 0},
  "top_anchors": 0,
  "lexicon_complete": true,
  "history_months": 48,
  "drop_date_source": "unknown",
  "prefilter": null
}
```

- `lexicon_complete = (number of lexicon_anchors rows < 250)`.
- `rows.unique` = distinct `link_hash` count for the run.
- `stats` is taken from the `stats` table; if the call failed, all four values are `null`.
- Written at the end of a run; the test replay helper recomputes it from raw payloads.

### 3.9 Cost Accounting

- Per request: `units_actual = int(x-api-units-cost-total-actual or 0)`; billed = `x-api-units-cost-total` (kept in the raw request record).
- `runs.units_actual` = sum of non-`limits_*` requests.
- The manifest summary reports both actual and billed totals; expected ~3.4k units/domain (range 2.7–4.1k) before cache effects.
- No cap is enforced in this milestone; the values are recorded, not policed.

### 3.10 Lexicon

`config/lexicon/v1.json`:

```json
{
  "version": "v1",
  "categories": {
    "gambling": ["casino", "slot", "gacor", "judi", "togel", "poker", "betting",
                 "sportsbook", "baccarat", "roulette", "카지노", "바카라", "토토", "казино"],
    "pharma": ["viagra", "cialis", "pharmacy", "pills", "tramadol", "kamagra", "cbd"],
    "adult": ["porn", "xxx", "sex", "escort", "nude", "cam girls"],
    "link_selling_seo": ["pbn", "backlink", "guest post", "seo", "link building", "buy links"],
    "evasions": ["c4sino", "v1agra", "p0rn"]
  }
}
```

- All categories are included in the single `where` `or` for `lexicon_anchors` (36 terms; the `link_selling_seo` restriction in §4.6 applies only to which categories may later *reject* on other surfaces, not to this search).
- `lexicon_terms` / `lexicon_categories` are computed locally with case-insensitive substring matching over `anchor_norm`; rows are never dropped for not matching locally.
- The lexicon is loaded once per process and its version recorded in `runs.config_json`.

### 3.11 CLI Surface

```text
python -m expired_domains enrich <domain> [--force] [--db PATH]
python -m expired_domains --version
```

`enrich` prints one line per request (`query_id`, status, rows, units) and a summary: run ID, raw directory, coverage flags, actual/billed units. There is no user-facing import command; replay exists only in the test harness (§3.12).

### 3.12 Fixture Replay (test-only)

- `tests/replay.py` exposes a helper that accepts a run directory containing `manifest.json` and `raw/` and inserts its contents into a temporary database using the production parsers. It is not exposed on the CLI.
- Supports both the corrected run shape (`stats`) and legacy shapes (`stats_live` + `stats_all_time`); when only the split pair exists, each payload carries all four counts, so either may populate `stats`.
- Query ID mapping: `stats|stats_live|stats_all_time` → `stats`; `refdomains_history` → `history`; `metrics_history` → `history`; `top_dr_backlinks|latest_backlinks` → `backlinks`; `lexicon_anchors|top_anchors` → `anchors`; `limits_*` → `requests` only.
- Unknown query IDs are recorded in `requests` and skipped with a warning, never treated as run failure.
- Each test receives a fresh temporary database; no production index is touched.
- Raw files are read-only inputs; the helper never rewrites them.
- Required for the fixture corpus: 5 domains, 7 run directories (3 under `smoke-fire.com`).

### 3.13 Known API Behaviours to Respect

| Behaviour | Consequence |
|---|---|
| `backlinks-stats` ignores `history`/`protocol`/`aggregation`/`select` and returns all four counts | Exactly one call; never attempt a `since:` count. |
| `anchors` hard-caps at 250 and ignores `offset` | Never paginate; set `lexicon_complete=false` at 250; do not split terms. |
| `refdomains-history` returns 49 rows for a 48-month inclusive window | Do not assert exactly 48. |
| `metrics-history` may return 0 rows (e.g. `abipr.com`) | Store zero rows; do not infer zero traffic or mark the run failed. |
| Lexicon substring matching is over-inclusive (`seo` ⊂ `Seoul`) | Store returned rows and local matches as evidence; no filtering. |
| Minimum charge 50 units/request | Expected per-domain cost ~3.4k; record actuals. |
| `aggregation=1_per_domain` representative row can vary between runs | Dedupe by `link_hash`; do not assume stable row identity. |

### 3.14 Testing Strategy

- All automated tests run offline against `tests/fixtures/runs/`; no API key required. Tests skip with a clear message if the fixture directory is absent.
- Parser/storage tests use the reference runs and assert known values: `smoke-fire.com` corrected run (stats 1159 / 5979 / 992 / 1599; 49 history rows; 70 top-DR; 30 latest; 165 lexicon; 50 top anchors; 1 traffic row), `123eft.com` (250 lexicon rows ⇒ incomplete; 36 traffic rows), `kanuksheepskinproducts.com` (250; 37), `longbeachcoffee.com` (188; 21), `abipr.com` (168; 0 traffic rows).
- Replay parity test covers all seven run directories, including the two legacy `smoke-fire.com` runs with split stats.
- One live check is marked `@pytest.mark.live` and skipped by default; the final verification task runs it manually once.

### 3.15 Decisions

| Decision | Rationale |
|---|---|
| v3.1 authoritative; report recommendations not adopted | User decision: v3 defines which rows to pull. |
| SQLite index + raw files | Spec allows SQLite/Postgres; SQLite is sufficient and needs no server or root access. |
| `httpx` sync client with bounded retries | Clear timeout/exception handling; easy to swap for async later. |
| 120 s timeout; retries on 429/5xx/network with exponential backoff (max 6 attempts) | Mirrors the legacy script's proven behaviour. |
| New immutable run per fetch, same-day guard with `--force` | Spec §10 freshness rule; preserves audit trail. |
| Continue on request failure, record error | Keeps partial evidence; avoids losing paid calls because one query failed. |
| Full raw payload per request retained | Spec principle "save first, compute later"; enables replay-based tests and re-parsing. |
| Fixture replay is test-only | Reuses the seven existing runs as a free regression corpus without adding a user-facing migration command. |
| `era`, `drop_date`, `prefilter_batch` always NULL | No drop date and no pre-filter in this milestone. |
| Typed result object returned by the enrich operation | Stable seam for a future agent/UI without designing the graph now. |

---

## 4. Affected Files

| File | Action |
|---|---|
| `pyproject.toml` | Modify — add runtime deps `httpx`, `tldextract`; add `live` pytest marker. |
| `.env.example` | Modify — mark `AHREFS_KEY` as required for this milestone (placeholder only; never add real values). |
| `src/expired_domains/config.py` | Modify — include `AHREFS_KEY` validation helper, `api_base`, and paths for raw/index. |
| `src/expired_domains/models.py` | Create — dataclasses for `RequestRecord`, `QuerySpec`, `RunSummary`, `Coverage`, `Units`. |
| `src/expired_domains/ahrefs/__init__.py` | Create — package marker. |
| `src/expired_domains/ahrefs/client.py` | Create — auth header, retries/backoff, timeout, header capture, raw file writing. |
| `src/expired_domains/ahrefs/queries.py` | Create — the §3.1 query registry and date-window calculation. |
| `src/expired_domains/ahrefs/lexicon.py` | Create — lexicon loading, `where` builder, local term/category classification. |
| `src/expired_domains/storage/__init__.py` | Create — package marker. |
| `src/expired_domains/storage/schema.sql` | Create — §7 schema (minus `refdomains`). |
| `src/expired_domains/storage/db.py` | Create — connection, migration/init, insert/upsert helpers, same-day check. |
| `src/expired_domains/storage/raw.py` | Create — run directory naming, raw/header file writing, relative paths. |
| `src/expired_domains/storage/parsers.py` | Create — payload → table rows per §3.5, normalisation, `link_hash`, coverage computation. |
| `src/expired_domains/enrich.py` | Create — orchestration of one run, summary construction. |
| `src/expired_domains/cli.py` | Modify — `enrich`, `--force`, `--db`; exit codes. |
| `config/lexicon/v1.json` | Create — terms from §3.10. |
| `tests/conftest.py` | Create — fixture paths, tmp DB/raw roots, skip when fixtures missing. |
| `tests/replay.py` | Create — test-only helper that loads a run directory into a temp DB via the production parsers. |
| `tests/test_client.py` | Create — retry/backoff, header capture, error recording (mocked transport). |
| `tests/test_queries.py` | Create — exact params, date window, lexicon `or`, no `history` on stats. |
| `tests/test_parsers.py` | Create — stats/history/backlinks/anchors mapping, hashing, dedupe, normalisation. |
| `tests/test_storage.py` | Create — schema creation, upserts, same-day guard, coverage. |
| `tests/test_replay_parity.py` | Create — all seven reference runs; counts/units/truncation flags. |
| `tests/test_cli.py` | Create — help/version, argument validation, exit codes, `--force`. |
| `tests/test_live.py` | Create — `@pytest.mark.live`, skipped by default; one real enrich. |

Do **not** create or modify `.env`. Do **not** create the `refdomains` table. Do **not** add a `data/` artifact to git.

---

## 5. Proposed Task Breakdown

**Milestone definition of done:** the fixture-replay tests reproduce the expected counts, units, and `lexicon_complete` flags for all seven reference runs; a live `enrich <domain>` stores raw payloads plus index rows, prints per-query results and totals, and exits 0; the full offline test suite is green.

### Phase 1 — First Vertical Slice (stats, cost, coverage)

1. **Stats retrieval end to end** — Implement config validation, `httpx` client with auth/retries/header capture, the `stats` query, raw file writing, the SQLite schema + `runs`/`requests`/`stats` inserts, and an `enrich` command that (for now) runs `limits_before` → `stats` → `limits_after` and prints results. Test by replaying `tests/fixtures/runs/smoke-fire.com/*084340Z*` with a mocked client and asserting 1159/5979/992/1599 and the units total.
2. **Coverage and cost** — Compute and store the coverage object (`mode`, `stats`, empty row counters, `history_months`, `drop_date_source`), actual/billed totals, and the run summary; assert cache-hit vs miss accounting from the fixture headers.

### Phase 2 — History Slice

3. **History series** — Add `refdomains_history` and `metrics_history` queries with the 48-month window, parse both into `history`, update coverage, and extend the CLI summary. Tests: 49 refdomain rows, `abipr.com` zero traffic rows stored without error, `123eft.com` 36 traffic rows.

### Phase 3 — Backlinks Slice

4. **Backlink slices** — Add `top_dr_backlinks` and `latest_backlinks`, implement URL normalisation, PSL root extraction, `link_hash`, cross-query dedupe with `source_queries`, and `backlinks` inserts; compute `rows.unique`. Tests: 70 + 30 returned, expected unique counts, 1-per-domain ordering preserved, null anchors preserved.

### Phase 4 — Anchors Slice

5. **Anchor slices** — Add `top_anchors` and `lexicon_anchors`, build the single `or` query from `config/lexicon/v1.json`, parse both into `anchors`, classify terms/categories locally, and compute `lexicon_complete`. Tests: `smoke-fire.com` 165 rows ⇒ complete; `123eft.com` 250 ⇒ incomplete; `top_anchors` last_seen/refdomains NULL.

### Phase 5 — Orchestration, Replay, Verification

6. **Full run orchestration** — Execute all nine requests in order, enforce the same-day guard with `--force`, record partial failures and `runs.error`, finalise coverage/manifest, set exit codes. Test with a mocked client that fails one query: run completes, error recorded, exit code 1.
7. **Fixture replay parity (test-only)** — Build `tests/replay.py` to load new and legacy run layouts (including `stats_live`/`stats_all_time`) into a temp DB via the production parsers; recompute coverage. Test all seven reference runs against the known counts, including the two split-stats `smoke-fire.com` runs. No CLI command and no production-index import.
8. **Live verification** — Run `enrich` once against a live domain (fresh domain preferred; if reusing one, note cache effects), confirm raw files, index rows, coverage flags, and reported units; record the observed actual cost. Keep this check behind `@pytest.mark.live` and out of the default suite.
