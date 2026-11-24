# Ahrefs Ingestion Spec — Save Everything First

**Purpose:** Given a domain, capture a complete, reproducible backlink snapshot from Ahrefs before computing any evaluation metrics. The ingestion layer must not make quality judgments. It should save raw payloads, normalized records, and fetch metadata so every later metric can be recomputed without another API call.

**Primary provider:** Ahrefs API v3.  
**Optional secondary provider:** DataForSEO Backlinks API for reconciliation.  
**Target example:** `smoke-fire.com`.

---

## 1. Can Ahrefs return more than 250 rows?

**Yes — but not with `offset` or a larger `limit`.**

What we verified against the live Ahrefs API:

| Attempt | Result |
|---|---|
| `limit=250` | 250 rows |
| `limit=500` | **250 rows** (silently capped) |
| `limit=1000` | **250 rows** (silently capped) |
| `offset=250` | ignored / same first page |
| `page=2` | unsupported |

The `all-backlinks` endpoint has no offset/page parameter in the OpenAPI schema. The hard row cap is 250 per request on this plan.

Tested explicitly **without any `where` filter**:

| Call | Rows returned |
|---|---:|
| `limit=250`, `history=live`, no `where` | 250 |
| `limit=1000`, `history=live`, no `where` | **250** |
| `limit=250`, `history=live`, `offset=250`, no `where` | 250, same first row as page 1 |
| `limit=250`, no `history`, no `where` | 250, but different rows (`all_time` default) |

So a plain call always gives you the first 250 rows only. The `where` cursor is what makes pagination possible.

**The workaround is keyset pagination:**

1. Sort by a monotonic field: `order_by=first_seen_link:asc,url_from:asc`.
2. Request 250 rows.
3. Take the last row’s `first_seen_link` (and `url_from` as a tiebreaker).
4. Request the next 250 rows with a `where` filter that excludes everything at or before that cursor.
5. Repeat until fewer than 250 rows are returned.

**Tested result on `smoke-fire.com`:** 5 pages, **1,160 individual live links**, matching the live anchor census. So yes, you can save the full backlink list; you just have to paginate with `where`, not `offset`.

This makes `all-backlinks` the **authoritative** source for anchor and rel metrics. The `anchors` endpoint is useful as a cross-check, but the saved backlink rows are what you should aggregate locally.

Two important defaults to override:

| Parameter | Default | Use for full snapshot |
|---|---|---|
| `history` | `all_time` | Set `live` and/or `all_time` explicitly |
| `aggregation` | `similar_links` | Set `all` to keep every link row |
| `mode` | `subdomains` | Set `domain` or `subdomains` intentionally |

`aggregation=all` is required if you want the actual link rows rather than grouped/similar links.

### Live vs all-time: do not assume

The API does **not** return only live links by default. The `all-backlinks` endpoint defaults to `history=all_time`. If you omit the parameter, you will get live + lost/historical links.

Concrete example for `smoke-fire.com`:

```json
{
  "live": 1159,
  "all_time": 5979
}
```

Our full keyset test explicitly used `history=live`, so the 1,160 rows were live rows. They were not live by default. Always:

1. Set `history=live` (or `all_time`) explicitly.
2. Save the snapshot label with the run.
3. Check `is_lost` on returned rows.
4. Reconcile the row count against `backlinks-stats.metrics.live` or `.all_time`.

“Live” here means “present in Ahrefs’ current index,” not “HTTP-verified today.” Treat it as a provider snapshot with a crawl timestamp.

---

## 2. Snapshot model

Do not save one undifferentiated “backlinks” blob. Save named snapshots:

| Snapshot | `history` value | Meaning |
|---|---|---|
| `live` | `live` | Links currently present |
| `all_time` | `all_time` | Live + lost/historical links |
| `since_YYYY_MM_DD` | `since:YYYY-MM-DD` | Links added/lost in a window |

For each run, store:

```json
{
  "run_id": "smoke-fire.com:2026-11-24T15:30:00Z:ahrefs",
  "domain": "smoke-fire.com",
  "provider": "ahrefs",
  "snapshot": "live",
  "mode": "domain",
  "aggregation": "all",
  "fetched_at": "2026-11-24T15:30:00Z",
  "api_version": "v3",
  "endpoint": "/site-explorer/all-backlinks",
  "params": {},
  "row_count": 1160,
  "expected_count": 1159,
  "complete": true,
  "cost_units": 4000,
  "notes": []
}
```

Always save the raw response body, the request parameters, and the response headers that matter:

```text
x-api-rows
x-api-units-cost-total
x-api-units-cost-total-actual
x-api-cache
```

---

## 3. Endpoints to ingest

For each domain, fetch these in order. Use the same `run_id` and snapshot label.

### 3.1 Subscription / limits (free)

```bash
curl -s -G "https://api.ahrefs.com/v3/subscription-info/limits-and-usage" \
  -H "Authorization: Bearer $AHREFS_KEY"
```

Use this to record remaining units before the run.

### 3.2 Backlink counts

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/backlinks-stats" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "date=2026-11-24"
```

Response:

```json
{
  "metrics": {
    "live": 1159,
    "all_time": 5979,
    "live_refdomains": 992,
    "all_time_refdomains": 1599
  }
}
```

This is the **completeness check** for paginated backlink rows.

### 3.3 Domain metrics

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/metrics" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "date=2026-11-24" \
  --data-urlencode "select=url,domain_rating,ahrefs_rank,backlinks,backlinks_spam_score,referring_domains,organic_keywords,organic_traffic,organic_value"
```

Store whatever fields are available. If a field is not valid for the endpoint, drop it and log the error; do not silently continue.

### 3.4 All backlinks — page 1

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/all-backlinks" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "limit=250" \
  --data-urlencode "history=live" \
  --data-urlencode "aggregation=all" \
  --data-urlencode "order_by=first_seen_link:asc,url_from:asc" \
  --data-urlencode "select=url_from,url_to,anchor,link_type,is_dofollow,is_nofollow,is_sponsored,is_ugc,domain_rating_source,traffic_domain,first_seen,first_seen_link,last_seen,is_lost,is_new"
```

> `is_spam` belongs to the `anchors` and `refdomains` endpoints, not `all-backlinks`. If a selected column is invalid on your plan, remove it and keep the core fields. Use the response error to discover valid columns.

### 3.5 All backlinks — page 2+ (keyset)

Take the last row from page 1:

```json
{
  "first_seen_link": "2026-09-28T16:35:57Z",
  "url_from": "https://example.com/page"
}
```

Next request:

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/all-backlinks" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "limit=250" \
  --data-urlencode "history=live" \
  --data-urlencode "aggregation=all" \
  --data-urlencode "order_by=first_seen_link:asc,url_from:asc" \
  --data-urlencode "select=url_from,url_to,anchor,link_type,is_dofollow,is_nofollow,is_sponsored,is_ugc,domain_rating_source,traffic_domain,first_seen,first_seen_link,last_seen,is_lost,is_new" \
  --data-urlencode 'where={"field":"first_seen_link","is":["gt","2026-09-28T16:35:57Z"]}'
```

Repeat until the response has fewer than 250 rows.

**Production tiebreaker:** timestamps can collide. Add `url_from` to the cursor so a page boundary inside the same timestamp cannot skip rows:

```json
{
  "or": [
    {
      "field": "first_seen_link",
      "is": ["gt", "2026-09-28T16:35:57Z"]
    },
    {
      "and": [
        {
          "field": "first_seen_link",
          "is": ["eq", "2026-09-28T16:35:57Z"]
        },
        {
          "field": "url_from",
          "is": ["gt", "https://example.com/page"]
        }
      ]
    }
  ]
}
```

If `url_from` can also collide (same page, multiple `url_to` targets), extend the cursor to `url_to` or `anchor`. The safe general rule is: **the cursor must be a tuple that is unique per link row**.

Cursor caveat: links with a `null` `first_seen_link` will not be reached by a `first_seen_link` cursor. If nulls are possible, either:
- query them separately with an `is_null` filter if the API supports it, or
- use `first_seen` as a fallback partition, or
- mark the run `complete = false` and report the unclassified count.

Do not assume the cursor method covered rows that have no timestamp.

### 3.6 Referring domains

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/refdomains" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "limit=250" \
  --data-urlencode "history=live" \
  --data-urlencode "order_by=domain:asc" \
  --data-urlencode "select=domain,links_to_target,dofollow_links,domain_rating,first_seen,last_seen,traffic_domain,is_spam"
```

Paginate with:

```json
{"field": "domain", "is": ["gt", "<last-domain>"]}
```

This gives one row per referring domain, including link counts and dofollow counts.

### 3.7 Anchors — full live anchor census

This endpoint is a useful **cross-check** because it includes both total links and dofollow links per anchor. It is not the authoritative source: the `anchors` endpoint also has the 250-row cap, and its string cursor is less reliable than the `first_seen_link` cursor on `all-backlinks`. For domains with more than 250 anchor groups, compute anchor aggregates from the saved backlink rows instead.

For `smoke-fire.com`, the live anchor census is small enough to fit in one page.

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/anchors" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "limit=250" \
  --data-urlencode "history=live" \
  --data-urlencode "order_by=anchor:asc" \
  --data-urlencode "select=anchor,refdomains,refpages,links_to_target,dofollow_links,first_seen,last_seen,is_spam"
```

A naive anchor cursor would be:

```json
{"field": "anchor", "is": ["gt", "<last-anchor>"]}
```

**Do not rely on this for production.** In testing, the `anchor > last` filter did not behave like a clean lexicographic keyset on long/punctuated anchor strings. Use the anchors endpoint for a one-page cross-check, or compute anchors from the full backlink rows.

For `smoke-fire.com`, this returned **119 anchor groups / 1,160 links** in one page, including:

- `links_to_target`
- `dofollow_links`
- `refdomains`
- `is_spam`

For small/medium profiles, this is the cleanest way to compute the live rel ratio without paging through 250-row backlink pages.

#### 3.7.1 Targeted anchor search (the scalable method)

For large profiles, do not export every link just to find category terms. Query the aggregated anchor table directly with a substring filter:

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/anchors" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "limit=250" \
  --data-urlencode "history=live" \
  --data-urlencode "select=anchor,links_to_target,dofollow_links,refdomains,is_spam" \
  --data-urlencode 'where={"field":"anchor","is":["isubstring","gambling"]}'
```

This returns **aggregated anchors**, already weighted by `links_to_target`, so it is much smaller than the raw backlink list. Tested results:

| Term | Anchor rows | Sum of links |
|---|---:|---:|
| `gambling` | 1 | 1 |
| `pbn` | 4 | 46 |
| `backlink` | 88 | 443 |

This is the preferred strategy for anchor-category checks at scale:

1. Run one targeted query per lexicon term.
2. Sum `links_to_target` across returned anchor rows.
3. If a single term returns 250 rows (the cap), split it with additional filters (e.g., `is_spam`, `links_to_target` ranges, date ranges, or narrower substrings).
4. Store the query, matched anchors, and summed link count as evidence.

### 3.8 Historical anchor census (optional but recommended)

Repeat 3.7 with:

```text
history=all_time
```

Store as a separate snapshot. This lets you see lost anchors and distinguish “current profile” from “historical profile.”

---

## 4. Pagination loop (pseudocode)

```python
def fetch_all_backlinks(domain, snapshot="live", aggregation="all"):
    rows = []
    cursor = None
    while True:
        params = {
            "target": domain,
            "mode": "domain",
            "limit": 250,
            "history": snapshot,
            "aggregation": aggregation,
            "order_by": "first_seen_link:asc,url_from:asc",
            "select": SELECT_BACKLINKS,
        }
        if cursor:
            params["where"] = json.dumps(cursor)

        payload = ahrefs_get("/site-explorer/all-backlinks", params)
        batch = payload.get("backlinks", [])
        save_raw_payload(payload, params)

        rows.extend(batch)
        if len(batch) < 250:
            break

        last = batch[-1]
        cursor = {
            "or": [
                {"field": "first_seen_link", "is": ["gt", last["first_seen_link"]]},
                {"and": [
                    {"field": "first_seen_link", "is": ["eq", last["first_seen_link"]]},
                    {"field": "url_from", "is": ["gt", last["url_from"]]},
                ]},
            ]
        }

    return rows
```

Add a hard stop and a completeness check:

```python
assert len(rows) >= expected_live_count or flag_incomplete()
```

Do not silently accept a short result. Save `complete: false` and the reason if the count does not match `backlinks-stats`.

---

## 5. Storage schema

Use SQLite/Postgres for the index and JSONL/object storage for raw payloads.

### `runs`

```sql
CREATE TABLE runs (
  run_id              TEXT PRIMARY KEY,
  domain              TEXT NOT NULL,
  provider            TEXT NOT NULL,
  snapshot            TEXT NOT NULL,        -- live / all_time / since_...
  mode                TEXT NOT NULL,        -- domain / subdomains
  aggregation         TEXT,
  fetched_at          TEXT NOT NULL,
  endpoint            TEXT NOT NULL,
  params_json         TEXT NOT NULL,
  row_count           INTEGER,
  expected_count      INTEGER,
  complete            INTEGER,
  api_units_cost      INTEGER,
  raw_payload_path    TEXT NOT NULL,
  error               TEXT
);
```

### `backlinks`

```sql
CREATE TABLE backlinks (
  run_id              TEXT NOT NULL,
  provider            TEXT NOT NULL,
  link_hash           TEXT NOT NULL,        -- hash of normalized tuple
  url_from            TEXT NOT NULL,
  url_to              TEXT NOT NULL,
  domain_from         TEXT NOT NULL,
  root_domain_from    TEXT,
  anchor_raw          TEXT,
  anchor_norm         TEXT,
  link_type           TEXT,
  is_dofollow         INTEGER,
  is_nofollow         INTEGER,
  is_sponsored        INTEGER,
  is_ugc              INTEGER,
  domain_rating       REAL,
  traffic_domain      INTEGER,
  first_seen          TEXT,
  first_seen_link     TEXT,
  last_seen           TEXT,
  is_lost             INTEGER,
  is_new              INTEGER,
  raw_json            TEXT,
  PRIMARY KEY (run_id, link_hash)
);
```

### `anchors`

```sql
CREATE TABLE anchors (
  run_id              TEXT NOT NULL,
  provider            TEXT NOT NULL,
  anchor_raw          TEXT,
  anchor_norm         TEXT,
  links_to_target     INTEGER,
  dofollow_links      INTEGER,
  refdomains          INTEGER,
  refpages            INTEGER,
  first_seen          TEXT,
  last_seen           TEXT,
  is_spam             INTEGER,
  raw_json            TEXT,
  PRIMARY KEY (run_id, anchor_norm)
);
```

### `refdomains`

```sql
CREATE TABLE refdomains (
  run_id              TEXT NOT NULL,
  provider            TEXT NOT NULL,
  domain              TEXT NOT NULL,
  root_domain         TEXT,
  links_to_target     INTEGER,
  dofollow_links      INTEGER,
  domain_rating       REAL,
  first_seen          TEXT,
  last_seen           TEXT,
  is_spam             INTEGER,
  raw_json            TEXT,
  PRIMARY KEY (run_id, domain)
);
```

### `raw_payloads`

Store the untouched JSON response per request:

```text
/data/raw/ahrefs/{domain}/{run_id}/{endpoint}/{page:04d}.json
```

Also store response headers in a sidecar file:

```json
{
  "x-api-rows": "250",
  "x-api-units-cost-total": "1000",
  "x-api-units-cost-total-actual": "1000",
  "x-api-cache": "miss"
}
```

---

## 6. Normalization rules

Apply these before deduplication and metric computation.

### URLs

```text
1. lowercase scheme and host
2. strip default ports :80/:443
3. strip trailing slash for comparison (keep raw)
4. strip leading "www." only for root-domain comparison, not for raw storage
5. decode HTML entities
6. keep query strings (they can identify distinct pages)
```

### Domains

```text
1. extract host from URL
2. compute registrable/root domain using a public suffix list
3. store both:
   - domain_from (host)
   - root_domain_from (registrable domain)
```

Do not use naive `split(".")[-2:]`; it breaks on `co.uk`, `com.au`, `co.jp`, etc.

### Anchors

```text
1. HTML-unescape
2. replace \r\n\t with spaces
3. collapse repeated whitespace
4. trim
5. lowercase for matching
6. keep raw anchor separately
```

### Rel flags

Normalize to booleans:

```text
is_dofollow  = not is_nofollow
is_nofollow  = rel contains "nofollow"
is_sponsored = rel contains "sponsored"
is_ugc       = rel contains "ugc"
```

If the provider gives only `is_dofollow` and `is_nofollow`, derive the rest from `attributes` if available. If a provider returns `null`, store `null`, not `false`.

### Deduplication

```text
link_hash = sha256(
  provider + "\n" +
  normalized_url_from + "\n" +
  normalized_url_to + "\n" +
  normalized_anchor + "\n" +
  first_seen_link
)
```

Dedupe within a run. Keep duplicates across runs so you can measure link churn.

---

## 7. Completeness checks

Every ingest run must answer: **did we actually save everything?**

Run these checks:

1. **Live backlink count**
   - `backlinks-stats.metrics.live` vs `count(backlinks where snapshot='live')`.
   - Treat any difference as a data-quality flag until explained.

   Tested completeness results:

   | Domain | Stats `live` | Keyset rows | Gap |
   |---|---:|---:|---:|
   | `smoke-fire.com` | 1,159 | 1,160 | +1 |
   | `coblaith.net` | 3,092 | 3,092 | 0 |
   | `pyracy.com` | 5,739 | 5,734 | **-5 (0.09%)** |

   The keyset method is not guaranteed to equal `backlinks-stats` on every domain. In the `pyracy.com` test there were no null `first_seen_link` values and no timestamp ties crossing page boundaries, so the five missing links are likely an index/counting difference (for example, a link type excluded by `all-backlinks`) rather than a cursor bug. Still, the run must be marked incomplete or “reconciled with gap” until those five are accounted for.

2. **Live referring-domain count**
   - `backlinks-stats.metrics.live_refdomains` vs `count(distinct domain_from)` / `count(distinct root_domain_from)`.
   - Record both subdomain and root-domain counts.

3. **Anchor census (cross-check)**
   - `sum(anchors.links_to_target)` vs backlink count, when the anchor snapshot is complete.
   - For `smoke-fire.com`: 1,160 vs 1,159/1,160.
   - For domains with more than 250 anchor groups, treat the locally computed anchor aggregates from `backlinks` as authoritative.

4. **Pagination exhaustion**
   - Last page must have `< 250` rows.
   - Loop must not hit the max-page guard.
   - Pagination exhaustion does **not** prove completeness. Always reconcile the total against `backlinks-stats`.

5. **Provider disagreement**
   - If DataForSEO is also ingested, compare live counts and rel totals.
   - Store a `provider_gap` flag; do not average the two providers.

6. **API errors**
   - Any 4xx/5xx, invalid `select`, or timeout must mark the run `complete = false` and be retried or surfaced.

---

## 8. Cost and rate limits

Ahrefs API units:

- Minimum 50 units per request.
- Per-row cost depends on selected fields.
- A 250-row backlinks request can cost ~1,000 units.
- A full `smoke-fire.com` live crawl (5 pages) is roughly 5,000 units plus other endpoints.

Practical controls:

```text
- Cache raw payloads by (endpoint, params hash, date).
- Do not re-fetch the same domain/snapshot within the same day unless forced.
- Log x-api-units-cost-total-actual per request.
- Set a per-domain budget and stop with partial data + complete=false.
```

DataForSEO fallback costs are much lower per row, but its coverage differs. Use it for reconciliation, not as a silent replacement.

---

## 9. Optional DataForSEO reconciliation

If you want a second source, ingest these and store under `provider = dataforseo`:

```bash
curl -s -X POST \
  -u "$DATAFORSEO_EMAIL:$DATAFORSEO_API_PASSWORD" \
  -H "Content-Type: application/json" \
  -d '[{"target":"smoke-fire.com","backlinks_status_type":"live","include_subdomains":true}]' \
  "https://api.dataforseo.com/v3/backlinks/summary/live"
```

```bash
curl -s -X POST \
  -u "$DATAFORSEO_EMAIL:$DATAFORSEO_API_PASSWORD" \
  -H "Content-Type: application/json" \
  -d '[{"target":"smoke-fire.com","limit":1000,"offset":0,"include_subdomains":true,"backlinks_status_type":"live","order_by":["rank,desc"]}]' \
  "https://api.dataforseo.com/v3/backlinks/backlinks/live"
```

DataForSEO supports `limit=1000` and `offset`, plus a `search_after_token`. It is easier to paginate than Ahrefs, but on `smoke-fire.com` it missed most of the 2026 spam campaign that Ahrefs had. Save it as a **separate provider snapshot** and compute a `provider_gap` metric.

---

## 10. What becomes computable from the saved data

Once the raw data is saved, these are deterministic:

### Link and rel metrics

```text
total_links
dofollow_links
nofollow_links
sponsored_links
ugc_links
good = dofollow AND NOT sponsored AND NOT ugc
bad  = total - good
good_share
good_bad_ratio
```

### Domain spread

```text
referring_domains (host level)
referring_root_domains
links_per_domain
links_per_root_domain
top1_share
top10_share
HHI
effective_domains = 1 / HHI
platform_share (blogspot, web.app, pages.dev, wixsite, wordpress)
```

### Anchor aggregates

Compute these from the saved `backlinks` rows, not only from the `anchors` endpoint:

```text
anchor_count
null_anchor_links
url_anchor_links
brand_anchor_links
exact_match_links
anchor_links_per_domain
anchor_concentration
anchor_dofollow_links
anchor_nofollow_links
```

### Provider reconciliation

```text
ahrefs_vs_dataforseo_link_count_gap
ahrefs_vs_dataforseo_refdomain_gap
ahrefs_vs_dataforseo_nofollow_gap
provider_disagreement_flag
```

### Later, rule-based

```text
gambling/pharma/adult anchor hits
link-selling anchor hits
name tokenization / vertical-term hits
```

The point is: **save first, compute later.** The saved snapshot should be sufficient to recompute every metric without hitting the API again.

---

## 11. Minimal implementation order

1. Build the Ahrefs client with retry, rate-limit handling, and raw-payload logging.
2. Implement `backlinks-stats` and `metrics`.
3. Implement `all-backlinks` keyset pagination with `aggregation=all` and `history=live`.
4. Add the completeness check against `backlinks-stats`.
5. Add `refdomains` and `anchors` (live + all_time).
6. Store normalized rows and raw payloads.
7. Add a `--verify` command that re-counts rows and reports gaps.
8. Only then add metric computation.

This keeps the ingestion layer honest: it either saved the full snapshot or it explicitly says it did not.

---

## 12. Large profiles: hundreds of thousands of backlinks

Full row-by-row export is mathematically possible with keyset pagination, but it is not always economically sensible.

### 12.1 Scale math

Ahrefs returns at most 250 rows per request. Costs depend on selected fields; observed cost for a 250-row backlink page with a small `select` was about **1,000 API units**. Treat that as a lower bound and measure with `x-api-units-cost-total-actual`.

| Live links | Requests | Approx. Ahrefs units | Sequential time (rough) |
|---:|---:|---:|---:|
| 1,000 | 4 | ~4k | seconds |
| 10,000 | 40 | ~40k | < 1 min |
| 100,000 | 400 | ~400k | ~7 min |
| 1,000,000 | 4,000 | ~4M | ~1+ hour |

At an 800k-unit monthly allowance, a 100k-link domain can consume about half the month for one full export. A 1M-link domain is not practical to export row-by-row on this plan.

DataForSEO is much cheaper for bulk links: 1,000 rows per request, offset/search-after supported. At roughly $0.000036 per result plus $0.024 per request, 100k links is on the order of a few dollars. Use it for bulk extraction if you need every row, but keep it as a separate provider snapshot because its coverage differs from Ahrefs.

### 12.2 Recommended tiered policy

| Live links | Strategy |
|---:|---|
| < 10,000 | Full backlink rows + full refdomains + full anchors |
| 10k–100k | Full refdomains + anchor aggregates + targeted anchor queries + stratified link sample |
| > 100k | Same as above; full link export only if explicitly required; otherwise use DataForSEO bulk or accept a sample |

For every run, store a coverage object:

```json
{
  "mode": "full",
  "total_links": 1160,
  "exported_links": 1160,
  "sample_method": null,
  "refdomains_complete": true,
  "anchors_complete": true,
  "targeted_anchor_queries_complete": true
}
```

or:

```json
{
  "mode": "sampled",
  "total_links": 250000,
  "exported_links": 5000,
  "sample_method": "stratified_by_domain_rating_and_rel_flag",
  "refdomains_complete": true,
  "anchors_complete": false,
  "targeted_anchor_queries_complete": true
}
```

Never call a sampled run “complete.”

### 12.3 What still works at scale

These checks do **not** require exporting every link:

- **Total counts:** `backlinks-stats`.
- **Domain spread:** `refdomains` gives one row per domain with `links_to_target`; much smaller than the link table.
- **Anchor category counts:** targeted `anchors?where=anchor isubstring <term>` queries give full weighted counts for each lexicon term.
- **SEO-spam counts:** targeted anchor queries for `pbn`, `backlink`, `guest post`, etc.
- **Rel ratio:** use anchor aggregates (`dofollow_links` / `links_to_target`) when the anchor table is complete. If the anchor table is too large, page it by `first_seen` date buckets or use a stratified link sample and report a confidence interval.

The only check that truly needs the full link table is a link-by-link audit or a metric that depends on rare per-link fields. For ratio and category checks, aggregated tables plus targeted queries are enough.

### 12.4 Sampling design, if you must sample

Use a deterministic sample, not a random head-of-list sample:

```text
- all links with is_sponsored = true
- all links with is_ugc = true
- all links with is_nofollow = true (up to a cap)
- all links from the top 100 referring domains
- stratified random sample across domain_rating_source bands
- all links matching anchor lexicon terms (via targeted queries)
```

Store the sample IDs and the sampling frame so the result can be reproduced.

### 12.5 Decision rule

For a mass-evaluation pipeline:

1. Always run the aggregate/targeted queries.
2. Run the full link export only when the live count is below your threshold.
3. If the full export is skipped, set `coverage.mode = "sampled"` or `"aggregate_only"`.
4. Make the final report display the coverage mode next to every metric.

That way, “hundreds of thousands of backlinks” does not break the pipeline; it just changes the evidence mode from census to sample/aggregate.
