"""Budgeted, automatic Ahrefs data collection for DR-passed domains.

The collection stage sits after the free DR check and the Wayback Machine
checks. A worker claims the highest-ranked eligible domains and runs the
standard-tier enrichment until the monthly unit budget (minus a reserve) is
exhausted. Everything the paid run
produces is stored by :mod:`expired_domains.enrich`; this module only decides
who runs next and keeps the page-facing summary.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expired_domains.config import (
    DEFAULT_DATA_COLLECTION_ESTIMATED_UNITS,
    DEFAULT_DATA_COLLECTION_MAX_PER_CYCLE,
    DEFAULT_DATA_COLLECTION_MONTHLY_UNITS,
    DEFAULT_DATA_COLLECTION_RESERVE_UNITS,
    ConfigError,
)
from expired_domains.models import RunSummary
from expired_domains.storage.db import (
    connect,
    latest_collection_runs,
    list_collection_candidates,
    units_spent_since,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionBudget:
    """The local monthly Ahrefs unit budget and what has been spent so far."""

    monthly_cap: int
    reserve: int
    spent: int

    @property
    def allowed(self) -> int:
        """Units the worker may spend this month after keeping the reserve."""
        return max(0, self.monthly_cap - self.reserve)

    @property
    def remaining(self) -> int:
        """Units still available under the monthly allowance."""
        return max(0, self.allowed - self.spent)

    def as_meta(self) -> dict[str, int]:
        """Return the page-facing budget shape."""
        return {
            "monthly_cap": self.monthly_cap,
            "reserve": self.reserve,
            "allowed": self.allowed,
            "spent": self.spent,
            "remaining": self.remaining,
        }


@dataclass(frozen=True)
class CollectionRecord:
    """The latest stored Ahrefs run for one domain."""

    domain: str
    run_id: str
    fetched_at: str
    tier: str
    units: int
    requests_ok: int
    requests_total: int
    error: str | None
    coverage_mode: str
    live_links: int | None
    all_time_links: int | None
    live_refdomains: int | None
    all_time_refdomains: int | None
    rows_unique: int | None
    top_anchors: int | None
    lexicon_complete: bool | None

    @property
    def status(self) -> str:
        """Return ``collected`` for a clean run or ``failed`` for a partial one."""
        return "failed" if self.error else "collected"

    def as_row(self, *, eligible: bool) -> dict[str, Any]:
        """Return the JSON-friendly shape consumed by the page."""
        return {
            "eligible": eligible,
            "status": self.status,
            "run_id": self.run_id,
            "fetched_at": self.fetched_at,
            "tier": self.tier,
            "units": self.units,
            "requests_ok": self.requests_ok,
            "requests_total": self.requests_total,
            "error": self.error,
            "coverage_mode": self.coverage_mode,
            "live_links": self.live_links,
            "all_time_links": self.all_time_links,
            "live_refdomains": self.live_refdomains,
            "all_time_refdomains": self.all_time_refdomains,
            "rows_unique": self.rows_unique,
            "top_anchors": self.top_anchors,
            "lexicon_complete": self.lexicon_complete,
        }


def collection_budget(
    conn: sqlite3.Connection,
    *,
    monthly_cap: int = DEFAULT_DATA_COLLECTION_MONTHLY_UNITS,
    reserve: int = DEFAULT_DATA_COLLECTION_RESERVE_UNITS,
    now: datetime | None = None,
) -> CollectionBudget:
    """Return this calendar month's local unit budget."""
    moment = _utc(now) or datetime.now(UTC)
    month_start = moment.strftime("%Y-%m-01")
    try:
        spent = units_spent_since(conn, month_start)
    except sqlite3.OperationalError:
        # An index created before the run tables existed has nothing to spend.
        spent = 0
    return CollectionBudget(
        monthly_cap=max(0, monthly_cap),
        reserve=max(0, reserve),
        spent=spent,
    )


def load_collection_records(conn: sqlite3.Connection) -> dict[str, CollectionRecord]:
    """Load the latest collected run per domain, keyed by domain."""
    try:
        rows = latest_collection_runs(conn)
    except sqlite3.OperationalError:
        # Legacy indexes without the run tables simply have no collected data.
        return {}
    records: dict[str, CollectionRecord] = {}
    for row in rows:
        coverage = _coverage(row["coverage_json"])
        stats = coverage.get("stats") if isinstance(coverage.get("stats"), dict) else {}
        coverage_rows = coverage.get("rows") if isinstance(coverage.get("rows"), dict) else {}
        records[row["domain"]] = CollectionRecord(
            domain=row["domain"],
            run_id=row["run_id"],
            fetched_at=row["fetched_at"],
            tier=row["tier"],
            units=int(row["units_actual"] or 0),
            requests_ok=int(row["requests_ok"] or 0),
            requests_total=int(row["requests_total"] or 0),
            error=_error_text(row["error"]),
            coverage_mode=str(coverage.get("mode") or ""),
            live_links=_int_or_none(stats.get("live")),
            all_time_links=_int_or_none(stats.get("all_time")),
            live_refdomains=_int_or_none(stats.get("live_refdomains")),
            all_time_refdomains=_int_or_none(stats.get("all_time_refdomains")),
            rows_unique=_int_or_none(coverage_rows.get("unique")),
            top_anchors=_int_or_none(coverage.get("top_anchors")),
            lexicon_complete=(
                coverage.get("lexicon_complete")
                if isinstance(coverage.get("lexicon_complete"), bool)
                else None
            ),
        )
    return records


def collect_pending(
    db_path: Path,
    *,
    collect: Callable[[str], RunSummary],
    monthly_cap: int = DEFAULT_DATA_COLLECTION_MONTHLY_UNITS,
    reserve_units: int = DEFAULT_DATA_COLLECTION_RESERVE_UNITS,
    estimated_units: int = DEFAULT_DATA_COLLECTION_ESTIMATED_UNITS,
    limit: int = DEFAULT_DATA_COLLECTION_MAX_PER_CYCLE,
    require_wayback: bool = True,
    now: datetime | None = None,
) -> int:
    """Collect up to ``limit`` eligible domains and return completed runs.

    The worker stops before starting a run when fewer than ``estimated_units``
    remain under the monthly allowance. Failures are recorded by ``enrich`` and
    the next cycle simply moves on; a failed run is retried on a later day.
    """
    moment = _utc(now) or datetime.now(UTC)
    cap = max(0, monthly_cap)
    reserve = max(0, reserve_units)
    estimate = max(1, estimated_units)
    wanted = int(limit)
    if wanted <= 0:
        return 0

    conn = connect(db_path)
    try:
        budget = collection_budget(
            conn,
            monthly_cap=cap,
            reserve=reserve,
            now=moment,
        )
        if budget.remaining < estimate:
            logger.info(
                "data collection paused: %s units remaining, %s estimated per run",
                budget.remaining,
                estimate,
            )
            return 0
        candidates = list_collection_candidates(
            conn,
            limit=wanted,
            today=moment.date(),
            require_wayback=require_wayback,
        )
    finally:
        conn.close()

    if not candidates:
        return 0

    collected = 0
    spent = budget.spent
    allowance = cap - reserve
    for candidate in candidates:
        if allowance - spent < estimate:
            logger.info(
                "data collection paused at %s units; monthly allowance is %s",
                spent,
                allowance,
            )
            break

        domain = str(candidate["domain"])
        try:
            summary = collect(domain)
        except ConfigError as exc:
            logger.warning("data collection cannot run: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001 - one domain must not stop the worker
            logger.warning("data collection for %s failed: %s", domain, exc)
            continue

        spent += int(summary.units.actual or 0)
        if summary.exit_code == 0:
            collected += 1
            logger.info(
                "collected %s units=%s run_id=%s",
                domain,
                summary.units.actual,
                summary.run_id,
            )
        else:
            logger.warning(
                "data collection did not finish for %s: %s",
                domain,
                summary.message or summary.error,
            )
    return collected


def _coverage(value: str | None) -> dict[str, Any]:
    """Decode a stored coverage blob, treating bad values as empty."""
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _error_text(value: str | None) -> str | None:
    """Turn the stored failure JSON into a compact human-readable message."""
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return str(value)
    if isinstance(decoded, dict):
        return "; ".join(f"{key}: {message}" for key, message in sorted(decoded.items()))
    return str(decoded)


def _int_or_none(value: Any) -> int | None:
    """Return an int for a numeric value, or ``None`` when absent/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc(value: datetime | None) -> datetime | None:
    """Return an aware UTC datetime, or ``None`` when no value was supplied."""
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
