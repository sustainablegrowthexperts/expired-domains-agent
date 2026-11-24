#!/usr/bin/env python3
"""Build one deterministic .xlsx per site from the saved raw Ahrefs payloads."""
import glob
import html
import json
import os
import re
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

LEXICON = {
    "gambling": ["casino", "slot", "gacor", "judi", "togel", "poker", "betting",
                 "sportsbook", "baccarat", "roulette", "카지노", "바카라", "토토", "казино"],
    "pharma": ["viagra", "cialis", "pharmacy", "pills", "tramadol", "kamagra", "cbd"],
    "adult": ["porn", "xxx", "sex", "escort", "nude", "cam girls"],
    "link_selling_seo": ["pbn", "backlink", "guest post", "seo", "link building", "buy links"],
    "evasions": ["c4sino", "v1agra", "p0rn"],
}
TERM_CAT = {t: c for c, ts in LEXICON.items() for t in ts}

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=14, color="1F3864")
LABEL_FONT = Font(bold=True, size=10)
WRAP = Alignment(vertical="top", wrap_text=True)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def norm(text):
    a = html.unescape(text or "").lower()
    return re.sub(r"\s+", " ", a).strip()


def domain_of(url):
    try:
        return url.split("://", 1)[1].split("/", 1)[0].lower()
    except Exception:
        return ""


def load_json(path):
    with open(path) as fh:
        return json.load(fh)


def sheet(wb, name, title, subtitle=None):
    ws = wb.create_sheet(name)
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    if subtitle:
        ws["A2"] = subtitle
        ws["A2"].font = Font(italic=True, size=9, color="595959")
        ws["A2"].alignment = WRAP
    ws.sheet_view.showGridLines = False
    return ws


def write_table(ws, start_row, headers, rows, widths=None, number_cols=()):
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=start_row, column=j, value=h)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
        c.border = BORDER
    for i, row in enumerate(rows, start_row + 1):
        for j, v in enumerate(row, 1):
            c = ws.cell(row=i, column=j, value=v)
            c.alignment = WRAP
            c.border = BORDER
            c.font = Font(size=9)
            if j in number_cols and isinstance(v, (int, float)):
                c.number_format = "#,##0"
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(headers))}{start_row + len(rows)}"
    if widths:
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w


def kv_table(ws, start_row, pairs, widths=(34, 100)):
    for i, (k, v) in enumerate(pairs, start_row):
        a = ws.cell(row=i, column=1, value=k)
        a.font = LABEL_FONT
        a.alignment = Alignment(vertical="top")
        a.border = BORDER
        b = ws.cell(row=i, column=2, value=v)
        b.alignment = WRAP
        b.font = Font(size=9)
        b.border = BORDER
    for j, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.sheet_view.showGridLines = False


def request_map(manifest):
    return {r["query_id"]: r for r in manifest["requests"]}


def find_run(domain):
    dirs = sorted(glob.glob(os.path.join(ROOT, "runs", domain, "*_ahrefs")))
    if not dirs:
        raise SystemExit(f"no run for {domain}")
    return dirs[-1]


def build(domain):
    run_dir = find_run(domain)
    manifest = load_json(os.path.join(run_dir, "manifest.json"))
    reqs = request_map(manifest)
    raw = lambda q: load_json(os.path.join(run_dir, "raw", f"{q}.p0.json"))["payload"]

    stats = (raw("stats") if os.path.exists(os.path.join(run_dir, "raw", "stats.p0.json"))
             else raw("stats_live")).get("metrics", {})
    ref_hist = raw("refdomains_history").get("refdomains", [])
    met_hist = raw("metrics_history").get("metrics", [])
    top_dr = raw("top_dr_backlinks").get("backlinks", [])
    latest = raw("latest_backlinks").get("backlinks", [])
    top_anchors = raw("top_anchors").get("anchors", [])
    lex_payload = raw("lexicon_anchors").get("anchors", [])

    completion_path = os.path.join(run_dir, "lexicon_term_complete.json")
    completion = load_json(completion_path) if os.path.exists(completion_path) else None
    lex_rows = completion["anchors"] if completion else lex_payload
    lex_complete = (len(lex_payload) < 250) or bool(completion and not completion["capped_terms"])

    # ---- cost accounting
    cost_rows = []
    for r in manifest["requests"]:
        if r["query_id"] in ("limits_before", "limits_after"):
            continue
        cost_rows.append([
            r["query_id"], r["endpoint"].replace("/site-explorer/", ""),
            r["rows"], int(r["units_cost_total"] or 0),
            int(r["units_cost_total_actual"] or 0),
            r["cache"], r["status"], r["error"] or "",
        ])
    spec_units = sum(r[3] for r in cost_rows)
    spec_actual = sum(r[4] for r in cost_rows)
    completion_units = completion["units_actual"] if completion else 0
    completion_billed = completion["units_billed"] if completion else 0

    # ---- workbook
    wb = Workbook()
    wb.remove(wb.active)

    # Summary
    ws = sheet(wb, "Summary", f"Ahrefs standard-tier slice — {domain}",
               "Source: Ahrefs API v3 only. Deterministic run, no LLM scoring. "
               "Raw payloads and cost headers saved alongside this workbook.")
    hist_note = (
        f"{len(met_hist)} monthly rows (starts {met_hist[0]['date'][:7]})" if met_hist
        else "0 rows returned — Ahrefs has no organic-traffic history for this target"
    )
    extra_stats_billed = int(reqs["stats_all_time"]["units_cost_total"] or 0) if "stats_all_time" in reqs else 0
    extra_stats_actual = int(reqs["stats_all_time"]["units_cost_total_actual"] or 0) if "stats_all_time" in reqs else 0
    pairs = [
        ("Domain", domain),
        ("Run ID", manifest["run_id"]),
        ("Fetched at (UTC)", manifest["fetched_at"]),
        ("Provider / API", "Ahrefs API v3"),
        ("Mode", manifest["mode"]),
        ("Tier", manifest["tier"]),
        ("Coverage mode", "top_n"),
        ("Drop date", manifest["drop_date"] or "NOT SUPPLIED"),
        ("Drop date source", manifest["drop_date_source"]),
        ("", ""),
        ("STATS — live links", stats.get("live")),
        ("STATS — all-time links", stats.get("all_time")),
        ("STATS — live referring domains", stats.get("live_refdomains")),
        ("STATS — all-time referring domains", stats.get("all_time_refdomains")),
        ("STATS — calls needed", "1 (all four counts returned together; `history` ignored)"),
        ("", ""),
        ("Backlinks — top-DR rows", len(top_dr)),
        ("Backlinks — latest rows", len(latest)),
        ("Backlinks — unique across both slices", len({(r.get('url_from'), r.get('url_to')) for r in top_dr + latest})),
        ("Anchors — top-50 rows", len(top_anchors)),
        ("Anchors — lexicon rows (deduped)", len(lex_rows)),
        ("Lexicon complete?", "YES" if lex_complete else "NO — batched query hit the 250-row cap"),
        ("", ""),
        ("History — refdomains months", f"{len(ref_hist)} monthly rows"),
        ("History — organic traffic months", hist_note),
        ("", ""),
        ("COST — units billed, spec queries as written", spec_units),
        ("COST — units actually charged, spec queries", spec_actual),
        ("COST — extra units to complete lexicon", completion_units),
        ("COST — total for this site (complete lexicon)", spec_actual + completion_units),
        ("COST — planning total, single stats call", spec_actual + completion_units - extra_stats_actual),
        ("COST — spec v2 estimate", "~1,800–2,300"),
        ("", ""),
        ("Cell semantics", "Blank = null from Ahrefs. 'unknown' = not supplied / not derivable."),
    ]
    kv_table(ws, 4, pairs)

    # Stats
    ws = sheet(wb, "Stats", "Backlink counts — all four baseline metrics from ONE call",
               "`backlinks-stats` returns links and referring domains for both live and all_time in a single row. "
               "The `history` parameter is accepted but ignored, so separate live/all_time/since calls are unnecessary.")
    write_table(ws, 4,
                ["metric", "value", "scope", "source_query", "note"],
                [["live links", stats.get("live"), "live", "stats",
                  "links currently indexed"],
                 ["all_time links", stats.get("all_time"), "all_time", "stats",
                  "includes lost links"],
                 ["live referring domains", stats.get("live_refdomains"), "live", "stats",
                  "subdomain-level (blogspot subdomains count separately)"],
                 ["all_time referring domains", stats.get("all_time_refdomains"), "all_time", "stats",
                  "subdomain-level"],
                 ["links since drop", None, "since_drop", "—",
                  "NOT AVAILABLE: backlinks-stats ignores history=since:*; use refdomains-history"],
                 ["referring domains since drop", None, "since_drop", "—",
                  "NOT AVAILABLE from stats; derive the spike from refdomains-history"]],
                widths=[28, 12, 14, 16, 66], number_cols=(2,))

    # History
    ws = sheet(wb, "History", "Monthly history — referring domains + organic traffic",
               "refdomains-history and metrics-history. Blank organic_traffic means Ahrefs returned no row "
               "for that month (not necessarily zero).")
    by_date = {}
    for r in ref_hist:
        by_date.setdefault(r["date"][:10], {})["refdomains"] = r.get("refdomains")
    for r in met_hist:
        by_date.setdefault(r["date"][:10], {})["org_traffic"] = r.get("org_traffic")
    rows = [[d, v.get("refdomains"), v.get("org_traffic")] for d, v in sorted(by_date.items())]
    write_table(ws, 4, ["date", "refdomains", "org_traffic"], rows,
                widths=[14, 14, 14], number_cols=(2, 3))

    # Backlinks
    def bl_rows(rows, source):
        out = []
        seen = set()
        for r in rows:
            key = (r.get("url_from"), r.get("url_to"))
            out.append([
                r.get("url_from"), domain_of(r.get("url_from") or ""), r.get("url_to"),
                r.get("anchor"), r.get("first_seen_link"),
                "yes" if r.get("is_dofollow") else "no",
                r.get("domain_rating_source"), source,
                "duplicate" if key in seen else "",
            ])
            seen.add(key)
        return out

    ws = sheet(wb, "Backlinks_TopDR", "Top 70 live backlinks by Domain Rating",
               "aggregation=1_per_domain; order_by=domain_rating_source:desc. Slice, not the full profile.")
    write_table(ws, 4, ["url_from", "domain_from", "url_to", "anchor", "first_seen_link",
                        "is_dofollow", "domain_rating", "source_query", "dup_flag"],
                bl_rows(top_dr, "top_dr"),
                widths=[46, 26, 34, 46, 22, 11, 13, 12, 10], number_cols=(7,))

    ws = sheet(wb, "Backlinks_Latest", "Latest 30 live backlinks",
               "aggregation=1_per_domain; order_by=first_seen_link:desc. Slice, not the full profile.")
    write_table(ws, 4, ["url_from", "domain_from", "url_to", "anchor", "first_seen_link",
                        "is_dofollow", "domain_rating", "source_query", "dup_flag"],
                bl_rows(latest, "latest"),
                widths=[46, 26, 34, 46, 22, 11, 13, 12, 10], number_cols=(7,))

    # Top anchors
    ws = sheet(wb, "Anchors_Top50", "Top 50 live anchors by link count",
               "history=live; order_by=links_to_target:desc. Slice (top by volume).")
    write_table(ws, 4, ["anchor", "links_to_target", "dofollow_links", "first_seen"],
                [[a.get("anchor"), a.get("links_to_target"), a.get("dofollow_links"), a.get("first_seen")]
                 for a in top_anchors],
                widths=[90, 16, 16, 22], number_cols=(2, 3))

    # Lexicon
    lex_out = []
    for a in lex_rows:
        anchor = a.get("anchor") or ""
        terms = a.get("_terms")
        cats = a.get("_categories")
        if terms is None:
            na = norm(anchor)
            terms = [t for t in TERM_CAT if t in na]
            cats = sorted({TERM_CAT[t] for t in terms})
        lex_out.append([
            anchor, a.get("links_to_target"), a.get("dofollow_links"), a.get("refdomains"),
            a.get("first_seen"), a.get("last_seen"),
            "yes" if a.get("last_seen") is None else "no",
            ", ".join(terms), ", ".join(cats),
        ])
    lex_out.sort(key=lambda r: (-(r[1] or 0), -len(r[7])))
    ws = sheet(wb, "Anchors_Lexicon", "Whole-profile lexicon matches (gambling / pharma / adult / link-selling)",
               ("history=all_time; union of per-term requests — COMPLETE (each term below the 250-row cap)."
                if lex_complete else
                "history=all_time; single batched OR request. INCOMPLETE: hit the 250-row cap; rows are a truncated sample."))
    write_table(ws, 4, ["anchor", "links_to_target", "dofollow_links", "refdomains", "first_seen",
                        "last_seen", "still_live", "matched_terms", "matched_categories"],
                lex_out, widths=[80, 15, 15, 12, 22, 22, 10, 26, 26], number_cols=(2, 3, 4))

    # Costs
    ws = sheet(wb, "Costs", "API cost per request",
               "Header values from x-api-units-cost-total (billed) and -actual (charged; 0 on cache hit).")
    write_table(ws, 4, ["query_id", "endpoint", "rows", "units_billed", "units_actual", "cache", "status", "error"],
                cost_rows, widths=[22, 26, 9, 13, 13, 9, 9, 20], number_cols=(3, 4, 5))
    start = 4 + len(cost_rows) + 1
    ws.cell(row=start, column=1, value="TOTAL — as run").font = LABEL_FONT
    ws.cell(row=start, column=4, value=spec_units).number_format = "#,##0"
    ws.cell(row=start, column=5, value=spec_actual).number_format = "#,##0"
    row = start + 1
    if extra_stats_billed or extra_stats_actual:
        ws.cell(row=row, column=1, value="Correction — duplicate stats call removed (one call returns all four counts)").font = LABEL_FONT
        ws.cell(row=row, column=4, value=-extra_stats_billed).number_format = "#,##0"
        ws.cell(row=row, column=5, value=-extra_stats_actual).number_format = "#,##0"
        row += 1
    if completion:
        ws.cell(row=row, column=1, value="Lexicon completion (39 per-term requests)").font = LABEL_FONT
        ws.cell(row=row, column=4, value=completion_billed).number_format = "#,##0"
        ws.cell(row=row, column=5, value=completion_units).number_format = "#,##0"
        row += 1
    ws.cell(row=row, column=1, value="TOTAL — corrected model (complete lexicon)").font = LABEL_FONT
    ws.cell(row=row, column=4, value=spec_units - extra_stats_billed + completion_billed).number_format = "#,##0"
    ws.cell(row=row, column=5, value=spec_actual - extra_stats_actual + completion_units).number_format = "#,##0"

    if completion:
        ws2 = sheet(wb, "Costs_LexiconDetail", "Lexicon completion — per term",
                    "Each term queried separately with the same select (10 units/row). Minimum 50 units per request.")
        write_table(ws2, 4, ["term", "category", "rows", "units_billed", "units_actual"],
                    [[r["term"], r["category"], int(r["rows"] or 0), int(r["units_total"] or 0),
                      int(r["units_actual"] or 0)] for r in completion["requests"]],
                    widths=[22, 20, 9, 13, 13], number_cols=(3, 4, 5))

    # Coverage / gaps
    ws = sheet(wb, "Coverage", "Coverage object and determinism gaps",
               "What this run can and cannot answer. Read before using any metric.")
    coverage = {
        "mode": "top_n",
        "stats": {"live": stats.get("live"), "all_time": stats.get("all_time"),
                  "since_drop": None,
                  "live_refdomains": stats.get("live_refdomains"),
                  "all_time_refdomains": stats.get("all_time_refdomains")},
        "rows": {"top_dr": len(top_dr), "latest": len(latest),
                 "unique": len({(r.get('url_from'), r.get('url_to')) for r in top_dr + latest})},
        "top_anchors": len(top_anchors),
        "lexicon_complete": lex_complete,
        "history_months": len(ref_hist),
        "drop_date_source": manifest["drop_date_source"],
    }
    gaps = []
    if not manifest["drop_date"]:
        gaps.append("Post-drop volume: NO drop date in run inputs; `since:` stats also ignored by the API.")
    if not met_hist:
        gaps.append("Traffic history: metrics-history returned 0 rows. Cannot tell 'no traffic' from 'no data'.")
    elif len(met_hist) < 12:
        gaps.append(f"Traffic history: only {len(met_hist)} monthly row(s), first {met_hist[0]['date'][:7]}. "
                    "No usable pre-drop baseline or cliff.")
    if not lex_complete:
        gaps.append("Toxic anchors: batched query truncated at 250 rows; profile-wide completeness not guaranteed.")
    gaps.append("Row-level metrics describe the 70+30 slice, not the whole profile.")
    gaps.append("No outbound-link / PBN-reuse signal in standard tier (spec keeps it finalist-only).")
    kv_table(ws, 4, [
        ("coverage_json", json.dumps(coverage, indent=2)),
        ("gap_1", gaps[0] if len(gaps) > 0 else ""),
        ("gap_2", gaps[1] if len(gaps) > 1 else ""),
        ("gap_3", gaps[2] if len(gaps) > 2 else ""),
        ("gap_4", gaps[3] if len(gaps) > 3 else ""),
        ("gap_5", gaps[4] if len(gaps) > 4 else ""),
    ])

    path = os.path.join(OUT, f"{domain}.xlsx")
    wb.save(path)
    print(f"wrote {path}  spec_units={spec_units} complete_units={spec_actual + completion_units}")
    uniq = {(r.get("url_from"), r.get("url_to")) for r in top_dr + latest}
    return {
        "domain": domain, "spec_units": spec_units, "spec_actual": spec_actual,
        "completion_units": completion_units,
        "total_complete": spec_actual + completion_units,
        "extra_stats_actual": extra_stats_actual,
        "corrected_total": spec_actual - extra_stats_actual + completion_units,
        "live": stats.get("live"), "all_time": stats.get("all_time"),
        "live_refdomains": stats.get("live_refdomains"),
        "all_time_refdomains": stats.get("all_time_refdomains"),
        "unique_slice": len(uniq),
        "overlap": len({(r.get("url_from"), r.get("url_to")) for r in top_dr}
                       & {(r.get("url_from"), r.get("url_to")) for r in latest}),
        "lexicon_rows": len(lex_rows), "lexicon_complete": lex_complete,
        "metrics_rows": len(met_hist), "ref_history_rows": len(ref_hist), "path": path,
    }


if __name__ == "__main__":
    out = [build(d) for d in ["smoke-fire.com", "123eft.com", "kanuksheepskinproducts.com",
                              "longbeachcoffee.com", "abipr.com"]]
    with open(os.path.join(OUT, "build_summary.json"), "w") as fh:
        json.dump(out, fh, indent=2)
