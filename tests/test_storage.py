"""SQLite schema, pragmas, and idempotent write helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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
