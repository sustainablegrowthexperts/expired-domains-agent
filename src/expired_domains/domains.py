"""Canonical domain values and candidate CSV ingestion.

Provider exports differ wildly in their columns, so only three things are
promoted to first-class fields: the canonical domain, the source that listed
it, and the date the export was collected. Every observation also carries two
JSON blobs:

* ``metrics`` uses one canonical key set for every source, so consumers can
  compare sources without branching; absent values are ``None``.
* ``raw`` preserves every remaining column from the export verbatim.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tldextract

from expired_domains.models import DomainObservation

SOURCE_EXPIREDDOMAINS = "expireddomains"
SOURCE_GODADDY_AUCTIONS = "godaddy_auctions"
SOURCE_SPAMZILLA = "spamzilla"

# Keys are lowercase, whitespace-collapsed, dash/underscore-free labels.
SOURCE_ALIASES = {
    "expireddomains": SOURCE_EXPIREDDOMAINS,
    "expired domains": SOURCE_EXPIREDDOMAINS,
    "expireddomains.net": SOURCE_EXPIREDDOMAINS,
    "expired domains.net": SOURCE_EXPIREDDOMAINS,
    "godaddy": SOURCE_GODADDY_AUCTIONS,
    "godaddy auctions": SOURCE_GODADDY_AUCTIONS,
    "gd auctions": SOURCE_GODADDY_AUCTIONS,
    "spamzilla": SOURCE_SPAMZILLA,
}

# Header sets that identify a known export without relying on its filename.
_SOURCE_SIGNATURES = {
    SOURCE_EXPIREDDOMAINS: {"domain", "end date", "bl"},
    SOURCE_GODADDY_AUCTIONS: {"domain name", "auction end time", "itemid"},
    SOURCE_SPAMZILLA: {"domain", "sz score", "date added"},
}

_DOMAIN_HEADERS = {
    SOURCE_EXPIREDDOMAINS: "domain",
    SOURCE_GODADDY_AUCTIONS: "domain name",
    SOURCE_SPAMZILLA: "domain",
}

# The canonical metric vocabulary. Every parsed observation has every key;
# a source that does not provide a metric leaves it as None (or []).
METRIC_DEFAULTS: dict[str, Any] = {
    # Pricing and auction timing.
    "price": None,
    "estimated_value": None,
    "bid_count": None,
    "auction_end_at": None,
    "sale_type": None,
    "listed_at": None,
    "expires_at": None,
    "drop_date": None,
    # Age and archive history.
    "domain_age_years": None,
    "archive_birth_year": None,
    "archive_captures": None,
    "registrations": None,
    "dmoz_listed": None,
    # Link and authority metrics.
    "backlinks": None,
    "referring_domains": None,
    "majestic_trust_flow": None,
    "majestic_citation_flow": None,
    "majestic_backlinks": None,
    "majestic_referring_domains": None,
    "moz_domain_authority": None,
    "moz_page_authority": None,
    "semrush_authority_score": None,
    "semrush_backlinks": None,
    "semrush_referring_domains": None,
    "semrush_indexed_pages": None,
    "semrush_organic_traffic": None,
    "semrush_organic_keywords": None,
    "semrush_rank": None,
    "semrush_search_volume": None,
    "semrush_cpc": None,
    "traffic": None,
    "top_referring_domains": [],
    # Content and site signals.
    "language": None,
    "site_language": None,
    "majestic_language": None,
    "majestic_topics": [],
    "google_indexed": None,
    "parked": None,
    "redirected": None,
    "has_archive_history": None,
    "drop_count": None,
    "spamzilla_score": None,
    "outbound_links_internal": None,
    "outbound_links_external": None,
    "outbound_domains_external": None,
    # Marketplace cross-reference counts.
    "exact_match_tlds": None,
    "keyword_registrations": None,
    "developed_tlds": None,
}

_SUFFIXES = tldextract.TLDExtract(suffix_list_urls=())

_FALSE_VALUES = {"0", "false", "n", "no"}
_TRUE_VALUES = {"1", "true", "y", "yes"}
_NULL_VALUES = {"", "-", "--", "n/a", "na", "none", "unknown"}


@dataclass(frozen=True)
class ParsedDomainCsv:
    """The source and observations read from one CSV export."""

    source: str
    observations: list[DomainObservation]


def empty_metrics() -> dict[str, Any]:
    """Return a fresh canonical metric object with every key present."""
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in METRIC_DEFAULTS.items()
    }


def normalize_domain(value: str | None) -> str:
    """Return the lowercase registrable domain, or an empty string if invalid.

    Schemes, credentials, ports, paths, query strings, fragments, a leading
    ``www.``, and surrounding dots are removed before the public-suffix lookup.
    """
    if value is None:
        return ""

    text = value.strip().lower()
    if not text:
        return ""

    if "://" in text:
        try:
            text = urlsplit(text).hostname or ""
        except ValueError:
            return ""
    else:
        text = text.split("/", 1)[0]
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = text.rsplit("@", 1)[-1]
    if text.startswith("["):
        return ""
    text = text.split(":", 1)[0].strip(".")
    if text.startswith("www."):
        text = text[4:]
    if not text or "." not in text or any(character.isspace() for character in text):
        return ""

    extracted = _SUFFIXES(text)
    return getattr(extracted, "top_domain_under_public_suffix", None) or text


def normalize_source(value: str) -> str:
    """Return a stable source slug, expanding known provider aliases.

    Unknown labels are slugified rather than rejected so a new marketplace can
    be stored without a code change.
    """
    text = " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())
    if not text:
        raise ValueError("domain source is required")

    alias = SOURCE_ALIASES.get(text)
    if alias is not None:
        return alias
    return text.replace(" ", "_").replace(".", "_")


def normalize_collected_date(value: str | date | None) -> str:
    """Return an ISO ``YYYY-MM-DD`` collection date, defaulting to today in UTC."""
    if value is None:
        return datetime.now(UTC).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid collection date: {value!r}") from exc


def detect_source(headers: Iterable[str]) -> str:
    """Identify a known export from its CSV header names."""
    present = {header.strip().lower() for header in headers if header}
    matches = [source for source, signature in _SOURCE_SIGNATURES.items() if signature <= present]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError("could not detect the domain source from the CSV headers; pass --source")
    raise ValueError("ambiguous domain source from the CSV headers; pass --source explicitly")


def parse_domain_csv(
    path: str | Path,
    *,
    source: str | None = None,
    collected_date: str | date | None = None,
) -> ParsedDomainCsv:
    """Read one provider export into normalised domain observations."""
    collected = normalize_collected_date(collected_date)

    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        canonical_source = normalize_source(source) if source else detect_source(headers)
        domain_header = _domain_header(headers, canonical_source)
        observations = [
            observation
            for row in reader
            if (observation := _observation(row, domain_header, canonical_source, collected))
            is not None
        ]

    return ParsedDomainCsv(source=canonical_source, observations=observations)


def _observation(
    row: dict[str, str | None],
    domain_header: str,
    source: str,
    collected_date: str,
) -> DomainObservation | None:
    """Convert one CSV row, skipping rows without a usable domain."""
    values = {header.strip().lower(): value for header, value in row.items() if header is not None}
    domain = normalize_domain(values.get(domain_header.strip().lower()))
    if not domain:
        return None

    raw = {
        header: value if value is not None else ""
        for header, value in row.items()
        if header is not None and header != domain_header
    }
    return DomainObservation(
        domain=domain,
        source=source,
        collected_date=collected_date,
        metrics=_metrics_for(source, values),
        raw=raw,
    )


def _domain_header(headers: list[str], source: str) -> str:
    """Return the header that contains the domain, matching case-insensitively."""
    lookup = {header.strip().lower(): header for header in headers if header}
    expected = _DOMAIN_HEADERS.get(source)
    if expected is not None and expected in lookup:
        return lookup[expected]
    for candidate in ("domain name", "domain"):
        if candidate in lookup:
            return lookup[candidate]
    raise ValueError("could not find a domain column in the CSV headers")


def _metrics_for(source: str, values: dict[str, str | None]) -> dict[str, Any]:
    """Map a source-specific row onto the canonical metric vocabulary."""
    if source == SOURCE_EXPIREDDOMAINS:
        return _expireddomains_metrics(values)
    if source == SOURCE_GODADDY_AUCTIONS:
        return _godaddy_metrics(values)
    if source == SOURCE_SPAMZILLA:
        return _spamzilla_metrics(values)
    return empty_metrics()


def _expireddomains_metrics(values: dict[str, str | None]) -> dict[str, Any]:
    metrics = empty_metrics()
    metrics.update(
        {
            "backlinks": _as_int(values.get("bl")),
            "referring_domains": _as_int(values.get("dp")),
            "archive_birth_year": _as_int(values.get("aby")),
            "archive_captures": _as_int(values.get("acr")),
            "dmoz_listed": _as_bool(values.get("dmoz")),
            "registrations": _as_int(values.get("reg")),
            "drop_date": _as_iso_datetime(values.get("end date")),
        }
    )
    return metrics


def _godaddy_metrics(values: dict[str, str | None]) -> dict[str, Any]:
    metrics = empty_metrics()
    metrics.update(
        {
            "price": _as_float(values.get("price")),
            "estimated_value": _as_float(values.get("estimated value")),
            "bid_count": _as_int(values.get("bids")),
            "auction_end_at": _as_iso_datetime(values.get("auction end time")),
            "sale_type": _as_text(values.get("sale type")),
            "domain_age_years": _as_float(values.get("domain age")),
            "traffic": _as_int(values.get("traffic")),
            "majestic_trust_flow": _as_int(values.get("majestic tf")),
            "majestic_citation_flow": _as_int(values.get("majestic cf")),
            "majestic_backlinks": _as_int(values.get("majestic backlinks")),
            "majestic_referring_domains": _as_int(values.get("majestic referring domains")),
            "exact_match_tlds": _as_int(values.get("exact match tlds")),
            "keyword_registrations": _as_int(values.get("keyword registrations")),
            "developed_tlds": _as_int(values.get("developed tlds")),
            "semrush_authority_score": _as_int(values.get("semrush as")),
            "semrush_backlinks": _as_int(values.get("semrush backlinks")),
            "semrush_referring_domains": _as_int(values.get("semrush referring domains")),
            "semrush_indexed_pages": _as_int(values.get("semrush indexed pages")),
            "semrush_search_volume": _as_int(values.get("semrush search volume")),
            "semrush_cpc": _as_float(values.get("semrush cpc")),
            "top_referring_domains": _as_csv_list(values.get("semrush top referring domains")),
        }
    )
    metrics["backlinks"] = metrics["majestic_backlinks"]
    metrics["referring_domains"] = metrics["majestic_referring_domains"]
    return metrics


def _spamzilla_metrics(values: dict[str, str | None]) -> dict[str, Any]:
    metrics = empty_metrics()
    metrics.update(
        {
            "majestic_trust_flow": _as_int(values.get("tf")),
            "majestic_citation_flow": _as_int(values.get("cf")),
            "majestic_backlinks": _as_int(values.get("maj bl")),
            "majestic_referring_domains": _as_int(values.get("maj rd")),
            "majestic_topics": _as_int_list(values.get("maj topics")),
            "majestic_language": _as_text(values.get("maj lang")),
            "site_language": _as_text(values.get("site lang")),
            "moz_domain_authority": _as_int(values.get("moz da")),
            "moz_page_authority": _as_int(values.get("moz pa")),
            "domain_age_years": _as_float(values.get("age")),
            "spamzilla_score": _as_int(values.get("sz score")),
            "redirected": _as_bool(values.get("sz redirects")),
            "parked": _as_bool(values.get("sz parked")),
            "has_archive_history": _as_bool(values.get("sz a/history")),
            "drop_count": _as_int(values.get("sz drops")),
            "google_indexed": _as_bool(values.get("google index")),
            "outbound_links_internal": _as_int(values.get("out links internal")),
            "outbound_links_external": _as_int(values.get("out linksexternal")),
            "outbound_domains_external": _as_int(values.get("out domainsexternal")),
            "semrush_organic_traffic": _as_int(values.get("semtraf")),
            "semrush_rank": _as_int(values.get("semrank")),
            "semrush_organic_keywords": _as_int(values.get("semkw")),
            "listed_at": _as_iso_datetime(values.get("date added")),
            "price": _as_float(values.get("price")),
            "expires_at": _as_iso_datetime(values.get("expires")),
        }
    )
    metrics["backlinks"] = metrics["majestic_backlinks"]
    metrics["referring_domains"] = metrics["majestic_referring_domains"]
    metrics["language"] = metrics["site_language"] or metrics["majestic_language"]
    return metrics


def _as_text(value: str | None) -> str | None:
    """Return stripped text, or ``None`` for a null marker."""
    if value is None:
        return None
    text = value.strip()
    return None if text.lower() in _NULL_VALUES else text


def _as_float(value: str | None) -> float | None:
    """Parse a numeric cell, including ``K``/``M``/``B`` suffixes."""
    if value is None:
        return None
    text = value.strip().replace(",", "").replace("$", "").replace("%", "")
    if text.lower() in _NULL_VALUES:
        return None

    multiplier = 1.0
    suffix = text[-1:].lower()
    if suffix in {"k", "m", "b"}:
        multiplier = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}[suffix]
        text = text[:-1].strip()
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _as_int(value: str | None) -> int | None:
    """Parse an integer count, returning ``None`` when the cell is not numeric."""
    number = _as_float(value)
    return None if number is None else int(number)


def _as_bool(value: str | None) -> bool | None:
    """Parse common yes/no and numeric flag values, preserving unknown as ``None``."""
    if value is None:
        return None
    text = value.strip().lower()
    if text in _NULL_VALUES:
        return None
    if text in _FALSE_VALUES:
        return False
    if text in _TRUE_VALUES:
        return True
    number = _as_float(text)
    if number is not None:
        return number > 0
    return True


def _as_iso_datetime(value: str | None) -> str | None:
    """Parse common export date formats into a UTC ISO-8601 timestamp."""
    text = _as_text(value)
    if text is None:
        return None

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%m/%d/%Y", "%m/%d/%Y %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            break

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_csv_list(value: str | None) -> list[str]:
    """Split a comma-separated cell into a clean list of strings."""
    text = _as_text(value)
    if text is None:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def _as_int_list(value: str | None) -> list[int]:
    """Split a whitespace- or comma-separated cell into integers."""
    text = _as_text(value)
    if text is None:
        return []

    items: list[int] = []
    for token in text.replace(",", " ").split():
        try:
            items.append(int(token))
        except ValueError:
            continue
    return items
