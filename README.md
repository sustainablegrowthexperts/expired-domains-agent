# Expired Domains Agent

Tooling for evaluating expired domains using Ahrefs data. This repository is a
Python 3.12 `src/`-layout project managed with [uv](https://docs.astral.sh/uv/).

## Layout

```text
.
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── opencode.json
├── task_runner.py
├── config/
│   ├── blocklist/          # disallowed domain categories (adult, gambling, ...)
│   ├── lexicon/            # anchor spam lexicon
│   ├── live/               # versioned Live check rules and thresholds
│   ├── niches/             # niche dictionaries for candidate uploads
│   ├── evaluation/         # versioned purchase-evaluation weights, flags, and lexicons
│   ├── prefilter/          # deterministic local prefilter gates
│   └── wayback/            # versioned Wayback scoring and thresholds
├── scripts/
│   └── run.sh              # enrich launcher with .env preflight checks
├── docs/
│   ├── reference/          # authoritative requirements and evaluation reports
│   └── archive/            # superseded requirement documents
├── reference/
│   ├── legacy/             # read-only endpoint reference; do not import
│   └── output/             # archived workbooks (gitignored)
├── specs/                  # change specifications
├── src/
│   └── expired_domains/
│       ├── __init__.py
│       ├── __main__.py
│       ├── blocklist.py    # deterministic disallowed-domain filter
│       ├── cli.py
│       ├── collection.py   # budgeted standard-tier Ahrefs collection
│       ├── config.py
│       ├── domains.py      # candidate CSV ingestion
│       ├── dr_check.py     # free Ahrefs Domain Rating stage
│       ├── evaluation.py   # deterministic purchase score, flags, verdict, optional LLM gate
│       ├── live_check.py   # DNS/HTTP live probe, landing classification, worker stage
│       ├── wayback.py      # Wayback CDX, snapshots, scoring, optional LLM
│       ├── niches.py       # lexicon-first niche categorisation
│       ├── prefilter.py    # deterministic local prefilter gates
│       ├── shortlist.py    # domain-discovery page renderer
│       ├── templates/      # shortlist HTML template
│       └── web/            # FastAPI app
├── tests/
│   ├── fixtures/
│   │   └── runs/           # local-only Ahrefs corpus (gitignored)
│   └── test_smoke.py
├── output/                 # generated shortlist HTML (gitignored)
└── data/                   # runtime payloads and SQLite index (gitignored)
```

## Setup

```bash
uv sync
cp .env.example .env   # then fill in real values
```

`uv sync` creates `.venv/` and the `uv.lock` lockfile.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `AHREFS_KEY` | Yes, for Ahrefs retrieval | Ahrefs API key. |
| `AHREFS_FREE_DR_CHECK` | Yes, for DR checks | Separate key for the free `public/domain-rating-free` endpoint. Leave blank to skip the DR stage. |
| `DR_FLOOR` | No | Minimum Ahrefs Domain Rating to pass the DR check. Default: `10`. |
| `DR_CHECK_MAX_ATTEMPTS` | No | Per-domain attempt budget before an API failure becomes terminal. Default: `3`. |
| `LIVE_CHECK_ENABLED` | No | Set to `0` to disable the automatic Live check stage and restore the DR-passed Wayback fallback. Default: on. |
| `LIVE_CHECK_MAX_ATTEMPTS` | No | Per-domain attempt budget before a Live check failure becomes terminal. Default: `3`. |
| `LIVE_CHECKS_PER_CYCLE` | No | Maximum domains checked per Live worker cycle. Default: `1`. |
| `LIVE_CHECK_TIMEOUT_SECONDS` | No | DNS/HTTP probe timeout in seconds. Default: `10`. |
| `LIVE_CHECK_MAX_REDIRECTS` | No | Redirects followed before the probe gives up. Default: `10`. |
| `LIVE_CHECK_MAX_BODY_BYTES` | No | Maximum landing-page bytes kept for content evaluation. Default: `50000`. |
| `WAYBACK_CHECKS_ENABLED` | No | Set to `0` to disable the automatic Wayback Machine stage. Default: on. |
| `WAYBACK_CHECK_MAX_ATTEMPTS` | No | Per-domain attempt budget before a Wayback failure becomes terminal. Default: `3`. |
| `WAYBACK_CHECKS_PER_CYCLE` | No | Maximum domains checked per Wayback worker cycle. Default: `1`. |
| `WAYBACK_RATE_PER_SEC` | No | Maximum Internet Archive requests per second. Default: `1`. |
| `WAYBACK_LLM_MODEL` | No | Model used only to resolve ambiguous Wayback content checks. Default: `google/gemini-3.5-flash`. |
| `EXPIRED_DOMAINS_DATA_ROOT` | No | Base data directory. Default: `<project_root>/data`. |
| `EXPIRED_DOMAINS_DB_PATH` | No | SQLite index path. Default: `<data_root>/index.sqlite3`. |
| `EXPIRED_DOMAINS_RAW_ROOT` | No | Raw Ahrefs payload directory. Default: `<data_root>/raw/ahrefs`. |
| `OPENROUTER_API_KEY` | Yes, for keyword and ambiguous Wayback checks | OpenRouter key used by the keyword-check stage and the Wayback LLM fallback; the niche LLM pass is currently stubbed. |
| `OPENROUTER_BASE_URL` | No | OpenRouter-compatible API base URL. Default: `https://openrouter.ai/api/v1`. |
| `NICHE_LLM_MODEL` | No | Model used for the niche pass. Default: `google/gemini-3.5-flash`. |
| `KEYWORD_CHECK_MODEL` | No | Model used to judge keyword reusability in batches of ten. Default: `google/gemini-2.5-flash-lite`. |
| `EVALUATION_LLM_MODEL` | No | Model used only for ambiguous name/topic relevance in the purchase evaluation. Default: `google/gemini-2.5-flash-lite`. |
| `EVALUATION_CHECKS_PER_CYCLE` | No | Maximum collected domains the automatic evaluation worker scores per cycle. Default: `5`. |
| `NICHE_WORKER_INTERVAL_SECONDS` | No | Seconds between automatic pending-niche scans. Default: `10`. |
| `DATA_COLLECTION_ENABLED` | No | Set to `1` to let the worker collect the Ahrefs standard tier for Live- and Wayback-passed domains. Default: off. |
| `DATA_COLLECTION_MONTHLY_UNITS` | No | Local monthly unit cap. Default: `800000`. |
| `DATA_COLLECTION_RESERVE_UNITS` | No | Units kept untouched at the end of the monthly allowance. Default: `50000`. |
| `DATA_COLLECTION_ESTIMATED_UNITS` | No | Estimated units per standard run used by the budget guard. Default: `3400`. |
| `DATA_COLLECTION_MAX_PER_CYCLE` | No | Maximum domains collected per worker cycle. Default: `1`. |

Real environment variables take precedence over values in `.env`. `.env` is
gitignored; only `.env.example` is committed. Configuration values are never
printed or logged.

> **Rotate exposed keys.** API keys have previously been shared in plaintext.
> Rotate `AHREFS_KEY` and `OPENROUTER_API_KEY` at their providers and update
> your local `.env` before using the tooling.

> **Agent note (AI assistants).** This repository ships `.env.example` with
> empty placeholders, and `.env` is gitignored. After cloning, do not run any
> retrieval command until `.env` exists with real values. **Ask the user for
> the required keys** (`AHREFS_KEY` is needed for any Ahrefs call) and
> **preferably create or update `.env` for them** — copy `.env.example` if the
> file is missing. Never invent or guess a key, never print or log secret
> values, and never commit `.env` or real values to version control. If the
> user prefers to paste the keys in themselves, create the file, give them the
> path, and wait.

## Usage

```bash
scripts/run.sh example.com                    # same as the enrich command below
uv run python -m expired_domains --help
uv run python -m expired_domains --version
uv run python -m expired_domains enrich example.com
uv run python -m expired_domains enrich example.com --force
uv run python -m expired_domains enrich example.com --db /tmp/index.sqlite3
```

`scripts/run.sh` is a thin wrapper: it checks that `uv` and `.env` with a
non-empty `AHREFS_KEY` exist, then runs `enrich "$@"`. It exits after one run —
there is no long-running dev server.

`enrich` makes the nine live Ahrefs requests for one domain and prints a
per-request summary followed by `run_id`, `raw_dir`, coverage, and unit totals.
It spends Ahrefs units and has no offline or dry-run mode.

- `--force` — fetch a new run even when a run already exists for today.
- `--db PATH` — write the SQLite index somewhere other than the configured path.

Import candidate-domain exports without spending Ahrefs units:

```bash
uv run python -m expired_domains import-domains reference/misc/spamzilla.csv
uv run python -m expired_domains import-domains \
  reference/misc/expireddomains.csv \
  reference/misc/gd_auctions_export_*.csv \
  reference/misc/spamzilla.csv
```

The source is detected from the CSV headers. Override it with `--source` (for
example `spamzilla`, `godaddy_auctions`, `expireddomains`, or a new label), and
set the collection date with `--collected-date YYYY-MM-DD` (defaults to today in
UTC). Re-importing the same export for the same source and date replaces its
payload instead of adding a duplicate.

On import, every domain whose niche is still `NULL` is queued for the
classifier. The deterministic blocklist in `config/blocklist/v1.json` runs
first: adult, gambling, pharma, payday-loan, crypto-scam, and
counterfeit/replica names are marked blocked, never sent to the LLM, and hidden
from the main discovery view. The remaining names check the versioned niche
dictionaries in `config/niches/v1.json` (medical, travel, legal/law, and
hospitality). A dictionary match is stored immediately. Names that match no
dictionary reach the LLM stage; that stage is currently stubbed and labels
every lexicon miss `STUB LLM NICHE`. The OpenRouter client remains available
for when the real pass is enabled again. Domains that already have a non-null
niche are skipped. The command prints how many pending domains it processed, a
per-niche count, and how many were blocked.

To apply the blocklist to domains that were imported or classified before the
dictionary existed, run it once against the index:

```bash
uv run python -m expired_domains blocklist
uv run python -m expired_domains blocklist --db /tmp/index.sqlite3
```

Blocked domains are terminal: they stay out of the pending queue. Re-running
the command scans only domains that are not blocked yet, so it is safe to run
after editing the dictionary. Add terms to a category's `terms` list, or add a
word to the top-level `allow_terms` list when a term causes a false positive.

The web app also starts automatic background workers. The main worker polls for
`NULL` niches, classifies them, runs the local prefilter over newly classified
domains, sends prefilter-passing keywords to the keyword-reusability LLM check,
and collects paid data when enabled. Separate Free DR, Live check, Wayback
Machine, and evaluation workers poll independently for keyword-passed,
DR-passed, Live-passed, and collected domains, so a slow batch does not hold up
the next queue. The evaluation worker picks up a domain as soon as its Ahrefs
run succeeds. Each worker sleeps between scans, so newly imported or collected
domains are picked up without another import or a manual command.
Run it standalone with `worker` if you are not serving the app:

```bash
uv run python -m expired_domains worker
uv run python -m expired_domains worker --once
uv run python -m expired_domains worker --interval 30 --db /tmp/index.sqlite3
```

An index created before niche support is upgraded automatically by the worker or
the next import. Its domains start with a `NULL` niche, so they show
`Pending classification` until the worker processes them.

### Local prefilter

The prefilter is the second evaluation stage, after discovery and niche
classification. It is deterministic, reads only stored observations, and spends
no Ahrefs units. The worker processes classified, unblocked domains whose
prefilter result is missing or was produced by an older config version:

```bash
uv run python -m expired_domains worker --once
```

The default gates live in `config/prefilter/v3.json`:

- registrable-name length from 3 to 35 characters, excluding the public suffix
- at most one hyphen
- at most one digit
- public-suffix allowlist restricted to `.com`, `.com.sg`, `.sg`, and `.org`
- known listing price at most `$500`
- at least 6 hours remaining when an auction deadline is known

Each gate has an `enabled` switch and can be overridden per niche under
`niche_overrides`; a disabled gate is skipped, not failed. A gate whose input is
missing (for example, no price on an ExpiredDomains row) is also skipped.
Domain age, Wayback checks, provider authority metrics, and duplicate
blocklist/name checks belong to later stages and are not part of this prefilter.

The verdict is stored on `domains` as `prefilter_pass`, `prefilter_reason`,
`prefilter_details_json`, `prefilter_config_version`, and
`prefilter_checked_at`. Bump the config `version` when the rules change so the
worker re-evaluates existing rows. The page adds an **Evaluation pipeline →
Passed pre-filter** view. It shows passed domains by default; the **Failed**
tab switches to failed domains, and reason chips filter which failure reasons
are shown. Both tabs include the known listing price and bid end date, with `—`
when the source did not provide them.

In the served web app, each failed row also has a **Pass** action. A manual pass
stores `prefilter_manual_pass=1`, changes the effective result to
`Manually passed`, and preserves the automated failure inside
`prefilter_details_json`. Manually passed domains are excluded from every later
prefilter run, including after a config version bump, so the filter cannot
re-fail them. The static `shortlist` file does not show the action because it
cannot write back.

### Keyword check

The keyword check is the third evaluation stage. The worker automatically
pulls every unblocked domain whose local prefilter passed and sends its keyword
to OpenRouter in batches of ten. The model, configured with
`KEYWORD_CHECK_MODEL`, decides whether the keyword is reusable: broad enough to
support another site or business, and free of trademark, brand, celebrity, or
overly narrow product/location specificity. The fixed rubric also returns a
confidence, a short reason, and risk flags such as `trademark`, `brand`,
`personal_name`, `too_specific`, or `location_specific`.

The verdict is stored on `domains` as `keyword_check_status`,
`keyword_check_pass`, `keyword_check_reason`, `keyword_check_details_json`,
`keyword_check_confidence`, `keyword_check_model`, and
`keyword_check_checked_at`. A batch is marked `processing` before the LLM call
and each completed batch is written immediately, so the page shows progress
instead of waiting for the whole backlog. The polling worker handles up to 25
batches per cycle, then sleeps before starting the next cycle, so newly
imported domains and recovered batches are not blocked behind a large backlog.
A keyword is rechecked when the configured model changes, and a batch left
`processing` after a crash or restart is retried once it is more than two
minutes old. Failed or malformed batches return to pending without blocking
other batches. The check never changes the prefilter verdict, and it is skipped
when `OPENROUTER_API_KEY` is unset.

The page adds an **Evaluation pipeline → Keyword check** view. Its segmented
control filters prefilter-passing keywords into **Passed**, **Failed**, and
**Processing** (queued or waiting for the LLM), with counts for each. Rows show
confidence, source, niche, price, and bid end date; the LLM reason appears as
an instant tooltip on the keyword-check result. The sidebar count next to
**Keyword check** is the number of passed keywords only.

### Free DR check

The Free DR check is the third pipeline step, between the keyword check and the
Wayback Machine Checks. A dedicated worker automatically pulls every keyword-passed,
unblocked domain that has no DR verdict and sends it to the free Ahrefs
`public/domain-rating-free` endpoint in batches of up to 1,000 targets. It uses
`AHREFS_FREE_DR_CHECK`, not `AHREFS_KEY`, and spends no paid units, so the
stage runs even when `DATA_COLLECTION_ENABLED=0`.

A domain passes when its `domain_rating` is at or above `DR_FLOOR`; a missing
rating is also a terminal failure. The verdict is stored on `domains` as
`dr_check_status`, `dr_check_pass`, `dr_rating`, `dr_check_reason`,
`dr_check_details_json`, `dr_check_attempts`, and `dr_check_checked_at`. A
batch is marked `processing` before the API call. A transient API failure
returns the domain to pending and counts the attempt; after
`DR_CHECK_MAX_ATTEMPTS` the domain becomes a terminal `failed` row instead of
being retried forever. A terminal pass or fail is never re-checked.

The page adds an **Evaluation pipeline → Free DR check** view. Its segmented control
filters keyword-passed domains into **Passed**, **Failed**, and **Processing**
(queued or waiting for the endpoint). Rows show the DR value, and the failure
reason appears as an instant tooltip. The sidebar count next to **Free DR check**
is the number of passed domains.

### Live check

The Live check is the fourth pipeline step, between the Free DR check and the
Wayback Machine Checks. A dedicated worker polls for unblocked DR-passed domains
that have no terminal Live verdict and probes the current domain. It resolves
A/AAAA records (MX records are deliberately excluded), requests the landing page
over HTTPS with an HTTP fallback, follows up to `LIVE_CHECK_MAX_REDIRECTS`
redirects, enforces `LIVE_CHECK_TIMEOUT_SECONDS`, and keeps roughly the first
`LIVE_CHECK_MAX_BODY_BYTES` of the response.

The deterministic verdict uses `config/live/v1.json`:

- `nxdomain` (no A/AAAA records), `no_http` (no usable response), and `parked`
  (parking or for-sale signatures) are **neutral passes**. Dropped domains
  often look like this and may still be available.
- A final host with a different registrable root from the starting host is
  `redirect_offsite` and fails unconditionally. Scheme, port, `www`, and other
  subdomains do not change the root.
- `content` fails when the final host matches the toxic lexicon, the `<title>`
  matches it, or the visible text contains at least `content_min_term_hits`
  distinct toxic terms. The toxic categories and parking terms are the same
  vocabulary used by the Wayback stage.

The verdict is stored on `domains` as `live_check_status`, `live_check_pass`,
`live_check_class`, `live_check_reason`, `live_check_details_json`,
`live_check_attempts`, `live_check_checked_at`, `live_check_manual_pass`,
`live_check_manual_pass_at`, `live_check_final_host`, `live_check_http_status`,
and `live_check_config_version`. Full DNS, request, redirect, and body evidence
is written under `data/raw/live/{domain}/{run_id}/` and is also embedded in the
details blob. Transient probe failures return the domain to pending and count
the attempt; after `LIVE_CHECK_MAX_ATTEMPTS` the domain becomes terminal. The
first enabled run records a `live_checks_started_at` checkpoint, so existing
DR-passed domains are not backfilled. Disabling the stage with
`LIVE_CHECK_ENABLED=0` makes Wayback fall back to its previous DR-passed
behaviour.

The page adds an **Evaluation Pipeline → Live check** view with **Passed**,
**Failed**, and **Processing** filters. Rows show the result, class, final host,
HTTP status, attempts, and the reason as an instant tooltip. The domain detail
modal lists DNS addresses, the redirect chain, final host, title, excerpt, and
toxic terms. In the served web app, failed Live rows have a **Pass** action. A
manual pass sets `live_check_manual_pass=1`, changes the effective result to
`Manually passed`, sets the class to `manual`, preserves the automated failure
inside `live_check_details_json`, and unblocks Wayback.

### Wayback Machine Checks

The Wayback stage is the fifth pipeline step, between the Live check and paid
data collection. A dedicated worker polls for unblocked domains that passed the
DR check and, when Live is enabled, the Live check. It selects eligible domains
that have no terminal Wayback verdict and fetches the first two and last two
200-OK `text/html` captures from the Internet Archive CDX API. It stores the raw
CDX rows and fetched HTML under `data/raw/wayback/{domain}/{run_id}/`.

Every delivered verdict is a **pass** or **fail**, with a reason and the layer
that decided it: `lexicon` or `llm`. The stage scores twelve checks out of 100
using `config/wayback/v1.json`; a score below `50` fails. Five checks are
critical and force a failure regardless of score: toxic content, repurposing,
archive existence, substantive snapshots, and recent snapshot quality. The
remaining scored checks cover archive span, live HTML coverage, content depth,
language stability, topic stability, parking history, and redirect history.

The deterministic pass reuses the existing blocklist, anchor lexicon, and niche
dictionaries against snapshot titles and text. If a weak toxic signal, a
language change, or a topic change cannot be resolved deterministically, the
worker sends only the extracted snapshot fields to `WAYBACK_LLM_MODEL` through
OpenRouter. The model can resolve the ambiguous checks but cannot override a
clear lexicon failure. Without `OPENROUTER_API_KEY`, ambiguous cases fail rather
than pass silently.

The verdict is stored on `domains` as `wayback_check_status`,
`wayback_check_pass`, `wayback_score`, `wayback_check_reason`,
`wayback_check_source`, `wayback_check_details_json`, `wayback_check_attempts`,
`wayback_check_checked_at`, `wayback_first_capture`, `wayback_last_capture`,
`wayback_age_years`, `wayback_capture_count`, and `wayback_config_version`.
Transient archive failures return the domain to pending and count the attempt;
after `WAYBACK_CHECK_MAX_ATTEMPTS` the domain becomes terminal. The first enabled run
records a `wayback_checks_started_at` checkpoint. When Live is enabled, the
stage pulls only `live_check_pass = 1` rows and compares the checkpoint with
`live_check_checked_at`; when Live is disabled it falls back to the DR-passed
queue and `dr_check_checked_at`. Setting `WAYBACK_CHECKS_ENABLED=0` also removes
the Wayback gate from data collection.

The page adds an **Evaluation Pipeline → Wayback Machine Checks** view with
**Passed**, **Failed**, **Connection failed**, and **Processing** filters.
**Connection failed** isolates failed rows with a zero score, normally archive
or network failures. Rows show the pass/fail result, weighted score, archive
age, deciding source, and the reason as an instant tooltip. The domain detail
modal lists every check and sampled snapshot.

In the served web app, failed Wayback rows have a **Pass** action. A manual pass
stores `wayback_check_manual_pass=1`, changes the effective result to
`Manually passed`, sets the deciding source to `manual`, and preserves the
automated score, source, and reason inside `wayback_check_details_json`.
Manually passed domains are excluded from later Wayback runs, including after a
config change, and become eligible for data collection. The static `shortlist`
file does not show the action because it cannot write back.

### Data collection

The next evaluation stage retrieves the v3.1 standard-tier Ahrefs dataset for
domains that passed the prefilter, the keyword check, the Free DR check, the
Live check, and the Wayback Machine checks. It is the paid stage, so it is opt-in:
the worker only collects when
`DATA_COLLECTION_ENABLED=1` and `AHREFS_KEY` is set. With the default
`DATA_COLLECTION_MONTHLY_UNITS=800000` and `DATA_COLLECTION_RESERVE_UNITS=50000`,
the worker may spend 750,000 units per calendar month — roughly 220 standard
runs at ~3,400 units each. It stops before touching the reserve and never starts
a run when fewer than `DATA_COLLECTION_ESTIMATED_UNITS` remain.

The worker ranks eligible domains by keyword confidence (highest first), then
listing price (lowest first), then domain name. A domain is eligible when it is
unblocked, passed the prefilter, keyword check, Free DR check, Live check, and
Wayback checks, has no successful run yet, and has not already been attempted
today. The Live and Wayback passes are what separate the data-collection queue
from the earlier queues. Failed runs are retried
on a later day, not in the same cycle. A collected domain is terminal for the
worker; use `uv run python -m expired_domains enrich DOMAIN --force` to refresh
one manually.

Each run executes the nine standard-tier requests from
`docs/reference/ahrefs-ingestion-spec-v3.md` §4: limits before/after, one
`backlinks-stats` call, 48 months of refdomain and organic-traffic history, the
top 70 and latest 30 live backlinks, the batched all-time lexicon anchor search,
and the top 50 live anchors. Raw payloads and headers are written under
`data/raw/ahrefs/{domain}/{run_id}/`; the structured rows go to `runs`,
`requests`, `stats`, `history`, `backlinks`, and `anchors`.

The page adds an **Evaluation pipeline → Data collection** view. Its segmented
control filters DR-, Live-, and Wayback-passed domains into **Queued**, **Collected**,
and **Failed**, with counts for each. Rows show the latest run time, coverage mode,
live links, live referring domains, actual units spent, and successful/total
requests; a failed run's error appears as an instant tooltip. Clicking a domain
opens a detail modal with the stored Ahrefs evidence: live and all-time stats,
referring-domain history, top anchors, strongest backlinks, the request audit
trail, and the marketplace observation that supplied the price and auction
date. The detail payload is embedded in both the served page and the generated
static file, so the modal works without the server. The sidebar count next to
**Data collection** is the number of collected domains, and the sidebar foot
reports this month's unit spend against the allowance.

Run the whole pipeline once, including collection when enabled:

```bash
DATA_COLLECTION_ENABLED=1 uv run python -m expired_domains worker --once
```

### Purchase evaluation

The purchase evaluation is the final, read-only stage. It applies the mechanism
in `docs/prototype-evaluation-mechanism.md` to the stored evidence and writes a
verdict to the `domains` row. A dedicated evaluation worker polls domains that
have a successful Ahrefs run and no current evaluation, so newly collected
domains are scored automatically. The CLI and API remain available for manual
re-runs:

```bash
uv run python -m expired_domains evaluate
uv run python -m expired_domains evaluate caseconverter.com ecochem.com
uv run python -m expired_domains evaluate --json --no-store caseconverter.com
```

It scores five normalised components with the documented weights — authority
25, history 25, links 20, relevance 20, price 10 — and derives
`score` (0–100) and `evidence_points` (0–90, excluding price). The deterministic
verdict engine hard-rejects on `adult`, `gambling`, `pharma_spam`,
`payday_spam`, `cjk_spam`, or `foreign_takeover`, rejects below `45`, buys at
`62+` with `54+` evidence and no gate-eligible cap flag, and otherwise returns
`Maybe`. Cap flags never change the numeric score. A `price_carried_score`
signal is added when the score reaches 62 only because of the price component.

The default config lives in `config/evaluation/v1.json` and includes the link
classifier seeds, the anchor-spam vocabulary, the history penalties, the
provider-specific Ahrefs-DR authority profile, and the price breakpoints. Every
signal carries its evidence, status, confidence, and `gate_eligible` value.
`--advisory-only` keeps cap flags advisory (the prototype's phase-1 policy) and
the recommendation becomes `Hold for manual review` when a high-severity flag
would otherwise be ignored.

The LLM is optional and deliberately narrow. `--llm` sends only the domain name
and substantive snapshot titles/excerpts to `EVALUATION_LLM_MODEL`; the model
returns a name class, per-snapshot on-topic booleans, a confidence, and a short
reason. The model never sees or sets authority, history, links, spam, or the
final verdict. Without `--llm`, the evaluator uses the configured niche when
available and a conservative deterministic name/topic fallback otherwise.

Stored fields are `evaluation_status`, `evaluation_score`,
`evaluation_evidence_points`, `evaluation_verdict`,
`evaluation_recommendation`, `evaluation_reason`, `evaluation_details_json`,
`evaluation_config_version`, and `evaluation_checked_at`. The same payload is
available at `GET /api/evaluation/{domain}` (add `?refresh=true` to recompute),
and the full component/flag vector is in `evaluation_details_json`.

The page adds **Evaluation pipeline → Evaluated Domains**. Its **Passed** tab
contains `Buy` and `Maybe` verdicts (survived the rejection gate; Maybe still
needs manual review), and **Failed** contains `Reject`. Rows show the verdict,
score, evidence points, recommendation, and the reason as an instant tooltip.
The sidebar count is the number of passed domains, and the live dot shows how
many collected domains are waiting for the evaluation worker.

All seven pipeline tabs show a small live dot while their stage has queued work:
the **Passed pre-filter** dot tracks domains still waiting for a prefilter
verdict, the **Keyword check** dot tracks pending plus in-flight keywords, the
**Free DR check** dot tracks pending plus in-flight DR checks, the **Live check**
dot tracks pending plus in-flight probes, the **Wayback Machine Checks** dot
tracks pending plus in-flight archive checks, the **Data collection** dot
tracks eligible domains still waiting for an Ahrefs run, and the **Evaluated
Domains** dot tracks collected domains waiting for the evaluation worker.
The dot pulses while work is actively processing, stays static while it is only
queued or collection is paused, and is hidden when the queue is empty.

Build the domain-discovery shortlist from the imported candidates:

```bash
uv run python -m expired_domains shortlist
uv run python -m expired_domains shortlist --db /tmp/index.sqlite3 \
  --output /tmp/shortlist.html
```

The command writes a self-contained HTML page (default:
`output/shortlist.html`) with the three discovery sources, search, source and
niche views, sorting, and pagination. It reads the SQLite index only, so it
spends no Ahrefs units. The page has one row per registrable domain: the latest
observation wins when a domain appears in more than one export. The niche column
shows `Pending classification` until the domain has been classified, then the
stored niche or `llm - unknown` when no confident label was available. The
progress column mirrors the same state as `Pending classification`,
`Classified`, `Passed pre-filter`, or `Failed pre-filter`.

Both the generated page and the served app load the SGE global navigation rail
from `https://sidebar.sustainable-growth-experts.com/` (`sge-sidebar.js` plus
`menu.json`). It reserves a 64 px column on desktop, becomes a fixed bottom bar
below 768 px, and follows the page's `prefers-color-scheme`. No API key or
credentials are sent. If the host is unreachable, the shortlist still renders
with an empty rail column.

Blocked domains are hidden from the main discovery view. A separate `Blocked`
view in the sidebar lists them with their blocklist category, matched terms,
and source so the rejection can be audited. The `/api/discovery` response keeps
them in a separate `blocked_rows` list with `blocked_count`, while `rows` and
`count` contain only the main view.

### Web app

Run the FastAPI app against the configured index:

```bash
uv run python -m expired_domains serve
uv run python -m expired_domains serve --host 0.0.0.0 --port 8000 \
  --db /tmp/index.sqlite3
uv run python -m expired_domains serve --no-worker
```

`serve` starts the pending-niche worker automatically. `--no-worker` disables
the worker when you only want the views and manual-pass action.

Or use the dev launcher, which picks a random unused port, enables auto-reload,
and prints the URL:

```bash
scripts/serve.sh
scripts/serve.sh --db /tmp/index.sqlite3
scripts/serve.sh --host 0.0.0.0 --port 8000 --no-reload
```

| Route | Purpose |
|---|---|
| `GET /` | Domain-discovery view. |
| `GET /shortlist` | Same view at an explicit path. |
| `GET /api/discovery` | Discovery rows and metadata as JSON; accepts `source` and `q` filters. |
| `GET /api/domains/{domain}` | Stored observation and Ahrefs evidence for one domain. |
| `GET /api/evaluation/{domain}` | Deterministic purchase evaluation; `?refresh=true` recomputes it. |
| `POST /api/prefilter/{domain}/pass` | Manually pass a failed prefilter result. |
| `POST /api/live/{domain}/pass` | Manually pass a failed Live check result. |
| `POST /api/wayback/{domain}/pass` | Manually pass a failed Wayback result. |
| `GET /healthz` | Liveness check plus whether the index exists. |
| `GET /docs` | FastAPI's generated OpenAPI documentation. |

For development with reload, run the app factory through uvicorn directly
(the automatic worker still starts through the app lifespan):

```bash
uv run uvicorn expired_domains.web.app:app --reload
```

The web app serves the discovery, prefilter, keyword-check, DR-check, Live,
Wayback, data-collection, and evaluated-domains views. Its background workers
write niche, automated prefilter, keyword-check, DR-check, Live, Wayback,
purchase-evaluation, and (when enabled) standard-tier Ahrefs collection
results, and the manual-pass endpoints write prefilter, Live, and Wayback
overrides back to `domains`. The static `shortlist` command remains available
for generating a file you can open or share without a server.

## Where results are written

Ahrefs runs and Wayback checks write to local, gitignored locations under
`<project_root>/data/`:

```text
data/
├── index.sqlite3                       # SQLite index (queryable rows)
└── raw/
    ├── ahrefs/
    │   └── gardenofdelights.com/
    │       └── gardenofdelights.com_20261125T103541Z_ahrefs/
    │           ├── manifest.json       # run metadata + per-request manifest
    │           ├── stats.p0.json       # untouched provider payloads
    │           ├── stats.p0.headers.json
    │           └── ...                 # one .json + .headers.json per request
    ├── live/
    │   └── gardenofdelights.com/
    │       └── live_20261127T120000000000Z/
    │           ├── dns.json            # A/AAAA lookup evidence
    │           ├── http.json           # attempts, final URL, title, excerpt
    │           ├── redirects.json      # followed redirect chain
    │           └── body.html           # capped landing-page body
    └── wayback/
        └── gardenofdelights.com/
            └── wayback_20261127T120000000000Z/
                ├── cdx.json            # CDX capture rows
                └── snapshot_01_....json  # fetched archive page + extracted fields
```

**Raw payloads** — `<raw_root>/<domain>/<run_id>/`, one directory per run.
Every request's response is stored verbatim alongside its headers; nothing is
compressed or rewritten, so runs can be re-parsed later without spending units.
Runs are never overwritten — each one gets its own timestamped directory.

**SQLite index** — `<data_root>/index.sqlite3`. Run-scoped tables are keyed by
`run_id` so multiple runs and domains can coexist:

| Table | Contents |
|---|---|
| `domains` | One canonical row per registrable domain, with first/last seen dates, niche, niche provenance, blocklist verdict, the local prefilter verdict including a manual-pass override, the keyword-reusability status/verdict, the free DR status/rating/attempts, the Live check class/final-host/HTTP-status/attempts/manual-pass override, the Wayback pass/fail score, source, captures, attempts, and manual-pass override, and the purchase-evaluation score/evidence/verdict/recommendation plus the full component and flag payload. |
| `pipeline_meta` | Small key/value checkpoints such as `live_checks_started_at` and `wayback_checks_started_at`, used to avoid backfilling existing queues when a worker stage is enabled. |
| `domain_observations` | One row per `(domain, source, collected_date)`; canonical metrics in `metrics_json`, verbatim export columns in `raw_json`. |
| `runs` | One row per run: domain, mode, timestamps, drop date/source, config, coverage, units. |
| `requests` | One audit row per HTTP call: status, params, row count, units, raw path. |
| `stats` | Live and all-time link/referring-domain snapshots. |
| `history` | Monthly referring-domain and organic-traffic series. |
| `backlinks` | Deduplicated backlink rows (URLs, anchor, rel, rating, raw JSON). |
| `anchors` | Top and lexicon-matched anchors with local classifications. |

Candidate domains are normalised before they are stored: schemes, paths, ports,
credentials, a leading `www.`, surrounding dots, and letter case are removed,
then the registrable domain is taken using the bundled public-suffix list.
Only the domain, source, and collection date are first-class columns.

Every observation also stores two JSON blobs:

- `metrics_json` uses one canonical key set for every source, so consumers can
  read `price`, `bid_count`, `auction_end_at`, `backlinks`,
  `referring_domains`, `majestic_referring_domains`,
  `semrush_authority_score`, `domain_age_years`, and so on without branching.
  Metrics a source did not provide are `null` (or `[]` for list fields).
  `backlinks` and `referring_domains` are the source's headline counts; the
  provider-specific Majestic, Moz, and Semrush keys are stored alongside them.
- `raw_json` preserves the remaining export columns verbatim for provenance
  and future remapping.

The canonical vocabulary lives in `expired_domains.domains.METRIC_DEFAULTS`.
Metrics are queryable in SQL too:

```sql
SELECT domain, json_extract(metrics_json, '$.price')
FROM domain_observations
WHERE source = 'godaddy_auctions';
```

Each successful run prints its `run_id` and absolute `raw_dir`, so results can
always be located without guessing. Override the defaults with
`EXPIRED_DOMAINS_DATA_ROOT`, `EXPIRED_DOMAINS_RAW_ROOT`, or
`EXPIRED_DOMAINS_DB_PATH`; `--db` overrides the index path for one run.

## Reference material

- `docs/reference/` holds the authoritative requirements document and the
  evaluation reports that informed it.
- `docs/archive/` keeps older requirement versions for historical context.
- `reference/legacy/` contains the original exploratory scripts. They document
  Ahrefs endpoints only, are read-only, and are **not** part of the package.
- `tests/fixtures/runs/` holds the zero-cost Ahrefs test corpus (5 domains, 7
  runs). It is local-only and gitignored; tests that need it skip when absent.

## Testing and linting

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
```

## Spec → task workflow

1. Discuss and write a change specification under `specs/`.
2. Break the spec into vertical-slice task files under `.tasks/`.
3. Run tasks with `task_runner.py`.
4. Verify with `uv run pytest` and update the spec/task status as work lands.
