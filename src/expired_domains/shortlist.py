"""Render the domain-discovery shortlist from imported candidate observations.

Discovery is the first stage of the prototype shortlist: every candidate
collected from a marketplace export. A deterministic blocklist runs first and
marks disallowed names; the remaining uploads are niche-classified from a local
dictionary, then an LLM pass for unknown names. This page shows the stored
niche alongside the source and collection date, with a separate blocked view.

The output is a single self-contained HTML file, matching the prototype's
look and feel without requiring a web server. All rows are embedded as JSON so
search, source views, sorting and pagination work offline.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expired_domains.blocklist import load_blocklist
from expired_domains.collection import CollectionBudget, CollectionRecord
from expired_domains.niches import (
    LLM_UNKNOWN_NICHE,
    STUB_LLM_NICHE,
    UNKNOWN_NICHE,
    load_niche_lexicon,
)
from expired_domains.prefilter import load_prefilter_config

TEMPLATE_PATH = Path(__file__).with_name("templates") / "shortlist.html"

# Long label, short table label, sidebar icon.
SOURCE_DISPLAY: dict[str, tuple[str, str, str]] = {
    "spamzilla": ("SpamZilla", "SpamZilla", "SZ"),
    "expireddomains": ("Expired Domains", "ExpiredDomains", "ED"),
    "godaddy_auctions": ("GoDaddy Auctions", "GoDaddy", "GD"),
}

# Display order for known sources. Unknown sources follow, alphabetically.
SOURCE_ORDER = ("spamzilla", "expireddomains", "godaddy_auctions")


# Display-order niche labels. Pending classification is added by the page data.
PENDING_NICHE = "pending"


def _niche_labels() -> dict[str, str]:
    """Return display labels for every terminal niche value."""
    labels = {key: niche.label for key, niche in load_niche_lexicon().niches.items()}
    labels[LLM_UNKNOWN_NICHE] = "LLM / Unknown"
    labels[STUB_LLM_NICHE] = STUB_LLM_NICHE
    return labels


NICHE_LABELS: dict[str, str] = _niche_labels()
BLOCK_LABELS: dict[str, str] = {
    key: category.label for key, category in load_blocklist().categories.items()
}

# One row per registrable domain. The latest observation wins so re-imported
# exports do not duplicate a domain; known sources break same-day ties.
_RANKED_DOMAIN_SQL = """
WITH ranked AS (
    SELECT
        domain,
        source,
        collected_date,
        metrics_json,
        ROW_NUMBER() OVER (
            PARTITION BY domain
            ORDER BY
                collected_date DESC,
                CASE source
                    WHEN 'godaddy_auctions' THEN 0
                    WHEN 'spamzilla' THEN 1
                    WHEN 'expireddomains' THEN 2
                    ELSE 3
                END,
                source
        ) AS row_number
    FROM domain_observations
)
"""

# A very old index may predate the metrics column. The page still reads it.
_RANKED_DOMAIN_SQL_WITHOUT_METRICS = """
WITH ranked AS (
    SELECT
        domain,
        source,
        collected_date,
        NULL AS metrics_json,
        ROW_NUMBER() OVER (
            PARTITION BY domain
            ORDER BY
                collected_date DESC,
                CASE source
                    WHEN 'godaddy_auctions' THEN 0
                    WHEN 'spamzilla' THEN 1
                    WHEN 'expireddomains' THEN 2
                    ELSE 3
                END,
                source
        ) AS row_number
    FROM domain_observations
)
"""


def _discovery_sql(conn: sqlite3.Connection) -> str:
    """Build the discovery query against whatever columns the index has.

    Older indexes are read without migration so the page still renders; missing
    columns become ``NULL`` and the UI shows the stage as not reached yet.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(domains)")}
    observation_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(domain_observations)")
    }
    ranked_sql = (
        _RANKED_DOMAIN_SQL
        if "metrics_json" in observation_columns
        else _RANKED_DOMAIN_SQL_WITHOUT_METRICS
    )

    def expression(column: str) -> str:
        return f"domains.{column}" if column in columns else "NULL"

    return (
        ranked_sql
        + f"""
SELECT
    ranked.domain AS domain,
    ranked.source AS source,
    ranked.collected_date AS collected_date,
    ranked.metrics_json AS metrics_json,
    {expression("niche")} AS niche,
    {expression("niche_source")} AS niche_source,
    {expression("blocked_category")} AS blocked_category,
    {expression("blocked_terms_json")} AS blocked_terms_json,
    {expression("prefilter_pass")} AS prefilter_pass,
    {expression("prefilter_reason")} AS prefilter_reason,
    {expression("prefilter_manual_pass")} AS prefilter_manual_pass,
    {expression("prefilter_config_version")} AS prefilter_config_version,
    {expression("keyword_check_status")} AS keyword_check_status,
    {expression("keyword_check_pass")} AS keyword_check_pass,
    {expression("keyword_check_reason")} AS keyword_check_reason,
    {expression("keyword_check_confidence")} AS keyword_check_confidence,
    {expression("keyword_check_model")} AS keyword_check_model,
    {expression("keyword_check_checked_at")} AS keyword_check_checked_at,
    {expression("dr_check_status")} AS dr_check_status,
    {expression("dr_check_pass")} AS dr_check_pass,
    {expression("dr_rating")} AS dr_rating,
    {expression("dr_check_reason")} AS dr_check_reason,
    {expression("dr_check_attempts")} AS dr_check_attempts,
    {expression("dr_check_checked_at")} AS dr_check_checked_at,
    {expression("live_check_status")} AS live_check_status,
    {expression("live_check_pass")} AS live_check_pass,
    {expression("live_check_class")} AS live_check_class,
    {expression("live_check_reason")} AS live_check_reason,
    {expression("live_check_attempts")} AS live_check_attempts,
    {expression("live_check_checked_at")} AS live_check_checked_at,
    {expression("live_check_manual_pass")} AS live_check_manual_pass,
    {expression("live_check_final_host")} AS live_check_final_host,
    {expression("live_check_http_status")} AS live_check_http_status,
    {expression("live_check_config_version")} AS live_check_config_version,
    {expression("wayback_check_status")} AS wayback_check_status,
    {expression("wayback_check_pass")} AS wayback_check_pass,
    {expression("wayback_score")} AS wayback_score,
    {expression("wayback_check_reason")} AS wayback_check_reason,
    {expression("wayback_check_source")} AS wayback_check_source,
    {expression("wayback_check_attempts")} AS wayback_check_attempts,
    {expression("wayback_check_checked_at")} AS wayback_check_checked_at,
    {expression("wayback_check_manual_pass")} AS wayback_check_manual_pass,
    {expression("wayback_first_capture")} AS wayback_first_capture,
    {expression("wayback_last_capture")} AS wayback_last_capture,
    {expression("wayback_age_years")} AS wayback_age_years,
    {expression("wayback_capture_count")} AS wayback_capture_count,
    {expression("evaluation_status")} AS evaluation_status,
    {expression("evaluation_score")} AS evaluation_score,
    {expression("evaluation_evidence_points")} AS evaluation_evidence_points,
    {expression("evaluation_verdict")} AS evaluation_verdict,
    {expression("evaluation_recommendation")} AS evaluation_recommendation,
    {expression("evaluation_reason")} AS evaluation_reason,
    {expression("evaluation_config_version")} AS evaluation_config_version,
    {expression("evaluation_checked_at")} AS evaluation_checked_at
FROM ranked
LEFT JOIN domains ON domains.domain = ranked.domain
WHERE ranked.row_number = 1
ORDER BY ranked.domain
"""
    )


# The latest collection date per source is the import date shown in the sidebar.
_SOURCE_IMPORTS_SQL = """
SELECT source, MAX(collected_date) AS last_import
FROM domain_observations
GROUP BY source
"""


@dataclass(frozen=True)
class DiscoveryRecord:
    """One candidate domain as it appears in the discovery view."""

    domain: str
    source: str
    collected_date: str
    niche: str | None = None
    niche_source: str | None = None
    reached: int = 1
    blocked_category: str | None = None
    blocked_terms: tuple[str, ...] = ()
    prefilter_pass: bool | None = None
    prefilter_reason: str | None = None
    prefilter_manual_pass: bool = False
    prefilter_config_version: str | None = None
    keyword_check_status: str | None = None
    keyword_check_pass: bool | None = None
    keyword_check_reason: str | None = None
    keyword_check_confidence: float | None = None
    keyword_check_model: str | None = None
    keyword_check_checked_at: str | None = None
    dr_check_status: str | None = None
    dr_check_pass: bool | None = None
    dr_rating: float | None = None
    dr_check_reason: str | None = None
    dr_check_attempts: int = 0
    dr_check_checked_at: str | None = None
    live_check_status: str | None = None
    live_check_pass: bool | None = None
    live_check_class: str | None = None
    live_check_reason: str | None = None
    live_check_attempts: int = 0
    live_check_checked_at: str | None = None
    live_check_manual_pass: bool = False
    live_check_final_host: str | None = None
    live_check_http_status: int | None = None
    live_check_config_version: str | None = None
    wayback_check_status: str | None = None
    wayback_check_pass: bool | None = None
    wayback_score: int | None = None
    wayback_check_reason: str | None = None
    wayback_check_source: str | None = None
    wayback_check_attempts: int = 0
    wayback_check_checked_at: str | None = None
    wayback_check_manual_pass: bool = False
    wayback_first_capture: str | None = None
    wayback_last_capture: str | None = None
    wayback_age_years: float | None = None
    wayback_capture_count: int | None = None
    evaluation_status: str | None = None
    evaluation_score: float | None = None
    evaluation_evidence_points: float | None = None
    evaluation_verdict: str | None = None
    evaluation_recommendation: str | None = None
    evaluation_reason: str | None = None
    evaluation_config_version: str | None = None
    evaluation_checked_at: str | None = None
    price: float | None = None
    bid_end_at: str | None = None

    @property
    def blocked(self) -> bool:
        """Return True when the deterministic blocklist rejected this domain."""
        return self.blocked_category is not None

    def _evaluation_row(self) -> dict[str, Any] | None:
        """Return the compact evaluation payload shown in the pipeline view."""
        if self.evaluation_status is None and self.evaluation_verdict is None:
            return None
        return {
            "status": self.evaluation_status or "",
            "score": self.evaluation_score,
            "evidence_points": self.evaluation_evidence_points,
            "verdict": self.evaluation_verdict or "",
            "recommendation": self.evaluation_recommendation or "",
            "reason": self.evaluation_reason or "",
            "config_version": self.evaluation_config_version or "",
            "checked_at": self.evaluation_checked_at or "",
        }

    def as_row(self) -> dict[str, Any]:
        """Return the JSON-friendly shape consumed by the page."""
        label, short, icon = source_display(self.source)
        if self.blocked:
            blocked_label = BLOCK_LABELS.get(self.blocked_category, self.blocked_category)
            return {
                "domain": self.domain,
                "source": self.source,
                "source_label": label,
                "source_short": short,
                "source_icon": icon,
                "niche": "blocked",
                "niche_label": f"Blocked · {blocked_label}",
                "niche_source": "blocklist",
                "collected": self.collected_date,
                "reached": 0,
                "progress_label": "Blocked",
                "blocked": True,
                "blocked_category": self.blocked_category,
                "blocked_label": blocked_label,
                "blocked_terms": list(self.blocked_terms),
                "prefilter": None,
                "keyword_check": None,
                "dr_check": None,
                "live_check": None,
                "wayback_check": None,
                "evaluation": None,
                "price": self.price,
                "bid_end_at": self.bid_end_at,
            }

        pending = self.niche is None or self.niche == UNKNOWN_NICHE
        niche = PENDING_NICHE if pending else self.niche
        prefilter = (
            {
                "pass": self.prefilter_pass,
                "reason": self.prefilter_reason or "",
                "manual": self.prefilter_manual_pass,
            }
            if self.prefilter_pass is not None
            else None
        )
        keyword_status = self.keyword_check_status or (
            None
            if self.keyword_check_pass is None
            else "reusable"
            if self.keyword_check_pass
            else "not_reusable"
        )
        keyword_check = (
            {
                "status": keyword_status,
                "reusable": (None if keyword_status == "processing" else self.keyword_check_pass),
                "reason": self.keyword_check_reason or "",
                "confidence": self.keyword_check_confidence,
                "model": self.keyword_check_model or "",
                "checked_at": self.keyword_check_checked_at or "",
            }
            if keyword_status is not None
            else None
        )
        if keyword_check is not None:
            reached = 2
            if keyword_status == "processing":
                progress_label = "Keyword check processing"
            elif self.keyword_check_pass:
                progress_label = "Keyword passed"
            else:
                progress_label = "Keyword failed"
        elif prefilter is not None:
            reached = 1
            if self.prefilter_manual_pass:
                progress_label = "Passed pre-filter (manual)"
            else:
                progress_label = "Passed pre-filter" if self.prefilter_pass else "Failed pre-filter"
        else:
            reached = 0
            progress_label = "Pending classification" if pending else "Classified"
        dr_status = self.dr_check_status or (
            None if self.dr_check_pass is None else "passed" if self.dr_check_pass else "failed"
        )
        dr_check = (
            {
                "status": dr_status,
                "passed": (None if dr_status == "processing" else self.dr_check_pass),
                "rating": self.dr_rating,
                "reason": self.dr_check_reason or "",
                "attempts": self.dr_check_attempts,
                "checked_at": self.dr_check_checked_at or "",
            }
            if dr_status is not None
            else None
        )
        if dr_check is not None:
            reached = 3
            if dr_status == "processing":
                progress_label = "DR check processing"
            elif self.dr_check_pass:
                progress_label = "DR passed"
            else:
                progress_label = "DR failed"
        live_status = self.live_check_status or (
            None if self.live_check_pass is None else "passed" if self.live_check_pass else "failed"
        )
        live_check = (
            {
                "status": live_status,
                "passed": (None if live_status == "processing" else self.live_check_pass),
                "class": self.live_check_class or "",
                "reason": self.live_check_reason or "",
                "attempts": self.live_check_attempts,
                "checked_at": self.live_check_checked_at or "",
                "manual": self.live_check_manual_pass,
                "final_host": self.live_check_final_host or "",
                "http_status": self.live_check_http_status,
                "config_version": self.live_check_config_version or "",
            }
            if live_status is not None
            else None
        )
        if live_check is not None:
            reached = 4
            if live_status == "processing":
                progress_label = "Live check processing"
            elif self.live_check_pass:
                progress_label = (
                    "Live passed (manual)" if self.live_check_manual_pass else "Live passed"
                )
            else:
                progress_label = "Live failed"
        wayback_status = self.wayback_check_status or (
            None
            if self.wayback_check_pass is None
            else "passed"
            if self.wayback_check_pass
            else "failed"
        )
        wayback_check = (
            {
                "status": wayback_status,
                "passed": (None if wayback_status == "processing" else self.wayback_check_pass),
                "score": self.wayback_score,
                "reason": self.wayback_check_reason or "",
                "source": self.wayback_check_source or "",
                "attempts": self.wayback_check_attempts,
                "checked_at": self.wayback_check_checked_at or "",
                "manual": self.wayback_check_manual_pass,
                "first_capture": self.wayback_first_capture or "",
                "last_capture": self.wayback_last_capture or "",
                "age_years": self.wayback_age_years,
                "capture_count": self.wayback_capture_count,
            }
            if wayback_status is not None
            else None
        )
        if wayback_check is not None:
            reached = 5
            if wayback_status == "processing":
                progress_label = "Wayback check processing"
            elif self.wayback_check_pass:
                progress_label = (
                    "Wayback passed (manual)"
                    if self.wayback_check_manual_pass
                    else "Wayback passed"
                )
            else:
                progress_label = "Wayback failed"
        return {
            "domain": self.domain,
            "source": self.source,
            "source_label": label,
            "source_short": short,
            "source_icon": icon,
            "niche": niche,
            "niche_label": (
                "Pending classification" if pending else NICHE_LABELS.get(niche, niche)
            ),
            "niche_source": self.niche_source or PENDING_NICHE,
            "collected": self.collected_date,
            "reached": reached,
            "progress_label": progress_label,
            "blocked": False,
            "blocked_category": None,
            "blocked_label": None,
            "blocked_terms": [],
            "prefilter": prefilter,
            "keyword_check": keyword_check,
            "dr_check": dr_check,
            "live_check": live_check,
            "wayback_check": wayback_check,
            "evaluation": self._evaluation_row(),
            "price": self.price,
            "bid_end_at": self.bid_end_at,
        }


def source_display(source: str) -> tuple[str, str, str]:
    """Return display labels for a source slug, with a readable fallback."""
    known = SOURCE_DISPLAY.get(source)
    if known is not None:
        return known

    label = " ".join(part.capitalize() for part in source.replace("-", "_").split("_") if part)
    label = label or source
    short = label.replace(" ", "")
    icon = label[:2].upper()
    return label, short, icon


def load_discovery_records(conn: sqlite3.Connection) -> list[DiscoveryRecord]:
    """Load one discovery row per canonical domain from ``domain_observations``."""
    rows = conn.execute(_discovery_sql(conn)).fetchall()
    return [
        DiscoveryRecord(
            domain=row["domain"],
            source=row["source"],
            collected_date=row["collected_date"],
            niche=row["niche"],
            niche_source=row["niche_source"],
            blocked_category=row["blocked_category"],
            blocked_terms=_blocked_terms(row["blocked_terms_json"]),
            prefilter_pass=(None if row["prefilter_pass"] is None else bool(row["prefilter_pass"])),
            prefilter_reason=row["prefilter_reason"],
            prefilter_manual_pass=bool(row["prefilter_manual_pass"]),
            prefilter_config_version=row["prefilter_config_version"],
            keyword_check_status=row["keyword_check_status"],
            keyword_check_pass=(
                None if row["keyword_check_pass"] is None else bool(row["keyword_check_pass"])
            ),
            keyword_check_reason=row["keyword_check_reason"],
            keyword_check_confidence=(
                None
                if row["keyword_check_confidence"] is None
                else float(row["keyword_check_confidence"])
            ),
            keyword_check_model=row["keyword_check_model"],
            keyword_check_checked_at=row["keyword_check_checked_at"],
            dr_check_status=row["dr_check_status"],
            dr_check_pass=(None if row["dr_check_pass"] is None else bool(row["dr_check_pass"])),
            dr_rating=(None if row["dr_rating"] is None else float(row["dr_rating"])),
            dr_check_reason=row["dr_check_reason"],
            dr_check_attempts=int(row["dr_check_attempts"] or 0),
            dr_check_checked_at=row["dr_check_checked_at"],
            live_check_status=row["live_check_status"],
            live_check_pass=(
                None if row["live_check_pass"] is None else bool(row["live_check_pass"])
            ),
            live_check_class=row["live_check_class"],
            live_check_reason=row["live_check_reason"],
            live_check_attempts=int(row["live_check_attempts"] or 0),
            live_check_checked_at=row["live_check_checked_at"],
            live_check_manual_pass=bool(row["live_check_manual_pass"]),
            live_check_final_host=row["live_check_final_host"],
            live_check_http_status=(
                None
                if row["live_check_http_status"] is None
                else int(row["live_check_http_status"])
            ),
            live_check_config_version=row["live_check_config_version"],
            wayback_check_status=row["wayback_check_status"],
            wayback_check_pass=(
                None if row["wayback_check_pass"] is None else bool(row["wayback_check_pass"])
            ),
            wayback_score=(None if row["wayback_score"] is None else int(row["wayback_score"])),
            wayback_check_reason=row["wayback_check_reason"],
            wayback_check_source=row["wayback_check_source"],
            wayback_check_attempts=int(row["wayback_check_attempts"] or 0),
            wayback_check_checked_at=row["wayback_check_checked_at"],
            wayback_check_manual_pass=bool(row["wayback_check_manual_pass"]),
            wayback_first_capture=row["wayback_first_capture"],
            wayback_last_capture=row["wayback_last_capture"],
            wayback_age_years=(
                None if row["wayback_age_years"] is None else float(row["wayback_age_years"])
            ),
            wayback_capture_count=(
                None if row["wayback_capture_count"] is None else int(row["wayback_capture_count"])
            ),
            evaluation_status=row["evaluation_status"],
            evaluation_score=(
                None if row["evaluation_score"] is None else float(row["evaluation_score"])
            ),
            evaluation_evidence_points=(
                None
                if row["evaluation_evidence_points"] is None
                else float(row["evaluation_evidence_points"])
            ),
            evaluation_verdict=row["evaluation_verdict"],
            evaluation_recommendation=row["evaluation_recommendation"],
            evaluation_reason=row["evaluation_reason"],
            evaluation_config_version=row["evaluation_config_version"],
            evaluation_checked_at=row["evaluation_checked_at"],
            price=_price(row["metrics_json"]),
            bid_end_at=_bid_end_at(row["metrics_json"]),
        )
        for row in rows
    ]


def _price(metrics_json: str | None) -> float | None:
    """Read the canonical price from a stored metrics blob, tolerating bad data."""
    if not metrics_json:
        return None
    try:
        metrics = json.loads(metrics_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(metrics, dict):
        return None
    value = metrics.get("price")
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bid_end_at(metrics_json: str | None) -> str | None:
    """Read the canonical auction end timestamp from a stored metrics blob."""
    if not metrics_json:
        return None
    try:
        metrics = json.loads(metrics_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(metrics, dict):
        return None
    value = metrics.get("auction_end_at")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _blocked_terms(value: str | None) -> tuple[str, ...]:
    """Decode a stored matched-terms list, tolerating older or bad values."""
    if not value:
        return ()
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return ()
    return tuple(str(term) for term in decoded) if isinstance(decoded, list) else ()


def load_source_imports(conn: sqlite3.Connection) -> dict[str, str]:
    """Return the latest collected date for each source in the index."""
    rows = conn.execute(_SOURCE_IMPORTS_SQL).fetchall()
    return {row["source"]: row["last_import"] for row in rows}


def build_page_data(
    records: list[DiscoveryRecord],
    *,
    db_path: Path | str,
    generated_at: str | datetime | None = None,
    source_last_import: Mapping[str, str] | None = None,
    writable: bool = False,
    collection: Mapping[str, CollectionRecord] | None = None,
    collection_budget: CollectionBudget | None = None,
    collection_enabled: bool = False,
    wayback_enabled: bool = True,
    live_enabled: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the row and metadata payloads embedded in the page.

    ``rows`` contains every record so the page can render the blocked-domains
    view, while the metadata totals and source counts describe the main view
    only. Blocked records are marked with ``blocked: true``. ``writable`` tells
    the page that the manual-pass API is available. ``collection`` adds the
    latest Ahrefs run for each domain and ``collection_budget`` reports the
    current monthly spend.
    """
    collections = dict(collection or {})
    rows: list[dict[str, Any]] = []
    for record in records:
        row = record.as_row()
        stored = collections.get(record.domain)
        wayback_ok = not wayback_enabled or bool(record.wayback_check_pass)
        live_ok = not live_enabled or bool(record.live_check_pass)
        eligible = (
            bool((record.dr_check_pass and live_ok and wayback_ok) or stored is not None)
            and not record.blocked
        )
        info = (
            stored.as_row(eligible=eligible)
            if stored is not None
            else {
                "eligible": eligible,
                "status": "queued",
                "run_id": "",
                "fetched_at": "",
                "tier": "",
                "units": 0,
                "requests_ok": 0,
                "requests_total": 0,
                "error": None,
                "coverage_mode": "",
                "live_links": None,
                "all_time_links": None,
                "live_refdomains": None,
                "all_time_refdomains": None,
                "rows_unique": None,
                "top_anchors": None,
                "lexicon_complete": None,
            }
        )
        row["collection"] = info
        row["collection_status"] = info["status"]
        row["collection_at"] = info["fetched_at"]
        row["coverage"] = info["coverage_mode"]
        row["live_links"] = info["live_links"]
        row["live_refdomains"] = info["live_refdomains"]
        row["collection_units"] = info["units"]
        row["requests_ok"] = info["requests_ok"]
        row["requests_total"] = info["requests_total"]
        if not row["blocked"]:
            if row.get("evaluation"):
                row["reached"] = 7
                verdict = row["evaluation"].get("verdict") or "unknown"
                row["progress_label"] = f"Evaluated: {verdict}"
            elif info["status"] == "collected":
                row["reached"] = max(int(row.get("reached") or 0), 6)
                row["progress_label"] = "Data collected"
        rows.append(row)

    visible = [record for record in records if not record.blocked]
    last_imports = dict(source_last_import or {})

    counts: dict[str, int] = {}
    for record in visible:
        counts[record.source] = counts.get(record.source, 0) + 1

    known = [source for source in SOURCE_ORDER if source in counts]
    extra = sorted(source for source in counts if source not in SOURCE_ORDER)
    sources = []
    for source in (*known, *extra):
        label, short, icon = source_display(source)
        fallback = max(
            (record.collected_date for record in visible if record.source == source),
            default=None,
        )
        sources.append(
            {
                "slug": source,
                "label": label,
                "short": short,
                "icon": icon,
                "count": counts[source],
                "last_import": last_imports.get(source) or fallback,
            }
        )

    prefilter_version = load_prefilter_config().version
    prefilter_queue = sum(
        1
        for record in visible
        if record.niche not in (None, UNKNOWN_NICHE)
        and not record.prefilter_manual_pass
        and (
            record.prefilter_pass is None
            or (
                record.prefilter_config_version is not None
                and record.prefilter_config_version != prefilter_version
            )
        )
    )
    prefilter_counts = {
        "passed": sum(1 for record in visible if record.prefilter_pass is True),
        "failed": sum(1 for record in visible if record.prefilter_pass is False),
        "pending": sum(1 for record in visible if record.prefilter_pass is None),
        "manual": sum(1 for record in visible if record.prefilter_manual_pass),
        "queue": prefilter_queue,
    }
    eligible_keywords = [record for record in visible if record.prefilter_pass is True]
    keyword_counts = {
        "reusable": sum(
            1
            for record in eligible_keywords
            if record.keyword_check_status == "reusable" or record.keyword_check_pass is True
        ),
        "not_reusable": sum(
            1
            for record in eligible_keywords
            if record.keyword_check_status == "not_reusable"
            or (record.keyword_check_status is None and record.keyword_check_pass is False)
        ),
        "processing": sum(
            1 for record in eligible_keywords if record.keyword_check_status == "processing"
        ),
        "pending": sum(
            1
            for record in eligible_keywords
            if record.keyword_check_status is None and record.keyword_check_pass is None
        ),
    }
    collection_eligible = [
        record
        for record in visible
        if (
            record.dr_check_pass is True
            and (not live_enabled or record.live_check_pass is True)
            and (not wayback_enabled or record.wayback_check_pass is True)
            and record.keyword_check_pass is True
            and record.prefilter_pass is True
        )
        or record.domain in collections
    ]
    collected = sum(
        1
        for record in collection_eligible
        if record.domain in collections and collections[record.domain].error is None
    )
    collection_failed = sum(
        1
        for record in collection_eligible
        if record.domain in collections and collections[record.domain].error is not None
    )
    collection_counts = {
        "eligible": len(collection_eligible),
        "collected": collected,
        "failed": collection_failed,
        "queued": max(0, len(collection_eligible) - collected - collection_failed),
    }
    dr_eligible = [
        record
        for record in visible
        if record.prefilter_pass is True and record.keyword_check_pass is True
    ]
    dr_counts = {
        "passed": sum(
            1
            for record in dr_eligible
            if record.dr_check_status == "passed"
            or (record.dr_check_status is None and record.dr_check_pass is True)
        ),
        "failed": sum(
            1
            for record in dr_eligible
            if record.dr_check_status == "failed"
            or (record.dr_check_status is None and record.dr_check_pass is False)
        ),
        "processing": sum(1 for record in dr_eligible if record.dr_check_status == "processing"),
        "pending": sum(
            1
            for record in dr_eligible
            if record.dr_check_status is None and record.dr_check_pass is None
        ),
    }
    live_eligible = [
        record
        for record in visible
        if record.prefilter_pass is True
        and record.keyword_check_pass is True
        and record.dr_check_pass is True
    ]
    live_counts = {
        "passed": sum(
            1
            for record in live_eligible
            if record.live_check_status == "passed"
            or (record.live_check_status is None and record.live_check_pass is True)
        ),
        "failed": sum(
            1
            for record in live_eligible
            if record.live_check_status == "failed"
            or (record.live_check_status is None and record.live_check_pass is False)
        ),
        "processing": sum(
            1 for record in live_eligible if record.live_check_status == "processing"
        ),
        "pending": sum(
            1
            for record in live_eligible
            if record.live_check_status is None and record.live_check_pass is None
        ),
    }
    wayback_eligible = [
        record
        for record in visible
        if record.prefilter_pass is True
        and record.keyword_check_pass is True
        and record.dr_check_pass is True
        and (not live_enabled or record.live_check_pass is True)
    ]
    wayback_counts = {
        "passed": sum(
            1
            for record in wayback_eligible
            if record.wayback_check_status == "passed"
            or (record.wayback_check_status is None and record.wayback_check_pass is True)
        ),
        "failed": sum(
            1
            for record in wayback_eligible
            if (
                record.wayback_check_status == "failed"
                or (record.wayback_check_status is None and record.wayback_check_pass is False)
            )
            and (record.wayback_score or 0) > 0
        ),
        "connection_failed": sum(
            1
            for record in wayback_eligible
            if (
                record.wayback_check_status == "failed"
                or (record.wayback_check_status is None and record.wayback_check_pass is False)
            )
            and not (record.wayback_score or 0) > 0
        ),
        "processing": sum(
            1 for record in wayback_eligible if record.wayback_check_status == "processing"
        ),
        "pending": sum(
            1
            for record in wayback_eligible
            if record.wayback_check_status is None and record.wayback_check_pass is None
        ),
    }
    evaluated = [record for record in visible if record.evaluation_verdict]
    evaluation_counts = {
        "passed": sum(1 for record in evaluated if record.evaluation_verdict != "Reject"),
        "failed": sum(1 for record in evaluated if record.evaluation_verdict == "Reject"),
        "total": len(evaluated),
        "pending": sum(
            1
            for record in visible
            if record.evaluation_verdict is None
            and record.domain in collections
            and collections[record.domain].error is None
        ),
    }
    budget_meta = (
        collection_budget.as_meta()
        if collection_budget is not None
        else {
            "monthly_cap": 0,
            "reserve": 0,
            "allowed": 0,
            "spent": 0,
            "remaining": 0,
        }
    )

    meta = {
        "generated_at": _as_iso(generated_at),
        "db_path": str(db_path),
        "total": len(visible),
        "blocked_total": len(records) - len(visible),
        "sources": sources,
        "niches": [
            {"value": PENDING_NICHE, "label": "Pending classification"},
            *({"value": key, "label": label} for key, label in NICHE_LABELS.items()),
        ],
        "prefilter": prefilter_counts,
        "keyword_check": keyword_counts,
        "dr_check": dr_counts,
        "live_check": {**live_counts, "enabled": live_enabled},
        "wayback_check": wayback_counts,
        "data_collection": {**collection_counts, "enabled": collection_enabled, **budget_meta},
        "evaluation": evaluation_counts,
        "writable": writable,
    }
    return rows, meta


def render_shortlist_html(
    records: list[DiscoveryRecord],
    *,
    db_path: Path | str,
    generated_at: str | datetime | None = None,
    source_last_import: Mapping[str, str] | None = None,
    writable: bool = False,
    collection: Mapping[str, CollectionRecord] | None = None,
    collection_budget: CollectionBudget | None = None,
    collection_enabled: bool = False,
    wayback_enabled: bool = True,
    live_enabled: bool = True,
    details: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render the self-contained discovery page for ``records``.

    ``details`` maps a domain to the payload shown when its data-collection row
    is opened. It is embedded in the page so the generated file works offline.
    """
    rows, meta = build_page_data(
        records,
        db_path=db_path,
        generated_at=generated_at,
        source_last_import=source_last_import,
        writable=writable,
        collection=collection,
        collection_budget=collection_budget,
        collection_enabled=collection_enabled,
        wayback_enabled=wayback_enabled,
        live_enabled=live_enabled,
    )
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__ROWS_DATA__", _json_for_script(rows))
        .replace("__META_DATA__", _json_for_script(meta))
        .replace("__DETAILS_DATA__", _json_for_script(dict(details or {})))
    )


def write_shortlist_html(
    output_path: Path,
    records: list[DiscoveryRecord],
    *,
    db_path: Path | str,
    generated_at: str | datetime | None = None,
    source_last_import: Mapping[str, str] | None = None,
    writable: bool = False,
    collection: Mapping[str, CollectionRecord] | None = None,
    collection_budget: CollectionBudget | None = None,
    collection_enabled: bool = False,
    wayback_enabled: bool = True,
    live_enabled: bool = True,
    details: Mapping[str, Mapping[str, Any]] | None = None,
) -> Path:
    """Render the page and write it to ``output_path``, creating parents."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_shortlist_html(
            records,
            db_path=db_path,
            generated_at=generated_at,
            source_last_import=source_last_import,
            writable=writable,
            collection=collection,
            collection_budget=collection_budget,
            collection_enabled=collection_enabled,
            wayback_enabled=wayback_enabled,
            live_enabled=live_enabled,
            details=details,
        ),
        encoding="utf-8",
    )
    return path


def _as_iso(value: str | datetime | None) -> str:
    """Return a UTC ISO-8601 timestamp, defaulting to now."""
    if value is None:
        return datetime.now(UTC).isoformat(timespec="seconds")
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat(timespec="seconds")
    return value


def _json_for_script(value: Any) -> str:
    """Serialise JSON for a ``<script>`` block without allowing tag escapes."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
