"""SQLite connection, schema initialisation, and write helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from datetime import date as date_type
from pathlib import Path
from typing import Any

from expired_domains.models import (
    AnchorRow,
    BacklinkRow,
    DomainObservation,
    DomainRatingVerdict,
    HistoryRow,
    KeywordVerdict,
    LiveCheckVerdict,
    PrefilterResult,
    WaybackVerdict,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_DOMAIN_OBSERVATION_COLUMNS = (
    "domain",
    "source",
    "collected_date",
    "metrics_json",
    "raw_json",
)

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

_ANCHOR_COLUMNS = (
    "run_id",
    "source_query",
    "anchor_raw",
    "anchor_norm",
    "links_to_target",
    "dofollow_links",
    "refdomains",
    "first_seen",
    "last_seen",
    "lexicon_terms",
    "lexicon_categories",
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
    """Create the slice tables and upgrade older domain-observation schemas."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(db_path)
    try:
        conn.executescript(schema)
        _migrate_domain_observations(conn)
        _migrate_domains(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_domains(conn: sqlite3.Connection) -> None:
    """Upgrade indexes created before niche, blocklist, and prefilter columns."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(domains)")}
    if not columns:
        return

    if "niche" not in columns:
        conn.execute("ALTER TABLE domains ADD COLUMN niche TEXT")
    if "niche_source" not in columns:
        conn.execute("ALTER TABLE domains ADD COLUMN niche_source TEXT")
    if "blocked_category" not in columns:
        conn.execute("ALTER TABLE domains ADD COLUMN blocked_category TEXT")
    if "blocked_terms_json" not in columns:
        conn.execute("ALTER TABLE domains ADD COLUMN blocked_terms_json TEXT")

    prefilter_columns = {
        "prefilter_pass": "INTEGER",
        "prefilter_reason": "TEXT",
        "prefilter_details_json": "TEXT",
        "prefilter_config_version": "TEXT",
        "prefilter_checked_at": "TEXT",
        "prefilter_manual_pass": "INTEGER NOT NULL DEFAULT 0",
        "prefilter_manual_pass_at": "TEXT",
    }
    for column, column_type in prefilter_columns.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {column} {column_type}")

    keyword_columns = {
        "keyword_check_status": "TEXT",
        "keyword_check_pass": "INTEGER",
        "keyword_check_reason": "TEXT",
        "keyword_check_details_json": "TEXT",
        "keyword_check_confidence": "REAL",
        "keyword_check_model": "TEXT",
        "keyword_check_checked_at": "TEXT",
    }
    for column, column_type in keyword_columns.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {column} {column_type}")

    dr_columns = {
        "dr_check_status": "TEXT",
        "dr_check_pass": "INTEGER",
        "dr_rating": "REAL",
        "dr_check_reason": "TEXT",
        "dr_check_details_json": "TEXT",
        "dr_check_attempts": "INTEGER NOT NULL DEFAULT 0",
        "dr_check_checked_at": "TEXT",
    }
    for column, column_type in dr_columns.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {column} {column_type}")

    live_columns = {
        "live_check_status": "TEXT",
        "live_check_pass": "INTEGER",
        "live_check_class": "TEXT",
        "live_check_reason": "TEXT",
        "live_check_details_json": "TEXT",
        "live_check_attempts": "INTEGER NOT NULL DEFAULT 0",
        "live_check_checked_at": "TEXT",
        "live_check_manual_pass": "INTEGER NOT NULL DEFAULT 0",
        "live_check_manual_pass_at": "TEXT",
        "live_check_final_host": "TEXT",
        "live_check_http_status": "INTEGER",
        "live_check_config_version": "TEXT",
    }
    for column, column_type in live_columns.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {column} {column_type}")

    wayback_columns = {
        "wayback_check_status": "TEXT",
        "wayback_check_pass": "INTEGER",
        "wayback_score": "INTEGER",
        "wayback_check_reason": "TEXT",
        "wayback_check_source": "TEXT",
        "wayback_check_details_json": "TEXT",
        "wayback_check_attempts": "INTEGER NOT NULL DEFAULT 0",
        "wayback_check_checked_at": "TEXT",
        "wayback_check_manual_pass": "INTEGER NOT NULL DEFAULT 0",
        "wayback_check_manual_pass_at": "TEXT",
        "wayback_first_capture": "TEXT",
        "wayback_last_capture": "TEXT",
        "wayback_age_years": "REAL",
        "wayback_capture_count": "INTEGER",
        "wayback_config_version": "TEXT",
    }
    for column, column_type in wayback_columns.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {column} {column_type}")

    evaluation_columns = {
        "evaluation_status": "TEXT",
        "evaluation_score": "REAL",
        "evaluation_evidence_points": "REAL",
        "evaluation_verdict": "TEXT",
        "evaluation_recommendation": "TEXT",
        "evaluation_reason": "TEXT",
        "evaluation_details_json": "TEXT",
        "evaluation_config_version": "TEXT",
        "evaluation_checked_at": "TEXT",
    }
    for column, column_type in evaluation_columns.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {column} {column_type}")

    # The indexes are created here, after the columns exist, so an old
    # ``domains`` table can be upgraded without the schema script failing.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_domains_niche ON domains (niche)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_domains_blocked ON domains (blocked_category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_domains_prefilter ON domains (prefilter_pass)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_domains_prefilter_version "
        "ON domains (prefilter_config_version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_domains_prefilter_manual ON domains (prefilter_manual_pass)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_domains_keyword_check ON domains (keyword_check_pass)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_domains_dr_check ON domains (dr_check_pass)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_domains_live_check ON domains (live_check_pass)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_domains_wayback_check ON domains (wayback_check_pass)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_domains_evaluation ON domains (evaluation_verdict)"
    )

    # ``unknown`` was the old placeholder for "not classified yet". New rows
    # use NULL, so normalise old values where the column is nullable.
    info = {row["name"]: row for row in conn.execute("PRAGMA table_info(domains)")}
    if not info["niche"]["notnull"]:
        conn.execute("UPDATE domains SET niche = NULL WHERE niche = 'unknown'")
    if not info["niche_source"]["notnull"]:
        conn.execute("UPDATE domains SET niche_source = NULL WHERE niche_source = 'unknown'")


def _migrate_domain_observations(conn: sqlite3.Connection) -> None:
    """Upgrade the first ``payload_json`` schema without discarding raw data."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(domain_observations)")}
    if not columns:
        return

    if "payload_json" in columns and "raw_json" not in columns:
        conn.execute("ALTER TABLE domain_observations RENAME COLUMN payload_json TO raw_json")
        columns.discard("payload_json")
        columns.add("raw_json")
    if "metrics_json" not in columns:
        conn.execute(
            "ALTER TABLE domain_observations ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'"
        )


def get_pipeline_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Return one pipeline checkpoint value, or ``None`` when unset."""
    row = conn.execute("SELECT value FROM pipeline_meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else None


def set_pipeline_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Create or replace one pipeline checkpoint value."""
    conn.execute(
        "INSERT INTO pipeline_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


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


def has_run_on_date(conn: sqlite3.Connection, domain: str, date: date_type | str) -> bool:
    """Return True when ``domain`` already has a run on the UTC ``date``."""
    return latest_run_id_for_date(conn, domain, date) is not None


def latest_run_id_for_date(
    conn: sqlite3.Connection, domain: str, date: date_type | str
) -> str | None:
    """Return the newest run ID for ``domain`` on the UTC ``date``, if any."""
    day = date.isoformat() if isinstance(date, date_type) else str(date)
    row = conn.execute(
        "SELECT run_id FROM runs "
        "WHERE domain = ? AND substr(fetched_at, 1, 10) = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (domain, day[:10]),
    ).fetchone()
    return row["run_id"] if row is not None else None


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


def upsert_anchors(
    conn: sqlite3.Connection,
    run_id: str,
    rows: Iterable[AnchorRow],
) -> None:
    """Insert anchors, refreshing metrics and lexical columns on conflict."""
    for row in rows:
        _upsert(
            conn,
            table="anchors",
            columns=_ANCHOR_COLUMNS,
            values=(
                run_id,
                row.source_query,
                row.anchor_raw,
                row.anchor_norm,
                row.links_to_target,
                row.dofollow_links,
                row.refdomains,
                row.first_seen,
                row.last_seen,
                json.dumps(row.lexicon_terms, separators=(",", ":")),
                json.dumps(row.lexicon_categories, separators=(",", ":")),
                row.raw_json,
            ),
            conflict_columns=("run_id", "source_query", "anchor_norm"),
        )


def store_domain_observations(
    conn: sqlite3.Connection,
    rows: Iterable[DomainObservation],
) -> int:
    """Upsert canonical domains and their per-source observations.

    A domain's first/last seen dates span every source and collection date.
    Re-importing the same export on the same date replaces its JSON blobs
    instead of creating a duplicate observation.
    """
    count = 0
    for row in rows:
        conn.execute(
            "INSERT INTO domains (domain, first_seen_date, last_seen_date) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT (domain) DO UPDATE SET "
            "first_seen_date = MIN(domains.first_seen_date, excluded.first_seen_date), "
            "last_seen_date = MAX(domains.last_seen_date, excluded.last_seen_date)",
            (row.domain, row.collected_date, row.collected_date),
        )
        _upsert(
            conn,
            table="domain_observations",
            columns=_DOMAIN_OBSERVATION_COLUMNS,
            values=(
                row.domain,
                row.source,
                row.collected_date,
                json.dumps(row.metrics, sort_keys=True, separators=(",", ":")),
                json.dumps(row.raw, sort_keys=True, separators=(",", ":")),
            ),
            conflict_columns=("domain", "source", "collected_date"),
        )
        count += 1
    return count


def set_domain_niches(
    conn: sqlite3.Connection,
    assignments: Mapping[str, tuple[str, str]],
) -> int:
    """Update the niche and its provenance for already-stored domains.

    ``assignments`` maps a domain to ``(niche, source)``. Domains that are not
    in the table are ignored, so callers can run this after an upsert.
    """
    updated = 0
    for domain, (niche, source) in assignments.items():
        cursor = conn.execute(
            "UPDATE domains SET niche = ?, niche_source = ? WHERE domain = ?",
            (niche, source, domain),
        )
        updated += cursor.rowcount
    return updated


def set_domain_blocks(
    conn: sqlite3.Connection,
    assignments: Mapping[str, tuple[str, Sequence[str]]],
) -> int:
    """Mark stored domains as blocked by a deterministic dictionary.

    ``assignments`` maps a domain to ``(blocked_category, matched_terms)``.
    Domains that are not in the table are ignored, so callers can run this
    after an upsert.
    """
    updated = 0
    for domain, (category, terms) in assignments.items():
        cursor = conn.execute(
            "UPDATE domains SET blocked_category = ?, blocked_terms_json = ? WHERE domain = ?",
            (
                category,
                json.dumps(list(terms), separators=(",", ":")),
                domain,
            ),
        )
        updated += cursor.rowcount
    return updated


def set_prefilter_results(
    conn: sqlite3.Connection,
    results: Mapping[str, PrefilterResult],
) -> int:
    """Store one local-prefilter verdict per domain.

    ``results`` maps a domain to its evaluated result. Domains that are not in
    the table are ignored, so callers can run this after an upsert. The stored
    details payload keeps every gate result, including skipped gates, for audit.

    A domain that was passed manually is never overwritten by a later automated
    run, even if the caller supplies a stale candidate list.
    """
    updated = 0
    for domain, result in results.items():
        cursor = conn.execute(
            "UPDATE domains SET prefilter_pass = ?, prefilter_reason = ?, "
            "prefilter_details_json = ?, prefilter_config_version = ?, "
            "prefilter_checked_at = ? "
            "WHERE domain = ? AND COALESCE(prefilter_manual_pass, 0) = 0",
            (
                int(result.passed),
                result.reason,
                json.dumps(result.details, sort_keys=True, separators=(",", ":")),
                result.config_version,
                result.checked_at,
                domain,
            ),
        )
        updated += cursor.rowcount
    return updated


def set_prefilter_manual_pass(
    conn: sqlite3.Connection,
    domain: str,
    *,
    checked_at: str | None = None,
) -> int:
    """Manually pass one failed prefilter result and protect it from re-runs.

    The automated failure is preserved inside ``prefilter_details_json``; the
    effective columns become a pass with the ``Manually passed`` reason. A
    second call, an already-passed domain, or a missing domain is ignored.
    """
    row = conn.execute(
        "SELECT prefilter_reason, prefilter_details_json FROM domains "
        "WHERE domain = ? AND prefilter_pass = 0 "
        "AND COALESCE(prefilter_manual_pass, 0) = 0",
        (domain,),
    ).fetchone()
    if row is None:
        return 0

    stamp = checked_at or datetime.now(UTC).isoformat(timespec="seconds")
    details = _json_object(row["prefilter_details_json"])
    details["manual_pass"] = True
    details["manual_pass_at"] = stamp
    details["manual_pass_original_reason"] = row["prefilter_reason"]

    cursor = conn.execute(
        "UPDATE domains SET prefilter_pass = 1, prefilter_reason = ?, "
        "prefilter_details_json = ?, prefilter_manual_pass = 1, "
        "prefilter_manual_pass_at = ? "
        "WHERE domain = ? AND prefilter_pass = 0 "
        "AND COALESCE(prefilter_manual_pass, 0) = 0",
        (
            "Manually passed",
            json.dumps(details, sort_keys=True, separators=(",", ":")),
            stamp,
            domain,
        ),
    )
    return cursor.rowcount


def mark_keyword_checks_processing(
    conn: sqlite3.Connection,
    domains: Iterable[str],
    *,
    model: str,
    started_at: str,
) -> int:
    """Mark a batch as in-flight before the LLM call starts."""
    updated = 0
    for domain in domains:
        cursor = conn.execute(
            "UPDATE domains SET keyword_check_status = 'processing', "
            "keyword_check_pass = NULL, keyword_check_reason = NULL, "
            "keyword_check_details_json = NULL, keyword_check_confidence = NULL, "
            "keyword_check_model = ?, keyword_check_checked_at = ? "
            "WHERE domain = ?",
            (model, started_at, domain),
        )
        updated += cursor.rowcount
    return updated


def reset_keyword_checks(conn: sqlite3.Connection, domains: Iterable[str]) -> int:
    """Return a batch to the pending state so a later cycle retries it."""
    updated = 0
    for domain in domains:
        cursor = conn.execute(
            "UPDATE domains SET keyword_check_status = NULL, keyword_check_pass = NULL, "
            "keyword_check_reason = NULL, keyword_check_details_json = NULL, "
            "keyword_check_confidence = NULL, keyword_check_model = NULL, "
            "keyword_check_checked_at = NULL "
            "WHERE domain = ?",
            (domain,),
        )
        updated += cursor.rowcount
    return updated


def set_keyword_check_results(
    conn: sqlite3.Connection,
    results: Mapping[str, KeywordVerdict],
    *,
    model: str,
    checked_at: str,
) -> int:
    """Store one reusable-keyword verdict per domain.

    ``results`` maps a domain to the validated LLM decision. Domains that are
    not in the table are ignored. The raw flags are kept in the details blob so
    a verdict can be audited without re-asking the model.
    """
    updated = 0
    for domain, verdict in results.items():
        status = "reusable" if verdict.reusable else "not_reusable"
        details = {
            "keyword": domain,
            "confidence": verdict.confidence,
            "flags": list(verdict.flags),
            "model": model,
            "status": status,
        }
        cursor = conn.execute(
            "UPDATE domains SET keyword_check_status = ?, keyword_check_pass = ?, "
            "keyword_check_reason = ?, keyword_check_details_json = ?, "
            "keyword_check_confidence = ?, keyword_check_model = ?, "
            "keyword_check_checked_at = ? "
            "WHERE domain = ?",
            (
                status,
                int(verdict.reusable),
                verdict.reason,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
                verdict.confidence,
                model,
                checked_at,
                domain,
            ),
        )
        updated += cursor.rowcount
    return updated


def mark_dr_checks_processing(
    conn: sqlite3.Connection,
    domains: Iterable[str],
    *,
    started_at: str,
) -> int:
    """Claim a DR batch and count the attempt before the API call starts."""
    updated = 0
    for domain in domains:
        cursor = conn.execute(
            "UPDATE domains SET dr_check_status = 'processing', dr_check_pass = NULL, "
            "dr_rating = NULL, dr_check_reason = NULL, dr_check_details_json = NULL, "
            "dr_check_checked_at = ?, "
            "dr_check_attempts = COALESCE(dr_check_attempts, 0) + 1 "
            "WHERE domain = ?",
            (started_at, domain),
        )
        updated += cursor.rowcount
    return updated


def reset_dr_checks(conn: sqlite3.Connection, domains: Iterable[str]) -> int:
    """Return a DR batch to the pending state after a retryable failure."""
    updated = 0
    for domain in domains:
        cursor = conn.execute(
            "UPDATE domains SET dr_check_status = NULL, dr_check_pass = NULL, "
            "dr_rating = NULL, dr_check_reason = NULL, dr_check_details_json = NULL, "
            "dr_check_checked_at = NULL "
            "WHERE domain = ?",
            (domain,),
        )
        updated += cursor.rowcount
    return updated


def set_dr_check_results(
    conn: sqlite3.Connection,
    results: Mapping[str, DomainRatingVerdict],
    *,
    dr_floor: int,
    checked_at: str,
) -> int:
    """Store one terminal DR verdict per domain.

    A domain passes when it has a rating at or above ``dr_floor``. A missing
    rating is a terminal failure, because retrying an unknown target forever
    would keep it in the queue. ``results`` maps the canonical domain to its
    parsed free-DR response.
    """
    floor = max(0, int(dr_floor))
    updated = 0
    for domain, verdict in results.items():
        rating = verdict.rating
        passed = rating is not None and rating >= floor
        status = "passed" if passed else "failed"
        if rating is None:
            reason = "value_dr: no domain rating returned"
        elif passed:
            reason = f"DR {rating:g} meets floor {floor}"
        else:
            reason = f"value_dr: DR {rating:g} below floor {floor}"
        details = {
            "rating": rating,
            "floor": floor,
            "license": verdict.license,
            "status": status,
        }
        cursor = conn.execute(
            "UPDATE domains SET dr_check_status = ?, dr_check_pass = ?, "
            "dr_rating = ?, dr_check_reason = ?, dr_check_details_json = ?, "
            "dr_check_checked_at = ? "
            "WHERE domain = ?",
            (
                status,
                int(passed),
                rating,
                reason,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
                checked_at,
                domain,
            ),
        )
        updated += cursor.rowcount
    return updated


def record_dr_check_errors(
    conn: sqlite3.Connection,
    domains: Iterable[str],
    *,
    error: str,
    checked_at: str,
    max_attempts: int,
) -> int:
    """Retry a failed DR call until the per-domain attempt budget is spent.

    Each claimed row already incremented ``dr_check_attempts``. Rows below the
    budget return to pending; rows at or above it become terminal failures so a
    permanently broken target cannot loop forever. Returns the number of rows
    made terminal.
    """
    limit = max(1, int(max_attempts))
    message = str(error).strip()[:500] or "DR check failed"
    terminal = 0
    for domain in domains:
        row = conn.execute(
            "SELECT COALESCE(dr_check_attempts, 0) AS attempts FROM domains WHERE domain = ?",
            (domain,),
        ).fetchone()
        if row is None:
            continue
        attempts = int(row["attempts"])
        if attempts >= limit:
            details = {
                "error": message,
                "attempts": attempts,
                "exhausted": True,
            }
            conn.execute(
                "UPDATE domains SET dr_check_status = 'failed', dr_check_pass = 0, "
                "dr_rating = NULL, dr_check_reason = ?, dr_check_details_json = ?, "
                "dr_check_checked_at = ? WHERE domain = ?",
                (
                    f"error: {message}",
                    json.dumps(details, sort_keys=True, separators=(",", ":")),
                    checked_at,
                    domain,
                ),
            )
            terminal += 1
        else:
            conn.execute(
                "UPDATE domains SET dr_check_status = NULL, dr_check_pass = NULL, "
                "dr_rating = NULL, dr_check_reason = NULL, dr_check_details_json = NULL, "
                "dr_check_checked_at = NULL WHERE domain = ?",
                (domain,),
            )
    return terminal


def mark_live_checks_processing(
    conn: sqlite3.Connection,
    domains: Iterable[str],
    *,
    started_at: str,
) -> int:
    """Claim a Live check domain and count the attempt before the probe starts."""
    updated = 0
    for domain in domains:
        cursor = conn.execute(
            "UPDATE domains SET live_check_status = 'processing', live_check_pass = NULL, "
            "live_check_class = NULL, live_check_reason = NULL, "
            "live_check_details_json = NULL, live_check_checked_at = ?, "
            "live_check_final_host = NULL, live_check_http_status = NULL, "
            "live_check_attempts = COALESCE(live_check_attempts, 0) + 1 "
            "WHERE domain = ?",
            (started_at, domain),
        )
        updated += cursor.rowcount
    return updated


def set_live_check_results(
    conn: sqlite3.Connection,
    results: Mapping[str, LiveCheckVerdict],
    *,
    checked_at: str,
) -> int:
    """Store one terminal Live check verdict per domain."""
    updated = 0
    for domain, verdict in results.items():
        details = verdict.details
        http = details.get("http") if isinstance(details.get("http"), dict) else {}
        config_version = details.get("config_version")
        cursor = conn.execute(
            "UPDATE domains SET live_check_status = ?, live_check_pass = ?, "
            "live_check_class = ?, live_check_reason = ?, live_check_details_json = ?, "
            "live_check_checked_at = ?, live_check_final_host = ?, "
            "live_check_http_status = ?, live_check_config_version = ? "
            "WHERE domain = ?",
            (
                "passed" if verdict.passed else "failed",
                int(verdict.passed),
                verdict.class_,
                verdict.reason,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
                checked_at,
                details.get("final_host") or http.get("final_host"),
                details.get("http_status") or http.get("status"),
                config_version,
                domain,
            ),
        )
        updated += cursor.rowcount
    return updated


def record_live_check_errors(
    conn: sqlite3.Connection,
    domains: Iterable[str],
    *,
    error: str,
    checked_at: str,
    max_attempts: int,
) -> int:
    """Retry a failed Live check until the per-domain attempt budget is spent."""
    limit = max(1, int(max_attempts))
    message = str(error).strip()[:500] or "Live check failed"
    terminal = 0
    for domain in domains:
        row = conn.execute(
            "SELECT COALESCE(live_check_attempts, 0) AS attempts FROM domains WHERE domain = ?",
            (domain,),
        ).fetchone()
        if row is None:
            continue
        attempts = int(row["attempts"])
        if attempts >= limit:
            details = {
                "error": message,
                "attempts": attempts,
                "exhausted": True,
            }
            conn.execute(
                "UPDATE domains SET live_check_status = 'failed', live_check_pass = 0, "
                "live_check_class = 'error', live_check_reason = ?, "
                "live_check_details_json = ?, live_check_checked_at = ? "
                "WHERE domain = ?",
                (
                    f"error: {message}",
                    json.dumps(details, sort_keys=True, separators=(",", ":")),
                    checked_at,
                    domain,
                ),
            )
            terminal += 1
        else:
            conn.execute(
                "UPDATE domains SET live_check_status = NULL, live_check_pass = NULL, "
                "live_check_class = NULL, live_check_reason = NULL, "
                "live_check_details_json = NULL, live_check_checked_at = NULL, "
                "live_check_final_host = NULL, live_check_http_status = NULL "
                "WHERE domain = ?",
                (domain,),
            )
    return terminal


def set_live_check_manual_pass(
    conn: sqlite3.Connection,
    domain: str,
    *,
    checked_at: str | None = None,
) -> int:
    """Manually pass one failed Live result and preserve its automated evidence.

    The automated class, reason, and details are kept inside
    ``live_check_details_json``; the effective row becomes a pass with the
    ``Manually passed`` reason. A second call or an already-passed domain is
    ignored.
    """
    row = conn.execute(
        "SELECT live_check_reason, live_check_class, live_check_details_json FROM domains "
        "WHERE domain = ? AND live_check_pass = 0 "
        "AND COALESCE(live_check_manual_pass, 0) = 0",
        (domain,),
    ).fetchone()
    if row is None:
        return 0

    stamp = checked_at or datetime.now(UTC).isoformat(timespec="seconds")
    details = _json_object(row["live_check_details_json"])
    details["manual_pass"] = True
    details["manual_pass_at"] = stamp
    details["manual_pass_original_reason"] = row["live_check_reason"]
    details["manual_pass_original_class"] = row["live_check_class"]

    cursor = conn.execute(
        "UPDATE domains SET live_check_status = 'passed', live_check_pass = 1, "
        "live_check_class = 'manual', live_check_reason = ?, "
        "live_check_details_json = ?, live_check_manual_pass = 1, "
        "live_check_manual_pass_at = ? "
        "WHERE domain = ? AND live_check_pass = 0 "
        "AND COALESCE(live_check_manual_pass, 0) = 0",
        (
            "Manually passed",
            json.dumps(details, sort_keys=True, separators=(",", ":")),
            stamp,
            domain,
        ),
    )
    return cursor.rowcount


def mark_wayback_checks_processing(
    conn: sqlite3.Connection,
    domains: Iterable[str],
    *,
    started_at: str,
) -> int:
    """Claim a Wayback domain and count the attempt before collection starts."""
    updated = 0
    for domain in domains:
        cursor = conn.execute(
            "UPDATE domains SET wayback_check_status = 'processing', "
            "wayback_check_pass = NULL, wayback_score = NULL, "
            "wayback_check_reason = NULL, wayback_check_source = NULL, "
            "wayback_check_details_json = NULL, wayback_check_checked_at = ?, "
            "wayback_check_attempts = COALESCE(wayback_check_attempts, 0) + 1 "
            "WHERE domain = ?",
            (started_at, domain),
        )
        updated += cursor.rowcount
    return updated


def set_wayback_check_results(
    conn: sqlite3.Connection,
    results: Mapping[str, WaybackVerdict],
    *,
    checked_at: str,
) -> int:
    """Store one terminal Wayback pass/fail verdict per domain."""
    updated = 0
    for domain, verdict in results.items():
        details = verdict.details
        cursor = conn.execute(
            "UPDATE domains SET wayback_check_status = ?, wayback_check_pass = ?, "
            "wayback_score = ?, wayback_check_reason = ?, wayback_check_source = ?, "
            "wayback_check_details_json = ?, wayback_check_checked_at = ?, "
            "wayback_first_capture = ?, wayback_last_capture = ?, "
            "wayback_age_years = ?, wayback_capture_count = ?, "
            "wayback_config_version = ? "
            "WHERE domain = ?",
            (
                "passed" if verdict.passed else "failed",
                int(verdict.passed),
                int(verdict.score),
                verdict.reason,
                verdict.source,
                json.dumps(details, sort_keys=True, separators=(",", ":")),
                checked_at,
                details.get("first_capture"),
                details.get("last_capture"),
                details.get("age_years"),
                details.get("capture_count"),
                details.get("config_version"),
                domain,
            ),
        )
        updated += cursor.rowcount
    return updated


def record_wayback_check_errors(
    conn: sqlite3.Connection,
    domains: Iterable[str],
    *,
    error: str,
    checked_at: str,
    max_attempts: int,
) -> int:
    """Retry a failed Wayback check until the per-domain attempt budget is spent."""
    limit = max(1, int(max_attempts))
    message = str(error).strip()[:500] or "Wayback check failed"
    terminal = 0
    for domain in domains:
        row = conn.execute(
            "SELECT COALESCE(wayback_check_attempts, 0) AS attempts FROM domains WHERE domain = ?",
            (domain,),
        ).fetchone()
        if row is None:
            continue
        attempts = int(row["attempts"])
        if attempts >= limit:
            details = {
                "error": message,
                "attempts": attempts,
                "exhausted": True,
            }
            conn.execute(
                "UPDATE domains SET wayback_check_status = 'failed', "
                "wayback_check_pass = 0, wayback_score = 0, "
                "wayback_check_reason = ?, wayback_check_source = 'lexicon', "
                "wayback_check_details_json = ?, wayback_check_checked_at = ? "
                "WHERE domain = ?",
                (
                    f"error: {message}",
                    json.dumps(details, sort_keys=True, separators=(",", ":")),
                    checked_at,
                    domain,
                ),
            )
            terminal += 1
        else:
            conn.execute(
                "UPDATE domains SET wayback_check_status = NULL, "
                "wayback_check_pass = NULL, wayback_score = NULL, "
                "wayback_check_reason = NULL, wayback_check_source = NULL, "
                "wayback_check_details_json = NULL, wayback_check_checked_at = NULL "
                "WHERE domain = ?",
                (domain,),
            )
    return terminal


def set_wayback_manual_pass(
    conn: sqlite3.Connection,
    domain: str,
    *,
    checked_at: str | None = None,
) -> int:
    """Manually pass one failed Wayback result and protect it from re-runs.

    The automated score, source, and details are preserved inside
    ``wayback_check_details_json``; the effective row becomes a pass with the
    ``Manually passed`` reason. A second call or an already-passed domain is
    ignored.
    """
    row = conn.execute(
        "SELECT wayback_check_reason, wayback_check_source, wayback_score, "
        "wayback_check_details_json FROM domains "
        "WHERE domain = ? AND wayback_check_pass = 0 "
        "AND COALESCE(wayback_check_manual_pass, 0) = 0",
        (domain,),
    ).fetchone()
    if row is None:
        return 0

    stamp = checked_at or datetime.now(UTC).isoformat(timespec="seconds")
    details = _json_object(row["wayback_check_details_json"])
    details["manual_pass"] = True
    details["manual_pass_at"] = stamp
    details["manual_pass_original_reason"] = row["wayback_check_reason"]
    details["manual_pass_original_source"] = row["wayback_check_source"]
    details["manual_pass_original_score"] = row["wayback_score"]

    cursor = conn.execute(
        "UPDATE domains SET wayback_check_status = 'passed', "
        "wayback_check_pass = 1, wayback_check_reason = ?, "
        "wayback_check_source = 'manual', wayback_check_details_json = ?, "
        "wayback_check_manual_pass = 1, wayback_check_manual_pass_at = ? "
        "WHERE domain = ? AND wayback_check_pass = 0 "
        "AND COALESCE(wayback_check_manual_pass, 0) = 0",
        (
            "Manually passed",
            json.dumps(details, sort_keys=True, separators=(",", ":")),
            stamp,
            domain,
        ),
    )
    return cursor.rowcount


def list_pending_domains(conn: sqlite3.Connection) -> list[str]:
    """Return domains that still need niche classification.

    ``NULL`` is the canonical pending state. The legacy ``unknown`` placeholder
    is included so indexes migrated from the first implementation are also
    processed instead of being stranded. Blocked domains are terminal: they are
    never classified and never returned as pending.
    """
    rows = conn.execute(
        "SELECT domain FROM domains "
        "WHERE (niche IS NULL OR niche = 'unknown') AND blocked_category IS NULL "
        "ORDER BY domain"
    ).fetchall()
    return [row["domain"] for row in rows]


def list_unblocked_domains(conn: sqlite3.Connection) -> list[str]:
    """Return every stored domain that has not already been marked blocked."""
    rows = conn.execute(
        "SELECT domain FROM domains WHERE blocked_category IS NULL ORDER BY domain"
    ).fetchall()
    return [row["domain"] for row in rows]


def list_collection_candidates(
    conn: sqlite3.Connection,
    *,
    limit: int,
    today: date_type | str,
    require_wayback: bool = True,
) -> list[sqlite3.Row]:
    """Return DR-passed domains that still need an Ahrefs run.

    A domain is eligible when it passed the prefilter, the keyword check, the
    free DR check, and the Wayback checks, is not blocked, has no successful run
    yet, and has no run on ``today`` (failed runs from earlier days are retried).
    The DR and Wayback gates keep data collection from re-claiming an earlier
    queue. Candidates are ranked by keyword confidence, then price, then domain
    so the paid runs go to the strongest candidates first.
    """
    day = today.isoformat() if isinstance(today, date_type) else str(today)[:10]
    wayback_clause = "AND COALESCE(d.wayback_check_pass, 0) = 1" if require_wayback else ""
    rows = conn.execute(
        f"""
        SELECT
            d.domain AS domain,
            d.keyword_check_confidence AS keyword_confidence,
            (
                SELECT CASE
                    WHEN json_valid(o.metrics_json)
                    THEN CAST(json_extract(o.metrics_json, '$.price') AS REAL)
                END
                FROM domain_observations o
                WHERE o.domain = d.domain
                ORDER BY o.collected_date DESC, o.source
                LIMIT 1
            ) AS price
        FROM domains d
        WHERE d.blocked_category IS NULL
          AND COALESCE(d.dr_check_pass, 0) = 1
          {wayback_clause}
          AND COALESCE(d.keyword_check_pass, 0) = 1
          AND (d.prefilter_pass = 1 OR COALESCE(d.prefilter_manual_pass, 0) = 1)
          AND NOT EXISTS (
              SELECT 1 FROM runs r
              WHERE r.domain = d.domain
                AND (
                    r.error IS NULL
                    OR substr(r.fetched_at, 1, 10) = ?
                )
          )
        ORDER BY
            (d.keyword_check_confidence IS NULL) ASC,
            d.keyword_check_confidence DESC,
            (price IS NULL) ASC,
            price ASC,
            d.domain ASC
        LIMIT ?
        """,
        (day, max(0, limit)),
    ).fetchall()
    return list(rows)


def units_spent_since(conn: sqlite3.Connection, since_date: date_type | str) -> int:
    """Return the actual Ahrefs units recorded for runs on or after ``since_date``."""
    day = since_date.isoformat() if isinstance(since_date, date_type) else str(since_date)[:10]
    row = conn.execute(
        "SELECT COALESCE(SUM(units_actual), 0) AS units FROM runs "
        "WHERE substr(fetched_at, 1, 10) >= ?",
        (day,),
    ).fetchone()
    return int(row["units"] or 0)


def latest_collection_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return the latest run per domain, preferring a successful run.

    A domain can have a failed retry after an earlier success; the successful
    run remains the collected evidence for the tab. Request totals are included
    so the page can show partial failures without a second query.
    """
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT
                r.*,
                ROW_NUMBER() OVER (
                    PARTITION BY r.domain
                    ORDER BY
                        CASE WHEN r.error IS NULL THEN 0 ELSE 1 END,
                        r.fetched_at DESC,
                        r.run_id DESC
                ) AS row_number
            FROM runs r
        )
        SELECT
            l.run_id,
            l.domain,
            l.fetched_at,
            l.tier,
            l.coverage_json,
            l.units_actual,
            l.error,
            (
                SELECT COUNT(*) FROM requests q WHERE q.run_id = l.run_id
            ) AS requests_total,
            (
                SELECT COUNT(*) FROM requests q
                WHERE q.run_id = l.run_id
                  AND q.error IS NULL
                  AND q.status BETWEEN 200 AND 299
            ) AS requests_ok
        FROM ranked l
        WHERE l.row_number = 1
        ORDER BY l.domain
        """
    ).fetchall()
    return list(rows)


def _json_object(value: str | None) -> dict[str, Any]:
    """Decode a stored JSON object, treating missing or bad values as empty."""
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


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
