"""Replay parity checks for the reference run corpus."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import conftest
from expired_domains.storage.db import connect
from replay import replay_run

SMOKE_FIRE_STATS = {
    "live": 1159,
    "all_time": 5979,
    "live_refdomains": 992,
    "all_time_refdomains": 1599,
}
SPLIT_STATS_QUERIES = ("stats_all_time", "stats_live")

EXPECTED_RUNS: dict[str, dict[str, Any]] = {
    "smoke-fire.com_20261124T084340Z_ahrefs": {
        "stats": SMOKE_FIRE_STATS,
        "stats_queries": ("stats",),
        "requests": 9,
        "refdomains": 49,
        "traffic": 1,
        "unique": 98,
        "lexicon": 165,
        "top_anchors": 50,
        "lexicon_complete": True,
        "units": (2500, 2844),
    },
    "smoke-fire.com_20261124T082958Z_ahrefs": {
        "stats": SMOKE_FIRE_STATS,
        "stats_queries": SPLIT_STATS_QUERIES,
        "requests": 10,
        "refdomains": 49,
        "traffic": 1,
        "unique": 98,
        "lexicon": 165,
        "top_anchors": 50,
        "lexicon_complete": True,
        "units": (2894, 2894),
    },
    "smoke-fire.com_20261124T083032Z_ahrefs": {
        "stats": SMOKE_FIRE_STATS,
        "stats_queries": SPLIT_STATS_QUERIES,
        "requests": 10,
        "refdomains": 49,
        "traffic": 1,
        "unique": 98,
        "lexicon": 165,
        "top_anchors": 50,
        "lexicon_complete": True,
        "units": (2744, 2894),
    },
    "123eft.com_20261124T083034Z_ahrefs": {
        "stats": {
            "live": 955,
            "all_time": 4669,
            "live_refdomains": 893,
            "all_time_refdomains": 1498,
        },
        "stats_queries": SPLIT_STATS_QUERIES,
        "requests": 10,
        "refdomains": 49,
        "traffic": 36,
        "unique": 96,
        "lexicon": 250,
        "top_anchors": 50,
        "lexicon_complete": False,
        "units": (4040, 4090),
    },
    "kanuksheepskinproducts.com_20261124T083038Z_ahrefs": {
        "stats": {
            "live": 1142,
            "all_time": 1705,
            "live_refdomains": 1095,
            "all_time_refdomains": 1503,
        },
        "stats_queries": SPLIT_STATS_QUERIES,
        "requests": 10,
        "refdomains": 49,
        "traffic": 37,
        "unique": 99,
        "lexicon": 250,
        "top_anchors": 50,
        "lexicon_complete": False,
        "units": (4101, 4101),
    },
    "longbeachcoffee.com_20261124T083044Z_ahrefs": {
        "stats": {
            "live": 736,
            "all_time": 1411,
            "live_refdomains": 723,
            "all_time_refdomains": 1031,
        },
        "stats_queries": SPLIT_STATS_QUERIES,
        "requests": 10,
        "refdomains": 49,
        "traffic": 21,
        "unique": 99,
        "lexicon": 188,
        "top_anchors": 50,
        "lexicon_complete": True,
        "units": (3255, 3305),
    },
    "abipr.com_20261124T083049Z_ahrefs": {
        "stats": {
            "live": 1034,
            "all_time": 2953,
            "live_refdomains": 906,
            "all_time_refdomains": 1513,
        },
        "stats_queries": SPLIT_STATS_QUERIES,
        "requests": 10,
        "refdomains": 49,
        "traffic": 0,
        "unique": 98,
        "lexicon": 168,
        "top_anchors": 50,
        "lexicon_complete": True,
        "units": (2874, 2924),
    },
}


@pytest.mark.parametrize("run_name", sorted(EXPECTED_RUNS))
def test_replay_reproduces_reference_counts_units_and_flags(
    run_name: str, tmp_db: Path
) -> None:
    expected = EXPECTED_RUNS[run_name]
    run_dir = conftest.reference_run_dir(run_name)

    result = replay_run(run_dir, tmp_db)

    actual_units, billed_units = expected["units"]
    assert result.warnings == []
    assert result.run_id == f"{run_dir.parent.name}:{run_name.rsplit('_', 2)[1]}:ahrefs"
    assert result.units_actual == actual_units
    assert result.units_billed == billed_units

    assert result.row_counts == {
        "runs": 1,
        "requests": expected["requests"],
        "stats": 2,
        "history": expected["refdomains"] + expected["traffic"],
        "backlinks": expected["unique"],
        "anchors": expected["lexicon"] + expected["top_anchors"],
    }

    coverage = result.coverage
    assert coverage.stats == expected["stats"]
    assert coverage.rows == {"top_dr": 70, "latest": 30, "unique": expected["unique"]}
    assert coverage.top_anchors == expected["top_anchors"]
    assert coverage.lexicon_complete is expected["lexicon_complete"]
    assert coverage.history_months == 48
    assert coverage.drop_date_source == "unknown"
    assert coverage.prefilter is None

    conn = connect(tmp_db)
    try:
        run = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (result.run_id,)
        ).fetchone()
        assert run["domain"] == run_dir.parent.name
        assert run["mode"] == "domain"
        assert run["tier"] == "standard"
        assert run["units_actual"] == actual_units
        assert json.loads(run["coverage_json"]) == coverage.to_dict()

        stats_rows = {
            row["snapshot"]: (row["links"], row["refdomains"])
            for row in conn.execute(
                "SELECT snapshot, links, refdomains FROM stats WHERE run_id = ?",
                (result.run_id,),
            )
        }
        assert stats_rows == {
            "live": (expected["stats"]["live"], expected["stats"]["live_refdomains"]),
            "all_time": (
                expected["stats"]["all_time"],
                expected["stats"]["all_time_refdomains"],
            ),
        }

        history_counts = {"refdomains": 0, "org_traffic": 0}
        history_counts.update(
            {
                row["series"]: row["count"]
                for row in conn.execute(
                    "SELECT series, COUNT(*) AS count FROM history "
                    "WHERE run_id = ? GROUP BY series",
                    (result.run_id,),
                )
            }
        )
        assert history_counts == {
            "refdomains": expected["refdomains"],
            "org_traffic": expected["traffic"],
        }

        anchor_counts = {
            row["source_query"]: row["count"]
            for row in conn.execute(
                "SELECT source_query, COUNT(*) AS count FROM anchors "
                "WHERE run_id = ? GROUP BY source_query",
                (result.run_id,),
            )
        }
        assert anchor_counts == {
            "lexicon_anchors": expected["lexicon"],
            "top_anchors": expected["top_anchors"],
        }

        request_counts = {
            row["query_id"]: row["row_count"]
            for row in conn.execute(
                "SELECT query_id, row_count FROM requests WHERE run_id = ?",
                (result.run_id,),
            )
        }
        assert len(request_counts) == expected["requests"]
        assert set(expected["stats_queries"]) <= set(request_counts)
        assert request_counts["top_dr_backlinks"] == 70
        assert request_counts["latest_backlinks"] == 30
        assert request_counts["top_anchors"] == expected["top_anchors"]
    finally:
        conn.close()


def test_unknown_query_id_warns_and_records_a_request_without_failing(
    tmp_path: Path, tmp_db: Path
) -> None:
    source = conftest.reference_run_dir("smoke-fire.com_20261124T084340Z_ahrefs")
    run_dir = tmp_path / source.parent.name / source.name
    shutil.copytree(source, run_dir)

    unknown_document = {
        "request": {
            "query_id": "mystery_query",
            "endpoint": "/site-explorer/unknown",
            "page": 0,
            "params": {"target": "smoke-fire.com"},
            "status": 200,
            "rows": "2",
            "units_cost_total": "10",
            "units_cost_total_actual": "4",
            "cache": "miss",
            "error": None,
            "parse_error": None,
        },
        "payload": {"mystery": [{"id": 1}, {"id": 2}]},
        "raw_body": None,
    }
    headers = {
        "x-api-units-cost-total": "10",
        "x-api-units-cost-total-actual": "4",
    }
    (run_dir / "raw" / "mystery_query.p0.json").write_text(
        json.dumps(unknown_document), encoding="utf-8"
    )
    (run_dir / "raw" / "mystery_query.p0.headers.json").write_text(
        json.dumps(headers), encoding="utf-8"
    )

    result = replay_run(run_dir, tmp_db)

    assert any("mystery_query" in warning for warning in result.warnings)
    assert result.row_counts["requests"] == 10
    assert result.units_actual == 2504
    assert result.units_billed == 2854

    conn = connect(tmp_db)
    try:
        row = conn.execute(
            "SELECT * FROM requests WHERE run_id = ? AND query_id = 'mystery_query'",
            (result.run_id,),
        ).fetchone()
        assert row is not None
        assert row["page"] == 0
        assert row["endpoint"] == "/site-explorer/unknown"
        assert row["status"] == 200
        assert row["row_count"] == 2
        assert row["units_actual"] == 4
    finally:
        conn.close()


def test_replay_is_idempotent_and_never_modifies_raw_fixtures(
    tmp_db: Path,
) -> None:
    run_dir = conftest.reference_run_dir("smoke-fire.com_20261124T084340Z_ahrefs")
    before = _file_fingerprints(run_dir)

    first = replay_run(run_dir, tmp_db)
    second = replay_run(run_dir, tmp_db)

    assert _file_fingerprints(run_dir) == before
    assert first.row_counts == second.row_counts
    assert first.coverage == second.coverage
    assert first.units_actual == second.units_actual
    assert first.units_billed == second.units_billed

    conn = connect(tmp_db)
    try:
        for table, count in second.row_counts.items():
            stored = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (second.run_id,)
            ).fetchone()[0]
            assert stored == count

        source_queries = [
            json.loads(row["source_queries"])
            for row in conn.execute(
                "SELECT source_queries FROM backlinks WHERE run_id = ?",
                (second.run_id,),
            )
        ]
        assert all(len(values) == len(set(values)) for values in source_queries)
    finally:
        conn.close()


def test_run_dirs_returns_only_manifest_bearing_reference_runs() -> None:
    directories = conftest.run_dirs()

    assert {directory.name for directory in directories} == set(EXPECTED_RUNS)
    assert all((directory / "manifest.json").is_file() for directory in directories)
    assert all("lexicon_probes" not in directory.parts for directory in directories)


def _file_fingerprints(root: Path) -> dict[str, tuple[int, str]]:
    """Return a content and mtime fingerprint for every fixture file under ``root``."""
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
