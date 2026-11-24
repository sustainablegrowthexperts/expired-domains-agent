#!/usr/bin/env python3
"""Combined cost / conformance workbook across all five sites."""
import json
import os
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=14, color="1F3864")
WRAP = Alignment(vertical="top", wrap_text=True)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def table(ws, r0, headers, rows, widths, num=()):
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=r0, column=j, value=h)
        c.fill, c.font, c.border = HEADER_FILL, HEADER_FONT, BORDER
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for i, row in enumerate(rows, r0 + 1):
        for j, v in enumerate(row, 1):
            c = ws.cell(row=i, column=j, value=v)
            c.font, c.border, c.alignment = Font(size=9), BORDER, WRAP
            if j in num and isinstance(v, (int, float)):
                c.number_format = "#,##0"
    ws.freeze_panes = ws.cell(row=r0 + 1, column=1)
    for j, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.sheet_view.showGridLines = False


rows = json.load(open(os.path.join(OUT, "build_summary.json")))
wb = Workbook()
wb.remove(wb.active)

ws = wb.create_sheet("CostPerSite")
ws["A1"] = "Ahrefs standard-tier data collection — cost per site"
ws["A1"].font = TITLE_FONT
ws["A2"] = ("Measured, not estimated. Units are from x-api-units-cost-total-actual. "
            "The last two columns add the cost of making the toxic-anchor search complete.")
ws["A2"].font = Font(italic=True, size=9, color="595959")
table(ws, 4,
      ["domain", "live links", "all-time links", "live refdomains", "all-time refdomains",
       "top-DR rows", "latest rows", "unique slice rows", "lexicon anchors", "lexicon complete",
       "spec units (billed)", "spec units (actual)", "lexicon completion", "TOTAL (complete)",
       "CORRECTED (1 stats call)"],
      [[r["domain"], r["live"], r["all_time"], r["live_refdomains"], r["all_time_refdomains"],
        70, 30, r["unique_slice"], r["lexicon_rows"],
        "yes" if r["lexicon_complete"] else "NO", r["spec_units"], r["spec_actual"],
        r["completion_units"], r["total_complete"], r["corrected_total"]] for r in rows],
      [30, 11, 14, 15, 17, 12, 11, 16, 14, 15, 15, 15, 17, 16, 20],
      num=(2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15))

ws = wb.create_sheet("Usage")
ws["A1"] = "Workspace API usage — before / after"
ws["A1"].font = TITLE_FONT
table(ws, 3, ["snapshot", "workspace units", "api-key units", "taken at"],
      [["before", 250832, 204747, "2026-11-24T08:29:34Z"],
       ["after", 292325, 246240, "2026-11-24T08:44Z"],
       ["delta", 41493, 41493, ""]],
      [14, 16, 16, 26], num=(2, 3))
ws["A7"] = "Reconciliation of the 41,493-unit delta"
ws["A7"].font = Font(bold=True, size=10)
table(ws, 8, ["item", "units"],
      [["Exploratory first run of smoke-fire.com", 2894],
       ["Five standard-tier runs (as written)", 17014],
       ["Two complete-lexicon runs (123eft, kanuksheepskin)", 15500],
       ["Diagnostic probes (metrics-history, stats history, anchors offset/term caps)", 3585],
       ["Validation run of corrected one-stats-call pipeline (smoke-fire)", 2500],
       ["Sum", 41493]],
      [70, 14], num=(2,))

ws = wb.create_sheet("SpecConformance")
ws["A1"] = "Spec v2 (v3-lean) — query-by-query conformance"
ws["A1"].font = TITLE_FONT
table(ws, 3,
      ["#", "spec query", "verified result", "unit cost observed", "spec estimate", "verdict"],
      [
        ["0", "subscription-info/limits-and-usage", "Works. Returns 800k workspace cap and usage.", "0", "0", "OK"],
        ["1", "backlinks-stats — 1 call (corrected)",
         "Returns all four counts in one row: live, all_time, live_refdomains, all_time_refdomains. `history`, `protocol`, `aggregation` and `select` are accepted but ignored. `since:` is impossible.",
         "50 per call", "150", "FIXED — 1 call returns all four"],
        ["2", "refdomains-history monthly, 48 mo",
         "Works. Returns 49 rows (inclusive month boundaries).",
         "6/row = 294", "~290", "OK (49 not 48)"],
        ["3", "metrics-history select=date,org_traffic",
         "Works but data-dependent: 0 rows for abipr.com, 1 for smoke-fire, 21-37 for others. Starts at first indexed month; missing row != zero traffic.",
         "11/row (231-407)", "~530", "PARTIAL — not a reliable 'traffic cliff'"],
        ["4", "top 70 live backlinks by DR, 1/domain",
         "Works. 70 unique domains, DR strictly descending.",
         "6/row = 420", "~420", "OK"],
        ["5", "latest 30 live backlinks, 1/domain",
         "Works. Strictly first_seen_link descending; 1-4 rows overlap #4.",
         "6/row = 180", "~180", "OK"],
        ["6", "one batched lexicon `or` on anchors, all_time",
         "Returns rows but HITS THE 250 CAP on 2 of 5 domains (250 of 273 / 267 real matches). 10 units/row. Completing it costs 7.6-7.9k, not 50-500.",
         "1,650-2,500 batched; 7,640-7,860 complete", "~50-500", "BROKEN for cost + completeness"],
        ["7", "top 50 live anchors by links",
         "Works.", "4/row = 200", "~200", "OK"],
      ],
      [4, 38, 78, 26, 14, 34])

ws = wb.create_sheet("Determinism")
ws["A1"] = "Is the spec enough to get the §8 signals deterministically?"
ws["A1"].font = TITLE_FONT
table(ws, 3,
      ["§8 signal", "whole profile?", "status", "why"],
      [
        ["Live / all-time size", "yes", "DETERMINISTIC", "backlinks-stats returns both in one row."],
        ["Post-drop size", "claimed yes", "NOT AVAILABLE",
         "No drop_date in run inputs; `history=since:*` ignored by the API. Only refdomains-history can proxy it."],
        ["Post-drop refdomain spike", "yes", "DETERMINISTIC but drop date must be inferred",
         "Monthly series is complete; the 'post' side is unknown without a drop date."],
        ["Traffic history / cliff / pre-drop baseline", "claimed yes", "NOT DETERMINISTIC",
         "metrics-history returns nothing or starts late for expired domains. Zero vs no-data is indistinguishable."],
        ["Toxic anchors anywhere", "claimed yes", "NOT DETERMINISTIC",
         "Batched OR truncates at 250; completing requires 39 extra requests and is impossible if a single term >250. No offset pagination."],
        ["Strongest 70 referring domains", "slice", "DETERMINISTIC", "Verified 70 unique domains, DR desc."],
        ["Current spam activity (latest 30)", "slice", "DETERMINISTIC", "Verified strict date desc."],
        ["Anchor mix (top 50)", "slice", "DETERMINISTIC", "Verified."],
        ["Topical fit", "slice", "DETERMINISTIC as far as the slice goes",
         "Only URLs/anchors in the 70+30 slice; no content signal."],
      ],
      [40, 16, 34, 82])

wb.save(os.path.join(OUT, "_all-sites-summary.xlsx"))
print("wrote output/_all-sites-summary.xlsx")
