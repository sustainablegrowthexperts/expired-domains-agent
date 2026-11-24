"""SQLite schema, pragmas, and idempotent write helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import expired_domains.storage.db as db
import expired_domains.storage.parsers as parsers
from conftest import make_settings, reference_run_dir
from expired_domains import models
from expired_domains.enrich import run_enrich
from expired_domains.storage.db import (
    connect,
    init_db,
    insert_run,
    transaction,
    upsert_request,
    upsert_stats,
)
from expired_domains.storage.parsers import compute_coverage

RUN_ID = "example.com:20261124T000000Z:ahrefs"
CONFIG_JSON = '{"spec": "ahrefs-ingestion-spec-v3.1", "history_months": 48}'
COVERAGE_JSON = (
    '{"mode": "top_n", "stats": {"live": 1, "all_time": 2, '
    '"live_refdomains": 3, "all_time_refdomains": 4}}'
)


def _open(db_path: Path) -> sqlite3.Connection:
    return connect(db_path)


def _primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row["name"] for row in rows if row["pk"]]


def _insert_sample_run(conn: sqlite3.Connection, *, units_actual: int = 0) -> None:
    insert_run(
        conn,
        run_id=RUN_ID,
        domain="example.com",
        mode="domain",
        tier="standard",
        prefilter_batch=None,
        drop_date=None,
        drop_date_source="unknown",
        fetched_at="2026-11-24T00:00:00+00:00",
        config_json=CONFIG_JSON,
        coverage_json=COVERAGE_JSON,
        units_actual=units_actual,
        error=None,
    )


def _month_starts(count: int) -> list[str]:
    """Return ``count`` consecutive month-start dates beginning in 2022-11."""
    year, month = 2022, 11
    dates: list[str] = []
    for _ in range(count):
        dates.append(f"{year:04d}-{month:02d}-01")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return dates


def _history_rows() -> list[models.HistoryRow]:
    """Build the 49 refdomain rows plus one zero-value traffic row."""
    rows = [
        models.HistoryRow(series="refdomains", date=value, value=index)
        for index, value in enumerate(_month_starts(49))
    ]
    rows.append(models.HistoryRow(series="org_traffic", date="2026-11-01", value=0))
    return rows


def test_init_db_creates_slice_tables_with_required_primary_keys(tmp_db: Path) -> None:
    init_db(tmp_db)

    conn = _open(tmp_db)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

        assert {"runs", "requests", "stats"} <= tables
        assert "refdomains" not in tables
        assert [row["name"] for row in conn.execute("PRAGMA table_info(runs)") if row["pk"]] == [
            "run_id"
        ]
        assert _primary_key(conn, "requests") == ["run_id", "query_id", "page"]
        assert _primary_key(conn, "stats") == ["run_id", "snapshot"]
    finally:
        conn.close()


def test_connect_enables_wal_and_foreign_keys(tmp_db: Path) -> None:
    init_db(tmp_db)

    conn = _open(tmp_db)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_db: Path) -> None:
    init_db(tmp_db)
    init_db(tmp_db)

    conn = _open(tmp_db)
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        assert tables.count("runs") == 1
        assert tables.count("requests") == 1
        assert tables.count("stats") == 1
    finally:
        conn.close()


def test_repeated_inserts_and_upserts_do_not_duplicate_rows(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        with transaction(conn):
            _insert_sample_run(conn, units_actual=0)
            _insert_sample_run(conn, units_actual=50)
            upsert_request(
                conn,
                run_id=RUN_ID,
                query_id="stats",
                page=0,
                endpoint="/site-explorer/backlinks-stats",
                params_json='{"target": "example.com"}',
                row_count=1,
                units_actual=50,
                raw_path="raw/stats.p0.json",
                status=200,
                error=None,
            )
            upsert_request(
                conn,
                run_id=RUN_ID,
                query_id="stats",
                page=0,
                endpoint="/site-explorer/backlinks-stats",
                params_json='{"target": "example.com"}',
                row_count=1,
                units_actual=0,
                raw_path="raw/stats.p0.json",
                status=200,
                error=None,
            )
            upsert_stats(conn, run_id=RUN_ID, snapshot="live", links=1159, refdomains=992)
            upsert_stats(conn, run_id=RUN_ID, snapshot="live", links=1160, refdomains=993)
            upsert_stats(conn, run_id=RUN_ID, snapshot="all_time", links=5979, refdomains=1599)

        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM stats").fetchone()[0] == 2

        request = conn.execute("SELECT * FROM requests").fetchone()
        assert request["units_actual"] == 0
        live = conn.execute("SELECT * FROM stats WHERE snapshot = 'live'").fetchone()
        assert live["links"] == 1160
        assert live["refdomains"] == 993
        run = conn.execute("SELECT * FROM runs").fetchone()
        assert run["units_actual"] == 50
    finally:
        conn.close()


def test_coverage_and_config_json_round_trip_through_runs(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        with transaction(conn):
            _insert_sample_run(conn)

        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (RUN_ID,)).fetchone()
        assert run["config_json"] == CONFIG_JSON
        assert run["coverage_json"] == COVERAGE_JSON
        assert run["drop_date_source"] == "unknown"
        assert run["prefilter_batch"] is None
        assert run["drop_date"] is None
    finally:
        conn.close()


def test_requests_are_rejected_without_a_parent_run(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn):
                upsert_request(
                    conn,
                    run_id="missing",
                    query_id="stats",
                    page=0,
                    endpoint="/site-explorer/backlinks-stats",
                    params_json="{}",
                    row_count=None,
                    units_actual=0,
                    raw_path=None,
                    status=500,
                    error="boom",
                )
    finally:
        conn.close()


def test_history_table_exists_with_composite_primary_key(tmp_db: Path) -> None:
    init_db(tmp_db)

    conn = _open(tmp_db)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "history" in tables
        assert _primary_key(conn, "history") == ["run_id", "series", "date"]
    finally:
        conn.close()


def test_upsert_history_rows_is_idempotent_on_the_primary_key(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        with transaction(conn):
            _insert_sample_run(conn)
            db.upsert_history_rows(conn, RUN_ID, _history_rows())
            db.upsert_history_rows(conn, RUN_ID, _history_rows())

        refdomains = conn.execute(
            "SELECT COUNT(*) FROM history WHERE run_id = ? AND series = 'refdomains'",
            (RUN_ID,),
        ).fetchone()[0]
        traffic = conn.execute(
            "SELECT COUNT(*) FROM history WHERE run_id = ? AND series = 'org_traffic'",
            (RUN_ID,),
        ).fetchone()[0]
        assert refdomains == 49
        assert traffic == 1
        assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 50
    finally:
        conn.close()


def test_upsert_history_rows_accepts_an_empty_list(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        with transaction(conn):
            _insert_sample_run(conn)
            db.upsert_history_rows(conn, RUN_ID, [])

        assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    finally:
        conn.close()


def test_history_rows_are_queryable_by_run_and_series_in_date_order(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        rows = list(reversed(_history_rows()))
        with transaction(conn):
            _insert_sample_run(conn)
            db.upsert_history_rows(conn, RUN_ID, rows)

        stored = conn.execute(
            "SELECT date, value FROM history "
            "WHERE run_id = ? AND series = 'refdomains' ORDER BY date",
            (RUN_ID,),
        ).fetchall()
        assert [row["date"] for row in stored] == _month_starts(49)
        assert [row["value"] for row in stored] == list(range(49))
    finally:
        conn.close()


def test_history_rows_are_rejected_without_a_parent_run(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn):
                db.upsert_history_rows(
                    conn,
                    "missing",
                    [models.HistoryRow(series="refdomains", date="2022-11-01", value=1)],
                )
    finally:
        conn.close()


CORRECTED_RUN = "smoke-fire.com_20261124T084340Z_ahrefs"


def _backlink_row(**overrides) -> models.BacklinkRow:
    values = {
        "link_hash": "hash-1",
        "source_queries": ["top_dr_backlinks"],
        "url_from": "https://example.com/a",
        "url_to": "https://example.org/",
        "domain_from": "example.com",
        "root_domain_from": "example.com",
        "anchor_raw": "Anchor",
        "anchor_norm": "anchor",
        "is_dofollow": True,
        "domain_rating": 42.0,
        "first_seen_link": "2026-01-02T03:04:05Z",
        "era": None,
        "raw_json": '{"url_from": "https://example.com/a"}',
    }
    values.update(overrides)
    return models.BacklinkRow(**values)


def _fixture_payload(query_id: str) -> dict:
    run_dir = reference_run_dir(CORRECTED_RUN)
    raw = json.loads((run_dir / "raw" / f"{query_id}.p0.json").read_text(encoding="utf-8"))
    return raw["payload"]


def test_backlinks_table_exists_with_composite_primary_key(tmp_db: Path) -> None:
    init_db(tmp_db)

    conn = _open(tmp_db)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "backlinks" in tables
        assert _primary_key(conn, "backlinks") == ["run_id", "link_hash"]
    finally:
        conn.close()


def test_upsert_backlinks_merges_source_queries_and_keeps_the_canonical_row(
    tmp_db: Path,
) -> None:
    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        with transaction(conn):
            _insert_sample_run(conn)
            db.upsert_backlinks(conn, RUN_ID, [_backlink_row()])
            db.upsert_backlinks(
                conn,
                RUN_ID,
                [
                    _backlink_row(
                        source_queries=["latest_backlinks"],
                        url_from="https://later.example/changed",
                        is_dofollow=False,
                    )
                ],
            )

        assert conn.execute("SELECT COUNT(*) FROM backlinks").fetchone()[0] == 1
        stored = conn.execute("SELECT * FROM backlinks").fetchone()
        assert json.loads(stored["source_queries"]) == [
            "latest_backlinks",
            "top_dr_backlinks",
        ]
        assert stored["url_from"] == "https://example.com/a"
        assert stored["is_dofollow"] == 1
        assert stored["era"] is None
        assert json.loads(stored["raw_json"]) == {"url_from": "https://example.com/a"}
    finally:
        conn.close()


def test_backlinks_are_rejected_without_a_parent_run(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(conn):
                db.upsert_backlinks(conn, "missing", [_backlink_row()])
    finally:
        conn.close()


def test_corrected_fixture_stores_98_unique_backlinks_from_100_rows(tmp_db: Path) -> None:
    top_dr = parsers.parse_backlinks(
        _fixture_payload("top_dr_backlinks"), "top_dr_backlinks"
    )
    latest = parsers.parse_backlinks(
        _fixture_payload("latest_backlinks"), "latest_backlinks"
    )

    init_db(tmp_db)
    conn = _open(tmp_db)
    try:
        with transaction(conn):
            _insert_sample_run(conn)
            db.upsert_backlinks(conn, RUN_ID, top_dr)
            db.upsert_backlinks(conn, RUN_ID, latest)

        assert conn.execute("SELECT COUNT(*) FROM backlinks").fetchone()[0] == 98
        overlaps = conn.execute(
            "SELECT COUNT(*) FROM backlinks "
            "WHERE source_queries LIKE '%top_dr_backlinks%' "
            "AND source_queries LIKE '%latest_backlinks%'"
        ).fetchone()[0]
        assert overlaps == 2
    finally:
        conn.close()

    coverage = compute_coverage([], top_dr, latest)
    assert coverage.rows == {"top_dr": 70, "latest": 30, "unique": 98}


class _FixtureClient:
    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = payloads
        self.closed = False

    def execute(self, spec, raw_dir):
        payload = self.payloads.get(spec.query_id, {})
        rows = str(len(payload.get("backlinks", [])))
        return models.RequestRecord(
            query_id=spec.query_id,
            endpoint=spec.endpoint,
            page=spec.page,
            params=dict(spec.params),
            url=f"https://api.ahrefs.com/v3{spec.endpoint}",
            status=200,
            started_at="2026-11-24T08:43:40+00:00",
            finished_at="2026-11-24T08:43:40+00:00",
            attempts=1,
            error=None,
            parse_error=None,
            rows=rows,
            units_cost_total="50",
            units_cost_total_actual="0",
            units_cost_row="12",
            cache="hit",
            headers={},
            payload=payload,
            raw_path=None,
        )

    def close(self) -> None:
        self.closed = True


def test_mocked_enrich_run_persists_both_backlink_slices_and_coverage(
    tmp_path: Path, tmp_db: Path
) -> None:
    payloads = {
        "stats": _fixture_payload("stats"),
        "refdomains_history": _fixture_payload("refdomains_history"),
        "metrics_history": _fixture_payload("metrics_history"),
        "top_dr_backlinks": _fixture_payload("top_dr_backlinks"),
        "latest_backlinks": _fixture_payload("latest_backlinks"),
    }
    settings = make_settings(tmp_path)

    summary = run_enrich(
        "smoke-fire.com",
        settings=settings,
        db_path=tmp_db,
        client=_FixtureClient(payloads),
        now=datetime(2026, 11, 24, 8, 43, 40, tzinfo=UTC),
    )

    assert summary.coverage.rows == {"top_dr": 70, "latest": 30, "unique": 98}

    conn = _open(tmp_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM backlinks").fetchone()[0] == 98
        counts = {
            row["query_id"]: row["row_count"]
            for row in conn.execute(
                "SELECT query_id, row_count FROM requests "
                "WHERE query_id IN ('top_dr_backlinks', 'latest_backlinks')"
            )
        }
        assert counts == {"top_dr_backlinks": 70, "latest_backlinks": 30}
        overlaps = conn.execute(
            "SELECT COUNT(*) FROM backlinks "
            "WHERE source_queries LIKE '%top_dr_backlinks%' "
            "AND source_queries LIKE '%latest_backlinks%'"
        ).fetchone()[0]
        assert overlaps == 2
        stored_coverage = json.loads(
            conn.execute("SELECT coverage_json FROM runs").fetchone()["coverage_json"]
        )
        assert stored_coverage["rows"] == {"top_dr": 70, "latest": 30, "unique": 98}
    finally:
        conn.close()
