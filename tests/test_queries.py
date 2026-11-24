"""Query-registry contract for the stats and history slices."""

from __future__ import annotations

from datetime import date

import expired_domains.ahrefs.queries as queries
from expired_domains.ahrefs.queries import build_run_queries

RUN_DATE = date(2026, 11, 24)
HISTORY_DATE_FROM = "2022-11-01"


def test_build_run_queries_returns_five_specs_in_order() -> None:
    specs = build_run_queries("example.com", RUN_DATE)

    assert [spec.query_id for spec in specs] == [
        "limits_before",
        "stats",
        "refdomains_history",
        "metrics_history",
        "limits_after",
    ]
    assert [spec.endpoint for spec in specs] == [
        "/subscription-info/limits-and-usage",
        "/site-explorer/backlinks-stats",
        "/site-explorer/refdomains-history",
        "/site-explorer/metrics-history",
        "/subscription-info/limits-and-usage",
    ]
    assert all(spec.page == 0 for spec in specs)


def test_history_date_from_is_first_day_of_month_48_months_before() -> None:
    assert queries.history_date_from(RUN_DATE) == date(2022, 11, 1)


def test_refdomains_history_spec_has_exact_params() -> None:
    spec = build_run_queries("example.com", RUN_DATE)[2]

    assert spec.params == {
        "target": "example.com",
        "mode": "domain",
        "date_from": HISTORY_DATE_FROM,
        "history_grouping": "monthly",
    }
    assert "select" not in spec.params


def test_metrics_history_spec_has_exact_params() -> None:
    spec = build_run_queries("example.com", RUN_DATE)[3]

    assert spec.params == {
        "target": "example.com",
        "mode": "domain",
        "date_from": HISTORY_DATE_FROM,
        "history_grouping": "monthly",
        "select": "date,org_traffic",
    }


def test_history_specs_share_the_window_and_never_send_date_to() -> None:
    refdomains, metrics = build_run_queries("example.com", RUN_DATE)[2:4]

    assert refdomains.params["date_from"] == metrics.params["date_from"] == HISTORY_DATE_FROM
    assert refdomains.params["history_grouping"] == "monthly"
    assert metrics.params["history_grouping"] == "monthly"
    assert "date_to" not in refdomains.params
    assert "date_to" not in metrics.params
    assert "select" not in refdomains.params
    assert metrics.params["select"] == "date,org_traffic"
    assert refdomains.params["mode"] == metrics.params["mode"] == "domain"
    assert refdomains.params["target"] == metrics.params["target"] == "example.com"


def test_stats_spec_has_exact_params_and_no_history() -> None:
    stats = build_run_queries("example.com", RUN_DATE)[1]

    assert stats.params == {
        "target": "example.com",
        "mode": "domain",
        "date": "2026-11-24",
    }
    assert "history" not in stats.params


def test_limits_specs_carry_no_params() -> None:
    specs = build_run_queries("example.com", RUN_DATE)

    assert specs[0].params == {}
    assert specs[-1].params == {}


def test_stats_spec_uses_the_backlinks_stats_path() -> None:
    stats = build_run_queries("example.com", RUN_DATE)[1]

    assert stats.endpoint == "/site-explorer/backlinks-stats"
