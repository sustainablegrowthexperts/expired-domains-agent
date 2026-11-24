#!/usr/bin/env python3
"""
Deterministic Ahrefs v3 ingestion for the expired-domain screening spec (v3, lean).

Implements the "standard tier" queries from ahrefs-ingestion-spec-v2.md, saving
every raw payload and its cost headers. No judgments, no LLM, no other provider.

Usage:
    python ahrefs_ingest.py --domain smoke-fire.com
    python ahrefs_ingest.py --all
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.ahrefs.com/v3"
ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(ROOT, "runs")
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Spec v2 (v3-lean) run inputs. These domains have no drop date supplied.
DOMAINS = [
    "smoke-fire.com",
    "123eft.com",
    "kanuksheepskinproducts.com",
    "longbeachcoffee.com",
    "abipr.com",
]

LEXICON = {
    "gambling": [
        "casino", "slot", "gacor", "judi", "togel", "poker", "betting",
        "sportsbook", "baccarat", "roulette", "카지노", "바카라", "토토", "казино",
    ],
    "pharma": [
        "viagra", "cialis", "pharmacy", "pills", "tramadol", "kamagra", "cbd",
    ],
    "adult": [
        "porn", "xxx", "sex", "escort", "nude", "cam girls",
    ],
    "link_selling_seo": [
        "pbn", "backlink", "guest post", "seo", "link building", "buy links",
    ],
    "evasions": [
        "c4sino", "v1agra", "p0rn",
    ],
}


def normalize(text):
    import html
    import re
    a = html.unescape(text or "").lower()
    return re.sub(r"\s+", " ", a).strip()


def load_key():
    key = os.environ.get("AHREFS_KEY")
    if key:
        return key
    env_path = os.path.join(ROOT, ".env")
    with open(env_path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("AHREFS_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("AHREFS_KEY not found")


class Ahrefs:
    def __init__(self, key, run_dir):
        self.key = key
        self.run_dir = run_dir
        self.manifest = []

    def _path(self, query_id, page, suffix):
        d = os.path.join(self.run_dir, "raw")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{query_id}.p{page}{suffix}")

    def get(self, query_id, endpoint, params, page=0, max_attempts=6):
        qs = urllib.parse.urlencode(sorted(params.items()))
        url = f"{API}{endpoint}?{qs}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
                "User-Agent": "expired-domains-agent/1.0",
            },
        )
        attempt = 0
        while True:
            attempt += 1
            started = datetime.now(timezone.utc).isoformat()
            status, headers, body, err = None, {}, None, None
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    status = resp.status
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    body = resp.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                status = exc.code
                headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                body = exc.read().decode("utf-8", "replace")
                err = f"HTTP {exc.code}"
            except Exception as exc:  # network
                err = repr(exc)

            payload = None
            parse_error = None
            if body:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    parse_error = "non-json body"

            retryable = (
                status in (429, 500, 502, 503, 504)
                or status is None
            )
            if retryable and attempt < max_attempts:
                time.sleep(min(2 ** attempt, 30))
                continue

            record = {
                "query_id": query_id,
                "endpoint": endpoint,
                "page": page,
                "params": params,
                "url": url,
                "status": status,
                "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "attempts": attempt,
                "error": err,
                "parse_error": parse_error,
                "rows": headers.get("x-api-rows"),
                "units_cost_total": headers.get("x-api-units-cost-total"),
                "units_cost_total_actual": headers.get("x-api-units-cost-total-actual"),
                "units_cost_row": headers.get("x-api-units-cost-row"),
                "cache": headers.get("x-api-cache"),
                "headers": {
                    k: v for k, v in headers.items()
                    if k.startswith("x-api-") or k == "content-type"
                },
            }

            with open(self._path(query_id, page, ".json"), "w") as fh:
                json.dump(
                    {
                        "request": record,
                        "payload": payload,
                        "raw_body": None if payload is not None else body,
                    },
                    fh,
                    indent=2,
                    ensure_ascii=False,
                )
            with open(self._path(query_id, page, ".headers.json"), "w") as fh:
                json.dump(headers, fh, indent=2, sort_keys=True)

            self.manifest.append(record)
            if payload is None and status == 200:
                raise SystemExit(f"Unparseable 200 response for {query_id}")
            return record, payload

        raise SystemExit("unreachable")


def build_queries(domain, drop_date=None):
    """Return ordered list of (query_id, endpoint, params)."""
    target = {"target": domain, "mode": "domain"}
    q = [
        # 1. Profile size. ONE call returns all four baseline counts:
        #    live, all_time, live_refdomains, all_time_refdomains.
        #    `history` is accepted but ignored by the API (verified), so the
        #    separate all_time / since:* calls in spec v2 are removed.
        ("stats", "/site-explorer/backlinks-stats",
         dict(target, date=TODAY)),
        # 2. Referring-domain history, monthly, last 48 months.
        ("refdomains_history", "/site-explorer/refdomains-history",
         dict(target, date_from="2022-11-01", history_grouping="monthly")),
        # 3. Organic traffic history. select is mandatory (default ~41 units/row).
        ("metrics_history", "/site-explorer/metrics-history",
         dict(target, date_from="2022-11-01", history_grouping="monthly",
              select="date,org_traffic")),
        # 4. Top 70 live backlinks by DR, one per referring domain.
        ("top_dr_backlinks", "/site-explorer/all-backlinks",
         dict(target, history="live", aggregation="1_per_domain", limit="70",
              order_by="domain_rating_source:desc",
              select="url_from,url_to,anchor,first_seen_link,is_dofollow,domain_rating_source")),
        # 5. Latest 30 live backlinks, one per referring domain.
        ("latest_backlinks", "/site-explorer/all-backlinks",
         dict(target, history="live", aggregation="1_per_domain", limit="30",
              order_by="first_seen_link:desc",
              select="url_from,url_to,anchor,first_seen_link,is_dofollow,domain_rating_source")),
        # 6. Batched whole-profile lexicon search on anchors, all_time.
        ("lexicon_anchors", "/site-explorer/anchors",
         dict(target, history="all_time", limit="250",
              select="anchor,links_to_target,dofollow_links,refdomains,first_seen,last_seen",
              where=json.dumps(lexicon_where(), ensure_ascii=False))),
        # 7. Top 50 live anchors by link count.
        ("top_anchors", "/site-explorer/anchors",
         dict(target, history="live", limit="50",
              order_by="links_to_target:desc",
              select="anchor,links_to_target,dofollow_links,first_seen")),
    ]
    # No `since:` stats variant: backlinks-stats ignores `history` (verified
    # 2026-11-24), so a post-drop link count cannot be obtained from this
    # endpoint. Use refdomains-history or a finalist export instead.
    return q


def lexicon_where():
    clauses = []
    for terms in LEXICON.values():
        for t in terms:
            clauses.append({"field": "anchor", "is": ["isubstring", t]})
    return {"or": clauses}


def run_domain(domain, drop_date=None):
    key = load_key()
    run_id = f"{domain}:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}:ahrefs"
    run_dir = os.path.join(RUNS_DIR, domain, run_id.replace(":", "_"))
    os.makedirs(run_dir, exist_ok=True)

    print(f"\n=== {domain} ===")
    print(f"run_dir: {run_dir}")

    api = Ahrefs(key, run_dir)

    # Query 0: free budget check before the run.
    limits_before, _ = api.get("limits_before", "/subscription-info/limits-and-usage", {})

    for query_id, endpoint, params in build_queries(domain, drop_date):
        record, payload = api.get(query_id, endpoint, params)
        cost = record["units_cost_total_actual"] or record["units_cost_total"] or "?"
        print(f"  {query_id:<22} status={record['status']} rows={record['rows']} units={cost} err={record['error']}")

    limits_after, _ = api.get("limits_after", "/subscription-info/limits-and-usage", {})

    manifest = {
        "run_id": run_id,
        "domain": domain,
        "provider": "ahrefs",
        "mode": "domain",
        "tier": "standard",
        "drop_date": drop_date,
        "drop_date_source": "unknown" if not drop_date else "provided",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "api_date": TODAY,
        "config": {
            "top_dr_rows": 70,
            "latest_rows": 30,
            "top_anchor_rows": 50,
            "history_months": 48,
            "anchor_lexicon": LEXICON,
            "spec": "ahrefs-ingestion-spec-v2.md (v3 lean)",
        },
        "requests": api.manifest,
        "units_actual_total": sum(
            int(r["units_cost_total_actual"] or 0) for r in api.manifest
            if r["query_id"] not in ("limits_before", "limits_after")
        ),
        "units_billed_total": sum(
            int(r["units_cost_total"] or 0) for r in api.manifest
            if r["query_id"] not in ("limits_before", "limits_after")
        ),
    }
    with open(os.path.join(run_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)

    print(f"  units billed: {manifest['units_billed_total']}  actual: {manifest['units_actual_total']}")
    return run_dir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--drop-date", default=None)
    args = ap.parse_args()

    if args.all:
        for d in DOMAINS:
            run_domain(d)
    elif args.domain:
        run_domain(args.domain, args.drop_date)
    else:
        ap.error("pass --domain or --all")


if __name__ == "__main__":
    main()
