"""Payload → model parsing and coverage computation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from expired_domains.models import Coverage, StatsRow, Units

STATS_SNAPSHOTS = ("live", "all_time")
COVERAGE_HISTORY_MONTHS = 48


def parse_stats(payload: dict[str, Any]) -> list[StatsRow]:
    """Map ``metrics`` to the two ``stats`` snapshot rows from §3.5."""
    metrics = payload.get("metrics") or {}
    return [
        StatsRow(
            snapshot="live",
            links=metrics.get("live"),
            refdomains=metrics.get("live_refdomains"),
        ),
        StatsRow(
            snapshot="all_time",
            links=metrics.get("all_time"),
            refdomains=metrics.get("all_time_refdomains"),
        ),
    ]


def extract_units(headers: Mapping[str, str]) -> Units:
    """Read actual and billed API units, treating missing/invalid values as zero."""
    return Units(
        actual=_integer_header(headers, "x-api-units-cost-total-actual"),
        billed=_integer_header(headers, "x-api-units-cost-total"),
    )


def compute_coverage(
    stats_rows: Sequence[StatsRow],
    backlink_rows: Sequence[Any] = (),
    anchor_rows: Sequence[Any] = (),
) -> Coverage:
    """Return the §3.8 coverage object for this slice."""
    del backlink_rows, anchor_rows  # Populated by later slices.

    stats: dict[str, int | None] = {
        "live": None,
        "all_time": None,
        "live_refdomains": None,
        "all_time_refdomains": None,
    }
    for row in stats_rows:
        if row.snapshot == "live":
            stats["live"] = row.links
            stats["live_refdomains"] = row.refdomains
        elif row.snapshot == "all_time":
            stats["all_time"] = row.links
            stats["all_time_refdomains"] = row.refdomains

    return Coverage(
        mode="top_n",
        stats=stats,
        rows={"top_dr": 0, "latest": 0, "unique": 0},
        top_anchors=0,
        lexicon_complete=True,
        history_months=COVERAGE_HISTORY_MONTHS,
        drop_date_source="unknown",
        prefilter=None,
    )


def _integer_header(headers: Mapping[str, str], name: str) -> int:
    """Return a case-insensitive header as an int, or zero when unusable."""
    value: str | None = None
    for key, candidate in headers.items():
        if key.lower() == name:
            value = candidate
            break
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
