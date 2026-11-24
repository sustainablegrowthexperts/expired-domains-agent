# Ahrefs Ingestion Spec v2 (v3-lean) — Cost & Determinism Evaluation

**Date:** 2026-11-24
**Sites:** `smoke-fire.com`, `123eft.com`, `kanuksheepskinproducts.com`, `longbeachcoffee.com`, `abipr.com`
**Provider:** Ahrefs API v3 only. No DataForSEO, no scraping, no LLM scoring.
**Inputs per spec §2:** `mode=domain`, `tier=standard`, `top_dr_rows=70`, `latest_rows=30`, `top_anchor_rows=50`, `history_months=48`, lexicon v1. **`drop_date` was not supplied for any site** → `drop_date_source=unknown` and the `since:` stats call was skipped, exactly as §2 prescribes.

---

## 1. Verdict

**The spec produces the row-level slices deterministically, but it does not deterministically produce three of the eight signals in §8, and its cost model is roughly 1.5× too low — 4–20× too low for the toxic-anchor query.**

| Question | Answer |
|---|---|
| Can I get the top-DR 70, latest 30, top-50 anchors, stats, refdomain history? | **Yes.** All verified working, correct ordering, correct row counts. |
| Can I get post-drop size? | **No.** No drop date supplied, and `history=since:*` is ignored by `backlinks-stats`. |
| Can I get a traffic cliff / pre-drop baseline? | **Not reliably.** `metrics-history` returned 0 rows for one site and 1 row for another. Absent rows are indistinguishable from zero traffic. |
| Can I get whole-profile toxic anchors? | **Not with one batched call.** The 250-row cap was hit on 2 of 5 sites. Completing it costs ~7.6–7.9k units, not the 50–500 the spec estimates. |
| Is the per-site cost estimate right? | **No.** Spec: ~1.8–2.3k. Measured as written: **2.7–4.1k** (mean 3.4k). With a complete lexicon: **2.5–11.9k** (mean 6.5k). |
| Can I get live / all-time links and refdomains? | **Yes — all four in one `backlinks-stats` call.** See §1.1. |

---

## 1.1 Getting the four baseline counts — one call is enough

Yes. All four arrive in a single `backlinks-stats` response; no parameter changes are needed. The fix is to stop making three calls and to read all four fields from the one payload.

```bash
curl -s -G "https://api.ahrefs.com/v3/site-explorer/backlinks-stats" \
  -H "Authorization: Bearer $AHREFS_KEY" \
  --data-urlencode "target=smoke-fire.com" \
  --data-urlencode "mode=domain" \
  --data-urlencode "date=2026-11-24"
```

```json
{"metrics": {"live": 1159, "all_time": 5979,
             "live_refdomains": 992, "all_time_refdomains": 1599}}
```

`history`, `protocol`, `aggregation` and `select` are all accepted but ignored — tested with `history=live`, `history=all_time`, `history=bogus`, `history=since:2025-08-14`, `protocol=both`, `aggregation=1_per_domain` and `select=live,all_time`; every variant returned the identical four numbers.

**Changes applied:**

1. `ahrefs_ingest.py` now issues one `stats` call, not `stats_live` + `stats_all_time`.
2. The `since:` variant is removed (it cannot work) and `coverage.stats.since_drop` is explicitly `null`.
3. The `Stats` sheet in every workbook now lists the four counts as first-class metrics, plus two explicitly-unavailable post-drop rows.
4. Spec §3 row 1 and §3.1 are updated to the single call.

**Cost:** 50 units instead of 150 billed (saves ~100/domain cold; in this run some duplicates were cache-free, so the measured saving was 50 on two sites and 0 on three).

**Two caveats:**

- These are **subdomain-level** referring-domain counts. `blogspot.com` subdomains each count as a separate referring domain. Registrable-root counts (the 377-vs-992 ambiguity in the earlier smoke-fire report) are *not* available from this endpoint and must be derived from a `refdomains` export.
- This does **not** give you post-drop counts. There is no `backlinks-history` endpoint (verified 404), and `backlinks-stats` ignores `history`. Post-drop link volume needs a finalist `first_seen_link > drop_date` export; at standard tier the only proxy is the `refdomains-history` series.

---

## 2. Cost per site (measured)

All figures are `x-api-units-cost-total-actual` from the response headers. "Billed" is `x-api-units-cost-total` (what the request costs before cache); "actual" is what was charged.

| Site | Live links | All-time links | Live refdomains | Lexicon anchors | Run units (billed) | Run units (actual) | Complete lexicon | **Total (complete)** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| smoke-fire.com | 1,159 | 5,979 | 992 | 165 | 2,844 | 2,500 | 0 | **2,500** |
| 123eft.com | 955 | 4,669 | 893 | 273 | 4,090 | 4,040 | 7,860 | **11,900** |
| kanuksheepskinproducts.com | 1,142 | 1,705 | 1,095 | 267 | 4,101 | 4,101 | 7,640 | **11,741** |
| longbeachcoffee.com | 736 | 1,411 | 723 | 188 | 3,305 | 3,255 | 0 | **3,255** |
| abipr.com | 1,034 | 2,953 | 906 | 168 | 2,924 | 2,874 | 0 | **2,874** |
| **Mean** | | | | | **3,453** | **3,354** | | **6,454** |

`smoke-fire.com` is the run that validates the corrected one-stats-call pipeline. Its original as-written run (two stats calls) billed 2,894 and charged 2,744. The other four rows still use the as-written two-call version, so the mean mixes both models; the "corrected" column in `_all-sites-summary.xlsx` isolates the difference.

### 2.1 Where the money actually goes

| Query | Spec estimate | Measured (per site) | Verdict |
|---|---:|---:|---|
| `backlinks-stats` ×3 | 150 | 50 (one call is enough) | Over-specified |
| `refdomains-history` | ~290 | 294 (49 rows) | Accurate |
| `metrics-history` | ~530 | 0–407 (data-dependent) | Estimate high, usefulness low |
| Top-70 backlinks | ~420 | 420 | Exact |
| Latest-30 backlinks | ~180 | 180 | Exact |
| Batched lexicon | 50–500 | **1,650–2,500** | **4–20× too low** |
| Top-50 anchors | ~200 | 200 | Exact |

Two structural cost errors:

1. **`backlinks-stats` was called three times but returns everything in one row.** The API ignores `history`; `live`, `all_time`, `live_refdomains`, `all_time_refdomains` all come back in a single 50-unit response. The `since:` call cannot work at all. Collapsing to one call saves 50 units/site versus the two calls actually made (100 versus the spec's three). See §1.1 — fixed in the scripts and spec.
2. **The lexicon search is priced as if it returns a handful of rows.** On these five profiles, generic link-selling terms (`seo`, `backlink`, `guest post`) match 150–226 anchors each. At 10 units/row that is 1.6–2.5k per site, and a *complete* search is 7.6–7.9k when the batched union exceeds 250.

### 2.2 Budget implication

| Planning basis | Units/domain | Domains per 800k |
|---|---:|---:|
| Spec estimate | ~2,000 | ~320 |
| Measured, spec as written | ~3,400 | ~235 |
| Measured, one stats call | ~3,354 | ~239 |
| Measured, complete lexicon | ~6,454 | ~124 |

The spec's "300–350 standard domains/month" only holds if the lexicon is left incomplete.

---

## 3. API usage before/after

| Snapshot | Workspace units | API-key units |
|---|---:|---:|
| Before (2026-11-24 08:29Z) | 250,832 | 204,747 |
| After (2026-11-24 ~08:44Z) | 292,325 | 246,240 |
| **Delta** | **41,493** | **41,493** |

The delta reconciles exactly:

| Item | Units |
|---|---:|
| Exploratory first run of `smoke-fire.com` | 2,894 |
| Five standard-tier runs as written | 17,014 |
| Two complete-lexicon runs | 15,500 |
| Diagnostic probes (metrics-history, `history` on stats, anchors offset/term caps) | 3,585 |
| Validation run of the corrected one-stats-call pipeline (`smoke-fire.com`) | 2,500 |
| **Sum** | **41,493** |

Probe spend was 9% of the total and bought the API-behaviour findings below.

---

## 4. What the spec gets right (verified)

These were open questions in §11 and are now answered:

| Behavior | Result |
|---|---|
| `order_by` + `aggregation=1_per_domain` on `all-backlinks` | **Works.** 70 rows = 70 unique domains, DR strictly descending. |
| `order_by=first_seen_link:desc` on `all-backlinks` | **Works.** Strictly descending; 1–4 rows overlap the top-DR slice; 96–99 unique rows from 100. |
| `aggregation=1_per_domain` list endpoint row cap | 250/request as documented. |
| `anchors` with a 39-clause `or` on `anchor` | **Works** (returns 200 + rows), but see cap issue. |
| `metrics-history` default select | Confirmed expensive: `date,org_traffic` = 11 units/row. Only 1 row is returned per month, so `select` is essential. |
| `refdomains-history` with `history_grouping=monthly` | Works; returns 49 rows for a 48-month inclusive window. |
| Minimum charge | 50 units/request confirmed (many empty lexicon term queries billed 50). |

---

## 5. What the spec gets wrong, and how it breaks determinism

### 5.1 `backlinks-stats` ignores `history`

All three calls returned byte-identical metrics. There is no way to get a `since:drop_date` link count from this endpoint. §3.1 suspects this ("check whether `history` is accepted"); the answer is **accepted but ignored**. One call returns all four baseline counts — see §1.1 for the fix.

**Impact:** §8 row 1 ("live / all-time / post-drop size — ✅ whole profile") is only two-thirds true. Post-drop volume must be inferred from `refdomains-history`, which measures referring domains, not links.

### 5.2 `metrics-history` does not answer "was it a real site" for expired domains

| Site | Monthly rows returned | First month |
|---|---:|---|
| smoke-fire.com | 1 | 2026-11 |
| abipr.com | **0** | — |
| longbeachcoffee.com | 21 | 2023-11 |
| 123eft.com | 36 | 2022-11 |
| kanuksheepskinproducts.com | 37 | 2022-11 |

A row of `org_traffic: 0` and a missing row mean different things, but the table cannot tell them apart. `abipr.com` returns `{"metrics": []}` — the spec's "traffic cliff" check silently produces nothing rather than failing loudly.

**Impact:** §8 row 3 is not deterministic. Any downstream rule of the form "traffic used to exist and dropped" must treat no-data as `unknown`, not `fail`.

### 5.3 The lexicon query is the expensive, incomplete one

The single batched `or` request returned 250 rows (the cap) for `123eft.com` and `kanuksheepskinproducts.com`. There is **no offset pagination** on `anchors` — `offset=250` returned the same rows as `offset=0` — so the only completion path is splitting the term list.

I completed both capped sites by running all 39 terms individually:

| Site | Batched rows | True unique anchors | Extra requests | Extra units |
|---|---:|---:|---:|---:|
| 123eft.com | 250 | **273** | 39 | 7,860 |
| kanuksheepskinproducts.com | 250 | **267** | 39 | 7,640 |

So the batched call captured 92–94% of matching anchors, not 100%. It is a *sample*, and the spec's `lexicon_complete` flag would be `false` only after an expensive split that still cannot terminate if any single term ever exceeds 250. `seo` alone returned 226 rows on `123eft.com` — close enough to the cap that a larger profile will hit it.

Additional lexicon defects:

- **1,500 units of the 7.6–7.9k is pure minimum-charge waste**: 30 of 39 terms returned zero rows and still billed 50 units each.
- **Substring matching is very over-inclusive**: `seo` matches `Seoul`, `sex` matches `sexton`, `cbd` matches any string containing those letters. The API does the matching; the spec's local classifier cannot undo a truncated result set.
- **The toxicity signal is dominated by link-selling terms**, not gambling/pharma/adult. On all five sites the gambling count is 0–1 anchors. The expensive part of the query is the cheap-to-classify `seo/backlink/guest post` group.

### 5.4 No drop date means the "post-drop" framing is unavailable

The task supplied domains only. §2 handles this gracefully (`drop_date_source=unknown`, skip `since:`), but the consequence is that:

- `stats.since_drop` is `null` for all five sites;
- the `era` column in the `backlinks` table (`pre_drop` / `post_drop`) cannot be populated;
- the coverage object in §7 cannot be filled as written (`since_drop`, `drop_date_source`).

This is a run-input gap, not an API defect — but it means the spec as delivered to me cannot produce the artifact the spec describes.

### 5.5 Smaller issues

- **`refdomains-history` 49 vs 48 rows.** Cosmetic, but if a downstream check asserts 48 it fails.
- **`aggregation=1_per_domain` does not specify which link represents a domain.** The chosen row can change between runs even when the domain's link set has not changed.
- **The spec's cost table says ~1,800–2,300, while its own line items sum to 1,820–2,270 and the measured total is 3,400.** The lexicon line is the only large gap; the rest is accurate.
- **No `is_spam` or traffic fields in the standard tier**, so "real site" can only be inferred from URL shape and anchors. That is a design choice, but §8 row 3 claims more than the data supports.

---

## 6. §8 signal-by-signal determinism

| Signal | Whole profile? | Deterministic? | Note |
|---|---|---|---|
| Live / all-time size | yes | **Yes** | One stats row. |
| Post-drop size | claimed yes | **No** | No drop date; `history` ignored. |
| Post-drop refdomain spike | yes | **Conditional** | Series is complete; "post" needs an inferred drop date. |
| Traffic history / cliff | claimed yes | **No** | Empty or late-starting series; zero vs no-data ambiguous. |
| Toxic anchors (whole profile) | claimed yes | **No** | 250-row cap; completion costs 3× and can be impossible. |
| Strongest 70 refdomains | slice | **Yes** | Verified 70 unique, DR desc. |
| Current spam activity (latest 30) | slice | **Yes** | Verified strict date desc. |
| Anchor mix (top 50) | slice | **Yes** | Verified. |
| Topical fit | slice | **Yes, slice-bounded** | Only URLs/anchors in the 100-row slice. |

**Bottom line:** 5 of 9 signals are deterministic as specified; 2 are impossible with the given inputs; 2 are data-dependent and can silently return nothing.

---

## 7. Recommended spec changes (v4)

1. **One `backlinks-stats` call, no `history`.** It returns live links, all-time links, live refdomains and all-time refdomains together. Saves 50–100 units/site and removes a false capability. (Applied — see §1.1.)
2. **Drop date is a required run input.** If it is truly unknown, do not emit a `coverage.stats.since_drop` field; mark it `null` and make the post-drop signal explicitly `unknown` rather than implied.
3. **Fix the lexicon cost model and algorithm.** Split the lexicon into two tiers:
   - *Tier A (cheap, always complete):* gambling / pharma / adult / evasions — run one batched `or` (these were <30 rows on all five sites, so 50–300 units).
   - *Tier B (link-selling):* run per-term or per-category, accept 50-unit minimums, and budget **~2.5–8k** depending on profile size.
   Remove `seo` as a bare substring term, or require a phrase (`"seo service"`, `"seo package"`); it is the term that blows the cap and it matches the least specific spam.
4. **Make `metrics-history` tri-state.** Request it, but store `traffic_history = "none" | "partial" | "complete"` based on row count and first month. Never let an empty series imply zero traffic.
5. **Use the free refdomain counts.** `backlinks-stats` already returns `live_refdomains` / `all_time_refdomains`; the coverage object can be populated without extra calls.
6. **Pin the `date` parameter** to the run date and cache on `(endpoint, params, date)`. Otherwise "deterministic" only means deterministic within a day.
7. **Budget on measured, not estimated, cost:** ~3.5k/domain as written, ~6.5k with a complete lexicon. At 800k/month that is ~230 or ~120 domains, not 300–350.

---

## 8. Artifacts

```
output/
  smoke-fire.com.xlsx
  123eft.com.xlsx
  kanuksheepskinproducts.com.xlsx
  longbeachcoffee.com.xlsx
  abipr.com.xlsx
  _all-sites-summary.xlsx        # cost, usage, spec conformance, determinism
runs/<domain>/<run_id>/
  manifest.json                  # params, headers, units per request
  raw/<query_id>.p0.json         # payload + request record
  raw/<query_id>.p0.headers.json # full response headers
  lexicon_term_complete.json     # only for the two capped-lexicon sites
```

Every workbook has the same sheets: `Summary`, `Stats`, `History`, `Backlinks_TopDR`, `Backlinks_Latest`, `Anchors_Top50`, `Anchors_Lexicon`, `Costs`, `Coverage` (plus `Costs_LexiconDetail` where the lexicon was completed).

Scripts (deterministic, re-runnable):
`ahrefs_ingest.py` (standard tier), `complete_lexicon.py` (term-level completion), `build_workbooks.py`, `build_summary_workbook.py`.
