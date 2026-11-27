# Prototype Domain Evaluation Mechanism

Status: inferred from the live prototype on 2026-11-26. This is not derived from the prototype's source code. It is reconstructed from:

- the saved shortlist page in `reference/misc/`
- the page's embedded `rows-data` and `meta-data` JSON
- the prototype's own detail pages and `/api/domains/<domain>` JSON
- the XLSX export's `Method & sources` sheet

No prototype or app files were modified.

Purpose: this document is intended to be used as an implementation brief. It explains what the prototype evaluates, how it scores and judges a domain, which sources it uses, and which parts are deterministic.

---

## 0. Short answer on determinism

The prototype's **scoring arithmetic and verdict logic are deterministic**. There is no LLM and no model inference in the evaluation.

Given the same:

- source snapshot
- extracted Wayback evidence
- backlink classifications
- flag set
- config thresholds

the score, verdict, and reason text are a pure function of those inputs.

However, the overall result is **not bit-for-bit reproducible across runs**, because the inputs themselves are not stable:

- GoDaddy auction inventory changes continuously.
- Semrush/Majestic values are embedded in the GoDaddy feed and dated to the feed build.
- Wayback CDX history and sampled snapshots can change as new captures appear or old ones become unavailable.
- The prototype samples at most 4 Wayback snapshots across 200-OK HTML captures. Different samples can change topic/spam/parking flags.
- Flag generation uses heuristic keyword lexicons and thresholds.
- The prefilter appears to be a per-niche top slice, ranked mainly by Semrush Authority Score.

So the correct framing is:

> **The judgment is deterministic rules over non-deterministic external evidence.** The score/verdict is deterministic *once the evidence and flags are frozen*. The evidence collection is not deterministic.

The browser-side decisions (`Dismiss`, `Watch`, `max bid`) are local UI state only. They do not change the score or verdict.

---

## 1. Pipeline overview

```text
GoDaddy Auctions feed
        |
        v
per-niche prefilter (name match + authority/volume ranking)
        |
        v
per-domain evidence collection
  - Semrush authority + referring domains + top RDs
  - Wayback CDX history
  - sampled Wayback snapshots
  - Common Crawl URL count
        |
        v
flag generation (hard reject / cap / warning / history penalty)
        |
        v
five component scores (0..1 each)
        |
        v
weighted score (0..100) + evidence score (0..90, excludes price)
        |
        v
verdict engine (hard flags, score thresholds, cap flags)
        |
        v
templated reason + claims
```

### Reference run in the saved snapshot

- Feed built: `2026-11-24 22:02` Taipei
- Last run: `2026-11-25 04:35` Taipei
- Sourced listings: `16,389`
- Prefiltered: `123`
- Vetted: `123`
- Verdicts: `23 Buy`, `67 Maybe`, `33 Reject`
- Active only: `21 Buy`, `52 Maybe`, `28 Reject`
- Niches: `healthcare`, `pi_legal`, `travel`

The 123 vetted rows were split as:

- Healthcare: 45
- PI / Legal: 45
- Travel: 33

That looks like a per-niche quota of approximately 45, with travel supply-limited.

---

## 2. Sources

### 2.1 GoDaddy Auctions public inventory feed

Source file named by the prototype: `all_expiring_auctions.json.zip`, from the GoDaddy public auction inventory feed.

Used for:

- domain inventory
- auction type and status
- auction end time
- number of bids
- current bid / Buy Now price
- registration age
- GoDaddy GoValue appraisal
- parking pageviews
- adult flag
- attached Semrush fields
- attached Majestic fields
- attached Estibot fields

The feed is the **listing source** and also the transport for most third-party metrics.

> **Important:** GoDaddy is only the listing source. The Semrush, Majestic, and Estibot values are attached to its feed as a convenience. They are not intrinsic to the auction listing and are not automatically available for domains from other marketplaces. If a domain comes from NameJet, DropCatch, ExpiredDomains.net, a manual CSV, or anywhere else, you must obtain equivalent metrics from the relevant provider or substitute another source.

### 2.2 Semrush metrics via the GoDaddy feed

These are not direct Semrush API calls. They are fields attached to the GoDaddy feed and dated to the feed build.

Used fields:

- `semrushAs` - Authority Score
- `semrushReferringDomains` - raw referring-domain count
- `semrushBacklinks` - backlink count
- `semrushTopReferringDomains` - top 3 referring domains
- `semrushSearchVolume` - monthly search volume of the name as a phrase
- `semrushCpc` - name CPC
- `semrushIndexedPages` - indexed page count

Scoring use:

- authority component
- links component
- display/diagnostics

### 2.3 Internet Archive Wayback Machine

The prototype uses two Wayback paths:

1. **CDX history**
   - first capture
   - last capture
   - archive span in years
   - month-collapsed capture history
   - 2xx / 3xx status mix
   - up to 1,500 distinct archived URLs
   - CJK URL count

2. **Sampled snapshots**
   - up to 4 snapshots sampled evenly across 200-OK HTML captures
   - raw `id_` mode
   - title, description, word count
   - top terms
   - language / lang attribute
   - topic hits against the niche lexicon
   - spam / gambling / pharma / payday / SEO-spam hits
   - parked / for-sale hits
   - CJK / non-Latin ratios
   - meta refresh
   - HTTP status
   - page excerpt

Scoring use:

- history component
- relevance component
- spam, takeover, parking, redirect, topic, language, and thin-content flags

### 2.4 Common Crawl

Latest crawl URL count, e.g. `CC-MAIN-2026-47`.

Used for display. I do not see it in the five-component score.

### 2.5 Top referring-domain classifier

The top 3 referring domains from the Semrush feed are classified as:

- trusted
- citation
- neutral
- spam

Used for:

- links component
- `spammy_backlinks` flag
- `rd_inflated` and `sitewide_link_spike` diagnostics

### 2.6 Sources that are wired but not active

- **Ahrefs DR / RD / anchors** - no API key, shown as `not available`
- **Majestic Trust Flow** - feed reports 0 for every listing on this date
- **Open PageRank** - no API key
- **Historic organic traffic** - no history API
- **Anchor text** - needs Ahrefs/Majestic anchors

### 2.7 Manual import slot

The method sheet mentions a manual CSV import slot for:

- ExpiredDomains.net
- NameJet
- DropCatch

It was not used in the saved run.

### 2.8 What is displayed but not scored

These metrics appear in the UI but do not feed the five score components:

- registration age
- GoDaddy valuation
- name search volume
- name CPC
- indexed pages
- parking pageviews
- Common Crawl URL count
- Majestic TF / Ahrefs DR / Open PageRank (unavailable)

### 2.9 Where each input is obtained

This is the acquisition map to implement. The scoring code should not fetch these itself. Fetch raw evidence first, persist it with source dates, then run pure flag/scoring functions over it.

| Needed input | Prototype source | Raw field / derivation | How to obtain |
|---|---|---|---|
| Domain, auction end, bids, price, auction type | GoDaddy Auctions feed | `domainName`, `auctionEndTime`, `numberOfBids`, `price`, `auctionType` | Download/parse the GoDaddy public auction inventory feed. Prototype file: `all_expiring_auctions.json.zip`; method sheet names `inventory.auctions.godaddy.com`. Exact download URL/auth is not exposed in the page. |
| Registration age, GoValue, pageviews, adult flag | GoDaddy Auctions feed | `domainAge`, `valuation`, `pageviews`, `isAdult` | Same GoDaddy feed. These are display/prefilter signals, not five-component inputs. |
| Semrush Authority Score | Semrush via GoDaddy feed | `semrushAs` | Read it from the feed. Direct Semrush API is optional; the prototype does not call it. |
| Raw referring domains | Semrush via GoDaddy feed | `semrushReferringDomains` | Same feed field. |
| Backlink count | Semrush via GoDaddy feed | `semrushBacklinks` | Same feed field. Used for `sitewide_link_spike` (`backlinks / RD > 50`). |
| Top 3 referring domains | Semrush via GoDaddy feed | `semrushTopReferringDomains` (comma-separated) | Same feed field. |
| Search volume, CPC, indexed pages | Semrush via GoDaddy feed | `semrushSearchVolume`, `semrushCpc`, `semrushIndexedPages` | Same feed fields. Display/diagnostics only; not scored. |
| First/last capture, archive span | Internet Archive CDX | `wayback_first`, `wayback_years` | Query CDX for the domain; compute first/last and span. |
| Captures, 200-OK months, status mix, max gap | Internet Archive CDX | `captures`, `html_200_months`, `status_mix`, `max_gap_months` | Query CDX; group by month; count 2xx/3xx; compute longest gap. |
| Distinct archived URLs, CJK URL count | Internet Archive CDX | `url_count_sampled`, `cjk_urls` | Query CDX URL list, deduplicate, cap around 1500 URLs. |
| Snapshot text, title, words, language, topic hits, spam/parking hits | Wayback snapshots | `snapshots[]`, `content_snapshots`, `niche_topic_snapshots`, `topic_sequence`, `first_last_similarity` | Sample up to 4 snapshots evenly across 200-OK HTML captures; fetch raw `id_` replay; run text/topic/spam lexicons. |
| Common Crawl URL count | Common Crawl index | `cc_urls` | Query the latest Common Crawl index for the domain; cap around 500 URLs. Display only. |
| Top-RD classes | Local classifier | `top_rds[]`, `top_rd_trusted`, `top_rd_spam` | Classify the top 3 Semrush RDs locally as trusted / citation / neutral / spam. No external service. |
| Niche name and topic terms | Local niche config | `niche_hits`, topic lexicons | Define primary/secondary name terms and topic terms in config. The prototype's full lexicon is not exposed. |
| Archive/spam/takeover flags | Local rules over Wayback evidence | `wayback.flags`, `scoring.flags` | Pure functions over snapshots, CDX history, language ratios, spam lexicons, and link metrics. |
| `price_carried_score` | Derived | `score`, `evidence_points` | Add when `score >= 62` and `evidence_points < 54`. |

### 2.10 Concrete acquisition recipes

These are the external endpoints. Exact query parameters should be tuned to the feed/source version you use.

**GoDaddy feed**

```text
Prototype source file: all_expiring_auctions.json.zip
Named host: inventory.auctions.godaddy.com
Exact public download URL/auth: not exposed in the saved page
```

If the exact feed endpoint is unavailable, the fallback is to use the same public GoDaddy Auctions inventory export or replay a saved feed. Preserve the feed build date.

**Wayback CDX**

Root/month-collapsed history:

```text
https://web.archive.org/cdx/search/cdx
  ?url=<domain>
  &matchType=domain
  &output=json
  &fl=timestamp,original,statuscode,mimetype,digest
  &filter=statuscode:200
  &collapse=timestamp:6
```

Distinct archived URLs, capped:

```text
https://web.archive.org/cdx/search/cdx
  ?url=<domain>/*
  &output=json
  &fl=original,timestamp,statuscode,mimetype
  &collapse=urlkey
  &limit=1500
```

Raw snapshot replay:

```text
https://web.archive.org/web/<timestamp>id_/<original_url>
```

Use `id_` mode for the unmodified archived response. Sample up to 4 200-OK HTML captures evenly across the capture set.

**Common Crawl**

```text
https://index.commoncrawl.org/collinfo.json
https://index.commoncrawl.org/<index-name>-index?url=<domain>/*&output=json
```

The saved run used `CC-MAIN-2026-47` and a cap of roughly 500 URLs.

**Semrush**

The prototype does not call Semrush directly. It reads the fields attached to the GoDaddy feed. If you switch to direct Semrush API access, map the same concepts:

- domain Authority Score
- referring domains
- backlinks
- top referring domains
- search volume / CPC / indexed pages (diagnostics only)

Record the metric date. Direct API data will not match the prototype's GoDaddy-attached estimates exactly.

**Top-RD classifier**

Input is only the top-3 referring-domain string. Classification is local and heuristic. Seed the classifier with the observed examples:

- trusted: `github.com`, `google.com`, `crunchbase.com`, `marketwatch.com`, `wa.gov`, `ufl.edu`, `washington.edu`
- citation: `yellowpages.com`, `expertise.com`
- spam: `seo-revealed.club`, `knows.monster`, `sitelike.org`

Treat these as examples, not a complete list.

**Niche lexicon**

This is local config, not an external source. You need:

- primary name terms that get relevance base `0.55`
- secondary/generic name terms that get relevance base `0.35`
- topic terms per niche for snapshot matching
- spam / parked / adult / gambling / pharma / payday / SEO-spam terms
- brand/trademark terms for `trademark_risk`

The observed primary/secondary terms are listed in section 5.2. The prototype's full lexicon is not exposed.

### 2.11 What to persist for reproducibility

For every candidate, store:

- raw feed record
- feed build timestamp
- Semrush/marketplace metric date
- CDX query parameters and raw CDX rows or a stable summary
- snapshot timestamps, URLs, titles, and extracted text/features
- Common Crawl index name and count
- top-RD classes and classifier version
- flag evidence, not just flag names
- component vector
- score and evidence points
- `scoring_version`, `config_version`, `source_snapshot_id`
- evaluation timestamp

This is what makes a "deterministic" verdict actually reproducible from frozen evidence.

### 2.12 Using domains that do not come from GoDaddy

The evaluation logic is source-agnostic. GoDaddy is just the convenient bundle: it provides the auction record **and** attaches third-party metrics to it. For another marketplace or a manual list, keep the same semantic inputs and swap the provider.

| Needed input | GoDaddy convenience field | Non-GoDaddy equivalent |
|---|---|---|
| Domain, price, auction end, bids, status | `domainName`, `price`, `auctionEndTime`, `numberOfBids`, `auctionType` | Other marketplace API/export, manual CSV, or a saved listing record |
| Semrush Authority Score | `semrushAs` | Direct Semrush Authority Score, or deliberately switch to Ahrefs DR with recalibration |
| Referring domains | `semrushReferringDomains` | Semrush Backlink Analytics, Ahrefs Site Explorer, Majestic, DataForSEO, etc. |
| Backlinks | `semrushBacklinks` | Same backlink provider |
| Top 3 referring domains | `semrushTopReferringDomains` | Same backlink provider; then run the local trusted/citation/spam classifier |
| Search volume, CPC, indexed pages | `semrushSearchVolume`, `semrushCpc`, `semrushIndexedPages` | Optional/display only; use Semrush, Google Keyword Planner, DataForSEO, etc. |
| Registration age | `domainAge` | RDAP/WHOIS or a domain-age data provider |
| GoDaddy valuation, parking pageviews | `valuation`, `pageviews` | No direct equivalent; omit or replace with your own display metric |
| Adult flag | `isAdult` | Local snapshot/lexicon check or marketplace field |
| Archive history | Wayback CDX | Same; Wayback is domain-based and works for any source |
| Snapshot content / topic / spam | Wayback snapshots | Same; Wayback is domain-based and works for any source |
| Common Crawl URL count | Common Crawl index | Same; works for any domain |
| Majestic Trust Flow | `majesticTf` (reported 0) | Majestic API if you want it |

Important calibration notes:

- **Do not silently plug Ahrefs DR into the `min(AS, 25) / 25` authority formula.** That formula is calibrated to Semrush Authority Score. DR has different scale/distribution. The method sheet says DR would replace AS when Ahrefs is available, but the exact DR normalization and minimum are not visible. Either map DR onto an AS-like scale or recalibrate the authority component and thresholds.
- **Keep the top-RD semantics stable.** The links formula assumes the top 3 referring domains, classified locally. If your provider returns 10, 50, or 100 RDs, either take the top 3 or recalibrate the formula.
- **Record the source and date for every metric.** A direct Semrush value will not exactly match the GoDaddy-attached Semrush field.
- **Price semantics matter.** `$1` current bid, Buy Now price, and estimated value are different things. The price component uses the current feed bid / Buy Now price, not an appraisal.
- **GoDaddy-specific fields are optional.** GoValue, parking pageviews, and the adult flag are not part of the five-component score. You can omit them for non-GoDaddy domains.

Minimal non-GoDaddy path:

```text
1. Import domain + price + auction end from the other source.
2. Enrich with a backlink/authority provider.
3. Fetch Wayback CDX + up to 4 snapshots.
4. Optionally fetch Common Crawl.
5. Run the same flag engine and component formulas.
6. Store provider + date + raw evidence.
```

The formulas do not depend on GoDaddy. Only the convenience of having the metrics pre-attached does.

### 2.13 Lexicons that must be built and maintained

None of the semantic signals are provided by GoDaddy or Semrush. They are local config/data. The prototype's exact lists are not exposed, so build and version them explicitly.

Required lexicons:

| Lexicon | Purpose | Examples / notes |
|---|---|---|
| **Niche name terms** | Relevance name base | Primary terms get base `0.55`; secondary/generic terms get `0.35`. Include synonyms, professional titles, and common misspellings. |
| **Niche topic terms** | Classify archived snapshots | Used for `niche_topic_snapshots`, `weak_topic_match`, `off_topic_history`, `topic_flip`. Include body-content terms, not just domain-name terms. Exclude boilerplate/navigation words. |
| **Gambling spam** | Hard reject + history zeroing | `casino`, `slot`, `gacor`, `togel`, `judi`, `poker`, `betting`, `sportsbook`, `baccarat`, `roulette`, etc. |
| **Pharma spam** | Hard reject + history zeroing | `viagra`, `cialis`, `pharmacy`, `pills`, `tramadol`, `kamagra`, etc. |
| **Payday spam** | Hard reject | `payday loan`, `cash advance`, etc. |
| **Adult** | Hard reject | Adult-content terms and URL slugs. |
| **SEO / link-selling spam** | `seo_spam`, `toxic_anchors`, link-quality flags | `pbn`, `backlink`, `guest post`, `seo`, `link building`, `buy links`, etc. |
| **Parked / for-sale** | `parked_once`, `parked_recent` | `domain for sale`, `buy this domain`, `parked`, `sedo`, `afternic`, etc. |
| **Language / takeover signals** | `non_english_site`, `language_change`, `cjk_spam`, `foreign_takeover` | Script detection, language sequence, CJK URL ratio, non-Latin ratio. |
| **Trademark / brand** | `trademark_risk` | Known brands, legal entities, and obvious trademark terms. |
| **Former business / legal suffixes** | `former_business_name` | `LLC`, `PLLC`, `Inc`, `Ltd`, `Law Offices of`, etc., combined with archived title matching. |
| **Money / toxic anchors** | `toxic_anchors` | Only relevant when anchor data is available (Ahrefs/Majestic). |

Lexicon rules:

- Store as data files, not code.
- Version each lexicon and record the version on every verdict.
- Use word-boundary matching; avoid matching substrings inside legitimate words.
- Exclude the domain's own name from spam checks where appropriate.
- Maintain positive and negative examples.
- Test multilingual and transliterated variants.
- Review false positives regularly; a single generic term should not trigger a hard reject.
- Because `topic_flip` and spam flags can cap Buy, use precision-first thresholds and allow manual review.

### 2.14 Multi-provider evaluation: Semrush vs Ahrefs vs others

The score formula expects semantic inputs, not a specific vendor. Do not fork the verdict engine per provider. Use provider adapters plus calibration.

Canonical inputs:

```text
authority_score     0..1 normalized
authority_source    semrush | ahrefs | majestic | other | missing
authority_date
rd_count
backlink_count
top_rds[]           top 3, each classified trusted/citation/neutral/spam
price, auction_end
wayback_evidence
cc_url_count
```

Provider adapters:

| Provider | Raw metric | Mapping |
|---|---|---|
| Semrush | Authority Score (0-100) | `min(AS, 25) / 25`, min AS 8 |
| Ahrefs | Domain Rating (0-100) | Must be calibrated; do not reuse `AS/25` blindly |
| Majestic | Trust Flow (0-100) | Optional; calibrate if used |
| Missing | none | Do not silently score zero; flag `insufficient_data` and cap at Maybe |

Three viable approaches:

1. **Calibrated mapping (recommended if providers are mixed)**
   - Collect a sample of domains that have both AS and DR.
   - Fit a monotonic mapping from DR to AS-equivalent, or use quantile mapping.
   - Store the mapping version and re-fit periodically.
   - Then the authority component remains `min(mapped_AS, 25) / 25`.

2. **Provider-specific normalization and thresholds**
   - Semrush profile: `AS/25`, min AS 8.
   - Ahrefs profile: `DR/DR_max`, min DR 15.
   - Compare only within the same provider profile.
   - Simpler, but scores are not directly comparable across providers.

3. **Percentile rank against a reference universe**
   - Convert AS or DR to a percentile against a fixed candidate universe.
   - Use that percentile as the authority component.
   - Robust, but requires maintaining the reference universe.

Rules:

- Never compare raw `AS 8` with raw `DR 15` as if they were the same scale.
- Store `authority_source` and `authority_date` on every score.
- Use one primary authority provider per domain; fall back only when it is missing.
- If authority is missing, do not award full points and do not silently treat it as zero. Mark `insufficient_data`, cap at Maybe, and surface it in the UI.
- For links, the top-3 RD classifier can stay the same. But RD counts and `backlinks/RD` ratios differ by provider, so make `below_min_rd`, `rd_inflated`, and `sitewide_link_spike` thresholds provider-specific.
- Add tests using domains that have both AS and DR; assert that scores are directionally consistent after calibration.
- If you do not calibrate, present separate score profiles and do not rank Semrush-scored and Ahrefs-scored domains in one list without a provider label.

### 2.15 Detecting topic flips reliably

The prototype's own description is "first vs last snapshot term overlap + topic lexicon." That is a reasonable base, but it is noisy. A more reliable implementation:

1. **Use only substantive snapshots**
   - 200-OK HTML.
   - Exclude parked, for-sale, redirect, empty, JS-only, and error pages.
   - Require at least 2 substantive snapshots; prefer 3+.

2. **Classify each snapshot into topics**
   - Run the niche topic lexicon over title + body text.
   - Record the best niche label, the score, and the top terms.
   - If no niche matches, label as `other`.

3. **Compare the sequence, not just the endpoints**
   - Get the first and last non-empty topic labels.
   - If `first_topic != last_topic`, mark as a candidate flip.
   - A transient one-off snapshot in the middle should not automatically count; prefer a sequence like `A, A, B, B` over `A, B, A`.
   - If the sequence is `A, A, A`, do not flag even if term overlap is low.

4. **Compute term overlap**
   - Remove boilerplate, navigation, legal footers, and the domain's own name.
   - Use Jaccard or cosine similarity on top terms.
   - Low overlap supports a flip, but is not sufficient on its own.
   - In the observed data, `ridhihospital.com` had `first_last_similarity = 0.0` but all snapshots were classified `healthcare`, so it was not a topic flip. `careforpa.com` had `other -> healthcare -> healthcare -> healthcare`, so it was.

5. **Require evidence**
   - Store the first/last titles, topic labels, top terms, and overlap score.
   - Show the evidence in the UI so a human can verify.
   - If there is only one snapshot, emit `insufficient_coverage`, not `topic_flip`.

6. **Calibrate only if it becomes a gate**
   - Build a labeled set of known flips and non-flips.
   - Tune thresholds only when you are ready to use `topic_flip` for a verdict.
   - In the first implementation, treat it as an advisory signal and let the frontend render it.

A practical default:

```text
topic_flip =
  at least 2 substantive snapshots
  AND first_topic != last_topic
  AND the last topic is not a one-snapshot blip
```

Treat this as a starting point, not a universal constant. The prototype's hidden exact thresholds are not exposed.

#### Minimal high-volume implementation

Do not add embeddings, an LLM call, or extra snapshots for topic-flip detection. Reuse the snapshots you already fetched for relevance/history.

```text
for each domain:
    snaps = up to 4 substantive, chronological 200-OK HTML snapshots
    labels = []

    for snap in snaps:
        label, margin = classify_with_niche_lexicon(snap.title, snap.excerpt)
        labels.append(label)   # niche label or "other"

    if len(labels) < 2:
        signal = "insufficient_coverage"
        confidence = "none"
    else:
        first_topic = labels[0]
        last_topic = labels[-1]
        last_run_length = count trailing labels equal to last_topic

        flip = (first_topic != last_topic)
        stable = (len(labels) == 2) or (last_run_length >= 2)

        signal = "topic_flip" if flip and stable else "stable"
        confidence = "high" if len(labels) >= 3 and last_run_length >= 2 else "low"
```

Store the whole sequence, not just the boolean:

```json
{
  "topic_sequence": ["other", "healthcare", "healthcare", "healthcare"],
  "topic_labels": ["other", "healthcare", "healthcare", "healthcare"],
  "topic_scores": [0.1, 0.8, 0.9, 0.7],
  "topic_flip": true,
  "topic_flip_confidence": "high",
  "topic_flip_evidence": {
    "first_topic": "other",
    "last_topic": "healthcare",
    "last_run_length": 3,
    "snapshots_used": 4
  }
}
```

Why this is cheap enough for thousands of domains:

- It complicates only the domains that already survived Stage 0/1/2.
- It uses the same 4 snapshots and the same niche lexicon as relevance.
- It is O(4) per domain, with no extra network calls and no LLM.
- It stores evidence, so a later calibration pass can change the verdict rule without re-fetching.

Examples:

- `careforpa.com`: `other -> healthcare -> healthcare -> healthcare` → flip, high confidence.
- `ridhihospital.com`: `healthcare -> healthcare -> healthcare -> healthcare` → stable, despite low term overlap.
- A 2-snapshot `A -> B` result → flip signal, low confidence. The frontend can show it as “possible topic change” and the scoring layer does not have to gate on it.

---

## 3. Prefilter

The prototype does not vet every sourced listing. In the saved run:

```text
16,389 sourced -> 123 prefiltered -> 123 vetted
```

Observed behavior:

- The sourced list is sorted mainly by Semrush Authority Score.
- The vetted set looks like a per-niche top slice.
- Healthcare: 45, all AS >= 8.
- Travel: 33, all AS >= 8.
- PI / Legal: 45, AS down to 3, with low-AS names carrying `below_min_authority`.

This is consistent with:

- a per-niche quota around 45
- a normal minimum Semrush AS of 8
- a lower practical threshold for PI/Legal to fill the quota
- names below the authority minimum are still vetted, then flagged and capped rather than silently dropped

The method sheet states these config criteria:

- minimum referring domains: 20
- minimum DR: 15 when Ahrefs is available
- minimum Semrush AS: 8 meanwhile
- minimum archive history: 3 years

Treat those as **vetting criteria/flags**, not necessarily hard prefilter drops. The saved data includes names below AS 8, below 3 years of archive, and below 20 RDs, and those names carry flags.

---

## 4. Flags

Flags are the core of the judgment layer. They are more important than the raw score.

### 4.1 Hard-reject flags

```text
adult
gambling
pharma_spam
payday_spam
cjk_spam
foreign_takeover
```

Effect:

- verdict becomes `Reject` regardless of score
- history component appears to be forced to 0 for hard spam flags
- reason becomes a short hard-reject message, e.g. "Reject: history shows gambling (hard-reject flag)."

### 4.2 Cap flags

These do not necessarily reduce the numeric score. They limit the verdict to at most `Maybe`.

```text
weak_topic_match
cjk_pages
rd_inflated
sitewide_link_spike
toxic_anchors
below_min_rd
insufficient_coverage
off_topic_history
non_english_site
language_change
trademark_risk
short_history
topic_flip
spammy_backlinks
redirecting
below_min_authority
little_real_content
wayback_unavailable
no_archive_history
seo_spam
```

### 4.3 Warning / annotation flags

These are shown to the user as "Check first" warnings. They are not in the documented cap list.

```text
former_business_name
long_gap
thin_content
parked_once
parked_recent
```

Some of these still affect the history score (see penalties below). `former_business_name` and `long_gap` do not prevent a Buy in the observed data.

### 4.4 Derived flag

```text
price_carried_score
```

Meaning: the total score is Buy-level, but the non-price evidence is below the Buy evidence threshold. The low price alone pushed the name over 62 points. It is capped at Maybe.

### 4.5 Observed flag meanings

| Flag | Meaning | Verdict effect | History penalty |
|---|---|---|---:|
| `adult` | Adult content in archived pages | Hard Reject | forced to 0 |
| `gambling` | Casino/gambling content or URL slugs | Hard Reject | forced to 0 |
| `pharma_spam` | Pharma spam terms | Hard Reject | forced to 0 |
| `payday_spam` | Payday-loan spam | Hard Reject | forced to 0 |
| `cjk_spam` | CJK content/URLs on an English name (keyword hack) | Hard Reject | forced to 0 |
| `foreign_takeover` | Non-Latin-script content, likely foreign re-registration | Hard Reject | forced to 0 |
| `weak_topic_match` | Under 1/3 of real archived pages match the niche | Cap | none |
| `cjk_pages` | Some CJK-language URLs on an English site; bilingual or keyword hack | Cap | none |
| `rd_inflated` | Many referring domains but almost no authority (AS <= 3) | Cap | none |
| `sitewide_link_spike` | Backlinks far exceed referring domains (backlinks/RD > 50) | Cap | none |
| `toxic_anchors` | Money/toxic anchors | Cap | none |
| `below_min_rd` | Below the referring-domain minimum (20) | Cap | none |
| `insufficient_coverage` | Archive evidence incomplete | Cap | -0.10 |
| `off_topic_history` | Real archived content never matched the niche | Cap | none |
| `non_english_site` | Archived content was never English | Cap | -0.25 |
| `language_change` | Content language changed over time | Cap | -0.15 |
| `trademark_risk` | Contains a known brand/trademark term | Cap | none |
| `short_history` | Less than the minimum archive years (3) | Cap | -0.15 |
| `topic_flip` | Topic changed between first and last real snapshot | Cap | -0.25 |
| `spammy_backlinks` | 2+ top referring domains look spammy | Cap | none |
| `redirecting` | Mostly redirects in the last two archived years | Cap | -0.15 |
| `below_min_authority` | Semrush AS below 8 | Cap | none |
| `little_real_content` | Fewer than 6 months with live HTML archived | Cap | -0.15 |
| `wayback_unavailable` | Wayback lookup failed | Cap | none |
| `no_archive_history` | Never archived | Cap | -0.25 |
| `seo_spam` | Link-selling / SEO spam | Cap | none |
| `former_business_name` | Former business/firm name seen in archived titles | Warning; observed cap in PI/Legal in one case | none |
| `long_gap` | Long gap in archive history | Warning | -0.10 |
| `thin_content` | Most snapshots are thin pages | Warning | -0.10 |
| `parked_once` | Parked or near-empty in an earlier period | Warning | -0.08 |
| `parked_recent` | Most recent sampled snapshot is parked/for sale | Warning | -0.05 |
| `price_carried_score` | Buy-level score only because of low price | Cap | none |

### 4.6 Known inconsistency

`scottileslaw.com` is the clearest edge case:

- score 70.8
- evidence 60.8
- flags `long_gap`, `former_business_name`
- neither is in the documented cap list
- verdict is `Maybe`
- reason says "capped at Maybe by: former_business_name"

Meanwhile healthcare and travel Buys exist with `former_business_name`.

Interpretation:

- either the PI/Legal niche has an extra, undocumented cap for former business names
- or the verdict/reason is stale or buggy for that row

If reimplementing, decide explicitly whether `former_business_name` caps Buy. The safest general rule is: make cap sets configurable per niche, and add a test for this exact case.

---

## 5. Component scoring

All components are normalized to `0..1`. The weighted score is:

```text
score = 25*authority + 25*history + 20*links + 20*relevance + 10*price
```

Rounded to 1 decimal for display.

The score is out of 100, but links caps at 0.70, so the practical maximum is:

```text
25 + 25 + 14 + 20 + 10 = 94
```

### 5.1 Authority

Weight: 25.

```text
authority = min(Semrush AS, 25) / 25
authority_points = min(Semrush AS, 25)
```

So:

- AS 8 -> 8 points
- AS 16 -> 16 points
- AS 25 or higher -> 25 points

If Ahrefs DR is enabled in a future version, the method sheet implies DR would be used as the authority input instead, with a minimum DR of 15.

### 5.2 Relevance

Weight: 20.

```text
relevance = name_base + 0.45 * (niche_topic_snapshots / content_snapshots)
clamp 0..1
```

Where:

- `name_base = 0.55` for a primary niche term
- `name_base = 0.35` for a secondary/generic niche term
- if `content_snapshots == 0`, the on-topic ratio is treated as 0

The matched name terms come from `niche_hits`, e.g. `{"pi_legal": ["law"]}`.

Primary-term examples observed:

```text
hotel, clinic, dental, health, hospital, attorney, lawyer, medical,
rehab, resort, tour/travel, therapy, chiropractic
```

Secondary/generic examples observed:

```text
law, legal, lawoffice, wellness, care, eyecare, lawgroup,
workerscomp, destination, trip, holiday
```

Consequences:

- perfect “lawyer” name + 4/4 on-topic -> 1.0 -> 20/20
- perfect “law” name + 4/4 on-topic -> 0.8 -> 16/20
- “law” name + 0/0 on-topic -> 0.35 -> 7/20

### 5.3 History

Weight: 25.

This formula fits all 123 saved rows to rounding.

```text
base = min(1,
    0.25
  + 0.40 * min(archive_years, 10) / 10
  + 0.35 * min(months_with_live_HTML, 48) / 48
)

history = max(0, base - sum(history_penalties))
```

History penalties:

| Flag | Penalty |
|---|---:|
| `non_english_site` | -0.25 |
| `no_archive_history` | -0.25 |
| `topic_flip` | -0.25 |
| `redirecting` | -0.15 |
| `short_history` | -0.15 |
| `language_change` | -0.15 |
| `little_real_content` | -0.15 |
| `long_gap` | -0.10 |
| `insufficient_coverage` | -0.10 |
| `thin_content` | -0.10 |
| `parked_once` | -0.08 |
| `parked_recent` | -0.05 |

Penalties are additive. Hard-reject spam flags force history to 0.

Examples:

- 21.3 years archived, 80 live months, `long_gap` -> base 1.0, history 0.90 -> 22.5/25
- 12.4 years archived, 40 live months, `long_gap` -> base 0.9417, history 0.8417 -> 21.0/25
- 4.5 years archived, 25 live months, no flags -> base 0.6123 -> 15.3/25

### 5.4 Links

Weight: 20.

Observed main rule:

```text
links = clamp(
    0.55
  + 0.08 * trusted_RDs
  + 0.03 * citation_RDs
  - 0.22 * spam_RDs,
  0, 0.70
)
```

`trusted_RDs`, `citation_RDs`, and `spam_RDs` are counts among the **top 3 referring domains**.

Examples:

| Top 3 RDs | Component |
|---|---:|
| 0 trusted, 0 citation, 0 spam | 0.55 |
| 0 trusted, 1 citation, 0 spam | 0.58 |
| 0 trusted, 2 citation, 0 spam | 0.61 |
| 1 trusted, 0 citation, 0 spam | 0.63 |
| 2 trusted, 0 citation, 0 spam | 0.70 cap |
| 1 trusted, 1 citation, 1 spam | 0.44 |
| 0 trusted, 1 citation, 1 spam | 0.36 |
| 0 trusted, 0 citation, 1 spam | 0.33 |
| 0 trusted, 0 citation, 2 spam | 0.11 |
| 0 trusted, 0 citation, 3 spam | 0.00 |

Additional observations:

- `spammy_backlinks` fires at 2+ spam top RDs.
- `rd_inflated` and `sitewide_link_spike` can lower the component slightly.
- Very thin RD profiles below roughly 400 RDs show small extra reductions.
- Because of the 0.70 cap, links can contribute at most 14/20.

Examples of the extra adjustments:

- `sportmalawi.com`: 2 trusted would normally give 0.70, but `rd_inflated` gives 0.56.
- `aquminmedical.com`: 3 trusted gives 0.69 instead of 0.70 with `sitewide_link_spike`.
- `c4wellness.org`: 314 RDs, 1 spam gives 0.3113 instead of 0.33.
- `avmlegal.com`: 383 RDs, 1 trusted gives 0.6193 instead of 0.63.

Treat the clean formula as the baseline, then add a configurable thin/inflated-profile correction if exact parity matters.

### 5.5 Price

Weight: 10.

Observed mapping:

| Price | Component | Points |
|---:|---:|---:|
| `$1` | 1.00 | 10.0 |
| `$11` | 1.00 | 10.0 |
| `$15` | 1.00 | 10.0 |
| `$305` | 0.65 | 6.5 |
| `$360` | 0.65 | 6.5 |

The exact cutoff is not exposed. It lies between $15 and $305. The intent is clear: cheap opening bids get the full 10-point bonus; expensive names lose 3.5 points.

In the saved run, 117 of 123 names were at `$1`, so price was almost always full points.

---

## 6. Score and evidence

```text
score = sum(component * weight)
evidence_points = score - price_points
```

Where `price_points = 10 * price`.

`evidence_points` is therefore out of 90, because it excludes the 10 price points.

Example:

```text
photonhospitals.com
authority 0.40 * 25 = 10.00
history   0.4895 * 25 = 12.24
links     0.55 * 20 = 11.00
relevance 1.00 * 20 = 20.00
price     1.00 * 10 = 10.00
total = 63.24 -> 63.2
evidence = 53.24 -> 53.2
```

Because evidence is below 54, it is capped at Maybe with `price_carried_score`.

---

## 7. Verdict engine

Suggested exact order:

```text
if any hard_reject_flag:
    return Reject

if score < 45:
    return Reject

if score >= 62
   and evidence_points >= 54
   and no cap_flag:
    return Buy

return Maybe
```

A `price_carried_score` flag should be added before the cap check when:

```text
score >= 62 and evidence_points < 54
```

### Thresholds

| Setting | Value |
|---|---:|
| `maybe_min_score` | 45 |
| `buy_min_score` | 62 |
| `buy_min_evidence` | 54 |

### Hard-reject flags

```text
adult, gambling, pharma_spam, payday_spam, cjk_spam, foreign_takeover
```

### Cap flags

```text
weak_topic_match, cjk_pages, rd_inflated, sitewide_link_spike,
toxic_anchors, below_min_rd, insufficient_coverage, off_topic_history,
non_english_site, language_change, trademark_risk, short_history,
topic_flip, spammy_backlinks, redirecting, below_min_authority,
little_real_content, wayback_unavailable, no_archive_history, seo_spam
```

### 7.1 Phase 1 policy for our version: score-first, flags-advisory

The verdict engine above is **reference behavior from the prototype**. Do not copy it as a hard gate until the flag trigger thresholds and lexicons are calibrated.

If a flag's trigger is not known or not yet tested, it must not decide the verdict. Emit it as an advisory signal and let the frontend render it conditionally.

Recommended output shape:

```json
{
  "score": 70.8,
  "evidence_points": 60.8,
  "components": {
    "authority": 0.40,
    "history": 0.8417,
    "links": 0.69,
    "relevance": 0.80,
    "price": 1.00
  },
  "signals": [
    {
      "key": "former_business_name",
      "status": "heuristic",
      "gate_eligible": false,
      "confidence": "medium",
      "evidence": "Former firm name seen in archived titles"
    },
    {
      "key": "topic_flip",
      "status": "heuristic",
      "gate_eligible": false,
      "confidence": "high",
      "evidence": {
        "sequence": ["other", "healthcare", "healthcare", "healthcare"]
      }
    }
  ],
  "provisional_verdict": null
}
```

Rules:

- `score` and `components` are always emitted.
- Every flag carries `status`, `confidence`, and `evidence`.
- `gate_eligible` is `false` until the flag has a calibrated trigger and tests.
- The frontend can choose its own thresholds and labels.
- The backend may still emit a provisional verdict from known score thresholds, but it must be clearly marked provisional.
- Do not silently drop unknown flags. Store them as signals so they can be promoted to gates later without re-fetching evidence.
- Only promote a flag to a gate when its trigger is documented, configurable, and covered by tests.

### Important distinction: blocker vs cap

The `blocker` field shown in the UI is a human-readable "check first" warning. It is not necessarily the reason a name failed to Buy.

Examples:

- A Buy can show `Former business name: verify identity`.
- A high score can be Maybe because of `redirecting`, while its blocker text says something else.
- The reason string is the authoritative explanation.

This is why the UI can show:

```text
Buy 81.0   Check first: Former business name: verify identity
```

without contradicting itself: the flag is a manual-review warning, not a hard gate.

---

## 8. Reason and claims generation

The prototype generates a templated reason from the components and flags. It does not call an LLM.

### 8.1 `reason` and `reason_plain`

The reason contains:

1. verdict and score
2. the two strongest components
3. the weakest component
4. if capped: `Score is Buy-level but capped at Maybe by: <flags>.`
5. otherwise, when relevant: `Watch: <flags>.`

Example:

```text
Maybe (70.8): strongest history [12.4 yrs archived, 40 months with live HTML;
flags: long_gap] and relevance [name: law; archived pages on-topic 4/4];
weakest authority [Semrush AS 10 (est.)]. Score is Buy-level but capped at
Maybe by: former_business_name.
```

`reason_plain` is the same content with humanized flag labels.

Hard rejects use a short reason instead:

```text
Reject: history shows gambling (hard-reject flag).
```

### 8.2 Claims

The shortlist also exposes a `claims` array with the top 3 components:

```json
{
  "k": "history",
  "label": "Archive history",
  "pts": 21.0,
  "w": 25,
  "text": "12.4 yrs archived, 40 months with live HTML; flags: long_gap",
  "tone": "strong",
  "tab": "timeline"
}
```

- `pts` = component * weight
- `w` = weight
- `tone` = `strong` or `weak`
- `tab` = UI anchor (`score`, `evidence`, `timeline`)

This is useful for a UI that explains the score without exposing raw internals.

---

## 9. Worked examples

### 9.1 Buy

`nvsrehab.org`

```text
AS 16                         -> authority 0.64 -> 16.0
21.3 yrs, 80 live months      -> history 0.90   -> 22.5
3 trusted RDs                 -> links 0.70     -> 14.0
name rehab, 4/4 on-topic      -> relevance 1.00 -> 20.0
$1                            -> price 1.00     -> 10.0
total = 82.5
evidence = 72.5
verdict = Buy
```

### 9.2 Maybe due to redirecting

`boneandjointclinicbr.com`

```text
score = 83.2
evidence = 73.2
flags include redirecting
verdict = Maybe
```

A high score cannot buy past a cap flag.

### 9.3 Maybe due to price-carried score

`photonhospitals.com`

```text
score = 63.2
evidence = 53.2
flags = price_carried_score
verdict = Maybe
```

The score passes 62 only because price contributes 10 points. The non-price evidence is below 54.

### 9.4 Reject despite passing the score threshold

`medcare-healthclinic.com`

```text
score = 54.0
flags include pharma_spam
verdict = Reject
```

Hard-reject flags override the score.

### 9.5 The documented-cap inconsistency

`scottileslaw.com`

```text
score = 70.8
evidence = 60.8
flags = long_gap, former_business_name
verdict = Maybe
reason says capped by former_business_name
```

This does not match the XLSX's documented cap list. Treat it as a niche-specific cap or a bug to resolve during reimplementation.

---

## 10. Determinism audit

### Deterministic given frozen inputs

- authority normalization
- relevance formula
- history base and penalty arithmetic
- links baseline formula
- price step
- weighted total
- evidence total
- hard-reject override
- score thresholds
- cap-flag check
- reason template
- claims ordering

### Not deterministic / not stable across runs

- GoDaddy feed contents and prices
- Semrush values attached to the feed
- Wayback availability and capture set
- which 4 snapshots are sampled
- keyword/topic/spam lexicon matches
- prefilter ordering and quota
- possible niche-specific config not visible in the export
- one-off inconsistencies such as `scottileslaw.com`

### Not part of the evaluation

- browser local storage decisions (`watch`, `dismiss`, `max bid`)
- UI sorting/filtering
- the manual "before you bid" checklist links

### Conclusion

Yes: the score and verdict are deterministic rules. But do not describe the whole pipeline as reproducible unless the source snapshot and flag generation are frozen and versioned.

---

## 11. Reimplementation checklist

If infusing this mechanism into another version, preserve these separations:

1. **Source adapters**
   - GoDaddy/listing feed
   - Semrush metrics
   - Wayback CDX
   - Wayback snapshots
   - Common Crawl
   - top-RD classifier

2. **Raw evidence store**
   - store every metric with source and date
   - store the feed build timestamp and Wayback capture timestamps
   - store snapshot-level evidence, not just the final score

3. **Flag engine**
   - pure functions from evidence to flags
   - keep hard flags, cap flags, warnings, and history penalties separate
   - make cap sets configurable per niche
   - emit a stable flag vocabulary

4. **Scoring engine**
   - components normalized to `0..1`
   - weights in config
   - no UI logic inside the scorer
   - round only at display time
   - keep `score` and `evidence_points` separate

5. **Verdict engine**
   - hard flags first
   - score floor
   - Buy thresholds
   - cap check
   - derived `price_carried_score`
   - do not let cap flags silently change the numeric score; only change the verdict

6. **Reason renderer**
   - deterministic template
   - top 2 strong + weakest component
   - cap/watch flag text
   - optional `reason_plain` with humanized labels
   - optional claims array for UI

7. **Versioning**
   - `scoring_version`
   - `config_version`
   - `source_snapshot_id`
   - `feed_built_at`
   - `evaluated_at`

8. **Tests**
   - unit tests for each formula
   - golden tests for the worked examples above
   - a test for the `scottileslaw.com` cap inconsistency
   - a test that hard flags always win
   - a test that `price_carried_score` appears only when score >= 62 and evidence < 54
   - a test that cap flags do not lower the score by themselves
   - a test that links maxes at 14/20

9. **Observability**
   - persist the component vector and flag set for every verdict
   - store the evidence used for each flag
   - make it possible to recompute a verdict from the stored evidence without re-fetching external sources

---

## 12. Open questions to resolve before copying constants

- Exact price cutoff between `$15` and `$305`.
- Exact link adjustments for low RD counts, `rd_inflated`, and `sitewide_link_spike`.
- Whether `former_business_name` is a global cap, a PI/Legal-only cap, or a bug.
- Whether `thin_content`, `parked_once`, and `parked_recent` ever cap Buy on their own.
- Exact thresholds for `long_gap` (months), `insufficient_coverage` (captures/snapshots), and `topic_flip` (similarity/overlap).
- Exact prefilter ranking within each niche.
- Whether Ahrefs DR replaces Semrush AS in the authority component when a key is present.

Until those are resolved, treat the formulas and lists in this document as the strongest observed approximation, not as a byte-for-byte copy of hidden source code.

---

## 13. One-paragraph brief for an implementing agent

> Reimplement a deterministic domain evaluation pipeline with five weighted components: authority 25, history 25, links 20, relevance 20, price 10. Authority is `min(Semrush AS, 25)` points. Relevance is a name-term base (0.55 primary, 0.35 secondary) plus `0.45 * on-topic snapshot ratio`, out of 20. History is `0.25 + 0.40 * min(years,10)/10 + 0.35 * min(live_months,48)/48`, minus additive penalties for archive/spam/takeover flags, out of 25. Links is `0.55 + 0.08*trusted + 0.03*citation - 0.22*spam`, clamped to 0..0.70, out of 20. Price is 1.0 for cheap bids and 0.65 for expensive ones, out of 10. Compute `score` out of 100 and `evidence_points` out of 90 by excluding price. Hard flags (`adult`, `gambling`, `pharma_spam`, `payday_spam`, `cjk_spam`, `foreign_takeover`) force Reject. Otherwise Reject below 45, Buy at 62+ with 54+ evidence and no cap flag, else Maybe. Cap flags only limit the verdict, not the numeric score. Add `price_carried_score` when score >= 62 but evidence < 54. Render a deterministic reason from the top components and flags. Persist source dates, snapshots, flags, and a scoring version so results are reproducible from frozen evidence. Do not describe the pipeline as fully deterministic: the rules are deterministic, but the external evidence collection is not.

---

## Appendix A: Suggested staged pipeline for our version (not in the prototype)

This is a recommendation, not observed prototype behavior.

### Stage 0 - free/local gates

Run before spending any API credits:

- listing is active and not already ended
- deduplicate by domain
- TLD allowlist (for example `.com`, `.net`, `.org`, or niche-specific)
- domain length sanity check (for example 3-35 characters)
- limit hyphens/numbers (for example max 1 hyphen, max 1 number)
- exclude exact-match trademark/brand terms
- exclude obvious adult/gambling/pharma/payday tokens in the name
- exclude blacklisted domains
- price is within budget
- enough time remains before auction close (avoid last-minute bids)

These are cheap and deterministic. Keep them conservative: it is better to let a questionable name reach the next stage than to lose a good one.

**Stage 0 must be fully configurable.** Do not hardcode these values. Config should support global defaults, per-niche overrides, and a way to disable any gate.

Example config shape:

```yaml
prefilter:
  enabled: true
  require_active: true
  dedupe: true

  tld_allow: [com, net, org]
  length: { min: 3, max: 35 }
  hyphens: { max: 1 }
  numbers: { max: 1 }

  exclude_name_terms:
    - viagra
    - casino
    - payday
    - porn

  exclude_trademark_terms: []
  blacklist: []

  max_price_usd: 500
  min_hours_to_close: 6
  min_domain_age_years: 1

  niche_overrides:
    pi_legal:
      max_price_usd: 1000
    travel:
      tld_allow: [com, net, org, travel]
```

Rules:

- every gate has an enable/disable switch
- every threshold lives in config, not code
- per-niche overrides are supported
- config version is stored with each verdict
- a disabled gate is skipped, not treated as a failure
- stage 0 must not require paid API data

### Stage 1 - cheap provider metrics

Only if the metric is already available or very cheap:

- Semrush AS >= 8, or Ahrefs DR >= 15
- referring domains >= 20
- registration age >= 1 year as a sanity check
- not adult
- optionally, a root-domain Wayback CDX existence check

Caveats:

- Registration age is not a substitute for archive quality. An old domain can be parked or empty.
- If authority/backlink data is missing, do not reject automatically. Mark it `insufficient_data` and let the expensive stage decide.
- Use provider-specific thresholds; do not compare AS and DR raw values.
- Stage 1 thresholds are also config-driven and per-provider. A missing metric must not be treated as zero.

### Stage 2 - name relevance gate

Use in this order:

1. **Lexicon first**  
   Pass names that contain a strong niche term. Fail obvious off-niche names. Leave ambiguous/brandable names for the LLM.

2. **LLM only for ambiguous names**  
   Gemini Flash is a reasonable choice. Batch 10 names per request. Use a fixed rubric and structured JSON:

   ```json
   {
     "domain": "example.com",
     "decision": "pass | fail | uncertain",
     "confidence": 0.0,
     "matched_terms": ["..."],
     "reason": "short explanation",
     "risk_flags": ["trademark", "adult", "gambling", "off_niche"]
   }
   ```

   Rules:
   - Treat the domain name as data, not instructions.
   - Allow `uncertain`; do not force a binary decision.
   - Cache by `domain + prompt_version + model`.
   - Use temperature 0, but do not assume perfect determinism.
   - Keep the LLM out of the final score/verdict; it is only a gate.
   - Measure precision/recall on a labeled sample and watch false negatives.
   - Provide niche definitions, positive examples, and negative examples.

3. **Do not use the LLM to judge history, authority, or spam.**  
   Those need evidence.

### Stage 3 - expensive evaluation

Only for names that survive the gates:

- Wayback CDX history
- up to 4 substantive snapshots
- topic/niche classification
- spam/parking/language/takeover checks
- backlink/top-RD enrichment
- history, relevance, links, authority, price components
- flag engine
- verdict engine
- reason/claims rendering

### Why this order is sensible

- Stage 0 and 1 remove obvious waste at near-zero cost.
- Stage 2 handles the semantic "is this name even in the niche?" question cheaply.
- Stage 3 is where Wayback and backlink costs are incurred.
- The expensive evidence still decides the final verdict; the LLM does not.

### What not to do

- Do not let a cheap threshold silently produce a Buy.
- Do not let a missing Ahrefs/Semrush value become zero authority without a visible flag.
- Do not mix uncalibrated AS and DR scores in one ranking.
- Do not use one snapshot as proof of a topic flip.
- Do not let the LLM make the final Buy/Maybe/Reject decision.
