"""Test-only replay of reference run directories into a temporary SQLite index.

This module deliberately lives under ``tests`` and is never exposed through the
``expired_domains`` package or CLI. It reads existing raw fixtures read-only and
uses the production parsers and write helpers, so the fixture corpus exercises
the same code paths as a live run without touching the network or the
production index.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from expired_domains.ahrefs.lexicon import Lexicon, load_lexicon
from expired_domains.config import PROJECT_ROOT
from expired_domains.models import AnchorRow, BacklinkRow, Coverage, HistoryRow, StatsRow
from expired_domains.storage.db import (
    connect,
    init_db,
    insert_run,
    transaction,
    upsert_anchors,
    upsert_backlinks,
    upsert_history_rows,
    upsert_request,
    upsert_stats,
)
from expired_domains.storage.parsers import (
    compute_coverage,
    extract_units,
    merge_backlink_rows,
    parse_anchors,
    parse_backlinks,
    parse_metrics_history,
    parse_refdomains_history,
    parse_stats,
)

_HISTORY_PARSER = Callable[[dict[str, Any]], Sequence[HistoryRow]]
_TABLE_NAMES = ("runs", "requests", "stats", "history", "backlinks", "anchors")


@dataclass(frozen=True)
class ReplayResult:
    """The observable outcome of replaying one run directory."""

    run_id: str
    domain: str
    warnings: list[str]
    coverage: Coverage
    units_actual: int
    units_billed: int
    row_counts: dict[str, int]


@dataclass(frozen=True)
class _Mapping:
    """Where a query ID's payload belongs in the index."""

    table: str
    parser: _HISTORY_PARSER | None = None
    slot: str | None = None


# One data-driven mapping table for every known query ID, including the legacy
# split ``stats_live`` / ``stats_all_time`` layout.
_QUERY_MAP: dict[str, _Mapping] = {
    "stats": _Mapping("stats"),
    "stats_live": _Mapping("stats"),
    "stats_all_time": _Mapping("stats"),
    "refdomains_history": _Mapping("history", parse_refdomains_history),
    "metrics_history": _Mapping("history", parse_metrics_history),
    "top_dr_backlinks": _Mapping("backlinks", slot="top_dr"),
    "latest_backlinks": _Mapping("backlinks", slot="latest"),
    "lexicon_anchors": _Mapping("anchors"),
    "top_anchors": _Mapping("anchors"),
    "limits_before": _Mapping("requests"),
    "limits_after": _Mapping("requests"),
}


@dataclass
class _ReplayState:
    """Mutable accumulator shared by the per-query handlers."""

    conn: sqlite3.Connection
    run_id: str
    lexicon: Lexicon
    top_dr_rows: list[BacklinkRow] = field(default_factory=list)
    latest_rows: list[BacklinkRow] = field(default_factory=list)
    backlink_rows: list[BacklinkRow] = field(default_factory=list)
    anchor_rows: list[AnchorRow] = field(default_factory=list)


_Handler = Callable[[_ReplayState, str, dict[str, Any], _Mapping], None]


def replay_run(run_dir: Path, db_path: Path) -> ReplayResult:
    """Load one manifest-bearing run directory into a fresh temporary index."""
    run_path = Path(run_dir)
    manifest_path = run_path / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"replay requires manifest.json in {run_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_id = str(manifest["run_id"])
    domain = str(manifest.get("domain") or "")

    init_db(db_path)
    conn = connect(db_path)
    warnings: list[str] = []
    units_actual = 0
    units_billed = 0
    try:
        with transaction(conn):
            _upsert_run(
                conn,
                manifest,
                coverage_json=_empty_coverage_json(),
                units_actual=0,
                error=None,
            )
            state = _ReplayState(
                conn=conn,
                run_id=run_id,
                lexicon=load_lexicon(),
            )

            for query_id, page, raw_path, document in _documents(run_path):
                request = document.get("request") or {}
                payload = document.get("payload") or {}
                mapping = _mapping_for(query_id, warnings)
                headers = _headers_for(raw_path, request)
                units = extract_units(headers)

                if not query_id.startswith("limits_"):
                    units_actual += units.actual
                    units_billed += units.billed

                upsert_request(
                    conn,
                    run_id=run_id,
                    query_id=query_id,
                    page=page,
                    endpoint=str(request.get("endpoint") or ""),
                    params_json=json.dumps(request.get("params") or {}, sort_keys=True),
                    row_count=_optional_int(request.get("rows")),
                    units_actual=units.actual,
                    raw_path=_relative_raw_path(raw_path),
                    status=_optional_int(request.get("status")),
                    error=_request_error(request),
                )
                _HANDLERS[mapping.table](state, query_id, payload, mapping)

            upsert_backlinks(conn, run_id, merge_backlink_rows(state.backlink_rows))
            upsert_anchors(conn, run_id, state.anchor_rows)

            coverage = _recompute_coverage(conn, run_id, state, manifest)
            _upsert_run(
                conn,
                manifest,
                coverage_json=json.dumps(coverage.to_dict(), sort_keys=True),
                units_actual=units_actual,
                error=_manifest_error(manifest),
            )

        row_counts = _row_counts(conn, run_id)
    finally:
        conn.close()

    return ReplayResult(
        run_id=run_id,
        domain=domain,
        warnings=warnings,
        coverage=coverage,
        units_actual=units_actual,
        units_billed=units_billed,
        row_counts=row_counts,
    )


def _mapping_for(query_id: str, warnings: list[str]) -> _Mapping:
    """Return the mapping for ``query_id``, warning for unknown IDs."""
    mapping = _QUERY_MAP.get(query_id)
    if mapping is not None:
        return mapping
    if not query_id.startswith("limits_"):
        warnings.append(f"unknown query id {query_id!r}; recorded in requests only")
    return _Mapping("requests")


def _documents(run_path: Path) -> list[tuple[str, int, Path, dict[str, Any]]]:
    """Read every ``raw/*.p0.json`` in deterministic query order."""
    documents: list[tuple[str, int, Path, dict[str, Any]]] = []
    for raw_path in sorted((run_path / "raw").glob("*.p0.json")):
        document = json.loads(raw_path.read_text(encoding="utf-8"))
        request = document.get("request") or {}
        query_id = str(request.get("query_id") or raw_path.name.removesuffix(".p0.json"))
        page = _optional_int(request.get("page")) or 0
        documents.append((query_id, page, raw_path, document))
    documents.sort(key=lambda item: (item[0], item[1]))
    return documents


def _headers_for(raw_path: Path, request: dict[str, Any]) -> dict[str, str]:
    """Return the response headers, falling back to the request record's copy."""
    headers_path = raw_path.with_suffix(".headers.json")
    if headers_path.is_file():
        loaded = json.loads(headers_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return {str(key): str(value) for key, value in loaded.items()}

    embedded = request.get("headers") or {}
    if not isinstance(embedded, dict):
        return {}
    return {str(key): str(value) for key, value in embedded.items()}


def _handle_stats(
    state: _ReplayState, query_id: str, payload: dict[str, Any], mapping: _Mapping
) -> None:
    """Populate both stats snapshots; split legacy payloads upsert idempotently."""
    for row in parse_stats(payload):
        upsert_stats(
            state.conn,
            run_id=state.run_id,
            snapshot=row.snapshot,
            links=row.links,
            refdomains=row.refdomains,
        )


def _handle_history(
    state: _ReplayState, query_id: str, payload: dict[str, Any], mapping: _Mapping
) -> None:
    """Populate a history series via the mapped production parser."""
    if mapping.parser is None:
        return
    upsert_history_rows(state.conn, state.run_id, mapping.parser(payload))


def _handle_backlinks(
    state: _ReplayState, query_id: str, payload: dict[str, Any], mapping: _Mapping
) -> None:
    """Accumulate backlinks, keeping the two query slices separately."""
    rows = parse_backlinks(payload, query_id)
    state.backlink_rows.extend(rows)
    slots = {"top_dr": state.top_dr_rows, "latest": state.latest_rows}
    if mapping.slot is not None:
        slots[mapping.slot].extend(rows)


def _handle_anchors(
    state: _ReplayState, query_id: str, payload: dict[str, Any], mapping: _Mapping
) -> None:
    """Parse and accumulate anchors for both source queries."""
    state.anchor_rows.extend(parse_anchors(payload, query_id, state.lexicon))


def _handle_requests_only(
    state: _ReplayState, query_id: str, payload: dict[str, Any], mapping: _Mapping
) -> None:
    """Leave limits and unknown payloads in the request audit trail only."""


_HANDLERS: dict[str, _Handler] = {
    "stats": _handle_stats,
    "history": _handle_history,
    "backlinks": _handle_backlinks,
    "anchors": _handle_anchors,
    "requests": _handle_requests_only,
}


def _recompute_coverage(
    conn: sqlite3.Connection,
    run_id: str,
    state: _ReplayState,
    manifest: dict[str, Any],
) -> Coverage:
    """Recompute §3.8 coverage from the replayed rows and request counts."""
    stats_rows = [
        StatsRow(
            snapshot=row["snapshot"],
            links=row["links"],
            refdomains=row["refdomains"],
        )
        for row in conn.execute(
            "SELECT snapshot, links, refdomains FROM stats WHERE run_id = ?",
            (run_id,),
        )
    ]
    coverage = compute_coverage(
        stats_rows,
        state.top_dr_rows,
        state.latest_rows,
        state.anchor_rows,
    )
    lexicon_rows = conn.execute(
        "SELECT COUNT(*) FROM anchors WHERE run_id = ? AND source_query = 'lexicon_anchors'",
        (run_id,),
    ).fetchone()[0]

    return replace(
        coverage,
        rows={
            **coverage.rows,
            "top_dr": _request_row_count(conn, run_id, "top_dr_backlinks"),
            "latest": _request_row_count(conn, run_id, "latest_backlinks"),
        },
        top_anchors=_request_row_count(conn, run_id, "top_anchors"),
        lexicon_complete=lexicon_rows < 250,
        drop_date_source=str(manifest.get("drop_date_source") or "unknown"),
        prefilter=None,
    )


def _request_row_count(conn: sqlite3.Connection, run_id: str, query_id: str) -> int:
    """Return the recorded request row count, treating missing values as zero."""
    row = conn.execute(
        "SELECT row_count FROM requests WHERE run_id = ? AND query_id = ?",
        (run_id, query_id),
    ).fetchone()
    return int(row["row_count"] or 0) if row is not None else 0


def _row_counts(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """Return one row count per index table for this run."""
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)).fetchone()[
            0
        ]
        for table in _TABLE_NAMES
    }


def _upsert_run(
    conn: sqlite3.Connection,
    manifest: dict[str, Any],
    *,
    coverage_json: str,
    units_actual: int,
    error: str | None,
) -> None:
    """Insert or refresh the run row from manifest metadata."""
    insert_run(
        conn,
        run_id=str(manifest["run_id"]),
        domain=str(manifest.get("domain") or ""),
        mode=str(manifest.get("mode") or "domain"),
        tier=str(manifest.get("tier") or "standard"),
        prefilter_batch=manifest.get("prefilter_batch"),
        drop_date=manifest.get("drop_date"),
        drop_date_source=str(manifest.get("drop_date_source") or "unknown"),
        fetched_at=str(manifest["fetched_at"]),
        config_json=json.dumps(manifest.get("config") or {}, sort_keys=True),
        coverage_json=coverage_json,
        units_actual=units_actual,
        error=error,
    )


def _empty_coverage_json() -> str:
    """Return the placeholder coverage written before replay parsing."""
    return json.dumps(compute_coverage([]).to_dict(), sort_keys=True)


def _relative_raw_path(path: Path) -> str:
    """Return the project-relative raw path, matching the live writer."""
    try:
        return Path(os.path.relpath(path, PROJECT_ROOT)).as_posix()
    except ValueError:
        return path.as_posix()


def _request_error(request: dict[str, Any]) -> str | None:
    """Return the first recorded request failure, if any."""
    value = request.get("error") or request.get("parse_error")
    return str(value) if value is not None else None


def _manifest_error(manifest: dict[str, Any]) -> str | None:
    """Serialise a manifest error object the same way the live writer does."""
    error = manifest.get("error")
    return json.dumps(error, sort_keys=True) if error else None


def _optional_int(value: Any) -> int | None:
    """Parse an optional integer, returning ``None`` for missing/invalid values."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
