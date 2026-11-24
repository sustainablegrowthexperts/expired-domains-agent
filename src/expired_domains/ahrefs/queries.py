"""Query registry for the stats, history, backlink, and anchor slices.

The full §3.1 contract is added slice by slice; this module returns the
requests that belong to the implemented vertical slices.
"""

from __future__ import annotations

from datetime import date

from expired_domains.ahrefs.lexicon import build_where, load_lexicon
from expired_domains.models import QuerySpec

HISTORY_MONTHS = 48

BACKLINK_SELECT = "url_from,url_to,anchor,first_seen_link,is_dofollow,domain_rating_source"
LEXICON_ANCHOR_SELECT = "anchor,links_to_target,dofollow_links,refdomains,first_seen,last_seen"
TOP_ANCHOR_SELECT = "anchor,links_to_target,dofollow_links,first_seen"


def history_date_from(run_date: date) -> date:
    """Return the first day of the month 48 months before ``run_date``."""
    months = run_date.year * 12 + (run_date.month - 1) - HISTORY_MONTHS
    year, month_index = divmod(months, 12)
    return date(year, month_index + 1, 1)


def build_run_queries(domain: str, run_date: date) -> list[QuerySpec]:
    """Return the ordered query specs for one run."""
    date_from = history_date_from(run_date).isoformat()

    return [
        QuerySpec(
            query_id="limits_before",
            endpoint="/subscription-info/limits-and-usage",
            params={},
        ),
        QuerySpec(
            query_id="stats",
            endpoint="/site-explorer/backlinks-stats",
            params={
                "target": domain,
                "mode": "domain",
                "date": run_date.isoformat(),
            },
        ),
        QuerySpec(
            query_id="refdomains_history",
            endpoint="/site-explorer/refdomains-history",
            params={
                "target": domain,
                "mode": "domain",
                "date_from": date_from,
                "history_grouping": "monthly",
            },
        ),
        QuerySpec(
            query_id="metrics_history",
            endpoint="/site-explorer/metrics-history",
            params={
                "target": domain,
                "mode": "domain",
                "date_from": date_from,
                "history_grouping": "monthly",
                "select": "date,org_traffic",
            },
        ),
        QuerySpec(
            query_id="top_dr_backlinks",
            endpoint="/site-explorer/all-backlinks",
            params={
                "target": domain,
                "mode": "domain",
                "history": "live",
                "aggregation": "1_per_domain",
                "limit": "70",
                "order_by": "domain_rating_source:desc",
                "select": BACKLINK_SELECT,
            },
        ),
        QuerySpec(
            query_id="latest_backlinks",
            endpoint="/site-explorer/all-backlinks",
            params={
                "target": domain,
                "mode": "domain",
                "history": "live",
                "aggregation": "1_per_domain",
                "limit": "30",
                "order_by": "first_seen_link:desc",
                "select": BACKLINK_SELECT,
            },
        ),
        QuerySpec(
            query_id="lexicon_anchors",
            endpoint="/site-explorer/anchors",
            params={
                "target": domain,
                "mode": "domain",
                "history": "all_time",
                "limit": "250",
                "select": LEXICON_ANCHOR_SELECT,
                "where": build_where(load_lexicon()),
            },
        ),
        QuerySpec(
            query_id="top_anchors",
            endpoint="/site-explorer/anchors",
            params={
                "target": domain,
                "mode": "domain",
                "history": "live",
                "limit": "50",
                "order_by": "links_to_target:desc",
                "select": TOP_ANCHOR_SELECT,
            },
        ),
        QuerySpec(
            query_id="limits_after",
            endpoint="/subscription-info/limits-and-usage",
            params={},
        ),
    ]
