"""Typed shapes shared by the retrieval pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Units:
    """Ahrefs API unit accounting for one request."""

    actual: int
    billed: int


@dataclass(frozen=True)
class QuerySpec:
    """One provider request to execute."""

    query_id: str
    endpoint: str
    params: dict[str, str]
    page: int = 0


@dataclass(frozen=True)
class StatsRow:
    """One ``stats`` snapshot row."""

    snapshot: str
    links: int | None
    refdomains: int | None


@dataclass(frozen=True)
class HistoryRow:
    """One monthly ``history`` series row."""

    series: str
    date: str
    value: int | None


@dataclass(frozen=True)
class BacklinkRow:
    """One ``backlinks`` row, after normalisation and hashing."""

    link_hash: str
    source_queries: list[str]
    url_from: str | None
    url_to: str | None
    domain_from: str | None
    root_domain_from: str | None
    anchor_raw: str | None
    anchor_norm: str | None
    is_dofollow: bool | None
    domain_rating: float | None
    first_seen_link: str | None
    era: None
    raw_json: str


@dataclass(frozen=True)
class RequestRecord:
    """Normalised result of one provider request."""

    query_id: str
    endpoint: str
    page: int
    params: dict[str, str]
    url: str
    status: int | None
    started_at: str
    finished_at: str
    attempts: int
    error: str | None
    parse_error: str | None
    rows: str | None
    units_cost_total: str | None
    units_cost_total_actual: str | None
    units_cost_row: str | None
    cache: str | None
    headers: dict[str, str]
    payload: dict[str, Any] | None = None
    raw_path: str | None = None

    @property
    def units(self) -> Units:
        """Return the parsed actual/billed unit pair for this request."""
        return Units(
            actual=_as_int(self.units_cost_total_actual),
            billed=_as_int(self.units_cost_total),
        )

    def as_request_dict(self) -> dict[str, Any]:
        """Return the ``request`` member of the raw payload pair."""
        return {
            "query_id": self.query_id,
            "endpoint": self.endpoint,
            "page": self.page,
            "params": dict(self.params),
            "url": self.url,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempts": self.attempts,
            "error": self.error,
            "parse_error": self.parse_error,
            "rows": self.rows,
            "units_cost_total": self.units_cost_total,
            "units_cost_total_actual": self.units_cost_total_actual,
            "units_cost_row": self.units_cost_row,
            "cache": self.cache,
            "headers": dict(self.headers),
        }


@dataclass(frozen=True)
class Coverage:
    """The §3.8 coverage object."""

    mode: str
    stats: dict[str, int | None]
    rows: dict[str, int]
    top_anchors: int
    lexicon_complete: bool
    history_months: int
    drop_date_source: str
    prefilter: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable copy of the coverage object."""
        return {
            "mode": self.mode,
            "stats": dict(self.stats),
            "rows": dict(self.rows),
            "top_anchors": self.top_anchors,
            "lexicon_complete": self.lexicon_complete,
            "history_months": self.history_months,
            "drop_date_source": self.drop_date_source,
            "prefilter": self.prefilter,
        }


@dataclass(frozen=True)
class RunSummary:
    """Typed result of one enrichment run."""

    run_id: str
    domain: str
    raw_dir: Path
    requests: list[RequestRecord]
    coverage: Coverage
    units: Units
    error: dict[str, str] | None
    exit_code: int


def _as_int(value: str | None) -> int:
    """Parse a header value as an integer, treating anything invalid as zero."""
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
