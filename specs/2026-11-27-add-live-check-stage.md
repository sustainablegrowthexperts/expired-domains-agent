# Change Summary Specification: Add Live Check Stage

**Date:** 2026-11-27

## 1. Objective & Overview

Insert a new deterministic **Live check** stage between the Free DR check and the Wayback Machine Checks. The stage probes DNS and HTTP, follows redirects, rejects redirects that leave the candidate's registrable root domain and landing pages that serve toxic content, treats unresolved/parked domains as neutral passes, and is processed automatically by a dedicated polling worker. This gives the pipeline an inexpensive current-state signal and ensures Wayback only sees domains that passed the live check.

## 2. Scope Boundaries

### In Scope

| Item | Detail |
|---|---|
| New pipeline stage | Live check becomes stage 4, between Free DR (3) and Wayback (5). Data collection becomes stage 6 in the UI but its eligibility logic is unchanged. |
| DNS probe | Resolve A/AAAA for the candidate domain. MX records are explicitly excluded. |
| HTTP probe | One `GET`, HTTPS first with HTTP fallback, following up to 10 redirects, 10 s timeout, keeping roughly the first 50 KB of the body. |
| Landing-content evaluation | Extract final URL/host, status codes, redirect chain, `<title>`, and visible text with tags stripped. |
| Root-domain rule | Compare the starting and final hosts by registrable root domain. Same root passes; a different root fails. |
| Neutral results | `nxdomain`, `no_http`, and `parked` are recorded as neutral and stored as passed so they continue to Wayback. |
| Toxic result | Fail when the final host matches the toxic lexicon, the title matches, or visible text contains at least `content_min_term_hits` distinct toxic terms. |
| Dedicated worker | Add `LiveCheckWorker` and `live_check_pending`, with processing markers, retry budget, stale-processing recovery, and its own `live_checks_started_at` checkpoint. |
| Wayback gating | Wayback pulls `live_check_pass = 1` and uses `live_check_checked_at` for its checkpoint when Live is enabled. |
| Manual pass | Add a manual pass action for failed Live results that preserves the automated failure evidence. |
| Persistence | Add the live-check columns, raw evidence under `data/raw/live/{domain}/{run_id}/`, and a versioned `config/live/v1.json`. |
| UI | Add the stage 4 Live check view, counts, filters, detail modal, and manual pass action; renumber Wayback to 5 and Data collection to 6. |
| Documentation | Update `README.md` and `.env.example`; add and update tests. |

### Out of Scope

| Item | Reason |
|---|---|
| MX record checks | User confirmed these do not relate to the app's purpose. |
| Mobile-user-agent second pass | User confirmed protocol/user-agent parity is not important; the stage only needs the landing content. |
| Protocol as a decision input | HTTP and HTTPS are probes of the same domain; protocol itself does not pass or fail a candidate. |
| Headless browser rendering | The spec explicitly calls for plain HTTP. |
| LLM evaluation of live content | Live check is deterministic lexicon/rule evaluation. |
| Automatic backfill of existing DR-passed rows | User will clear columns later to simulate a requeue. |
| Data-collection eligibility changes | Data collection remains gated by Wayback; Live becomes an upstream gate transitively. |
| Changes to DR check behavior | The existing Free DR worker and thresholds remain unchanged. |
| Changes to Wayback scoring | Only the Wayback candidate query and checkpoint source change. |

## 3. Technical Architecture & Decisions

| Decision | Rationale |
|---|---|
| Pipeline order is Prefilter → Keyword → Free DR → Live → Wayback → Data collection. | Matches the requested insertion point and keeps the cheap HTTP check before the rate-limited archive stage. |
| One HTTP probe: HTTPS first, fall back to HTTP only if HTTPS fails. | User explicitly does not want protocol to be a decision input; one successful content read is enough. |
| Follow up to 10 redirects with a 10 s timeout and ~50 KB body cap. | Taken from the v3 spec and keeps the stage cheap and bounded. |
| Compare final host to start host by registrable root domain. | `example.com`, `www.example.com`, `shop.example.com`, `http`, `https`, and ports all count as the same root; `example.co.uk` vs `example.com` does not. |
| `nxdomain` / `no_http` are neutral passes. | The v3 spec calls these normal for dropped domains and the user confirmed they are acceptable; they may indicate the domain is still available. |
| `parked` is a neutral pass. | The v3 spec classifies parked/for-sale pages as neutral. |
| Off-root redirect fails unconditionally. | User explicitly requires a final domain different from the starting domain to fail, which is stricter than the spec's "inspect the final host" wording. |
| Toxic checks reuse existing blocklist, content lexicon, toxic categories, and parking terms. | Avoids a second semantic vocabulary and keeps live/archive judgments consistent. |
| Fail on a toxic final host, toxic title, or at least `content_min_term_hits` distinct toxic body terms. | Matches the v3 stage 3 rule and avoids rejecting a page that mentions one risky word. |
| Live check owns `live_checks_started_at`; no automatic backfill. | Prevents deployment from silently reprocessing the existing queue. The user will manually clear columns to simulate requeue. |
| Wayback uses `live_check_pass = 1` and `live_check_checked_at` when Live is enabled. | Ensures the new stage actually gates the next stage. |
| When `LIVE_CHECK_ENABLED=0`, Wayback falls back to its current DR-passed behavior and `dr_check_checked_at` checkpoint. | Keeps the stage disableable and preserves existing behavior. |
| Data collection is not changed. | Wayback remains its direct gate; Live is enforced transitively through Wayback. |
| Persist `live_check_status`, `live_check_pass`, `live_check_class`, `live_check_reason`, `live_check_details_json`, `live_check_attempts`, `live_check_checked_at`, `live_check_manual_pass`, `live_check_manual_pass_at`, `live_check_final_host`, `live_check_http_status`, and `live_check_config_version`. | Gives the UI and audit trail the same shape as the DR and Wayback stages. |
| Store full DNS/HTTP/redirect/body evidence in `live_check_details_json` and under `data/raw/live/{domain}/{run_id}/`. | Keeps verdicts reproducible from frozen evidence. |
| Manual pass sets `live_check_manual_pass=1`, changes the effective result to passed, and preserves the automated failure in the details blob. | Mirrors the existing Wayback manual-pass behavior and prevents re-checking. |
| New environment settings: `LIVE_CHECK_ENABLED`, `LIVE_CHECK_MAX_ATTEMPTS`, `LIVE_CHECKS_PER_CYCLE`, `LIVE_CHECK_TIMEOUT_SECONDS`, `LIVE_CHECK_MAX_REDIRECTS`, `LIVE_CHECK_MAX_BODY_BYTES`. | Follows the existing stage configuration pattern without introducing secrets. |
| `config/live/v1.json` stores the version, enabled flag, timeout, redirect limit, body cap, content threshold, toxic categories, and parking terms. | Versioned config keeps the live rules auditable and tunable without code changes. |

## 4. Affected Files

| File | Action |
|---|---|
| `config/live/v1.json` | Create — versioned Live check configuration. |
| `src/expired_domains/live_check.py` | Create — DNS/HTTP client, evidence extraction, classification, evaluation, raw evidence writer, candidate loader, and `live_check_pending`. |
| `src/expired_domains/models.py` | Modify — add `LiveCheckVerdict` and any shared details shape. |
| `src/expired_domains/config.py` | Modify — add Live check settings and environment parsing. |
| `src/expired_domains/storage/schema.sql` | Modify — add the `live_check_*` columns to `domains`. |
| `src/expired_domains/storage/db.py` | Modify — add migrations, indexes, processing/result/error/manual-pass functions, and candidate queries where appropriate. |
| `src/expired_domains/worker.py` | Modify — add `LiveCheckWorker` and keep the existing worker structure. |
| `src/expired_domains/cli.py` | Modify — construct the Live client, run the stage in `worker --once`, start/stop the polling worker, and report `live_checked`. |
| `src/expired_domains/web/app.py` | Modify — start/stop the Live worker and add the manual-pass endpoint. |
| `src/expired_domains/shortlist.py` | Modify — expose Live check fields, stage counts, stage numbering, and collection eligibility display. |
| `src/expired_domains/templates/shortlist.html` | Modify — add the Live check sidebar item/view, filters, columns, detail modal, manual-pass action, and renumber later stages. |
| `src/expired_domains/domain_detail.py` | Modify — include Live check details in the domain detail payload. |
| `tests/test_live_check.py` | Create — unit and integration coverage for parsing, classification, evaluation, queueing, retries, and manual pass. |
| `tests/test_wayback.py` | Modify — seed Live-passed rows and assert Wayback’s new gating/checkpoint behavior. |
| `tests/test_worker.py` | Modify — assert the Live worker is started/processed where appropriate. |
| `tests/test_web.py` | Modify — cover the new view and manual-pass endpoint. |
| `tests/test_shortlist.py` | Modify — cover Live rows, counts, stage numbering, and rendering. |
| `tests/test_domain_detail.py` | Modify — cover Live details in the detail payload. |
| `tests/test_cli.py` | Modify — cover the `worker --once` summary and worker wiring if affected. |
| `tests/conftest.py` | Modify — clear the new `LIVE_CHECK_*` environment variables in the autouse fixture. |
| `.env.example` | Modify — document the new Live check settings and placeholders. |
| `README.md` | Modify — document the new stage, order, worker, schema fields, UI, and manual pass. |

## 5. Proposed Task Breakdown

### Phase 1 — Data Model, Config, and Storage

1. **Add the Live check model** — Define `LiveCheckVerdict` in `models.py` with `passed`, `class`, `reason`, and `details`, matching the existing verdict shapes.
2. **Add versioned Live config** — Create `config/live/v1.json` and a cached loader in `live_check.py` with version, enabled, timeout, redirect limit, body cap, `content_min_term_hits`, toxic categories, and parking terms.
3. **Add environment settings** — Extend `Settings` and `load_settings` with `LIVE_CHECK_ENABLED`, `LIVE_CHECK_MAX_ATTEMPTS`, `LIVE_CHECKS_PER_CYCLE`, `LIVE_CHECK_TIMEOUT_SECONDS`, `LIVE_CHECK_MAX_REDIRECTS`, and `LIVE_CHECK_MAX_BODY_BYTES`.
4. **Add schema and migrations** — Add the `live_check_*` columns to `schema.sql` and `_migrate_domains` in `db.py`, plus a `live_check_pass` index.
5. **Add storage functions** — Implement `mark_live_checks_processing`, `set_live_check_results`, `record_live_check_errors`, `set_live_check_manual_pass`, and any shared candidate loader needed by the stage.

### Phase 2 — Live Check Engine

6. **Implement DNS resolution** — Resolve A/AAAA only; record success/failure and the resolved addresses in the evidence. Do not add MX handling.
7. **Implement the HTTP fetch** — Try HTTPS first, fall back to HTTP if HTTPS fails, follow up to the configured redirect limit, enforce the timeout, cap the body, and record the full redirect chain, final URL, final host, status codes, title, and visible text.
8. **Implement classification and evaluation** — Compare the final host to the starting host by registrable root, classify `nxdomain` / `no_http` / `parked` / `redirect_offsite` / `content`, apply the toxic host/title/body rules, and return a pass/fail verdict.
9. **Implement raw evidence writing** — Write DNS, request/response, redirect-chain, and body evidence under `data/raw/live/{domain}/{run_id}/` without overwriting prior runs.
10. **Add engine tests** — Cover same-root redirects, off-root failures, `www`/subdomain equivalence, `nxdomain`/`no_http`/`parked` neutral passes, toxic host/title/body failures, the term threshold, and malformed/empty responses.

### Phase 3 — Worker and Pipeline Wiring

11. **Implement the queue** — Add `live_check_pending` with the `live_checks_started_at` checkpoint, stale-processing recovery, per-domain attempt budget, and per-cycle limit.
12. **Implement the worker class** — Add `LiveCheckWorker` alongside the existing DR and Wayback workers, with the same start/stop/error-isolation behavior.
13. **Update Wayback gating** — Change the Wayback candidate query to require `live_check_pass = 1` and use `live_check_checked_at` for its cutoff when Live is enabled; fall back to the DR condition when `LIVE_CHECK_ENABLED=0`.
14. **Wire the CLI** — Construct the Live client, include `live_checked` in `worker --once`, start the polling worker in long-running mode, and stop/close it cleanly.
15. **Wire the web app** — Start and stop the Live worker in the app lifespan under the same settings gate.
16. **Add pipeline tests** — Assert that Live pulls DR-passed rows, Wayback does not pull rows without a Live pass, the checkpoint prevents backfill, retry/exhaustion works, manual pass unblocks Wayback, and disabling Live restores the DR fallback.

### Phase 4 — UI, Manual Pass, and Documentation

17. **Extend the shortlist data** — Add Live check fields to `DiscoveryRecord`, compute Live counts, and update `reached`/progress labels and stage numbering.
18. **Update the template** — Add the stage 4 Live check sidebar item and view, Passed/Failed/Processing filters, the class/final-host/status/title columns, the detail modal section, the manual-pass action, and renumber Wayback to 5 and Data collection to 6.
19. **Extend the domain detail payload** — Add a `live` section to `load_domain_detail` so the modal can show DNS, redirect chain, final host, title, excerpt, toxic terms, and attempts.
20. **Add the manual-pass endpoint** — Add `POST /api/live/{domain}/pass`, guarded so only a failed Live row can be manually passed.
21. **Update documentation** — Document the new stage order, behavior, neutral classes, worker, schema fields, UI, and manual pass in `README.md`; add the new settings to `.env.example`.
22. **Run regression coverage** — Update the affected tests, run the full suite, and verify the pipeline still works when Live is disabled.
