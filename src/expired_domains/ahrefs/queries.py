"""Query registry for the stats slice.

The full §3.1 contract is added slice by slice; this module currently returns
the three requests that belong to the first vertical slice.
"""

from __future__ import annotations

from datetime import date

from expired_domains.models import QuerySpec


def build_run_queries(domain: str, run_date: date) -> list[QuerySpec]:
    """Return the ordered query specs for one run."""
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
            query_id="limits_after",
            endpoint="/subscription-info/limits-and-usage",
            params={},
        ),
    ]
