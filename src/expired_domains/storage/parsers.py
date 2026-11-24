"""Payload → model parsing and coverage computation."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import tldextract

from expired_domains.ahrefs.lexicon import Lexicon, classify
from expired_domains.models import (
    AnchorRow,
    BacklinkRow,
    Coverage,
    HistoryRow,
    StatsRow,
    Units,
)

STATS_SNAPSHOTS = ("live", "all_time")
COVERAGE_HISTORY_MONTHS = 48

_DEFAULT_PORTS = {"http": 80, "https": 443}
_WHITESPACE = re.compile(r"\s+")
_SUFFIXES = tldextract.TLDExtract(suffix_list_urls=())


def parse_stats(payload: dict[str, Any]) -> list[StatsRow]:
    """Map ``metrics`` to the two ``stats`` snapshot rows from §3.5."""
    metrics = payload.get("metrics") or {}
    return [
        StatsRow(
            snapshot="live",
            links=metrics.get("live"),
            refdomains=metrics.get("live_refdomains"),
        ),
        StatsRow(
            snapshot="all_time",
            links=metrics.get("all_time"),
            refdomains=metrics.get("all_time_refdomains"),
        ),
    ]


def parse_refdomains_history(payload: dict[str, Any]) -> list[HistoryRow]:
    """Map every ``refdomains`` entry to a ``refdomains`` history row."""
    entries = payload.get("refdomains") or []
    return [
        HistoryRow(
            series="refdomains",
            date=_normalise_date(entry["date"]),
            value=entry.get("refdomains"),
        )
        for entry in entries
    ]


def parse_metrics_history(payload: dict[str, Any]) -> list[HistoryRow]:
    """Map every ``metrics`` entry to an ``org_traffic`` history row."""
    entries = payload.get("metrics") or []
    return [
        HistoryRow(
            series="org_traffic",
            date=_normalise_date(entry["date"]),
            value=entry.get("org_traffic"),
        )
        for entry in entries
    ]


def normalize_url(value: str | None) -> str | None:
    """Lowercase the scheme/host, strip default ports, and keep path casing."""
    if value is None:
        return None

    try:
        parts = urlsplit(value)
        host = parts.hostname
        if not parts.scheme or not host:
            return value

        scheme = parts.scheme.lower()
        netloc = host.lower()
        port = parts.port
        if port is not None and port != _DEFAULT_PORTS.get(scheme):
            netloc = f"{netloc}:{port}"
        return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return value


def normalize_anchor(value: str | None) -> str | None:
    """Unescape, collapse whitespace, trim, and lowercase an anchor."""
    if value is None:
        return None
    return _WHITESPACE.sub(" ", html.unescape(value)).strip().lower()


def root_domain(host: str | None) -> str | None:
    """Return the registrable domain for ``host`` using the bundled PSL snapshot."""
    if not host or any(character.isspace() for character in host):
        return None
    extracted = _SUFFIXES(host)
    registrable = getattr(extracted, "top_domain_under_public_suffix", None)
    return registrable or extracted.registered_domain or None


def link_hash(
    url_from: str | None,
    url_to: str | None,
    anchor_norm: str | None,
    first_seen_link: str | None,
) -> str:
    """Return the §3.7 digest for one normalised backlink identity."""
    payload = link_hash_input(url_from, url_to, anchor_norm, first_seen_link)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_backlinks(payload: dict[str, Any], source_query: str) -> list[BacklinkRow]:
    """Map ``backlinks[]`` to rows, normalising URLs and preserving raw values."""
    rows: list[BacklinkRow] = []
    for raw in payload.get("backlinks") or []:
        raw_url_from = raw.get("url_from")
        raw_url_to = raw.get("url_to")
        url_from = normalize_url(raw_url_from)
        url_to = normalize_url(raw_url_to)
        anchor_raw = raw.get("anchor")
        anchor_norm = normalize_anchor(anchor_raw)
        domain_from = _url_host(url_from)
        first_seen_link = raw.get("first_seen_link")

        rows.append(
            BacklinkRow(
                link_hash=link_hash(raw_url_from, raw_url_to, anchor_norm, first_seen_link),
                source_queries=[source_query],
                url_from=url_from,
                url_to=url_to,
                domain_from=domain_from,
                root_domain_from=root_domain(domain_from),
                anchor_raw=anchor_raw,
                anchor_norm=anchor_norm,
                is_dofollow=raw.get("is_dofollow"),
                domain_rating=raw.get("domain_rating_source"),
                first_seen_link=first_seen_link,
                era=None,
                raw_json=json.dumps(raw, sort_keys=True, separators=(",", ":")),
            )
        )
    return rows


def merge_backlink_rows(rows: Sequence[BacklinkRow]) -> list[BacklinkRow]:
    """Collapse rows sharing a ``link_hash`` and union their source queries."""
    merged: dict[str, BacklinkRow] = {}
    for row in rows:
        existing = merged.get(row.link_hash)
        if existing is None:
            merged[row.link_hash] = row
            continue
        sources = sorted({*existing.source_queries, *row.source_queries})
        merged[row.link_hash] = replace(existing, source_queries=sources)
    return list(merged.values())


def parse_anchors(
    payload: dict[str, Any],
    source_query: str,
    lexicon: Lexicon,
) -> list[AnchorRow]:
    """Map ``anchors[]`` to rows, classifying locally and never dropping any."""
    top_anchors = source_query == "top_anchors"
    rows: list[AnchorRow] = []
    for raw in payload.get("anchors") or []:
        anchor_raw = raw.get("anchor")
        anchor_norm = normalize_anchor(anchor_raw)
        terms, categories = classify(anchor_norm, lexicon)
        rows.append(
            AnchorRow(
                source_query=source_query,
                anchor_raw=anchor_raw,
                anchor_norm=anchor_norm,
                links_to_target=raw.get("links_to_target"),
                dofollow_links=raw.get("dofollow_links"),
                refdomains=None if top_anchors else raw.get("refdomains"),
                first_seen=raw.get("first_seen"),
                last_seen=None if top_anchors else raw.get("last_seen"),
                lexicon_terms=terms,
                lexicon_categories=categories,
                raw_json=json.dumps(raw, sort_keys=True, separators=(",", ":")),
            )
        )
    return rows


def extract_units(headers: Mapping[str, str]) -> Units:
    """Read actual and billed API units, treating missing/invalid values as zero."""
    return Units(
        actual=_integer_header(headers, "x-api-units-cost-total-actual"),
        billed=_integer_header(headers, "x-api-units-cost-total"),
    )


def compute_coverage(
    stats_rows: Sequence[StatsRow],
    top_dr_rows: Sequence[BacklinkRow] = (),
    latest_rows: Sequence[BacklinkRow] = (),
    anchor_rows: Sequence[AnchorRow] = (),
) -> Coverage:
    """Return the §3.8 coverage object for this slice."""
    stats: dict[str, int | None] = {
        "live": None,
        "all_time": None,
        "live_refdomains": None,
        "all_time_refdomains": None,
    }
    for row in stats_rows:
        if row.snapshot == "live":
            stats["live"] = row.links
            stats["live_refdomains"] = row.refdomains
        elif row.snapshot == "all_time":
            stats["all_time"] = row.links
            stats["all_time_refdomains"] = row.refdomains

    unique = len({row.link_hash for row in (*top_dr_rows, *latest_rows)})
    top_anchors = sum(1 for row in anchor_rows if row.source_query == "top_anchors")
    lexicon_anchors = sum(
        1 for row in anchor_rows if row.source_query == "lexicon_anchors"
    )
    return Coverage(
        mode="top_n",
        stats=stats,
        rows={"top_dr": len(top_dr_rows), "latest": len(latest_rows), "unique": unique},
        top_anchors=top_anchors,
        lexicon_complete=lexicon_anchors < 250,
        history_months=COVERAGE_HISTORY_MONTHS,
        drop_date_source="unknown",
        prefilter=None,
    )


def link_hash_input(
    url_from: str | None,
    url_to: str | None,
    anchor_norm: str | None,
    first_seen_link: str | None,
) -> str:
    """Assemble the canonical hash input shared by parsing and tests."""
    return "\n".join(
        (
            "ahrefs",
            normalize_url(url_from) or "",
            normalize_url(url_to) or "",
            anchor_norm or "",
            first_seen_link or "",
        )
    )


def _url_host(value: str | None) -> str | None:
    """Return a lowercased hostname, or ``None`` when the URL cannot be parsed."""
    if value is None:
        return None
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def _normalise_date(value: str) -> str:
    """Return the ``YYYY-MM-DD`` prefix of an ISO-8601 date string."""
    return value[:10]


def _integer_header(headers: Mapping[str, str], name: str) -> int:
    """Return a case-insensitive header as an int, or zero when unusable."""
    value: str | None = None
    for key, candidate in headers.items():
        if key.lower() == name:
            value = candidate
            break
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
