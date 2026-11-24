#!/usr/bin/env python3
"""Complete the §3.6 lexicon search for domains where the batched `or` query
returned exactly `limit` rows (250).

Deterministic strategy: run each lexicon term as its own request with the same
`select`/`history`, union the rows locally. This measures the real cost of a
complete whole-profile toxic-anchor search, which the spec's single batched
request cannot deliver once the union exceeds the row cap.
"""
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ing", os.path.join(ROOT, "ahrefs_ingest.py"))
ing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ing)

KEY = ing.load_key()


def request(qid, outdir, params):
    url = "https://api.ahrefs.com/v3/site-explorer/anchors?" + urllib.parse.urlencode(sorted(params.items()))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json"})
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status = r.status
                h = {k.lower(): v for k, v in r.headers.items()}
                payload = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 6:
                time.sleep(2 ** attempt)
                continue
            raise
    rec = {
        "query_id": qid, "params": params, "url": url, "status": status,
        "rows": h.get("x-api-rows"),
        "units_total": h.get("x-api-units-cost-total"),
        "units_actual": h.get("x-api-units-cost-total-actual"),
        "cache": h.get("x-api-cache"),
    }
    with open(os.path.join(outdir, qid + ".json"), "w") as fh:
        json.dump({"request": rec, "payload": payload, "headers": h}, fh, indent=2, ensure_ascii=False)
    return rec, payload


def main(domain, run_dir):
    outdir = os.path.join(run_dir, "lexicon_term_complete")
    os.makedirs(outdir, exist_ok=True)
    base = {
        "target": domain, "mode": "domain", "history": "all_time", "limit": "250",
        "select": "anchor,links_to_target,dofollow_links,refdomains,first_seen,last_seen",
    }
    seen = {}
    requests = []
    capped_terms = []
    for cat, terms in ing.LEXICON.items():
        for t in terms:
            qid = f"term_{cat}_{abs(hash(t)) % 10**8}"
            params = dict(base, where=json.dumps({"field": "anchor", "is": ["isubstring", t]}, ensure_ascii=False))
            rec, payload = request(qid, outdir, params)
            rec["term"], rec["category"] = t, cat
            requests.append(rec)
            rows = payload.get("anchors", [])
            if len(rows) >= 250:
                capped_terms.append(t)
            for row in rows:
                key = ing.normalize(row.get("anchor"))
                if key not in seen:
                    seen[key] = dict(row, _terms=[], _categories=[])
                if t not in seen[key]["_terms"]:
                    seen[key]["_terms"].append(t)
                if cat not in seen[key]["_categories"]:
                    seen[key]["_categories"].append(cat)
            units = rec["units_actual"] or rec["units_total"]
            print(f"  {t:<16} rows={len(rows):>3} units={units}")
            time.sleep(0.3)
    out = {
        "domain": domain,
        "terms": sum(len(v) for v in ing.LEXICON.values()),
        "capped_terms": capped_terms,
        "unique_anchors": len(seen),
        "rows_returned_total": sum(int(r["rows"] or 0) for r in requests),
        "units_billed": sum(int(r["units_total"] or 0) for r in requests),
        "units_actual": sum(int(r["units_actual"] or 0) for r in requests),
        "requests": requests,
        "anchors": sorted(seen.values(), key=lambda x: -(x.get("links_to_target") or 0)),
    }
    with open(os.path.join(run_dir, "lexicon_term_complete.json"), "w") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print(f"  unique={len(seen)} units={out['units_actual']} capped_terms={capped_terms}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
