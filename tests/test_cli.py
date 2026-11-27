"""CLI surface for the ``enrich`` slice."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

import expired_domains.enrich as enrich
from conftest import reference_run_dir
from expired_domains import cli
from expired_domains.ahrefs.client import AhrefsClient
from expired_domains.config import PROJECT_ROOT
from expired_domains.models import Coverage, RequestRecord, RunSummary, Units
from expired_domains.storage.db import connect, init_db, insert_run, transaction
from expired_domains.storage.raw import make_run_id, run_dir_name

FIXTURE_RUN = "smoke-fire.com_20261124T084340Z_ahrefs"
QUERY_IDS = [
    "limits_before",
    "stats",
    "refdomains_history",
    "metrics_history",
    "top_dr_backlinks",
    "latest_backlinks",
    "lexicon_anchors",
    "top_anchors",
    "limits_after",
]


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


def _install_fixture_client(
    monkeypatch, *, fail_query: str | None = None
) -> tuple[list[str], dict[str, dict]]:
    """Install a MockTransport-backed client that replays the corrected fixture."""
    run_dir = reference_run_dir(FIXTURE_RUN)
    payloads: dict[str, dict] = {}
    headers: dict[str, dict] = {}
    for query_id in QUERY_IDS:
        payload_doc = json.loads(
            (run_dir / "raw" / f"{query_id}.p0.json").read_text(encoding="utf-8")
        )
        payloads[query_id] = payload_doc["payload"]
        headers[query_id] = json.loads(
            (run_dir / "raw" / f"{query_id}.p0.headers.json").read_text(encoding="utf-8")
        )

    attempts: list[str] = []
    limits_seen = 0

    def resolve(request: httpx.Request) -> str:
        nonlocal limits_seen
        path = request.url.path
        params = dict(request.url.params)
        if path.endswith("/subscription-info/limits-and-usage"):
            limits_seen += 1
            return "limits_before" if limits_seen == 1 else "limits_after"
        if path.endswith("/site-explorer/all-backlinks"):
            return "top_dr_backlinks" if params.get("limit") == "70" else "latest_backlinks"
        if path.endswith("/site-explorer/anchors"):
            return "lexicon_anchors" if params.get("history") == "all_time" else "top_anchors"
        for suffix, query_id in {
            "/site-explorer/backlinks-stats": "stats",
            "/site-explorer/refdomains-history": "refdomains_history",
            "/site-explorer/metrics-history": "metrics_history",
        }.items():
            if path.endswith(suffix):
                return query_id
        raise AssertionError(f"unexpected request path: {path}")

    def handler(request: httpx.Request) -> httpx.Response:
        query_id = resolve(request)
        attempts.append(query_id)
        if query_id == fail_query:
            return httpx.Response(
                500,
                json={"error": "server error"},
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json=payloads[query_id], headers=headers[query_id])

    mock_transport = httpx.MockTransport(handler)

    def factory(settings, **_kwargs):
        return AhrefsClient(settings, transport=mock_transport, sleep=lambda _delay: None)

    monkeypatch.setattr(enrich, "AhrefsClient", factory)
    return attempts, payloads


def _attempt_order(attempts: list[str]) -> list[str]:
    """Collapse consecutive retries so each query appears once, in execution order."""
    order: list[str] = []
    for query_id in attempts:
        if not order or order[-1] != query_id:
            order.append(query_id)
    return order


def _seed_existing_run(
    db_path: Path, *, run_id: str, fetched_at: str, units_actual: int = 0
) -> None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        with transaction(conn):
            insert_run(
                conn,
                run_id=run_id,
                domain="smoke-fire.com",
                mode="domain",
                tier="standard",
                prefilter_batch=None,
                drop_date=None,
                drop_date_source="unknown",
                fetched_at=fetched_at,
                config_json=json.dumps({"spec": "test"}),
                coverage_json=json.dumps({"mode": "top_n"}),
                units_actual=units_actual,
                error=None,
            )
    finally:
        conn.close()


def _seed_today_run(settings) -> tuple[str, str]:
    """Insert a run fetched today (UTC) with a timestamp 30s away from now."""
    now = datetime.now(UTC)
    seed_time = now.replace(second=(now.second + 30) % 60, microsecond=0)
    run_id = make_run_id("smoke-fire.com", seed_time)
    _seed_existing_run(settings.db_path, run_id=run_id, fetched_at=seed_time.isoformat())
    return run_id, seed_time.isoformat()


def _manifest_dirs(settings) -> list[Path]:
    domain_root = settings.raw_root / "smoke-fire.com"
    return [path for path in domain_root.iterdir() if path.is_dir()]


def test_full_mocked_run_prints_nine_lines_and_summary(
    tmp_path: Path, settings_factory, capsys, monkeypatch
) -> None:
    settings = settings_factory()
    attempts, _payloads = _install_fixture_client(monkeypatch)

    exit_code = cli.main(
        ["enrich", "smoke-fire.com", "--db", str(settings.db_path)],
        settings=settings,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert _attempt_order(attempts) == QUERY_IDS
    assert re.findall(r"^(\w+): status=", captured.out, flags=re.MULTILINE) == QUERY_IDS
    assert "units: actual=2500 billed=2844" in captured.out
    assert "1159" in captured.out
    assert "5979" in captured.out
    assert "992" in captured.out
    assert "1599" in captured.out
    assert "lexicon_complete=true" in captured.out

    conn = connect(settings.db_path)
    try:
        run = conn.execute("SELECT * FROM runs").fetchone()
        assert run["units_actual"] == 2500
        assert run["error"] is None
        coverage = json.loads(run["coverage_json"])
        assert coverage["stats"] == {
            "live": 1159,
            "all_time": 5979,
            "live_refdomains": 992,
            "all_time_refdomains": 1599,
        }
        assert coverage["rows"] == {"top_dr": 70, "latest": 30, "unique": 98}
        assert coverage["top_anchors"] == 50
        assert coverage["lexicon_complete"] is True
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 9
    finally:
        conn.close()

    run_dirs = _manifest_dirs(settings)
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider"] == "ahrefs"
    assert manifest["api_date"] == datetime.now(UTC).date().isoformat()
    assert manifest["units_actual_total"] == 2500
    assert manifest["units_billed_total"] == 2844
    assert manifest["coverage"]["rows"]["unique"] == 98
    assert len(manifest["requests"]) == 9
    assert all(entry["error"] is None for entry in manifest["requests"])
    assert manifest["error"] is None
    assert all((run_dirs[0] / f"{query_id}.p0.json").is_file() for query_id in QUERY_IDS)


def test_same_day_run_without_force_is_refused(settings_factory, capsys, monkeypatch) -> None:
    settings = settings_factory()
    existing_id, _fetched_at = _seed_today_run(settings)
    attempts, _payloads = _install_fixture_client(monkeypatch)

    exit_code = cli.main(["enrich", "smoke-fire.com"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert attempts == []
    assert existing_id in captured.out
    assert "--force" in captured.out

    conn = connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        conn.close()
    assert not settings.raw_root.exists()


def test_force_bypasses_the_guard_and_keeps_the_previous_run(
    settings_factory, capsys, monkeypatch
) -> None:
    settings = settings_factory()
    existing_id, _fetched_at = _seed_today_run(settings)
    previous_dir = settings.raw_root / "smoke-fire.com" / run_dir_name(existing_id)
    previous_dir.mkdir(parents=True)
    sentinel = previous_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    attempts, _payloads = _install_fixture_client(monkeypatch)

    exit_code = cli.main(["enrich", "smoke-fire.com", "--force"], settings=settings)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert _attempt_order(attempts) == QUERY_IDS
    assert "units: actual=2500 billed=2844" in captured.out
    assert sentinel.read_text(encoding="utf-8") == "keep"

    conn = connect(settings.db_path)
    try:
        run_ids = {row["run_id"] for row in conn.execute("SELECT run_id FROM runs")}
        assert len(run_ids) == 2
        assert existing_id in run_ids
        new_run_id = next(run_id for run_id in run_ids if run_id != existing_id)
        assert (
            conn.execute(
                "SELECT units_actual FROM runs WHERE run_id = ?", (existing_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()

    new_dir = settings.raw_root / "smoke-fire.com" / run_dir_name(new_run_id)
    assert (new_dir / "manifest.json").is_file()
    assert previous_dir.is_dir()


def test_one_failed_query_is_recorded_and_the_run_continues(
    settings_factory, capsys, monkeypatch
) -> None:
    settings = settings_factory()
    attempts, _payloads = _install_fixture_client(monkeypatch, fail_query="metrics_history")

    exit_code = cli.main(
        ["enrich", "smoke-fire.com", "--db", str(settings.db_path)],
        settings=settings,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert _attempt_order(attempts) == QUERY_IDS
    assert attempts.count("metrics_history") == 6
    assert "metrics_history" in captured.out
    assert "status=500" in captured.out

    conn = connect(settings.db_path)
    try:
        statuses = {
            row["query_id"]: row["status"]
            for row in conn.execute("SELECT query_id, status FROM requests")
        }
        assert len(statuses) == 9
        assert statuses.pop("metrics_history") == 500
        assert set(statuses.values()) == {200}

        run = conn.execute("SELECT * FROM runs").fetchone()
        assert json.loads(run["error"]) == {"metrics_history": "HTTP 500 after 6 attempt(s)"}
        assert (
            conn.execute("SELECT COUNT(*) FROM history WHERE series = 'org_traffic'").fetchone()[0]
            == 0
        )
    finally:
        conn.close()

    run_dirs = _manifest_dirs(settings)
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    failures = {
        entry["query_id"]: entry["error"] for entry in manifest["requests"] if entry["error"]
    }
    assert failures == {"metrics_history": "HTTP 500 after 6 attempt(s)"}
    assert manifest["error"] == failures

    for query_id in QUERY_IDS:
        assert (run_dirs[0] / f"{query_id}.p0.json").is_file()
        assert (run_dirs[0] / f"{query_id}.p0.headers.json").is_file()

    failed_raw = json.loads((run_dirs[0] / "metrics_history.p0.json").read_text(encoding="utf-8"))
    assert failed_raw["payload"] is None
    assert failed_raw["request"]["status"] == 500
    assert failed_raw["request"]["attempts"] == 6
    assert failed_raw["request"]["error"] == "HTTP 500 after 6 attempt(s)"


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
    assert "backlinks: top_dr=0 latest=0 unique=0" in captured.out
