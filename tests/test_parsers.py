"""Payload parsing, unit extraction, and coverage-object contract."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from conftest import reference_run_dir
from expired_domains.models import StatsRow, Units
from expired_domains.storage.parsers import compute_coverage, extract_units, parse_stats

STATS_RUN = "smoke-fire.com_20261124T084340Z_ahrefs"


def _reference_stats_payload() -> dict[str, Any]:
    run_dir = reference_run_dir(STATS_RUN)
    raw = json.loads((run_dir / "raw" / "stats.p0.json").read_text(encoding="utf-8"))
    return raw["payload"]


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
