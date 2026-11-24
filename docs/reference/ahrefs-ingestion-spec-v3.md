# Ahrefs Ingestion Spec — Expired Domain Evaluation (v3.1, lean + pre-filter)

**Purpose:** For each expired domain, capture a small, deliberate slice of Ahrefs data that lets a downstream AI agent screen it, within a **monthly cap of 800k API units**. A cheap pre-filter funnel decides which candidates reach that step. A full backlink export happens only for finalists.

**Primary provider:** Ahrefs API v3.
**Target example:** `smoke-fire.com`.

---

## 0. What changed

| Version | Approach | Units / domain |
|---|---|---:|
| v1 | Full keyset export of every backlink | ~30k+ |
| v2 / v2.1 | Full export ≤ 5k rows; complete aggregates + 5k capped rows above | ~56k (smoke-fire, est.) |
| v3 | Top-N slice (100 backlink rows) + cheap whole-profile aggregates; full export for finalists only | **~3,350 (measured)** |
| **v3.1** | **v3 + pre-filter funnel (§3): free checks and a ~12-unit bulk check decide who gets the v3 run** | **~12 per screened candidate; ~3,350 per standard run** |

Decisions behind v3:

- **Live matters; "latest" does not mean "valuable."** For expired domains, the newest links are mostly post-drop spam. Value sits in older links. So the 100 rows are split: **70 strongest by DR + 30 newest**.
- **One row per referring domain** (`aggregation=1_per_domain`) so sitewide links can't consume the slice.
- **Toxic anchors are still checked across the whole profile** via one batched server-side anchor search, not just within the 100 rows.
- **Pay only for selected fields.** Ahrefs bills rows × sum of selected field costs, minimum 50 units per request. Fields used only in `where` / `order_by` are not billed. Avoid 10-unit fields (`traffic_domain`, `org_traffic`, etc.) and 5-unit fields except where rows are few.

Decisions behind v3.1:

- **Filter before spending.** The standard run is the expensive step (~3,350 units). Every candidate first passes a funnel ordered cheapest-first: name → free DR → live check → `batch-analysis` → Wayback.
- **Reject only on positive evidence.** A dead page, no archive captures, or a parking page never rejects a domain on its own. Value-floor rejects (too few referring domains, DR too low) are allowed but labeled separately from toxicity rejects.
- **Every reject has a reason code and evidence; rejects are spot-checked monthly.**

---

## 1. Principles

1. **Save first, compute later.** Store every raw payload, request params, and the cost headers. Ingestion makes no quality judgments beyond the explicit pre-filter rules in §3.
2. **Deliberate slice, honest label.** Standard runs are `coverage.mode = "top_n"`. Never call them complete for row-level questions.
3. **Whole-profile answers come from aggregates.** Size, timing, history, and toxic-anchor presence are answered over the entire profile even though rows are capped.
4. **Escalate, don't expand.** Need more depth? Promote the domain to finalist tier; don't grow the standard slice.
5. **Cheapest check first.** Free and local checks run before anything that costs units; slow free checks (Wayback) run on the smallest list.

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

If `drop_date` is unknown, set `drop_date_source = "unknown"`; the agent can infer the drop from the history series.

Pre-filter config (thresholds are placeholders to tune from the first month's spot-checks):

```json
{
  "prefilter": {
    "name_lexicon": "lexicon/v1.json",
    "tld_blocklist": [],
    "dr_floor": 10,
    "refdomains_floor": 20,
    "linked_domains_cap": 500,
    "subnet_ratio_flag": 0.5,
    "content_min_term_hits": 2,
    "reject_pre_drop_toxic": false,
    "wayback_max_snapshot_fetches": 3,
    "wayback_rate_per_sec": 1,
    "standard_run_monthly_cap": 180
  }
}
```

---

## 3. Pre-filter funnel (every candidate)

| Stage | Check | Cost | Reject when |
|---|---|---|---|
| 1 | Domain name | Free, local | Name matches the lexicon; TLD on blocklist |
| 2 | Free DR (`public-domain-rating-free`) | 0 units | DR < `dr_floor` |
| 3 | Live check (DNS + HTTP) | Free | Redirects to or serves toxic content |
| 4 | `batch-analysis` | ~12 units/domain | Refdomains below floor; link-farm outbound counts |
| 5 | Wayback CDX + up to 3 snapshot fetches | Free, rate-limited | Toxic or repurposed content after the drop |
| → | Standard tier (§4) | ~3,350 units | — |

Stages 1–2 run in bulk on the raw list. Stage 3 runs in parallel. Stage 4 runs in batches of 100. Stage 5 runs last because Wayback throttles aggressive clients.

**Reason codes.** Every stage writes `pass`, `reject`, or `flag` plus a reason code and evidence (§3.7). Reason codes are prefixed `value_` (below a value floor) or `toxic_` (positive evidence of a problem) so the two kinds of reject can be tuned and audited separately.

**Run cap.** If more candidates pass stage 5 than `standard_run_monthly_cap` allows, rank survivors by stage-4 `refdomains` then `domain_rating` and run the top ones. Carry the rest to next month; don't re-screen them unless the data is more than 30 days old.

### 3.1 Stage 1 — Domain name (local)

- Decode IDN/punycode, split the label on digits and hyphens, and match against the anchor lexicon (§4.6) plus name-specific patterns (e.g. `slot\d+`, `judi`, `togel`, `casino`, `bet\d+`).
- Reject on any match: `toxic_name`.
- Reject if TLD is in `tld_blocklist`: `value_tld`.
- Dedupe the list (case, trailing dots, `www.`).

### 3.2 Stage 2 — Free DR

```bash
curl -s -X POST "https://api.ahrefs.com/v3/public/domain-rating-free" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"targets": ["smoke-fire.com", "example.net", "..."]}'
```

- Up to **1,000 targets per call**, documented as **free** (`unitCost.kind = "free"`). Verify the path and method against the OpenAPI schema; the response includes a `license` URL. **Check those license terms before automated use.**
- Reject if `domain_rating < dr_floor`: `value_dr`.
- Store the DR; stage 4 refreshes it.

### 3.3 Stage 3 — Live check

Plain HTTP, not a headless browser.

1. DNS: resolve A/AAAA (and record MX).
2. HTTP: `GET http://<domain>/` and `https://<domain>/`, follow up to 10 redirects, 10 s timeout, keep the first ~50 KB of body.
3. Second pass with a mobile user agent and `Referer: https://www.google.com/` to catch simple conditional redirects.
4. Record: DNS result, status codes, full redirect chain, final host, `<title>`, visible text (tags stripped).

Classify the result:

| Class | Meaning | Decision |
|---|---|---|
| `nxdomain` / `no_http` | Doesn't resolve or no HTTP response | Neutral (normal for dropped domains) |
| `parked` | Registrar/marketplace parking or for-sale page (signatures: Sedo, Afternic, Dan, GoDaddy "may be for sale", Bodis, ParkingCrew, HugeDomains, …) | Neutral |
| `redirect_offsite` | Final host ≠ candidate domain | Check final host and its content with the lexicon |
| `content` | Serves its own page | Check content with the lexicon |

- Reject `toxic_live` if: the final host matches the lexicon, **or** the title matches, **or** visible text has ≥ `content_min_term_hits` distinct toxic terms. The threshold avoids rejecting a page that mentions "poker" once.
- Most useful for auction / pending-delete domains, where the current holder still controls DNS. Truly dropped domains will mostly be `nxdomain` or `parked`.

### 3.4 Stage 4 — `batch-analysis` (bulk Ahrefs metrics)

```bash
curl -s -X POST "https://api.ahrefs.com/v3/batch-analysis/batch-analysis" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "select": ["url","domain_rating","backlinks","refdomains","org_keywords",
               "outgoing_links","linked_domains","refips_subnets"],
    "targets": [
      {"url": "smoke-fire.com", "mode": "domain", "protocol": "both"},
      "... up to 100 targets ..."
    ]
  }'
```

- Up to **100 targets per request**; cost per row = 1+1+1+**5**+1+1+1+1 = **12 units/domain** (min 50/request). Verify the path against the OpenAPI schema.
- Do **not** add `org_traffic` / `org_cost` (10 units each) or `refdomains_dofollow` / `refdomains_nofollow` (5 each).

Rules:

| Condition | Decision | Code |
|---|---|---|
| `refdomains < refdomains_floor` | Reject | `value_refdomains` |
| `linked_domains > linked_domains_cap` | Reject | `toxic_link_farm` |
| `refips_subnets / refdomains < subnet_ratio_flag` | Flag (many referring domains on few networks: PBN signal) | `flag_subnet_cluster` |
| `org_keywords = 0` | Info only (normal for dropped domains) | — |

`linked_domains` / `outgoing_links` reflect the last crawl of the domain itself; a reused PBN or casino site links out heavily.

### 3.5 Stage 5 — Wayback

**One CDX call per domain** (distinct content versions only):

```text
https://web.archive.org/cdx/search/cdx
  ?url=<domain>
  &output=json
  &fl=timestamp,statuscode,mimetype,digest,original
  &collapse=digest
  &filter=mimetype:text/html
```

Derive: `first_capture`, `last_capture`, `years_active`, `distinct_versions`, `versions_pre_drop`, `versions_post_drop`, and redirect captures (3xx).

**Fetch the text of up to `wayback_max_snapshot_fetches` (3) captures** using the raw form (no Wayback toolbar):

```text
https://web.archive.org/web/<timestamp>id_/<original>
```

- 1 × the last `200` capture before `drop_date − 12 months` (the "real site" baseline).
- 2 × the latest `200` captures after `drop_date`. If `drop_date` is unknown, the latest two.

Run the lexicon, the parking signatures, and language detection on each.

| Condition | Decision | Code |
|---|---|---|
| Post-drop capture has ≥ `content_min_term_hits` toxic terms | Reject | `toxic_wayback_post_drop` |
| Post-drop language ≠ pre-drop language, and post-drop isn't parked | Reject | `toxic_repurposed` |
| Pre-drop capture is toxic (the site itself was a casino/pharma/adult site) | Flag, or reject if `reject_pre_drop_toxic` | `flag_pre_drop_toxic` |
| Topic change without language change | Flag for the agent | `flag_topic_change` |
| No captures at all **and** stage-4 `refdomains < 2 × refdomains_floor` | Reject | `value_no_history` |
| No captures, refdomains above that | Flag | `flag_no_archive` |

Politeness: ≤ `wayback_rate_per_sec` requests/second, back off on 429/503, cache CDX and snapshot responses by domain. Never re-fetch on the same day.

### 3.6 What the pre-filter passes downstream

Every survivor carries its pre-filter evidence into the standard run, so the agent gets these signals at zero extra Ahrefs cost:

- Live check: class, redirect chain, final host, title.
- `batch-analysis` row: DR, backlinks, refdomains, keywords, outbound counts, subnet ratio.
- Wayback summary: years active, distinct versions, pre/post-drop language, flags, snapshot text excerpts.

### 3.7 Pre-filter storage and audit

```sql
CREATE TABLE prefilter (
  domain        TEXT NOT NULL,
  batch_id      TEXT NOT NULL,          -- monthly or per-list batch
  stage         INTEGER NOT NULL,       -- 1..5
  decision      TEXT NOT NULL,          -- pass / reject / flag
  reason_code   TEXT,                   -- value_* / toxic_* / flag_*
  evidence_json TEXT,                   -- matched terms, URLs, counts, snapshot timestamps
  checked_at    TEXT NOT NULL,
  units_actual  INTEGER DEFAULT 0,
  PRIMARY KEY (domain, batch_id, stage)
);
```

Raw responses (HTTP bodies, CDX JSON, snapshot text, `batch-analysis` payloads) go under `/data/raw/prefilter/{batch_id}/{domain}/`.

**Monthly spot-check:** sample 20 rejects per reason code (or 5%, whichever is larger) for human review. Track false-reject rate per code; loosen any rule above ~10%. Without this, a filter can quietly drop good domains.

---

## 4. Standard tier (pre-filter survivors)

| # | Query | Answers | Units (measured where noted) |
|---|---|---|---:|
| 0 | `subscription-info/limits-and-usage` | Remaining budget | 0 |
| 1 | `backlinks-stats` × 1 | Live + all-time links and referring domains (all four counts in one row) | 50 |
| 2 | `refdomains-history`, monthly, last 48 months | Post-drop spike, growth pattern | ~290 |
| 3 | `metrics-history`, `select=date,org_traffic`, last 48 months | Was it a real site; traffic cliff | ~530 |
| 4 | Top 70 live backlinks by DR, 1 per domain | Value core | ~420 |
| 5 | Latest 30 live backlinks, 1 per domain | Current spam activity | ~180 |
| 6 | One batched lexicon search on `anchors`, all_time | Toxic anchors anywhere in profile | **1,600–2,500 (measured)** |
| 7 | Top 50 live anchors by link count | Anchor mix (brand / URL / exact-match / spam) | ~200 |
| | **Total** | | **~3,350 (measured, range 2,744–4,101)** |

Costs are from each endpoint's `unitCost` in the v3 OpenAPI docs, corrected by measured `x-api-units-cost-total-actual` on 5 domains (2026-11-24).

### 4.1 Backlink counts

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

### 4.2 Referring-domain history

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/refdomains-history" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "date_from=2022-11-01" \
  --data-urlencode "history_grouping=monthly"
```

6 units/row (`date` 1 + `refdomains` 5) × ~48 rows.

### 4.3 Organic traffic history

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

### 4.4 Top 70 live backlinks by DR

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

### 4.5 Latest 30 live backlinks

Same as 4.4 with:

```text
limit=30
order_by=first_seen_link:desc
```

Overlap with 4.4 is possible; dedupe locally and record which query returned each row.

### 4.6 Batched lexicon search (whole profile)

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
- 10 units/row (`refdomains` is 5 of those). **Measured 1,600–2,500 units per domain** — matches are not "few rows" on spammed expired domains; many return 160–250 rows. See open question 8 for a cheaper select.
- `all_time` so lost spam (past campaigns, possible penalties) is visible too. `last_seen IS NULL` = still live.
- If it returns 250 rows, the result is truncated. Splitting until complete costs ~7.6–7.9k extra on those profiles (measured), so by default **don't split in the standard tier**: record `lexicon_complete = false` and the 250 matched rows. A 250-row hit is already decisive evidence of heavy toxic-anchor exposure. Split only at finalist tier.
- Substring matches are over-inclusive (`bet` ⊂ `better`); keep raw matches, filter downstream.

**Lexicon (starter, versioned; shared with pre-filter stages 1, 3, 5):**

| Category | Terms |
|---|---|
| Gambling | casino, slot, gacor, judi, togel, poker, betting, sportsbook, baccarat, roulette, 카지노, 바카라, 토토, казино |
| Pharma | viagra, cialis, pharmacy, pills, tramadol, kamagra, cbd |
| Adult | porn, xxx, sex, escort, nude, cam girls |
| Link selling / SEO | pbn, backlink, guest post, seo, link building, buy links |
| Evasions | c4sino, v1agra, p0rn (extend as discovered) |

The link-selling/SEO category applies to anchors only. Don't use it to reject on domain names or page content (an SEO agency's site would trip it).

### 4.7 Top 50 live anchors

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

## 5. Finalist tier (escalation only)

Run when a domain is about to be bought, or when the agent flags a contradiction it can't resolve from the standard slice (e.g. a large post-drop refdomain spike with no visible spam in the slice or lexicon).

### 5.1 What runs

| Query | Notes |
|---|---|
| Full live backlink export (keyset, §5.2) | 6 units/row; smoke-fire ≈ 1,159 rows ≈ 7k units |
| Full live refdomains | `select=domain,domain_rating,links_to_target,dofollow_links,positions_source_domain,is_spam,first_seen` (7 units/row), keyset on `domain:asc` |
| Outbound links (`linked-anchors-external`, `limit=100`) | Reused PBN/casino sites link out to money sites |
| Complete lexicon (split §4.6 until no request hits 250) | ~7.6–7.9k extra on heavily spammed profiles (measured) |
| Optional: full all_time export | Only if pre-drop link history is needed; can be large |

Budget guard: if `backlinks-stats.live > 20,000`, export DR-filtered rows only (`where domain_rating_source >= 20`, filter not billed) and mark `coverage.mode = "finalist_filtered"`.

### 5.2 Keyset pagination (verified)

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

## 6. Field selection rules

| Rule | Detail |
|---|---|
| Name the metric | Every selected column must feed a named metric or the cursor |
| Avoid 10-unit fields | `traffic_domain`, `traffic`, `traffic_page`, `org_traffic`, `org_cost`, … — only where rows are few (e.g. `metrics-history`) |
| Avoid 5-unit fields on big tables | `refdomains`, `refdomains_source*`, `dofollow_refdomains`, `class_c`, `live_refdomains`, `refdomains_dofollow` |
| Proxy for "real site" | `positions_source_domain` (1 unit) instead of `traffic_domain` (10) |
| Derive locally | `domain_from` / `root_domain_from` from `url_from` (public suffix list); nofollow = `NOT is_dofollow`; lost = `last_seen IS NOT NULL` |
| Filter/sort for free | Put conditions in `where` / `order_by`, not `select` |
| Always pass `select` | Defaults can be expensive (e.g. `metrics-history` default ≈ 41 units/row) |
| Bulk before per-domain | `public-domain-rating-free` (free, 1,000/call) and `batch-analysis` (100/call) before any per-domain endpoint |

---

## 7. Storage

SQLite/Postgres index + raw JSON at
`/data/raw/ahrefs/{domain}/{run_id}/{query_id}.json` with a headers sidecar (`x-api-rows`, `x-api-units-cost-total`, `x-api-units-cost-total-actual`, `x-api-cache`). Pre-filter storage is in §3.7.

```sql
CREATE TABLE runs (
  run_id            TEXT PRIMARY KEY,   -- domain:fetched_at:provider
  domain            TEXT NOT NULL,
  mode              TEXT NOT NULL,      -- domain / subdomains
  tier              TEXT NOT NULL,      -- standard / finalist
  prefilter_batch   TEXT,               -- links to prefilter rows
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
  run_id TEXT, snapshot TEXT,           -- live / all_time (backlinks-stats has no since: count)
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

## 8. Coverage object

Standard tier:

```json
{
  "mode": "top_n",
  "stats": {"live": 1159, "all_time": 5979,
            "live_refdomains": 992, "all_time_refdomains": 1599},
  "rows": {"top_dr": 70, "latest": 30, "unique": 96},
  "top_anchors": 50,
  "lexicon_complete": true,
  "history_months": 48,
  "drop_date_source": "registrar_list",
  "prefilter": {"batch_id": "2026-11", "flags": ["flag_subnet_cluster"]}
}
```

Finalist tier adds:

```json
{
  "mode": "finalist_full",
  "live_rows_exported": 1159,
  "live_gap_vs_stats": 0,
  "refdomains_exported": 992,
  "lexicon_complete": true,
  "complete": true
}
```

The agent must see `coverage.mode` next to every metric. Row-level metrics from a `top_n` run describe the slice, not the profile.

There is no since-drop link count: `backlinks-stats` cannot produce one (see §4.1). Post-drop movement comes from `refdomains-history` and, at finalist tier, from `first_seen_link` on exported rows. Never substitute the live count.

---

## 9. What the agent gets (standard tier)

| Signal | Source | Whole profile? |
|---|---|---|
| Live / all-time size | stats | ✅ |
| Post-drop refdomain spike | refdomains-history | ✅ |
| Traffic history, cliff, pre-drop baseline | metrics-history | ✅ |
| Toxic anchors (gambling/pharma/adult/link-selling) | lexicon search | ✅ (lexicon-limited; truncated at 250 rows unless finalist) |
| Strongest 70 referring domains + their links | top DR rows | slice |
| Current spam activity | latest 30 rows | slice |
| Anchor mix | top 50 anchors | slice (top by volume) |
| Topical fit | top DR rows (URLs, anchors) | slice |
| Current live state (parked / redirect / content) | pre-filter stage 3 | ✅ |
| Outbound link volume, subnet clustering | pre-filter stage 4 | ✅ |
| Archive history, pre/post-drop language and content | pre-filter stage 5 | sampled (≤ 3 snapshots) |

---

## 10. Budget

- **Pre-filter:** stages 1–3 and 5 cost 0 units. Stage 4 costs **~12 units per candidate** (~1,200 per 100). Measure on the first batch.
- **Standard tier:** **measured 3,400 units/domain** (range 2,744–4,101 across 5 domains, 2026-11-24), **~3,350 with a single stats call**. The lexicon search is 1.6–2.5k of that. A *complete* lexicon adds ~7.6–7.9k on profiles whose matches exceed the 250-row cap — finalist tier only.
- **Finalist tier:** ≈ **15–40k units/domain** depending on profile size (smoke-fire ≈ 7k live + ~7k refdomains + up to ~8k complete lexicon + small extras).
- Suggested allocation of 800k/month:

  | Bucket | Units | Capacity |
  |---|---:|---|
  | Pre-filter stage 4 | 40k (5%) | ~3,300 candidates reaching stage 4 |
  | Standard screening | 600k (75%) | **~180 domains** |
  | Finalist exports | 160k (20%) | ~4–10 finalists |

  Tune the stage 1–3 thresholds so that roughly `standard_run_monthly_cap` domains survive stage 5. If far more survive, rank and cap (§3); if far fewer, loosen value floors first, never toxicity rules.

- Controls: check `limits-and-usage` before each run; stop the batch at a monthly reserve threshold; cache raw payloads by `(endpoint, params hash, date)`; never re-fetch a domain on the same day unless forced; alert if actual cost deviates > 20% from estimate.

---

## 11. Out of scope (companion collectors)

- **Common Crawl index** — second archive, for domains with thin Wayback coverage. Free.
- **WHOIS / DNS history** — `drop_date`, ownership continuity, historical MX records.
- **Headless browser fetch** — only for JS cloaking not caught by stage 3's second pass.

(Wayback and the live fetch moved into the pre-filter, §3.3 and §3.5.)

---

## 12. Open questions

1. **Raw candidates per month.** Needed to size stage 4's budget and to tune thresholds toward ~180 standard runs/month.
2. **Untested API behavior:** `order_by` with `aggregation=1_per_domain`; large `or` filters on `anchors`; the exact paths for `public-domain-rating-free` and `batch-analysis`. Verify on one domain or batch before rollout. (`backlinks-stats` ignoring `history` is verified, §4.1.)
3. **Free DR license.** Confirm the `license` terms returned by `public-domain-rating-free` allow this automated use.
4. **Pre-filter thresholds.** `dr_floor = 10`, `refdomains_floor = 20`, `linked_domains_cap = 500`, `subnet_ratio_flag = 0.5` are placeholders. Set from the first month's spot-checks.
5. **Pre-drop toxic policy.** Should a domain that was itself a casino/pharma/adult site before dropping be rejected (`reject_pre_drop_toxic = true`) or only flagged? Default: flag.
6. **Dofollow-only top 70?** Filtering `is_dofollow = true` is free and focuses on link equity, but drops high-DR nofollow links (e.g. Wikipedia) that still signal a real site. Default: unfiltered.
7. **Outbound links in standard tier?** Stage 4 now gives outbound counts for every candidate. `linked-anchors-external` (`limit=30`, ~2 fields, ~100 units) would add the actual outbound anchors. Currently finalist-only.
8. **Cheaper lexicon select.** Dropping `refdomains` (5 units) from §4.6 halves its cost (≈ 1.6–2.5k → ≈ 0.8–1.25k per domain, ~25–30% of the standard run). Cost: you lose per-anchor referring-domain counts (one sitewide link vs many domains). Not applied yet.
9. **History window.** 48 months monthly. Shorter = cheaper; longer = better pre-drop baseline for older drops.
10. **`mode`.** `domain` vs `subdomains` for expired domains (applies to stage 4 targets too).
11. **Drop date source.** Provided by the upstream expired-domain list, or inferred? Stage 5 works without it, but less precisely.
