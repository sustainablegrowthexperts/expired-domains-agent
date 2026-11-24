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


def _synthetic_backlink(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "url_from": "HTTPS://Example.COM:443/Path?Q=1",
        "url_to": "http://Example.com:80/",
        "anchor": "  Foo &amp;\n\tBar  ",
        "first_seen_link": "2026-01-02T03:04:05Z",
        "is_dofollow": True,
        "domain_rating_source": 42.0,
    }
    row.update(overrides)
    return row


def test_normalize_url_lowercases_scheme_and_host_and_strips_default_ports() -> None:
    assert (
        parsers.normalize_url("HTTPS://Example.COM:443/Path?Q=1")
        == "https://example.com/Path?Q=1"
    )
    assert parsers.normalize_url("http://example.com:80/") == "http://example.com/"


def test_normalize_url_preserves_non_default_ports_and_fragments() -> None:
    assert (
        parsers.normalize_url("HTTPS://Example.COM:8443/Path?Q=1#Frag")
        == "https://example.com:8443/Path?Q=1#Frag"
    )


def test_normalize_url_returns_the_raw_value_when_parsing_fails() -> None:
    assert parsers.normalize_url("not a url") == "not a url"
    assert parsers.normalize_url(None) is None


def test_normalize_anchor_unescapes_collapses_whitespace_and_lowercases() -> None:
    assert parsers.normalize_anchor("  Foo &amp;\n\tBar  ") == "foo & bar"
    assert parsers.normalize_anchor(None) is None
    assert parsers.normalize_anchor("") == ""


def test_root_domain_uses_the_offline_public_suffix_list() -> None:
    assert parsers.root_domain("blog.example.co.uk") == "example.co.uk"
    assert parsers.root_domain("www.example.com") == "example.com"
    assert parsers.root_domain("not a host") is None


def test_link_hash_is_stable_sensitive_to_each_input_and_matches_the_contract() -> None:
    args = (
        "HTTPS://Example.COM:443/Path?Q=1",
        "http://Example.com:80/",
        "hello & world",
        "2026-01-02T03:04:05Z",
    )
    digest = parsers.link_hash(*args)

    assert digest == parsers.link_hash(*args)
    assert digest == "c8ae19057d2e8b5d3e005e65a6c5605df6cc4894a06725ad74e87717f494e29a"
    for index in range(4):
        changed = list(args)
        changed[index] = changed[index] + "x"
        assert parsers.link_hash(*changed) != digest


def test_link_hash_input_assembles_the_spec_contract() -> None:
    payload = parsers.link_hash_input(
        "HTTPS://Example.COM:443/Path?Q=1",
        "http://Example.com:80/",
        "hello & world",
        "2026-01-02T03:04:05Z",
    )

    assert payload == (
        "ahrefs\nhttps://example.com/Path?Q=1\nhttp://example.com/\n"
        "hello & world\n2026-01-02T03:04:05Z"
    )


def test_link_hash_uses_empty_string_for_null_values() -> None:
    with_null = parsers.link_hash("https://example.com/", None, None, None)
    with_empty = parsers.link_hash("https://example.com/", "", "", "")

    assert with_null == with_empty


def test_parse_backlinks_maps_the_corrected_fixture_counts() -> None:
    top_dr = parsers.parse_backlinks(
        _reference_payload(STATS_RUN, "top_dr_backlinks"), "top_dr_backlinks"
    )
    latest = parsers.parse_backlinks(
        _reference_payload(STATS_RUN, "latest_backlinks"), "latest_backlinks"
    )

    assert len(top_dr) == 70
    assert len(latest) == 30

    first = top_dr[0]
    assert first.source_queries == ["top_dr_backlinks"]
    assert first.url_from == "https://www.dexknows.com/toledo-oh/pottery"
    assert first.url_to == "http://smoke-fire.com/"
    assert first.domain_from == "www.dexknows.com"
    assert first.root_domain_from == "dexknows.com"
    assert first.anchor_raw == "Icon - external link Visit Website"
    assert first.anchor_norm == "icon - external link visit website"
    assert first.is_dofollow is False
    assert first.domain_rating == 78.0
    assert first.first_seen_link == "2026-09-26T10:43:40Z"
    assert first.era is None
    assert json.loads(first.raw_json) == _synthetic_backlink(
        url_from="https://www.dexknows.com/toledo-oh/pottery",
        url_to="http://smoke-fire.com/",
        anchor="Icon - external link Visit Website",
        first_seen_link="2026-09-26T10:43:40Z",
        is_dofollow=False,
        domain_rating_source=78.0,
    )


def test_parse_backlinks_preserves_null_anchors_and_null_first_seen() -> None:
    rows = parsers.parse_backlinks(
        {
            "backlinks": [
                _synthetic_backlink(anchor=None, first_seen_link=None),
            ]
        },
        "top_dr_backlinks",
    )

    assert len(rows) == 1
    assert rows[0].anchor_raw is None
    assert rows[0].anchor_norm is None
    assert rows[0].first_seen_link is None
    assert rows[0].link_hash == parsers.link_hash(
        "HTTPS://Example.COM:443/Path?Q=1", "http://Example.com:80/", None, None
    )


def test_parse_backlinks_keeps_rows_with_invalid_urls_but_nulls_derived_fields() -> None:
    rows = parsers.parse_backlinks(
        {"backlinks": [_synthetic_backlink(url_from="not a url", url_to="also bad")]},
        "latest_backlinks",
    )

    assert len(rows) == 1
    assert rows[0].url_from == "not a url"
    assert rows[0].url_to == "also bad"
    assert rows[0].domain_from is None
    assert rows[0].root_domain_from is None


def test_merge_backlink_rows_collapses_cross_query_duplicates() -> None:
    payload = {"backlinks": [_synthetic_backlink()]}
    top_dr = parsers.parse_backlinks(payload, "top_dr_backlinks")
    latest = parsers.parse_backlinks(payload, "latest_backlinks")

    merged = parsers.merge_backlink_rows([*top_dr, *latest])

    assert len(merged) == 1
    assert merged[0].source_queries == ["latest_backlinks", "top_dr_backlinks"]
    assert merged[0].link_hash == top_dr[0].link_hash


def test_compute_coverage_reports_backlink_slice_counts() -> None:
    top_dr = parsers.parse_backlinks(
        _reference_payload(STATS_RUN, "top_dr_backlinks"), "top_dr_backlinks"
    )
    latest = parsers.parse_backlinks(
        _reference_payload(STATS_RUN, "latest_backlinks"), "latest_backlinks"
    )

    data = asdict(compute_coverage(parse_stats(_reference_stats_payload()), top_dr, latest))

    assert data["rows"] == {"top_dr": 70, "latest": 30, "unique": 98}
