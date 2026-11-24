"""Orchestration for one single-domain enrichment run."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from expired_domains.ahrefs.client import AhrefsClient
from expired_domains.ahrefs.lexicon import load_lexicon
from expired_domains.ahrefs.queries import build_run_queries
from expired_domains.config import Settings, load_settings
from expired_domains.models import Coverage, RequestRecord, RunSummary, Units
from expired_domains.storage.db import (
    connect,
    has_run_on_date,
    init_db,
    insert_run,
    latest_run_id_for_date,
    transaction,
    upsert_anchors,
    upsert_backlinks,
    upsert_history_rows,
    upsert_request,
    upsert_stats,
)
from expired_domains.storage.parsers import (
    compute_coverage,
    merge_backlink_rows,
    parse_anchors,
    parse_backlinks,
    parse_metrics_history,
    parse_refdomains_history,
    parse_stats,
)
from expired_domains.storage.raw import (
    create_run_dir,
    make_run_id,
    run_dir_name,
    write_manifest,
)

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
    force: bool = False,
) -> RunSummary:
    """Retrieve the configured slices for ``domain`` and persist their evidence."""
    resolved = settings if settings is not None else load_settings()
    resolved.require_ahrefs_key()

    now_utc = _as_utc(now)
    run_date = now_utc.date()
    target_db = Path(db_path) if db_path is not None else resolved.db_path
    init_db(target_db)

    conn = connect(target_db)
    try:
        if not force and has_run_on_date(conn, domain, run_date):
            existing_run_id = latest_run_id_for_date(conn, domain, run_date) or ""
            message = (
                f"{domain} already has a run for {run_date.isoformat()} "
                f"(run_id={existing_run_id}); use --force to fetch again."
            )
            return RunSummary(
                run_id=existing_run_id,
                domain=domain,
                raw_dir=Path(resolved.raw_root)
                / domain
                / run_dir_name(existing_run_id),
                requests=[],
                coverage=compute_coverage([]),
                units=Units(actual=0, billed=0),
                error=None,
                exit_code=1,
                message=message,
            )

        run_id, run_time = _choose_run_id(
            conn, Path(resolved.raw_root), domain, now_utc
        )
        raw_dir = create_run_dir(resolved.raw_root, domain, run_id)

        owns_client = client is None
        active_client = client if client is not None else AhrefsClient(resolved)
        lexicon = load_lexicon()
        specs = build_run_queries(domain, run_date)
        requests: list[RequestRecord] = []
        stats_rows = []
        top_dr_rows = []
        latest_rows = []
        backlink_rows = []
        anchor_rows = []
        failures: dict[str, str] = {}

        try:
            with transaction(conn):
                _write_run(
                    conn,
                    run_id=run_id,
                    domain=domain,
                    fetched_at=run_time.isoformat(),
                    config_json=_config_json(lexicon.version),
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

                    if _request_succeeded(record):
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
                        elif spec.query_id == "top_dr_backlinks":
                            top_dr_rows = parse_backlinks(record.payload or {}, spec.query_id)
                            backlink_rows.extend(top_dr_rows)
                        elif spec.query_id == "latest_backlinks":
                            latest_rows = parse_backlinks(record.payload or {}, spec.query_id)
                            backlink_rows.extend(latest_rows)
                        elif spec.query_id in ("lexicon_anchors", "top_anchors"):
                            anchor_rows.extend(
                                parse_anchors(record.payload or {}, spec.query_id, lexicon)
                            )

                    message = _failure_message(record)
                    if message:
                        failures[record.query_id] = message

                upsert_backlinks(conn, run_id, merge_backlink_rows(backlink_rows))
                upsert_anchors(conn, run_id, anchor_rows)
                coverage = compute_coverage(
                    stats_rows, top_dr_rows, latest_rows, anchor_rows
                )
                units = _totals(requests)
                error = failures or None
                _write_run(
                    conn,
                    run_id=run_id,
                    domain=domain,
                    fetched_at=run_time.isoformat(),
                    config_json=_config_json(lexicon.version),
                    coverage_json=json.dumps(coverage.to_dict(), sort_keys=True),
                    units_actual=units.actual,
                    error=json.dumps(error, sort_keys=True) if error else None,
                )
        finally:
            if owns_client:
                active_client.close()

        manifest = _build_manifest(
            run_id=run_id,
            domain=domain,
            now_utc=run_time,
            run_date=run_date,
            config=_config_dict(lexicon.version),
            coverage=coverage,
            requests=requests,
            units=units,
            error=error,
        )
        write_manifest(raw_dir, manifest)

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
    finally:
        conn.close()


def _choose_run_id(
    conn: Any,
    raw_root: Path,
    domain: str,
    now_utc: datetime,
) -> tuple[str, datetime]:
    """Return a run ID/time that collides with neither the index nor raw storage."""
    candidate = now_utc
    for _ in range(86_400):
        run_id = make_run_id(domain, candidate)
        in_index = conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        run_dir = Path(raw_root) / domain / run_dir_name(run_id)
        if in_index is None and not run_dir.exists():
            return run_id, candidate
        candidate += timedelta(seconds=1)
    raise RuntimeError(f"could not allocate a unique run ID for {domain}")


def _config_dict(lexicon_version: str) -> dict[str, Any]:
    """Return the run configuration including the loaded lexicon version."""
    return {**CONFIG, "lexicon_version": lexicon_version}


def _config_json(lexicon_version: str) -> str:
    """Return the run configuration as deterministic JSON."""
    return json.dumps(_config_dict(lexicon_version), sort_keys=True)


def _write_run(
    conn: Any,
    *,
    run_id: str,
    domain: str,
    fetched_at: str,
    config_json: str,
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
        config_json=config_json,
        coverage_json=coverage_json,
        units_actual=units_actual,
        error=error,
    )


def _failure_message(record: RequestRecord) -> str | None:
    """Classify a request outcome: transport/status errors first, then parse errors."""
    return record.error or record.parse_error


def _request_succeeded(record: RequestRecord) -> bool:
    """Return True only when a request produced parseable JSON without failure."""
    return (
        _failure_message(record) is None
        and record.status is not None
        and 200 <= record.status < 300
        and record.payload is not None
    )


def _build_manifest(
    *,
    run_id: str,
    domain: str,
    now_utc: datetime,
    run_date: date,
    config: dict[str, Any],
    coverage: Coverage,
    requests: list[RequestRecord],
    units: Units,
    error: dict[str, str] | None,
) -> dict[str, Any]:
    """Assemble the §3.2 run manifest from the completed run."""
    return {
        "run_id": run_id,
        "domain": domain,
        "provider": "ahrefs",
        "mode": "domain",
        "tier": "standard",
        "drop_date": None,
        "drop_date_source": "unknown",
        "fetched_at": now_utc.isoformat(),
        "api_date": run_date.isoformat(),
        "config": config,
        "requests": [_request_summary(record) for record in requests],
        "coverage": coverage.to_dict(),
        "units_actual_total": units.actual,
        "units_billed_total": units.billed,
        "error": error,
    }


def _request_summary(record: RequestRecord) -> dict[str, Any]:
    """Return the manifest summary for a single request."""
    return {
        "query_id": record.query_id,
        "endpoint": record.endpoint,
        "page": record.page,
        "status": record.status,
        "rows": record.rows,
        "row_count": _row_count(record.rows),
        "units_actual": record.units.actual,
        "units_billed": record.units.billed,
        "cache": record.cache,
        "error": _failure_message(record),
    }


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
