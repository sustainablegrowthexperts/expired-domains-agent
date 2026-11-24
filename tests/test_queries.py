"""Query-registry contract for the stats slice."""

from __future__ import annotations

from datetime import date

from expired_domains.ahrefs.queries import build_run_queries

RUN_DATE = date(2026, 11, 24)


def test_build_run_queries_returns_three_specs_in_order() -> None:
    specs = build_run_queries("example.com", RUN_DATE)

    assert [spec.query_id for spec in specs] == [
        "limits_before",
        "stats",
        "limits_after",
    ]
    assert [spec.endpoint for spec in specs] == [
        "/subscription-info/limits-and-usage",
        "/site-explorer/backlinks-stats",
        "/subscription-info/limits-and-usage",
    ]
    assert all(spec.page == 0 for spec in specs)


def test_stats_spec_has_exact_params_and_no_history() -> None:
    stats = build_run_queries("example.com", RUN_DATE)[1]

    assert stats.params == {
        "target": "example.com",
        "mode": "domain",
        "date": "2026-11-24",
    }
    assert "history" not in stats.params


def test_limits_specs_carry_no_params() -> None:
    limits_before, _, limits_after = build_run_queries("example.com", RUN_DATE)

    assert limits_before.params == {}
    assert limits_after.params == {}


def test_stats_spec_uses_the_backlinks_stats_path() -> None:
    stats = build_run_queries("example.com", RUN_DATE)[1]

    assert stats.endpoint == "/site-explorer/backlinks-stats"
