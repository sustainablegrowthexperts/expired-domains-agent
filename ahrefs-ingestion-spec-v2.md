# Ahrefs Ingestion Spec — Expired Domain Evaluation (v3, lean)

**Purpose:** For each expired domain, capture a small, deliberate slice of Ahrefs data that lets a downstream AI agent screen it, within a **monthly cap of 800k API units**. A full backlink export happens only for finalists.

**Primary provider:** Ahrefs API v3.
**Target example:** `smoke-fire.com`.

---

## 0. What changed

| Version | Approach | Est. units / domain |
|---|---|---:|
| v1 | Full keyset export of every backlink | ~30k+ |
| v2 / v2.1 | Full export ≤ 5k rows; complete aggregates + 5k capped rows above | ~56k (smoke-fire) |
| **v3** | **Top-N slice (100 backlink rows) + cheap whole-profile aggregates; full export for finalists only** | **~2k** |

Decisions behind v3:

- **Live matters; "latest" does not mean "valuable."** For expired domains, the newest links are mostly post-drop spam. Value sits in older links. So the 100 rows are split: **70 strongest by DR + 30 newest**.
- **One row per referring domain** (`aggregation=1_per_domain`) so sitewide links can't consume the slice.
- **Toxic anchors are still checked across the whole profile** via one batched server-side anchor search, not just within the 100 rows.
- **Pay only for selected fields.** Ahrefs bills rows × sum of selected field costs, minimum 50 units per request. Fields used only in `where` / `order_by` are not billed. Avoid 10-unit fields (`traffic_domain`, `org_traffic`, etc.) and 5-unit fields except where rows are few.

---

## 1. Principles

1. **Save first, compute later.** Store every raw payload, request params, and the cost headers. Ingestion makes no quality judgments.
2. **Deliberate slice, honest label.** Standard runs are `coverage.mode = "top_n"`. Never call them complete for row-level questions.
3. **Whole-profile answers come from aggregates.** Size, timing, history, and toxic-anchor presence are answered over the entire profile even though rows are capped.
4. **Escalate, don't expand.** Need more depth? Promote the domain to finalist tier; don't grow the standard slice.

---

## 2. Run inputs

```json
{
  "domain": "smoke-fire.com",
  "mode": "domain",
  "drop_date": "2025-08-14",
  "drop_date_source": "registrar_list | whois_history | inferred | unknown",
  "tier": "standard | finalist",
  "top_dr_rows": 70,
  "latest_rows": 30,
  "top_anchor_rows": 50,
  "history_months": 48,
  "anchor_lexicon": "lexicon/v1.json"
}
```

If `drop_date` is unknown, skip the `since:` stats call and set `drop_date_source = "unknown"`; the agent can infer the drop from the history series.

---

## 3. Standard tier (every domain)

| # | Query | Answers | Est. units |
|---|---|---|---:|
| 0 | `subscription-info/limits-and-usage` | Remaining budget | 0 |
| 1 | `backlinks-stats` × 1 | Live + all-time links and referring domains (all four counts in one row) | 50 |
| 2 | `refdomains-history`, monthly, last 48 months | Post-drop spike, growth pattern | ~290 |
| 3 | `metrics-history`, `select=date,org_traffic`, last 48 months | Was it a real site; traffic cliff | ~530 |
| 4 | Top 70 live backlinks by DR, 1 per domain | Value core | ~420 |
| 5 | Latest 30 live backlinks, 1 per domain | Current spam activity | ~180 |
| 6 | One batched lexicon search on `anchors`, all_time | Toxic anchors anywhere in profile | ~50–500 |
| 7 | Top 50 live anchors by link count | Anchor mix (brand / URL / exact-match / spam) | ~200 |
| | **Total** | | **~1,800–2,300** |

Costs are from each endpoint's `unitCost` in the v3 OpenAPI docs. Confirm with `x-api-units-cost-total-actual` on the first runs.

### 3.1 Backlink counts

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/backlinks-stats" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "date=2026-11-24"
```

One row (12 units) → billed at the 50-unit minimum. Returns all four counts together:

```json
{"metrics": {"live": 1159, "all_time": 5979,
             "live_refdomains": 992, "all_time_refdomains": 1599}}
```

**Verified 2026-11-24:** `history`, `protocol`, `aggregation` and `select` are accepted but **ignored** — `history=live`, `history=all_time` and `history=since:2025-08-14` all return the same payload. Do **not** make separate calls per snapshot, and do not expect a `since:` count from this endpoint. For post-drop movement use #2 (`refdomains-history`) or, at finalist tier, a `first_seen_link > drop_date` export.

The refdomain counts are **subdomain-level** (e.g. `foo.blogspot.com` and `bar.blogspot.com` count separately). Registrable-root counts are not exposed by this endpoint; derive them locally from a `refdomains` export.

### 3.2 Referring-domain history

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/refdomains-history" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "date_from=2022-11-01" \
  --data-urlencode "history_grouping=monthly"
```

6 units/row (`date` 1 + `refdomains` 5) × ~48 rows.

### 3.3 Organic traffic history

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/metrics-history" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "date_from=2022-11-01" \
  --data-urlencode "history_grouping=monthly" \
  --data-urlencode "select=date,org_traffic"
```

**Always pass `select`.** The default select includes four 10-unit fields (41 units/row); `date,org_traffic` is 11 units/row.

`date_from` default: today − 48 months. If `drop_date` is known and older than that, use `drop_date − 24 months` so the pre-drop baseline is visible.

### 3.4 Top 70 live backlinks by DR

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/all-backlinks" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "history=live" \
  --data-urlencode "aggregation=1_per_domain" \
  --data-urlencode "limit=70" \
  --data-urlencode "order_by=domain_rating_source:desc" \
  --data-urlencode "select=url_from,url_to,anchor,first_seen_link,is_dofollow,domain_rating_source"
```

6 units/row. Each row is a different referring domain.

### 3.5 Latest 30 live backlinks

Same as 3.4 with:

```text
limit=30
order_by=first_seen_link:desc
```

Overlap with 3.4 is possible; dedupe locally and record which query returned each row.

### 3.6 Batched lexicon search (whole profile)

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/anchors" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "history=all_time" \
  --data-urlencode "limit=250" \
  --data-urlencode "select=anchor,links_to_target,dofollow_links,refdomains,first_seen,last_seen" \
  --data-urlencode 'where={"or":[{"field":"anchor","is":["isubstring","casino"]},{"field":"anchor","is":["isubstring","viagra"]}, ...]}'
```

- **All lexicon terms in one `or` request.** Classify matches to terms/categories locally.
- 10 units/row; usually few rows. `last_seen IS NULL` = still live.
- `all_time` so lost spam (past campaigns, possible penalties) is visible too.
- If it returns 250 rows, split the term list in half and repeat until each request is under the cap. Mark `lexicon_complete = false` only if a single term still hits 250.
- Substring matches are over-inclusive (`bet` ⊂ `better`); keep raw matches, filter downstream.

**Lexicon (starter, versioned):**

| Category | Terms |
|---|---|
| Gambling | casino, slot, gacor, judi, togel, poker, betting, sportsbook, baccarat, roulette, 카지노, 바카라, 토토, казино |
| Pharma | viagra, cialis, pharmacy, pills, tramadol, kamagra, cbd |
| Adult | porn, xxx, sex, escort, nude, cam girls |
| Link selling / SEO | pbn, backlink, guest post, seo, link building, buy links |
| Evasions | c4sino, v1agra, p0rn (extend as discovered) |

### 3.7 Top 50 live anchors

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/anchors" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "history=live" \
  --data-urlencode "limit=50" \
  --data-urlencode "order_by=links_to_target:desc" \
  --data-urlencode "select=anchor,links_to_target,dofollow_links,first_seen"
```

4 units/row.

---

## 4. Finalist tier (escalation only)

Run when a domain is about to be bought, or when the agent flags a contradiction it can't resolve from the standard slice (e.g. a large post-drop count with no visible spam in the slice or lexicon).

### 4.1 What runs

| Query | Notes |
|---|---|
| Full live backlink export (keyset, §4.2) | 6 units/row; smoke-fire ≈ 1,159 rows ≈ 7k units |
| Full live refdomains | `select=domain,domain_rating,links_to_target,dofollow_links,positions_source_domain,is_spam,first_seen` (7 units/row), keyset on `domain:asc` |
| Outbound links (`linked-anchors-external`, `limit=100`) | Reused PBN/casino sites link out to money sites |
| Optional: full all_time export | Only if pre-drop link history is needed; can be large |

Budget guard: if `backlinks-stats.live > 20,000`, export DR-filtered rows only (`where domain_rating_source >= 20`, filter not billed) and mark `coverage.mode = "finalist_filtered"`.

### 4.2 Keyset pagination (verified)

- `all-backlinks` returns **at most 250 rows per request** on this plan; `limit>250` is silently capped, `offset` is ignored.
- Paginate with `where` on a cursor that is unique per row:

```text
order_by = first_seen_link:asc,url_from:asc,url_to:asc
aggregation = all
history = live
select = url_from,url_to,anchor,first_seen_link,is_dofollow,domain_rating_source
```

```json
{"or": [
  {"field": "first_seen_link", "is": ["gt", "<t>"]},
  {"and": [{"field": "first_seen_link", "is": ["eq", "<t>"]},
           {"field": "url_from", "is": ["gt", "<from>"]}]},
  {"and": [{"field": "first_seen_link", "is": ["eq", "<t>"]},
           {"field": "url_from", "is": ["eq", "<from>"]},
           {"field": "url_to", "is": ["gt", "<to>"]}]}
]}
```

- Stop when a page returns < 250 rows. Enforce a max-page guard.
- Rows with null `first_seen_link` are unreachable by this cursor; query with `"is_null"` separately or record the gap.
- Reconcile row count against `backlinks-stats.live`. Verified: `smoke-fire.com` 1,160 vs 1,159 (check for a cross-page duplicate), `coblaith.net` exact, `pyracy.com` −5. Any unexplained gap → `complete = false`.

```python
def fetch_all_live_backlinks(domain, base_filter=None):
    rows, cursor, page = [], None, 0
    while True:
        clauses = [c for c in (base_filter, cursor) if c]
        params = {
            "target": domain, "mode": "domain", "limit": 250,
            "history": "live", "aggregation": "all",
            "order_by": "first_seen_link:asc,url_from:asc,url_to:asc",
            "select": "url_from,url_to,anchor,first_seen_link,is_dofollow,domain_rating_source",
        }
        if clauses:
            params["where"] = json.dumps(clauses[0] if len(clauses) == 1 else {"and": clauses})
        payload, headers = ahrefs_get("/site-explorer/all-backlinks", params)
        save_raw(payload, headers, params, page)
        batch = payload.get("backlinks", [])
        rows.extend(batch); page += 1
        if len(batch) < 250: break
        if page >= MAX_PAGES: raise IncompleteRun("max-page guard")
        t, f, u = batch[-1]["first_seen_link"], batch[-1]["url_from"], batch[-1]["url_to"]
        cursor = {"or": [
            {"field": "first_seen_link", "is": ["gt", t]},
            {"and": [{"field": "first_seen_link", "is": ["eq", t]},
                     {"field": "url_from", "is": ["gt", f]}]},
            {"and": [{"field": "first_seen_link", "is": ["eq", t]},
                     {"field": "url_from", "is": ["eq", f]},
                     {"field": "url_to", "is": ["gt", u]}]},
        ]}
    return rows
```

---

## 5. Field selection rules

| Rule | Detail |
|---|---|
| Name the metric | Every selected column must feed a named metric or the cursor |
| Avoid 10-unit fields | `traffic_domain`, `traffic`, `traffic_page`, `org_traffic`, `org_cost`, … — only where rows are few (e.g. `metrics-history`) |
| Avoid 5-unit fields on big tables | `refdomains`, `refdomains_source*`, `dofollow_refdomains`, `class_c`, `live_refdomains` |
| Proxy for "real site" | `positions_source_domain` (1 unit) instead of `traffic_domain` (10) |
| Derive locally | `domain_from` / `root_domain_from` from `url_from` (public suffix list); nofollow = `NOT is_dofollow`; lost = `last_seen IS NOT NULL` |
| Filter/sort for free | Put conditions in `where` / `order_by`, not `select` |
| Always pass `select` | Defaults can be expensive (e.g. `metrics-history` default ≈ 41 units/row) |

---

## 6. Storage

SQLite/Postgres index + raw JSON at
`/data/raw/ahrefs/{domain}/{run_id}/{query_id}.json` with a headers sidecar (`x-api-rows`, `x-api-units-cost-total`, `x-api-units-cost-total-actual`, `x-api-cache`).

```sql
CREATE TABLE runs (
  run_id            TEXT PRIMARY KEY,   -- domain:fetched_at:provider
  domain            TEXT NOT NULL,
  mode              TEXT NOT NULL,      -- domain / subdomains
  tier              TEXT NOT NULL,      -- standard / finalist
  drop_date         TEXT,
  drop_date_source  TEXT,
  fetched_at        TEXT NOT NULL,
  config_json       TEXT NOT NULL,
  coverage_json     TEXT NOT NULL,
  units_actual      INTEGER,
  error             TEXT
);

CREATE TABLE requests (
  run_id TEXT, query_id TEXT, page INTEGER DEFAULT 0,
  endpoint TEXT, params_json TEXT, row_count INTEGER,
  units_actual INTEGER, raw_path TEXT, status INTEGER, error TEXT,
  PRIMARY KEY (run_id, query_id, page)
);

CREATE TABLE stats (
  run_id TEXT, snapshot TEXT,           -- live / all_time / since_drop
  links INTEGER, refdomains INTEGER,
  PRIMARY KEY (run_id, snapshot)
);

CREATE TABLE history (
  run_id TEXT, series TEXT,             -- refdomains / org_traffic
  date TEXT, value REAL,
  PRIMARY KEY (run_id, series, date)
);

CREATE TABLE backlinks (
  run_id TEXT, link_hash TEXT,
  source_queries TEXT,                  -- JSON: ["top_dr"], ["latest"], ["full"], ...
  url_from TEXT, url_to TEXT, domain_from TEXT, root_domain_from TEXT,
  anchor_raw TEXT, anchor_norm TEXT,
  is_dofollow INTEGER, domain_rating REAL,
  first_seen_link TEXT,
  era TEXT,                             -- pre_drop / post_drop / unknown
  raw_json TEXT,
  PRIMARY KEY (run_id, link_hash)
);

CREATE TABLE anchors (
  run_id TEXT, source_query TEXT,       -- top_anchors / lexicon
  anchor_raw TEXT, anchor_norm TEXT,
  links_to_target INTEGER, dofollow_links INTEGER, refdomains INTEGER,
  first_seen TEXT, last_seen TEXT,
  lexicon_terms TEXT, lexicon_categories TEXT,   -- filled for lexicon rows
  raw_json TEXT,
  PRIMARY KEY (run_id, source_query, anchor_norm)
);

CREATE TABLE refdomains (               -- finalist tier only
  run_id TEXT, domain TEXT, root_domain TEXT,
  domain_rating REAL, links_to_target INTEGER, dofollow_links INTEGER,
  positions_source_domain INTEGER, is_spam INTEGER, first_seen TEXT,
  raw_json TEXT,
  PRIMARY KEY (run_id, domain)
);
```

Normalization (unchanged from v1): lowercase scheme/host, strip default ports, keep raw values; public-suffix-list root domains; anchors HTML-unescaped, whitespace-collapsed, lowercased for matching; `null` stays `null`.

```text
link_hash = sha256(provider \n norm_url_from \n norm_url_to \n norm_anchor \n first_seen_link)
```

---

## 7. Coverage object

Standard tier:

```json
{
  "mode": "top_n",
  "stats": {"live": 1159, "all_time": 5979, "since_drop": 3410,
            "live_refdomains": 992, "all_time_refdomains": 1599},
  "rows": {"top_dr": 70, "latest": 30, "unique": 96},
  "top_anchors": 50,
  "lexicon_complete": true,
  "history_months": 48,
  "drop_date_source": "registrar_list"
}
```

Finalist tier adds:

```json
{
  "mode": "finalist_full",
  "live_rows_exported": 1159,
  "live_gap_vs_stats": 0,
  "refdomains_exported": 992,
  "complete": true
}
```

The agent must see `coverage.mode` next to every metric. Row-level metrics from a `top_n` run describe the slice, not the profile.

If `drop_date` is unknown, `stats.since_drop` must be `null` — never substitute the live count. `backlinks-stats` cannot produce a since-drop count at all (see §3.1).

---

## 8. What the agent gets (standard tier)

| Signal | Source | Whole profile? |
|---|---|---|
| Live / all-time / post-drop size | stats | ✅ |
| Post-drop refdomain spike | refdomains-history | ✅ |
| Traffic history, cliff, pre-drop baseline | metrics-history | ✅ |
| Toxic anchors (gambling/pharma/adult/link-selling) | lexicon search | ✅ (lexicon-limited) |
| Strongest 70 referring domains + their links | top DR rows | slice |
| Current spam activity | latest 30 rows | slice |
| Anchor mix | top 50 anchors | slice (top by volume) |
| Topical fit | top DR rows (URLs, anchors) | slice |

---

## 9. Budget

- Standard tier: **measured 3,400 units/domain** as written (range 2,744–4,101 across 5 domains, 2026-11-24), or **~3,350 after collapsing the stats calls to one**. The earlier ~2,000 estimate was low because the lexicon search costs 1.6–2.5k, not 50–500. A *complete* lexicon adds ~7.6–7.9k on profiles whose matches exceed the 250-row cap.
- Finalist tier ≈ **7–30k units/domain** depending on profile size (smoke-fire ≈ 7k live + ~7k refdomains + small extras).
- Suggested allocation of 800k/month:

  | Bucket | Units | Capacity |
  |---|---:|---|
  | Standard screening | 640k (80%) | ~190 domains (measured), ~320 only with an incomplete lexicon |
  | Finalist exports | 160k (20%) | ~8–15 finalists |

- Controls: check `limits-and-usage` before each run; stop the batch at a monthly reserve threshold; cache raw payloads by `(endpoint, params hash, date)`; never re-fetch a domain on the same day unless forced; alert if actual cost deviates > 20% from estimate.

---

## 10. Out of scope (companion collectors, free or non-Ahrefs)

- **Wayback CDX** — capture timeline, content `digest` changes, pre/post-drop language/topic. Free.
- **Common Crawl index** — second archive. Free.
- **WHOIS / DNS history** — `drop_date`, ownership continuity, MX records.
- **Live fetch** — plain HTTP; headless browser only for JS cloaking.

These are the main "was it real / what happened after the drop" signals that cost zero Ahrefs units.

---

## 11. Open questions

1. **Domains per month.** Allocation above assumes ~300 standard + ~10 finalists. Adjust once known.
2. **Untested API behavior:** `order_by` with `aggregation=1_per_domain`; `history` on `backlinks-stats`; large `or` filters on `anchors`. Verify on one domain before rollout.
3. **Dofollow-only top 70?** Filtering `is_dofollow = true` is free and focuses on link equity, but drops high-DR nofollow links (e.g. Wikipedia) that still signal a real site. Default: unfiltered.
4. **Outbound links in standard tier?** `linked-anchors-external` (`limit=30`, ~2 fields) would add ~100 units and is a strong reuse/PBN signal. Currently finalist-only.
5. **History window.** 48 months monthly. Shorter = cheaper; longer = better pre-drop baseline for older drops.
6. **`mode`.** `domain` vs `subdomains` for expired domains.
7. **Drop date source.** Provided by the upstream expired-domain list, or inferred?
