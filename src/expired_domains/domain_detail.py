"""Load the evidence behind one domain for the data-collection detail modal.

The discovery page only needs a compact summary per domain. When a collected
row is opened, the modal shows the stored Ahrefs run: stats, history, anchors,
backlinks, and the request audit trail. This module assembles that payload from
the same local index, so the page stays a read-only view.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

DETAIL_BACKLINK_LIMIT = 100
DETAIL_ANCHOR_LIMIT = 50

# Match the discovery view: the latest observation wins, then the known
# marketplace sources break same-day ties.
_LATEST_OBSERVATION_SQL = """
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
    WHERE domain = ?
)
SELECT source, collected_date, metrics_json
FROM ranked
WHERE row_number = 1
"""

# A successful run always wins over a later failed retry, matching the row
# summary in the data-collection tab.
_LATEST_RUN_SQL = """
SELECT *
FROM runs
WHERE domain = ?
ORDER BY
    CASE WHEN error IS NULL THEN 0 ELSE 1 END,
    fetched_at DESC,
    run_id DESC
LIMIT 1
"""

_REQUESTS_SQL = """
SELECT query_id, page, endpoint, row_count, units_actual, status, error
FROM requests
WHERE run_id = ?
ORDER BY query_id, page
"""

_STATS_SQL = """
SELECT snapshot, links, refdomains
FROM stats
WHERE run_id = ?
ORDER BY snapshot
"""

_HISTORY_SQL = """
SELECT series, date, value
FROM history
WHERE run_id = ?
ORDER BY series, date
"""

_TOP_ANCHORS_SQL = """
SELECT
    source_query,
    anchor_raw,
    links_to_target,
    dofollow_links,
    refdomains,
    lexicon_terms,
    lexicon_categories
FROM anchors
WHERE run_id = ? AND source_query = 'top_anchors'
ORDER BY links_to_target DESC, anchor_raw
LIMIT ?
"""

_LEXICON_ANCHORS_SQL = """
SELECT
    source_query,
    anchor_raw,
    links_to_target,
    dofollow_links,
    refdomains,
    lexicon_terms,
    lexicon_categories
FROM anchors
WHERE run_id = ? AND source_query = 'lexicon_anchors'
ORDER BY links_to_target DESC, anchor_raw
LIMIT ?
"""

_BACKLINK_SUMMARY_SQL = """
SELECT
    COUNT(*) AS total,
    COALESCE(SUM(CASE WHEN is_dofollow = 1 THEN 1 ELSE 0 END), 0) AS dofollow,
    COALESCE(SUM(CASE WHEN is_dofollow = 0 THEN 1 ELSE 0 END), 0) AS nofollow,
    COUNT(DISTINCT root_domain_from) AS unique_root_domains,
    MAX(domain_rating) AS max_domain_rating,
    AVG(domain_rating) AS average_domain_rating
FROM backlinks
WHERE run_id = ?
"""

_BACKLINKS_SQL = """
SELECT
    source_queries,
    url_from,
    url_to,
    domain_from,
    root_domain_from,
    anchor_raw,
    is_dofollow,
    domain_rating,
    first_seen_link,
    era
FROM backlinks
WHERE run_id = ?
ORDER BY
    (domain_rating IS NULL),
    domain_rating DESC,
    first_seen_link DESC,
    url_from
LIMIT ?
"""

_WAYBACK_SQL = """
SELECT
    wayback_check_status,
    wayback_check_pass,
    wayback_score,
    wayback_check_reason,
    wayback_check_source,
    wayback_check_details_json,
    wayback_first_capture,
    wayback_last_capture,
    wayback_age_years,
    wayback_capture_count,
    wayback_check_checked_at
FROM domains
WHERE domain = ?
"""

_LIVE_SQL = """
SELECT
    live_check_status,
    live_check_pass,
    live_check_class,
    live_check_reason,
    live_check_details_json,
    live_check_attempts,
    live_check_checked_at,
    live_check_manual_pass,
    live_check_final_host,
    live_check_http_status,
    live_check_config_version
FROM domains
WHERE domain = ?
"""


def load_domain_details(
    conn: sqlite3.Connection,
    domains: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Return the detail payload for each domain that exists in the index."""
    details: dict[str, dict[str, Any]] = {}
    for domain in dict.fromkeys(domains):
        detail = load_domain_detail(conn, domain)
        if detail is not None:
            details[domain] = detail
    return details


def load_domain_detail(
    conn: sqlite3.Connection,
    domain: str,
) -> dict[str, Any] | None:
    """Return the stored observation and latest Ahrefs run for ``domain``.

    The result is JSON-friendly. A domain without a completed run still
    returns its marketplace observation so the modal can explain that no paid
    collection has happened yet.
    """
    observation = conn.execute(_LATEST_OBSERVATION_SQL, (domain,)).fetchone()
    run = conn.execute(_LATEST_RUN_SQL, (domain,)).fetchone()
    if observation is None and run is None:
        return None

    detail: dict[str, Any] = {
        "domain": domain,
        "observation": (
            {
                "source": observation["source"],
                "collected_date": observation["collected_date"],
                "metrics": _json_object(observation["metrics_json"]),
            }
            if observation is not None
            else None
        ),
        "run": None,
        "requests": [],
        "stats": {},
        "history": [],
        "anchors": [],
        "backlinks": {
            "total": 0,
            "dofollow": 0,
            "nofollow": 0,
            "unique_root_domains": 0,
            "max_domain_rating": None,
            "average_domain_rating": None,
            "items": [],
        },
        "wayback": _wayback_detail(conn, domain),
        "live": _live_detail(conn, domain),
    }
    if run is None:
        return detail

    run_id = str(run["run_id"])
    detail["run"] = {
        "run_id": run_id,
        "fetched_at": run["fetched_at"],
        "mode": run["mode"],
        "tier": run["tier"],
        "units": int(run["units_actual"] or 0),
        "error": _error_text(run["error"]),
        "coverage": _json_object(run["coverage_json"]),
        "config": _json_object(run["config_json"]),
    }
    requests = conn.execute(_REQUESTS_SQL, (run_id,)).fetchall()
    detail["requests"] = [
        {
            "query_id": row["query_id"],
            "page": row["page"],
            "endpoint": row["endpoint"],
            "row_count": row["row_count"],
            "units": int(row["units_actual"] or 0),
            "status": row["status"],
            "error": row["error"],
        }
        for row in requests
    ]
    detail["run"]["requests_ok"] = sum(
        1
        for row in requests
        if row["error"] is None and row["status"] is not None and 200 <= int(row["status"]) < 300
    )
    detail["run"]["requests_total"] = len(requests)
    detail["stats"] = {
        row["snapshot"]: {"links": row["links"], "refdomains": row["refdomains"]}
        for row in conn.execute(_STATS_SQL, (run_id,))
    }
    if not detail["stats"]:
        detail["stats"] = _coverage_stats(detail["run"]["coverage"])
    detail["history"] = _history(conn, run_id)
    detail["anchors"] = _anchors(conn, run_id)
    detail["backlinks"] = _backlinks(conn, run_id)
    return detail


def _live_detail(conn: sqlite3.Connection, domain: str) -> dict[str, Any] | None:
    """Return the stored Live check verdict and evidence for the modal."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(domains)")}
    if "live_check_status" not in columns:
        return None
    row = conn.execute(_LIVE_SQL, (domain,)).fetchone()
    if row is None or row["live_check_status"] is None:
        return None
    details = _json_object(row["live_check_details_json"])
    http = details.get("http") if isinstance(details.get("http"), dict) else {}
    return {
        "status": row["live_check_status"],
        "passed": _bool_or_none(row["live_check_pass"]),
        "class": row["live_check_class"] or "",
        "reason": row["live_check_reason"] or "",
        "attempts": row["live_check_attempts"],
        "checked_at": row["live_check_checked_at"],
        "manual": bool(row["live_check_manual_pass"]),
        "final_host": row["live_check_final_host"] or "",
        "http_status": row["live_check_http_status"],
        "config_version": row["live_check_config_version"] or "",
        "dns": details.get("dns") if isinstance(details.get("dns"), dict) else {},
        "http": http,
        "redirects": http.get("redirects") if isinstance(http.get("redirects"), list) else [],
        "title": details.get("title") or http.get("title") or "",
        "excerpt": details.get("excerpt") or http.get("excerpt") or "",
        "toxic_terms": details.get("toxic_terms")
        if isinstance(details.get("toxic_terms"), list)
        else [],
        "toxic_categories": (
            details.get("toxic_categories")
            if isinstance(details.get("toxic_categories"), list)
            else []
        ),
        "parking": bool(details.get("parking")),
        "neutral": bool(details.get("neutral")),
    }


def _wayback_detail(conn: sqlite3.Connection, domain: str) -> dict[str, Any] | None:
    """Return the stored Wayback verdict and snapshot evidence for the modal."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(domains)")}
    if "wayback_check_status" not in columns:
        return None
    row = conn.execute(_WAYBACK_SQL, (domain,)).fetchone()
    if row is None or row["wayback_check_status"] is None:
        return None
    details = _json_object(row["wayback_check_details_json"])
    return {
        "status": row["wayback_check_status"],
        "passed": _bool_or_none(row["wayback_check_pass"]),
        "score": row["wayback_score"],
        "reason": row["wayback_check_reason"] or "",
        "source": row["wayback_check_source"] or "",
        "first_capture": row["wayback_first_capture"],
        "last_capture": row["wayback_last_capture"],
        "age_years": row["wayback_age_years"],
        "capture_count": row["wayback_capture_count"],
        "checked_at": row["wayback_check_checked_at"],
        "checks": details.get("checks") if isinstance(details.get("checks"), list) else [],
        "snapshots": (
            details.get("snapshots") if isinstance(details.get("snapshots"), list) else []
        ),
        "llm": details.get("llm"),
    }


def _coverage_stats(coverage: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Reconstruct stats snapshots from a coverage blob when rows are absent."""
    raw = coverage.get("stats")
    if not isinstance(raw, dict):
        return {}
    stats: dict[str, dict[str, Any]] = {}
    for snapshot, links_key, refdomains_key in (
        ("live", "live", "live_refdomains"),
        ("all_time", "all_time", "all_time_refdomains"),
    ):
        if raw.get(links_key) is None and raw.get(refdomains_key) is None:
            continue
        stats[snapshot] = {
            "links": _int_or_none(raw.get(links_key)),
            "refdomains": _int_or_none(raw.get(refdomains_key)),
        }
    return stats


def _history(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Group stored history rows by series, in date order."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(_HISTORY_SQL, (run_id,)):
        grouped.setdefault(str(row["series"]), []).append(
            {"date": row["date"], "value": row["value"]}
        )
    return [
        {"series": series, "points": points} for series, points in sorted(grouped.items()) if points
    ]


def _anchors(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Return the top anchors, falling back to the lexicon slice."""
    rows = conn.execute(_TOP_ANCHORS_SQL, (run_id, DETAIL_ANCHOR_LIMIT)).fetchall()
    if not rows:
        rows = conn.execute(_LEXICON_ANCHORS_SQL, (run_id, DETAIL_ANCHOR_LIMIT)).fetchall()
    return [
        {
            "source": row["source_query"],
            "anchor": row["anchor_raw"] or "",
            "links": row["links_to_target"],
            "dofollow": row["dofollow_links"],
            "refdomains": row["refdomains"],
            "terms": _json_list(row["lexicon_terms"]),
            "categories": _json_list(row["lexicon_categories"]),
        }
        for row in rows
    ]


def _backlinks(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Return backlink counts plus the strongest stored links."""
    summary = conn.execute(_BACKLINK_SUMMARY_SQL, (run_id,)).fetchone()
    items = []
    for row in conn.execute(_BACKLINKS_SQL, (run_id, DETAIL_BACKLINK_LIMIT)):
        items.append(
            {
                "source_queries": _json_list(row["source_queries"]),
                "url_from": row["url_from"],
                "url_to": row["url_to"],
                "domain_from": row["domain_from"],
                "root_domain_from": row["root_domain_from"],
                "anchor": row["anchor_raw"] or "",
                "dofollow": _bool_or_none(row["is_dofollow"]),
                "domain_rating": row["domain_rating"],
                "first_seen": row["first_seen_link"],
                "era": row["era"],
            }
        )
    return {
        "total": int(summary["total"] or 0) if summary is not None else 0,
        "dofollow": int(summary["dofollow"] or 0) if summary is not None else 0,
        "nofollow": int(summary["nofollow"] or 0) if summary is not None else 0,
        "unique_root_domains": (
            int(summary["unique_root_domains"] or 0) if summary is not None else 0
        ),
        "max_domain_rating": (summary["max_domain_rating"] if summary is not None else None),
        "average_domain_rating": (
            summary["average_domain_rating"] if summary is not None else None
        ),
        "items": items,
    }


def _json_object(value: str | None) -> dict[str, Any]:
    """Decode a stored JSON object, treating bad values as empty."""
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json_list(value: str | None) -> list[str]:
    """Decode a stored JSON string list, tolerating bad values."""
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _bool_or_none(value: Any) -> bool | None:
    """Return a bool for a stored SQLite flag, preserving an unknown value."""
    if value is None:
        return None
    return bool(value)


def _int_or_none(value: Any) -> int | None:
    """Return an int for a numeric value, or ``None`` when absent/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
