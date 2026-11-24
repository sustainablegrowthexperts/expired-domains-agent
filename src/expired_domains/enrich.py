"""Orchestration for one single-domain enrichment run."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expired_domains.ahrefs.client import AhrefsClient
from expired_domains.ahrefs.queries import build_run_queries
from expired_domains.config import Settings, load_settings
from expired_domains.models import RequestRecord, RunSummary, Units
from expired_domains.storage.db import (
    connect,
    init_db,
    insert_run,
    transaction,
    upsert_history_rows,
    upsert_request,
    upsert_stats,
)
from expired_domains.storage.parsers import (
    compute_coverage,
    parse_metrics_history,
    parse_refdomains_history,
    parse_stats,
)
from expired_domains.storage.raw import create_run_dir, make_run_id

CONFIG: dict[str, Any] = {
    "spec": "ahrefs-ingestion-spec-v3.1",
    "history_months": 48,
    "top_dr_rows": 70,
    "latest_rows": 30,
    "top_anchor_rows": 50,
}


def run_enrich(
    domain: str,
    *,
    settings: Settings | None = None,
    db_path: Path | None = None,
    client: AhrefsClient | None = None,
    now: datetime | None = None,
) -> RunSummary:
    """Retrieve the stats slice for ``domain`` and persist its evidence."""
    resolved = settings if settings is not None else load_settings()
    resolved.require_ahrefs_key()

    now_utc = _as_utc(now)
    run_id = make_run_id(domain, now_utc)
    run_date = now_utc.date()
    raw_dir = create_run_dir(resolved.raw_root, domain, run_id)

    target_db = Path(db_path) if db_path is not None else resolved.db_path
    init_db(target_db)

    owns_client = client is None
    active_client = client if client is not None else AhrefsClient(resolved)
    specs = build_run_queries(domain, run_date)
    requests: list[RequestRecord] = []
    stats_rows = []
    failures: dict[str, str] = {}

    try:
        conn = connect(target_db)
        try:
            with transaction(conn):
                _write_run(
                    conn,
                    run_id=run_id,
                    domain=domain,
                    fetched_at=now_utc.isoformat(),
                    coverage_json=json.dumps(compute_coverage([]).to_dict(), sort_keys=True),
                    units_actual=0,
                    error=None,
                )

                for spec in specs:
                    record = active_client.execute(spec, raw_dir)
                    requests.append(record)
                    upsert_request(
                        conn,
                        run_id=run_id,
                        query_id=record.query_id,
                        page=record.page,
                        endpoint=record.endpoint,
                        params_json=json.dumps(record.params, sort_keys=True),
                        row_count=_row_count(record.rows),
                        units_actual=record.units.actual,
                        raw_path=record.raw_path,
                        status=record.status,
                        error=record.error or record.parse_error,
                    )

                    if _request_available(record):
                        if spec.query_id == "stats":
                            stats_rows = parse_stats(record.payload or {})
                            for row in stats_rows:
                                upsert_stats(
                                    conn,
                                    run_id=run_id,
                                    snapshot=row.snapshot,
                                    links=row.links,
                                    refdomains=row.refdomains,
                                )
                        elif spec.query_id == "refdomains_history":
                            upsert_history_rows(
                                conn,
                                run_id,
                                parse_refdomains_history(record.payload or {}),
                            )
                        elif spec.query_id == "metrics_history":
                            upsert_history_rows(
                                conn,
                                run_id,
                                parse_metrics_history(record.payload or {}),
                            )

                    message = record.error or record.parse_error
                    if message:
                        failures[record.query_id] = message

                coverage = compute_coverage(stats_rows)
                units = _totals(requests)
                error = failures or None
                _write_run(
                    conn,
                    run_id=run_id,
                    domain=domain,
                    fetched_at=now_utc.isoformat(),
                    coverage_json=json.dumps(coverage.to_dict(), sort_keys=True),
                    units_actual=units.actual,
                    error=json.dumps(error, sort_keys=True) if error else None,
                )
        finally:
            conn.close()
    finally:
        if owns_client:
            active_client.close()

    return RunSummary(
        run_id=run_id,
        domain=domain,
        raw_dir=raw_dir,
        requests=requests,
        coverage=coverage,
        units=units,
        error=error,
        exit_code=1 if error else 0,
    )


def _write_run(
    conn: Any,
    *,
    run_id: str,
    domain: str,
    fetched_at: str,
    coverage_json: str,
    units_actual: int,
    error: str | None,
) -> None:
    """Insert or refresh the ``runs`` row for this slice."""
    insert_run(
        conn,
        run_id=run_id,
        domain=domain,
        mode="domain",
        tier="standard",
        prefilter_batch=None,
        drop_date=None,
        drop_date_source="unknown",
        fetched_at=fetched_at,
        config_json=json.dumps(CONFIG, sort_keys=True),
        coverage_json=coverage_json,
        units_actual=units_actual,
        error=error,
    )


def _request_available(record: RequestRecord) -> bool:
    """Return True only when a request produced parseable JSON."""
    return (
        record.status is not None
        and 200 <= record.status < 300
        and record.parse_error is None
        and record.payload is not None
    )


def _row_count(value: str | None) -> int | None:
    """Convert the ``x-api-rows`` header to an integer when possible."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _totals(requests: list[RequestRecord]) -> Units:
    """Sum actual/billed units across every non-``limits_*`` request."""
    billable = [record for record in requests if not record.query_id.startswith("limits_")]
    return Units(
        actual=sum(record.units.actual for record in billable),
        billed=sum(record.units.billed for record in billable),
    )


def _as_utc(value: datetime | None) -> datetime:
    """Return an aware UTC datetime, treating naive input as UTC."""
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
