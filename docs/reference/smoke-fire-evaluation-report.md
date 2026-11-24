# Domain Evaluation Report: `smoke-fire.com`

**Date:** 2026-11-24  
**Scope:** Domain-name specificity, rel-attribute quality ratio, link-to-domain spread, and anchor-text category safety.  
**Brand-conflict check:** Removed from scope per instruction.  
**Data sources:** Ahrefs API v3, DataForSEO Backlinks API, DataForSEO Google Ads keyword data. No browser or manual SERP inspection was used.  
**Method:** All pass/fail results are rule-based and reproducible. No LLM was used to score the domain.

---

## 1. Executive summary

| # | Criterion | Result | Headline metric |
|---|---|---|---|
| 1 | Domain keywords not too specific | **PASS** | 2 generic tokens, 0 vertical/geo/model tokens |
| 2 | Brand conflict | **REMOVED** | — |
| 3 | Rel-attribute good-to-bad ratio | **PROVIDER-DEPENDENT** | Ahrefs: **0.82:1** good:bad; DataForSEO: **9.28:1** |
| 4 | Link volume vs unique-domain spread | **PASS, WITH CAVEAT** | 1.17–3.07 links/domain; only ~8.8 effective root domains |
| 5 | Anchors free of gambling/pharma/adult | **STRICT FAIL / PRACTICALLY CLEAN** | 1 gambling mention (1 link); 0 pharma; 0 adult |

The two backlink providers disagree sharply because they index different link populations. Ahrefs contains the 2026 link-spam campaign; DataForSEO mostly does not. This changes the verdict on criterion 3 and hides the gambling anchor in criterion 5.

---

## 2. Criterion 1 — Domain keywords not too specific

**Result: PASS (medium confidence)**

Deterministic proxy:

| Check | Result |
|---|---|
| Domain stem | `smoke-fire` |
| Tokens | `smoke`, `fire` |
| Tokens that are service/product/industry nouns | 0 / 2 |
| Geography modifiers | 0 |
| Numbers or model names | 0 |
| Token count | 2 |

`smoke-fire` is not locked to one vertical. It can plausibly support BBQ/grilling, fire safety, fireplace/wood heat, smoked foods, ceramics, outdoor equipment, cannabis, or a general lifestyle brand. That breadth is why it is reusable; the trade-off is that it is not distinctive.

Caveat: this is the least deterministic criterion in the set. For mass evaluation, use a versioned `vertical_terms` + `geo_terms` + `common_words` lexicon and a human-reviewed exception list for ambiguous words like `fire`.

---

## 3. Criterion 3 — Rel-attribute good-to-bad ratio

### 3.1 Definition

```text
good = dofollow AND NOT sponsored AND NOT ugc
bad  = nofollow OR sponsored OR ugc
```

### 3.2 Results

| Source | Total links | Good (dofollow) | Bad | Good share | Good:Bad |
|---|---:|---:|---:|---:|---:|
| Ahrefs live anchors | 1,160 | 522 | 638 | 45.0% | **0.82 : 1** |
| DataForSEO live links | 1,038 | 937 | 101 | 90.3% | **9.28 : 1** |

`sponsored` is **0** in both datasets. Ahrefs reports **0 ugc**; DataForSEO reports **12 ugc** links, all of which also carry nofollow, so the DataForSEO “any bad attribute” union remains 101. Every “bad” link in both datasets is nofollow.

Ahrefs’ backlinks-stats endpoint reports **1,159** live links; the live anchor census sums to **1,160**. The one-link difference is a rounding/aggregation artifact and does not affect the ratio materially.

DataForSEO’s summary endpoint reports **1,168** backlinks; its link-level endpoint returned **1,038** rows, while its anchor endpoint sums to **1,168**. The table above uses the 1,038 link records actually returned by the link-level endpoint.

### 3.3 Quality-adjusted ratio

Ahrefs flags 103 of 119 live anchor groups, covering **1,032 of 1,160 links (89.0%)**, as `is_spam = true` at the domain/group level.

| Bucket | Links | Share |
|---|---:|---:|
| Clean dofollow (outside spam-flagged groups) | 116 | 10.0% |
| Dofollow inside spam-flagged groups | 406 | 35.0% |
| Nofollow | 638 | 55.0% |
| **Clean good:everything else** | **116 : 1,044** | **0.11 : 1** |

So “rel-good” is not the same as “quality-good.” The pure rel ratio is already poor under Ahrefs; the quality-adjusted ratio is much worse.

### 3.4 Example backlinks — dofollow

| Referring URL | Anchor | Source | Rank | Rel |
|---|---|---|---:|---|
| `https://brandfetch.com/smoke-fire.com` | `smoke-fire.com` | Ahrefs | DR 73 | dofollow |
| `https://aligow.com/website-list-1515/` | `smoke-fire` | Ahrefs | DR 61 | dofollow |
| `http://vcr-inc.com/wp/movie-appearances/` | `Smoke & Fire Company` | DataForSEO | 57 / 1000 | dofollow |
| `http://lathrophouse.blogspot.com/2005/04/smoke-and-fire-company.html` | `www.smoke-fire.com` | DataForSEO | 102 / 1000 | dofollow |
| `http://tehranwebseo.com/dir/contextual-link-building-192861` | `Premium Backlinks to Strengthen SEO Authority and Rankings smoke-fire.com` | Ahrefs | DR 8 | dofollow |
| `https://betulcrime.com/all/2668/6.html` | `High Quality Dofollow Backlinks DA 50 PA 40 Premium PBN Network Service smoke-fire.com Rank First Page Google Fast SEO Link Building Buy Backlinks Online Cheap` | Ahrefs | DR 39 | dofollow |

The `tehranwebseo.com` example is the top anchor by volume: **291 links / 291 referring domains**, all dofollow, all in a group flagged `is_spam = true`. That single anchor is 25% of the live link profile.

### 3.5 Example backlinks — nofollow

| Referring URL | Anchor | Source | Rank | Rel |
|---|---|---|---:|---|
| `https://seoflx.net/q/jpavv-rank-website-on-first-page/` | `Two small steps changed smoke-fire.com’s ranking story—check SeoFlox.com.` | Ahrefs | DR 48 | nofollow |
| `https://ekenkoshop.shop/i-was-frustrated-learning-seo-as-an-average-shop-owner-until-seoexpress-com-offered-budget-friendly-packages...` | `Back when I first launched smoke-fire.com... check out SEOExpress.org! 📈` | Ahrefs | DR 49 | nofollow |
| `https://pyracy.com/topic/15707-table-plans/` | `Smoke & Fire` | DataForSEO | 218 / 1000 | nofollow, external |
| `https://www.siteprice.org/competitors/jarnaginco.com` | *(empty anchor)* | DataForSEO | 371 / 1000 | nofollow, external |
| `https://rankpilot.shop/0m2yubi-ready-to-rank-fast-choose-your-plan-manual-work-google-friendly/` | `Rank smoke-fire.com all niches — PBN links, full SEO, monthly SEO, DR80+, DR/DA/TF — gambling accepted. \| Visit Rankva.com for any Query` | Ahrefs | DR 13 | nofollow |

Note: DataForSEO `domain_from_rank` is on a 0–1000 scale, not Ahrefs DR. Values shown are from the DataForSEO record and are not directly comparable to Ahrefs DR.

### 3.6 Why the providers disagree

- Ahrefs’ live index includes the 2026 SEO-spam campaign: `.shop`, `.store`, `.agency`, and directory-style domains with exact-match/SEO anchors.
- DataForSEO’s 1,038-link set is dominated by older/historical links: `vcr-inc.com`, Blogspot pages, `reenactor.ru`, `pyracy.com`, `jothut.com`.
- DataForSEO’s anchor set did not contain the live 291-link `Premium Backlinks...` anchor or the live `gambling accepted` anchor.
- Ahrefs reports 992 referring domains (subdomain level) and 377 main domains; DataForSEO reports 425 referring domains and 377 main domains. Similar totals, different composition.
- Ahrefs `all-backlinks` has a **250-row hard cap and no offset pagination**, and defaults to `history=all_time` plus `aggregation=similar_links`. The reliable full census was obtained from the anchors endpoint with `history=live` and `dofollow_links`.

**Implication:** a pipeline using only DataForSEO would return a false pass on criterion 3 and would miss the gambling anchor.

---

## 4. Criterion 4 — Link volume vs unique-domain spread

**Result: PASS on raw ratio; CAUTION on effective diversity**

| Metric | Ahrefs | DataForSEO |
|---|---:|---:|
| Live links | 1,159 | 1,038 |
| Referring domains | 992 | 425 |
| Links per referring domain | **1.17** | **2.44** |
| Main/root domains | 377 | 377 |
| Links per main domain | **3.07** | **2.75** |
| Top domain share | — | 8.1% |
| Top 10 share (subdomain) | — | 39.1% |
| Top 10 share (root/platform) | — | 54.2% |
| Blogspot.com share | — | **32.1%** |
| Effective root domains (`1/HHI`) | — | **~8.8** |

“Effective root domains” means `1 / HHI` after collapsing subdomains to registrable roots. It answers: *how many independent domains does this profile effectively behave like?* The answer is about **9**, despite 425 raw referring domains.

### 4.1 Top referring domains (DataForSEO)

| Referring domain | Links | Share of 1,038 |
|---|---:|---:|
| `vcr-inc.com` | 84 | 8.09% |
| `bakerhomeenergyaudit.blogspot.com` | 75 | 7.23% |
| `twoeagleskitchen.blogspot.com` | 72 | 6.94% |
| `msotas.blogspot.com` | 60 | 5.78% |
| `reenactor.ru` | 22 | 2.12% |
| `www.reenactor.ru` | 22 | 2.12% |
| `pyracy.com` | 20 | 1.93% |
| `good-clothes.web.app` | 19 | 1.83% |
| `fortlajonquiere.blogspot.com` | 18 | 1.73% |
| `jothut.com` | 14 | 1.35% |
| `www.jothut.com` | 12 | 1.16% |

Platform concentration:

| Platform | Links | Share |
|---|---:|---:|
| `blogspot.com` subdomains | 333 | 32.1% |
| `web.app` | 19 | 1.8% |
| `pages.dev` | 8 | 0.8% |
| `wixsite.com` | 1 | 0.1% |
| **Total free-platform links** | **361** | **34.8%** |

Raw spread is acceptable: 1.17–3.07 links per domain, median 1 link per domain. The caveat is that one platform (Blogspot) supplies a third of the profile, so the raw domain count overstates independence.

---

## 5. Criterion 5 — Anchor texts free of gambling, pharma, adult

**Result: STRICT FAIL / PRACTICALLY CLEAN**

Rule: lowercase → HTML-unescape → collapse whitespace → word-boundary regex → weight by `links_to_target`.

| Category | Anchors | Links | Share of 1,160 |
|---|---:|---:|---:|
| Gambling | 1 | 1 | 0.09% |
| Pharma | 0 | 0 | 0.00% |
| Adult | 0 | 0 | 0.00% |

### 5.1 The single gambling match

| Field | Value |
|---|---|
| Referring URL | `https://rankpilot.shop/0m2yubi-ready-to-rank-fast-choose-your-plan-manual-work-google-friendly/` |
| Anchor | `Rank smoke-fire.com all niches — PBN links, full SEO, monthly SEO, DR80+, DR/DA/TF — gambling accepted. \| Visit Rankva.com for any Query` |
| Links | 1 |
| Rel | nofollow |
| DR | 13 |

This is a link-selling/PBN advertisement, not an anchor from a gambling site. It is evidence that the domain was targeted by a link-selling campaign that accepts gambling niches, not evidence that `smoke-fire.com` itself operated in gambling.

- **Literal rule:** FAIL — the anchor text contains “gambling.”
- **Intent-aware rule:** PASS — exclude link-selling boilerplate, then there are zero relevant matches.

That policy choice must be explicit in any mass-evaluation spec.

### 5.2 Example anchors by category

| Category | Anchor | Links | Source | Rel |
|---|---|---:|---|---|
| Gambling | `Rank smoke-fire.com all niches — PBN links... gambling accepted...` | 1 | Ahrefs live | nofollow |
| Brand/exact | `Smoke & Fire Company` | 222 | DataForSEO anchors | mixed |
| Exact | `smoke-fire` | 54 | Ahrefs live | mixed |
| Exact | `smoke-fire.com` | 52 | Ahrefs live | mixed |
| URL | `http://www.smoke-fire.com/` | 25 | DataForSEO anchors | mixed |
| SEO link-selling | `High Quality Dofollow Backlinks DA 50 PA 40 Premium PBN Network Service smoke-fire.com Rank First Page Google Fast SEO Link Building Buy Backlinks Online Cheap` | 42 | Ahrefs live | dofollow |
| SEO link-selling | `Premium Backlinks to Strengthen SEO Authority and Rankings smoke-fire.com` | 291 | Ahrefs live | dofollow |

---

## 6. Anchor-spam assessment (additional signal, not part of criterion 5)

The earlier “spammy anchor” label was a heuristic, not a deterministic rule. The reproducible replacement is a phrase lexicon with explicit boolean logic:

```text
HARD = ["pbn", "buy backlinks", "dofollow backlinks",
        "link building service", "guest post package"]

SOFT_GROUPS = {
  "link_building": ["backlink", "link building", "guest post", "link velocity"],
  "metrics":       ["da ", "dr ", "pa ", "tf ", "domain rating", "domain authority"],
  "ranking":       ["rank first page", "ranking", "serp", "seo"],
  "commercial":    ["buy", "cheap", "premium", "service"]
}

is_link_selling = any(HARD match)
                  OR (number of distinct SOFT_GROUPS matched >= 2)
```

| Signal | Anchors | Links | Share |
|---|---:|---:|---:|
| Link-selling vocabulary rule | 92 / 119 | 463 | 39.9% |
| Ahrefs `is_spam` domain/group flag | 103 / 119 | 1,032 | 89.0% |
| Clean dofollow outside spam groups | — | 116 | 10.0% |

Examples:

| Anchor | Links | Refdomains | `is_spam` |
|---|---:|---:|---|
| `Premium Backlinks to Strengthen SEO Authority and Rankings smoke-fire.com` | 291 | 291 | true |
| `Need on-page SEO that holds up in Southeast Asia? smoke-fire.com delivers it...` | 219 | 219 | true |
| `Back when I first launched smoke-fire.com... SEOExpress.org...` | 237 | 207 | true |
| `High Quality Dofollow Backlinks DA 50 PA 40 Premium PBN Network Service...` | 42 | 40 | true |
| `Scale smoke-fire.com with premium guest posts, high-quality backlinks...` | 17 | 17 | true |
| `Powerful Dofollow Link Building for smoke-fire.com for Higher DA and DR Scores...` | 17 | 17 | true |

Both signals point the same way: the live anchor profile is dominated by link-selling/SEO-spam language, even though literal gambling/pharma/adult matches are almost absent.

---

## 7. Tool gaps found

1. **Ahrefs row cap:** `all-backlinks` returns at most 250 rows and has no offset pagination. It also defaults to `history=all_time` and `aggregation=similar_links`.
   - Workaround: use `/site-explorer/anchors?history=live&select=links_to_target,dofollow_links,is_spam` to get the full live anchor census.
2. **DataForSEO coverage gap:** its backlink/anchor set misses most of the 2026 SEO-spam campaign. It reports 101 nofollow links; Ahrefs reports 638.
3. **No anchor-level category field:** neither provider labels gambling/pharma/adult anchors. A lexicon is required.
4. **`is_spam` is domain/group-level:** it can over-attribute links within a group. Treat it as a signal, not ground truth.
5. **Subdomain vs root-domain ambiguity:** providers report 425 vs 377 domains, and 992 vs 377 for Ahrefs. Ratios can differ by 2–3x depending on normalization.
6. **Rel-good ≠ quality-good:** 406 of 522 dofollow links sit in spam-flagged groups. A pure rel ratio can look acceptable while the profile is still spam-dominated.
7. **No provider field for “link-selling anchor”:** must be a versioned lexicon/rule.
8. **SERP domain parsing:** DataForSEO’s organic SERP returned `google.com/goto` redirect URLs with `domain=google.com` for most results. Not used for this report, but relevant if SERP evidence is added later.

---

## 8. Deterministic implementation spec

### 8.1 Rel ratio

```text
total = backlinks
good  = dofollow AND NOT sponsored AND NOT ugc
bad   = total - good
good_share = good / total
good_bad_ratio = good / bad
```

Preferred source: Ahrefs live anchors (`dofollow_links` / `links_to_target`).  
Cross-check: DataForSEO `dofollow` + `attributes` (`nofollow`, `sponsored`, `ugc`).

Suggested thresholds:

| Good:Bad | Interpretation |
|---|---|
| ≥ 4:1 | Pass |
| 1:1 – 4:1 | Caution |
| < 1:1 | Fail |

### 8.2 Anchor category checks

```python
import html, re

LEXICON = {
    "gambling": ["casino", "poker", "slots", "betting", "wager", "roulette",
                 "blackjack", "jackpot", "lottery", "bingo", "sportsbook",
                 "bookmaker", "gambling", "gamble", "parlay", "craps",
                 "baccarat", "keno", "free spins", "sweepstakes"],
    "pharma":   ["pharmacy", "viagra", "cialis", "levitra", "tramadol",
                 "oxycodone", "hydrocodone", "xanax", "diazepam", "valium",
                 "adderall", "fentanyl", "prescription", "medication", "meds",
                 "pills", "tablet", "tablets", "capsule", "capsules",
                 "drugstore", "steroid", "steroids", "anabolic", "sildenafil",
                 "tadalafil", "dapoxetine", "modafinil", "pharmaceutical"],
    "adult":    ["porn", "porno", "xxx", "sex", "sexual", "escort", "escorts",
                 "nude", "nudes", "naked", "erotic", "milf", "onlyfans",
                 "camgirl", "webcam", "adult", "sex toys", "hookup", "hookups"],
}

def normalize(anchor: str) -> str:
    a = html.unescape(anchor or "").lower()
    return re.sub(r"\s+", " ", a).strip()

def hits(anchor: str):
    a = normalize(anchor)
    return {
        cat: [t for t in terms
              if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", a)]
        for cat, terms in LEXICON.items()
    }
```

Log every match with: anchor, link count, referring-domain count, matched term.

### 8.3 Link/domain spread

```text
links_per_domain = backlinks / referring_domains
links_per_root   = backlinks / referring_main_domains
top1_share       = links_on_top_domain / backlinks
top10_share      = links_on_top_10_domains / backlinks
HHI              = sum((links_on_domain / backlinks)^2) * 10000
effective_domains = 1 / (HHI / 10000)
```

Report both subdomain and registrable-root versions. Add platform concentration for `blogspot.com`, `web.app`, `pages.dev`, `wixsite.com`, `wordpress.com`.

### 8.4 Domain specificity

```text
tokens = split(domain_stem, "-")
vertical_hits = tokens ∩ vertical_lexicon
geo_hits      = tokens ∩ geo_lexicon
model_hits    = tokens ∩ regex(r"\d+")
pass = (not vertical_hits) and (not geo_hits) and (not model_hits)
```

This is a proxy, not a semantic truth. Keep ambiguous words (`fire`, `smoke`, `pilot`, `summit`, etc.) in a human-reviewed exception list.

---

## 9. Bottom line

- `smoke-fire.com` has a **reusable, non-niche name** (criterion 1 pass).
- Its **rel profile is poor under Ahrefs** (0.82:1 good:bad) and looks clean only under DataForSEO (9.28:1) because DataForSEO misses the spam campaign.
- Its **raw domain spread is acceptable**, but the effective independent root-domain count is only about **9**, largely because Blogspot supplies 32% of links.
- Its **anchor categories are literally clean except for one gambling mention** in a link-selling anchor; pharma and adult are zero.
- The **dominant risk signal is SEO/link-selling spam**, not gambling/pharma/adult: 39.9% of links by anchor vocabulary and 89% by Ahrefs’ `is_spam` flag.

For mass evaluation, use Ahrefs as the primary live-backlink source, DataForSEO as a cross-check, and versioned lexicons for all text-based checks. Do not let a single provider or an LLM decide pass/fail.
