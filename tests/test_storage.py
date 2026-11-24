"""SQLite schema, pragmas, and idempotent write helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import expired_domains.storage.db as db
from expired_domains import models
from expired_domains.storage.db import (
    connect,
    init_db,
    insert_run,
    transaction,
    upsert_request,
    upsert_stats,
)

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
