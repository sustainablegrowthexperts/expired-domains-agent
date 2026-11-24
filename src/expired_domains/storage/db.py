"""SQLite connection, schema initialisation, and write helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from expired_domains.models import BacklinkRow, HistoryRow

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_RUN_COLUMNS = (
    "run_id",
    "domain",
    "mode",
    "tier",
    "prefilter_batch",
    "drop_date",
    "drop_date_source",
    "fetched_at",
    "config_json",
    "coverage_json",
    "units_actual",
    "error",
)

_REQUEST_COLUMNS = (
    "run_id",
    "query_id",
    "page",
    "endpoint",
    "params_json",
    "row_count",
    "units_actual",
    "raw_path",
    "status",
    "error",
)

_STATS_COLUMNS = ("run_id", "snapshot", "links", "refdomains")

_HISTORY_COLUMNS = ("run_id", "series", "date", "value")

_BACKLINK_COLUMNS = (
    "run_id",
    "link_hash",
    "source_queries",
    "url_from",
    "url_to",
    "domain_from",
    "root_domain_from",
    "anchor_raw",
    "anchor_norm",
    "is_dofollow",
    "domain_rating",
    "first_seen_link",
    "era",
    "raw_json",
)


def connect(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` with row access by column name and safe pragmas."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> None:
    """Create the slice tables if they do not exist yet."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(db_path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit a unit of index writes atomically, or roll everything back."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    domain: str,
    mode: str,
    tier: str,
    prefilter_batch: str | None,
    drop_date: str | None,
    drop_date_source: str,
    fetched_at: str,
    config_json: str,
    coverage_json: str,
    units_actual: int,
    error: str | None,
) -> None:
    """Insert a run or refresh its mutable summary columns."""
    _upsert(
        conn,
        table="runs",
        columns=_RUN_COLUMNS,
        values=(
            run_id,
            domain,
            mode,
            tier,
            prefilter_batch,
            drop_date,
            drop_date_source,
            fetched_at,
            config_json,
            coverage_json,
            units_actual,
            error,
        ),
        conflict_columns=("run_id",),
    )


def upsert_request(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    query_id: str,
    page: int,
    endpoint: str,
    params_json: str,
    row_count: int | None,
    units_actual: int,
    raw_path: str | None,
    status: int | None,
    error: str | None,
) -> None:
    """Insert or replace one request audit row."""
    _upsert(
        conn,
        table="requests",
        columns=_REQUEST_COLUMNS,
        values=(
            run_id,
            query_id,
            page,
            endpoint,
            params_json,
            row_count,
            units_actual,
            raw_path,
            status,
            error,
        ),
        conflict_columns=("run_id", "query_id", "page"),
    )


def upsert_stats(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    snapshot: str,
    links: int | None,
    refdomains: int | None,
) -> None:
    """Insert or replace one stats snapshot row."""
    _upsert(
        conn,
        table="stats",
        columns=_STATS_COLUMNS,
        values=(run_id, snapshot, links, refdomains),
        conflict_columns=("run_id", "snapshot"),
    )


def upsert_history_rows(
    conn: sqlite3.Connection,
    run_id: str,
    rows: Iterable[HistoryRow],
) -> None:
    """Insert or replace monthly history rows, accepting an empty list."""
    for row in rows:
        _upsert(
            conn,
            table="history",
            columns=_HISTORY_COLUMNS,
            values=(run_id, row.series, row.date, row.value),
            conflict_columns=("run_id", "series", "date"),
        )


def upsert_backlinks(
    conn: sqlite3.Connection,
    run_id: str,
    rows: Iterable[BacklinkRow],
) -> None:
    """Insert backlinks, merging ``source_queries`` on an existing ``link_hash``."""
    placeholders = ", ".join("?" for _ in _BACKLINK_COLUMNS)
    column_list = ", ".join(_BACKLINK_COLUMNS)
    sql = (
        f"INSERT INTO backlinks ({column_list}) VALUES ({placeholders}) "
        "ON CONFLICT (run_id, link_hash) DO UPDATE SET source_queries = ("
        "SELECT json_group_array(value) FROM ("
        "SELECT value FROM json_each(backlinks.source_queries) "
        "UNION SELECT value FROM json_each(excluded.source_queries) "
        "ORDER BY value))"
    )

    for row in rows:
        conn.execute(
            sql,
            (
                run_id,
                row.link_hash,
                json.dumps(sorted(set(row.source_queries)), separators=(",", ":")),
                row.url_from,
                row.url_to,
                row.domain_from,
                row.root_domain_from,
                row.anchor_raw,
                row.anchor_norm,
                int(row.is_dofollow) if row.is_dofollow is not None else None,
                row.domain_rating,
                row.first_seen_link,
                row.era,
                row.raw_json,
            ),
        )


def _upsert(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    values: tuple[Any, ...],
    conflict_columns: tuple[str, ...],
) -> None:
    """Run a parameterised ``INSERT ... ON CONFLICT ... DO UPDATE``."""
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(columns)
    conflict = ", ".join(conflict_columns)
    updates = ", ".join(
        f"{column} = excluded.{column}" for column in columns if column not in conflict_columns
    )
    sql = (
        f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
    )
    conn.execute(sql, values)
