"""Query-registry contract for the stats and history slices."""

from __future__ import annotations

from datetime import date

import expired_domains.ahrefs.queries as queries
from expired_domains.ahrefs.queries import build_run_queries

RUN_DATE = date(2026, 11, 24)
HISTORY_DATE_FROM = "2022-11-01"


def test_build_run_queries_returns_seven_specs_in_order() -> None:
    specs = build_run_queries("example.com", RUN_DATE)

    assert [spec.query_id for spec in specs] == [
        "limits_before",
        "stats",
        "refdomains_history",
        "metrics_history",
        "top_dr_backlinks",
        "latest_backlinks",
        "limits_after",
    ]
    assert [spec.endpoint for spec in specs] == [
        "/subscription-info/limits-and-usage",
        "/site-explorer/backlinks-stats",
        "/site-explorer/refdomains-history",
        "/site-explorer/metrics-history",
        "/site-explorer/all-backlinks",
        "/site-explorer/all-backlinks",
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


BACKLINK_SELECT = (
    "url_from,url_to,anchor,first_seen_link,is_dofollow,domain_rating_source"
)


def test_top_dr_backlinks_spec_has_exact_params() -> None:
    spec = build_run_queries("example.com", RUN_DATE)[4]

    assert spec.query_id == "top_dr_backlinks"
    assert spec.endpoint == "/site-explorer/all-backlinks"
    assert spec.params == {
        "target": "example.com",
        "mode": "domain",
        "history": "live",
        "aggregation": "1_per_domain",
        "limit": "70",
        "order_by": "domain_rating_source:desc",
        "select": BACKLINK_SELECT,
    }
    assert "offset" not in spec.params


def test_latest_backlinks_spec_has_exact_params() -> None:
    spec = build_run_queries("example.com", RUN_DATE)[5]

    assert spec.query_id == "latest_backlinks"
    assert spec.endpoint == "/site-explorer/all-backlinks"
    assert spec.params == {
        "target": "example.com",
        "mode": "domain",
        "history": "live",
        "aggregation": "1_per_domain",
        "limit": "30",
        "order_by": "first_seen_link:desc",
        "select": BACKLINK_SELECT,
    }
    assert "offset" not in spec.params


def test_backlink_specs_share_every_parameter_except_limit_and_order() -> None:
    top_dr, latest = build_run_queries("example.com", RUN_DATE)[4:6]

    differing = {
        key
        for key in set(top_dr.params) | set(latest.params)
        if top_dr.params.get(key) != latest.params.get(key)
    }
    assert differing == {"limit", "order_by"}
    assert top_dr.params["history"] == latest.params["history"] == "live"
    assert top_dr.params["aggregation"] == latest.params["aggregation"] == "1_per_domain"
    assert top_dr.params["select"] == latest.params["select"] == BACKLINK_SELECT
