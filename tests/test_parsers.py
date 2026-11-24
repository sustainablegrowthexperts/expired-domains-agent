"""Payload parsing, unit extraction, and coverage-object contract."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import expired_domains.storage.parsers as parsers
from conftest import reference_run_dir
from expired_domains.models import StatsRow, Units
from expired_domains.storage.parsers import compute_coverage, extract_units, parse_stats

STATS_RUN = "smoke-fire.com_20261124T084340Z_ahrefs"
TRAFFIC_RUN = "123eft.com_20261124T083034Z_ahrefs"
ZERO_TRAFFIC_RUN = "abipr.com_20261124T083049Z_ahrefs"


def _reference_payload(run_name: str, query_id: str) -> dict[str, Any]:
    run_dir = reference_run_dir(run_name)
    raw = json.loads((run_dir / "raw" / f"{query_id}.p0.json").read_text(encoding="utf-8"))
    return raw["payload"]


def _reference_stats_payload() -> dict[str, Any]:
    return _reference_payload(STATS_RUN, "stats")


def test_parse_stats_maps_the_corrected_fixture_counts() -> None:
    rows = parse_stats(_reference_stats_payload())

    assert len(rows) == 2
    by_snapshot = {row.snapshot: row for row in rows}
    assert set(by_snapshot) == {"live", "all_time"}

    assert by_snapshot["live"].links == 1159
    assert by_snapshot["live"].refdomains == 992
    assert by_snapshot["all_time"].links == 5979
    assert by_snapshot["all_time"].refdomains == 1599


def test_parse_stats_keeps_null_values_as_none() -> None:
    rows = parse_stats(
        {
            "metrics": {
                "live": None,
                "all_time": 10,
                "live_refdomains": None,
                "all_time_refdomains": 3,
            }
        }
    )
    by_snapshot = {row.snapshot: row for row in rows}

    assert by_snapshot["live"] == StatsRow(snapshot="live", links=None, refdomains=None)
    assert by_snapshot["all_time"] == StatsRow(snapshot="all_time", links=10, refdomains=3)


def test_parse_refdomains_history_maps_the_corrected_fixture() -> None:
    rows = parsers.parse_refdomains_history(_reference_payload(STATS_RUN, "refdomains_history"))

    assert len(rows) == 49
    assert {row.series for row in rows} == {"refdomains"}
    assert rows[0].date == "2022-11-01"
    assert rows[-1].date == "2026-11-01"
    assert [row.value for row in rows[:2]] == [48, 22]
    assert all(isinstance(row.value, int) for row in rows)
    assert all(len(row.date) == 10 and row.date[4] == row.date[7] == "-" for row in rows)


def test_parse_metrics_history_maps_the_corrected_fixture() -> None:
    rows = parsers.parse_metrics_history(_reference_payload(STATS_RUN, "metrics_history"))

    assert len(rows) == 1
    assert rows[0].series == "org_traffic"
    assert rows[0].date == "2026-11-01"
    assert rows[0].value == 0


def test_parse_metrics_history_maps_all_36_traffic_rows() -> None:
    rows = parsers.parse_metrics_history(_reference_payload(TRAFFIC_RUN, "metrics_history"))

    assert len(rows) == 36
    assert {row.series for row in rows} == {"org_traffic"}
    assert rows[0].date == "2022-11-01"
    assert rows[-1].date == "2025-10-01"


def test_parse_metrics_history_accepts_an_empty_zero_row_payload() -> None:
    rows = parsers.parse_metrics_history(_reference_payload(ZERO_TRAFFIC_RUN, "metrics_history"))

    assert rows == []


def test_parse_metrics_history_treats_missing_or_null_list_as_empty() -> None:
    assert parsers.parse_metrics_history({}) == []
    assert parsers.parse_metrics_history({"metrics": None}) == []


def test_parse_metrics_history_keeps_a_null_value_as_none() -> None:
    rows = parsers.parse_metrics_history(
        {"metrics": [{"date": "2026-11-01T00:00:00Z", "org_traffic": None, "extra": 1}]}
    )

    assert len(rows) == 1
    assert rows[0].series == "org_traffic"
    assert rows[0].date == "2026-11-01"
    assert rows[0].value is None


def test_parse_refdomains_history_ignores_extra_fields() -> None:
    rows = parsers.parse_refdomains_history(
        {"refdomains": [{"date": "2026-11-01T00:00:00Z", "refdomains": 5, "extra": "ignored"}]}
    )

    assert len(rows) == 1
    assert rows[0].series == "refdomains"
    assert rows[0].date == "2026-11-01"
    assert rows[0].value == 5


def test_extract_units_reads_actual_and_billed() -> None:
    headers = {
        "x-api-cache": "hit",
        "x-api-units-cost-total": "50",
        "x-api-units-cost-total-actual": "0",
    }

    assert extract_units(headers) == Units(actual=0, billed=50)


def test_extract_units_tolerates_missing_blank_and_invalid_values() -> None:
    assert extract_units({}) == Units(actual=0, billed=0)
    assert (
        extract_units(
            {
                "x-api-units-cost-total": "",
                "x-api-units-cost-total-actual": "not-a-number",
            }
        )
        == Units(actual=0, billed=0)
    )


def test_compute_coverage_has_the_exact_spec_shape() -> None:
    coverage = compute_coverage(parse_stats(_reference_stats_payload()))
    data = asdict(coverage)

    assert set(data) == {
        "mode",
        "stats",
        "rows",
        "top_anchors",
        "lexicon_complete",
        "history_months",
        "drop_date_source",
        "prefilter",
    }
    assert data["mode"] == "top_n"
    assert data["stats"] == {
        "live": 1159,
        "all_time": 5979,
        "live_refdomains": 992,
        "all_time_refdomains": 1599,
    }
    assert data["rows"] == {"top_dr": 0, "latest": 0, "unique": 0}
    assert data["top_anchors"] == 0
    assert data["lexicon_complete"] is True
    assert data["history_months"] == 48
    assert data["drop_date_source"] == "unknown"
    assert data["prefilter"] is None


def test_compute_coverage_nulls_all_stats_when_stats_are_skipped() -> None:
    data = asdict(compute_coverage([]))

    assert data["stats"] == {
        "live": None,
        "all_time": None,
        "live_refdomains": None,
        "all_time_refdomains": None,
    }
