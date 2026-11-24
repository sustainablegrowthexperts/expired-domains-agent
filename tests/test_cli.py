"""CLI surface for the ``enrich`` slice."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from expired_domains import cli
from expired_domains.config import PROJECT_ROOT
from expired_domains.models import Coverage, RequestRecord, RunSummary, Units


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "expired_domains", *args],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
    )


def _fixed_summary(raw_dir: Path) -> RunSummary:
    coverage = Coverage(
        mode="top_n",
        stats={
            "live": 1159,
            "all_time": 5979,
            "live_refdomains": 992,
            "all_time_refdomains": 1599,
        },
        rows={"top_dr": 0, "latest": 0, "unique": 0},
        top_anchors=0,
        lexicon_complete=True,
        history_months=48,
        drop_date_source="unknown",
        prefilter=None,
    )
    request = RequestRecord(
        query_id="stats",
        endpoint="/site-explorer/backlinks-stats",
        page=0,
        params={"target": "smoke-fire.com", "mode": "domain", "date": "2026-11-24"},
        url="https://api.ahrefs.com/v3/site-explorer/backlinks-stats",
        status=200,
        started_at="2026-11-24T08:43:41+00:00",
        finished_at="2026-11-24T08:43:41+00:00",
        attempts=1,
        error=None,
        parse_error=None,
        rows="1",
        units_cost_total="50",
        units_cost_total_actual="0",
        units_cost_row="12",
        cache="hit",
        headers={"x-api-cache": "hit"},
        payload={"metrics": {}},
    )
    return RunSummary(
        run_id="smoke-fire.com:20261124T084340Z:ahrefs",
        domain="smoke-fire.com",
        raw_dir=raw_dir,
        requests=[request],
        coverage=coverage,
        units=Units(actual=0, billed=50),
        error=None,
        exit_code=0,
    )


def test_help_lists_the_enrich_command() -> None:
    result = _run_module("--help")

    assert result.returncode == 0
    assert "enrich" in result.stdout


def test_version_still_exits_zero() -> None:
    result = _run_module("--version")

    assert result.returncode == 0


def test_missing_key_exits_two_and_names_the_variable(
    tmp_path: Path, settings_factory, capsys, monkeypatch
) -> None:
    settings = settings_factory(ahrefs_key=None)

    exit_code = cli.main(
        ["enrich", "example.com", "--db", str(tmp_path / "index.sqlite3")],
        settings=settings,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "AHREFS_KEY" in captured.err
    assert not settings.raw_root.exists()


def test_successful_run_prints_query_line_and_summary(
    tmp_path: Path, settings_factory, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "run_enrich", lambda *args, **kwargs: _fixed_summary(tmp_path / "run"))

    exit_code = cli.main(
        ["enrich", "smoke-fire.com", "--db", str(tmp_path / "index.sqlite3")],
        settings=settings_factory(),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "stats" in captured.out
    assert "status=200" in captured.out
    assert "1159" in captured.out
    assert "5979" in captured.out
    assert "992" in captured.out
    assert "1599" in captured.out
