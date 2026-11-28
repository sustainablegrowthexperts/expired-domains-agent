# Add the purchase evaluation stage

**Status:** implemented
**Date:** 2026-11-27
**Docs basis:** `docs/prototype-evaluation-mechanism.md`,
`docs/reference/smoke-fire-evaluation-report.md`,
`docs/reference/ahrefs-ingestion-spec-v3.md`

## Objective

Turn the stored evidence for a collected domain into a reproducible
`Buy` / `Maybe` / `Reject` purchase recommendation. The rules, score, flags,
and reason are deterministic functions of frozen evidence. An optional LLM may
review the collected evidence for domain health and acquisition risk; it may
not score authority, history, links, or price, and it may not make the final
verdict.

## Scope

In scope:

- Five normalised components with the documented weights: authority 25,
  history 25, links 20, relevance 20, price 10.
- `score` out of 100 and `evidence_points` out of 90 (score excluding price).
- Hard-reject, cap, and warning flags with evidence, confidence, and
  `gate_eligible`.
- Deterministic verdict engine, `price_carried_score`, and a templated reason.
- Optional OpenRouter health review over the collected Ahrefs, Wayback,
  backlink, anchor, and Live evidence.
- Persistence on `domains`, a CLI command, and an API endpoint.
- An automatic `EvaluationWorker` that queues domains with successful Ahrefs
  runs and processes them independently of collection.
- An **Evaluated Domains** pipeline view with **Passed** and **Failed** tabs.
- Versioned config in `config/evaluation/v2.json`.

Out of scope:

- New external data collection. The stage reads the local index only.
- UI rendering beyond the API payload.
- Recalibrating the prototype constants against a labelled purchase sample.
- Replacing the existing keyword-check or Wayback LLM passes.

## Architecture

```text
domains / runs / stats / history / backlinks / anchors / wayback / live
                              |
                              v
                    evaluation.evaluate_domain
                              |
        +---------------------+---------------------+
        |                     |                     |
        v                     v                     v
  component builders     flag engine          optional LLM review
  (authority, history,   (hard/cap/warning,   (health label + structured
   links, relevance,      evidence, versions)  findings with evidence)
   price)
        |                     |                     |
        +---------------------+---------------------+
                              v
                   score / evidence / verdict / reason
                              |
                              v
              domains.evaluation_* + API + CLI output
```

### Components

- **Authority**: Ahrefs DR profile, `min(DR, 25) / 25`, with `min_dr = 15`.
  Missing DR is not scored as zero; it caps the verdict and emits
  `authority_missing`.
- **History**: `0.25 + 0.40 * min(years, 10)/10 + 0.35 * min(live_months, 48)/48`,
  minus additive penalties from the configured history flags. No Wayback
  evidence yields `0` history, `insufficient_coverage`, `wayback_unavailable`,
  `no_archive_history`, `short_history`, and `little_real_content`.
- **Links**: top-3 referring domains classified locally as trusted, citation,
  neutral, or spam; `0.55 + 0.08*trusted + 0.03*citation - 0.22*spam`, clamped
  to `0..0.70`. Emits `spammy_backlinks`, `rd_inflated`, `below_min_rd`, and
  `sitewide_link_spike`. A whole-profile anchor rule emits `seo_spam` and
  `toxic_anchors`.
- **Relevance**: `name_base + 0.45 * on_topic_ratio`, with name bases 0.55
  (primary), 0.35 (secondary/generic), 0.0 (off-topic). The configured niche is
  used when available. Unknown niches fall back to a conservative name/token
  match and emit `relevance_unresolved`; the evaluation LLM does not infer a
  niche or judge topical relevance.
- **Price**: full component at or below `$15`, reduced component `0.65` at or
  above `$305`, linear between. A missing price is `null`, not zero, and caps
  the verdict at Maybe.

### Flags

- Hard reject: `adult`, `gambling`, `pharma_spam`, `payday_spam`, `cjk_spam`,
  `foreign_takeover`.
- Cap: the prototype's documented cap vocabulary, with per-flag triggers.
- Warning: `former_business_name`, `long_gap`, `thin_content`, `parked_once`,
  `parked_recent`, and missing-evidence warnings.
- `price_carried_score` is added when `score >= 62` and
  `evidence_points < 54`.
- A flag only changes the verdict when `gate_eligible` is true. The config can
  switch cap flags to advisory mode (`--advisory-only`).

### LLM boundary

The optional health-review model receives the collected evidence: Ahrefs
stats, history, backlinks, and top/lexicon anchors; Wayback checks and
snapshots; the Live check; and the listing source/date. It returns:

```json
{
  "health": "risky",
  "confidence": 0.93,
  "summary": "The anchor and referring-domain profile looks like a paid-link network.",
  "findings": [
    {
      "key": "paid_link_network",
      "severity": "high",
      "confidence": 0.91,
      "evidence": "Most top anchors are link-selling phrases."
    }
  ]
}
```

Only high-confidence, well-formed responses are accepted. Findings become
`llm_*` signals with evidence, severity, and confidence. They never change the
numeric score or the deterministic verdict. By default they are advisory: a
high-severity finding turns a would-be Buy into `Hold for manual review`.
`flag_policy.gate_llm_findings` can promote findings at or above
`llm_gate_min_severity` to cap flags.

## Files

- `config/evaluation/v2.json` — weights, thresholds, lexicons, price
  breakpoints, flag sets, gate eligibility, and LLM review limits.
- `src/expired_domains/evaluation.py` — loaders, component builders, flag
  engine, verdict engine, reason renderer, storage, and relevance LLM client.
- `src/expired_domains/storage/schema.sql` — evaluation columns.
- `src/expired_domains/storage/db.py` — migration for the new columns.
- `src/expired_domains/worker.py` — `EvaluationWorker` and `evaluate_pending`.
- `src/expired_domains/cli.py` — `evaluate` command and worker integration.
- `src/expired_domains/shortlist.py` / `templates/shortlist.html` — the
  **Evaluated Domains** view and its **Passed** / **Failed** tabs.
- `src/expired_domains/web/app.py` — `GET /api/evaluation/{domain}` and the
  automatic evaluation worker.
- `src/expired_domains/config.py` / `.env.example` — `EVALUATION_LLM_MODEL`.
- `tests/test_evaluation.py` — TDD coverage.

## TDD execution plan

1. **RED** — add tests for word-boundary anchor classification, referring-domain
   classification, the happy-path Buy, the SEO-spam cap, the hard-reject
   override, missing price, missing Wayback, the evidence-out-of-90 calculation,
   persistence, LLM health-review response validation, advisory findings, and
   the optional LLM gate.
2. **GREEN** — implement the config loader, component builders, flag engine,
   verdict engine, storage, CLI, and API until the tests pass.
3. **REFACTOR** — deduplicate evidence parsing, keep the LLM interface narrow,
   and ensure every flag carries evidence.

## Acceptance criteria

- `uv run pytest` passes.
- `uv run ruff check src tests` passes.
- `uv run python -m expired_domains evaluate --no-store` produces a verdict for
  every domain with a successful Ahrefs run.
- The evaluation worker evaluates a newly collected domain without a manual
  command and does not re-evaluate it under the same config version.
- The **Evaluated Domains** view shows **Passed** (Buy + Maybe) and **Failed**
  (Reject) tabs, with the verdict, score, evidence, and recommendation.
- The four reference domains produce:
  - `caseconverter.com` — Maybe, cap `seo_spam`, score around 86.
  - `ecochem.com` — Maybe, cap `seo_spam`, missing price, score around 84.
  - `clicker.org` — Reject, low authority/history, spam links.
  - `mapo-naru.com` — Reject, low authority, incomplete archive, spam links.
- A hard-reject flag always wins over a high score.
- Cap flags never change the numeric score by themselves.
- `evidence_points` is out of 90 and equals `score - price_points` when all
  components are present.
- LLM findings are advisory by default and turn a would-be Buy into
  `Hold for manual review`; they cap the verdict only when
  `flag_policy.gate_llm_findings` is enabled.
- The evaluation LLM receives no niche or relevance question; relevance stays
  deterministic and reports `relevance_unresolved` when the niche is missing.

## Open calibration items

- Exact price cutoff between `$15` and `$305`.
- Whether Ahrefs DR should be calibrated to a Semrush-AS-equivalent mapping
  rather than the documented provider-specific profile.
- Exact `long_gap`, `insufficient_coverage`, and `topic_flip` thresholds.
- Whether `seo_spam` should gate by default or remain advisory until a labelled
  sample exists. The config exposes both modes.
