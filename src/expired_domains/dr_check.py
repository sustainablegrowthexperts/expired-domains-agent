"""Free Ahrefs Domain Rating checks for keyword-passing candidate domains.

The DR check is the stage between the keyword check and paid data collection.
A worker claims every unblocked domain that passed the keyword check, asks the
free ``public/domain-rating-free`` endpoint in batches of up to 1,000 targets,
and stores a terminal pass/fail verdict. The stage needs its own Ahrefs key
(``AHREFS_FREE_DR_CHECK``) and spends no paid units.

Transient API failures return the domain to pending and increment its attempt
counter. Once ``dr_check_max_attempts`` is reached the domain is marked failed
instead of being retried forever.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from expired_domains.config import DEFAULT_DR_CHECK_MAX_ATTEMPTS, DEFAULT_DR_FLOOR
from expired_domains.models import DomainRatingVerdict
from expired_domains.storage.db import (
    connect,
    mark_dr_checks_processing,
    record_dr_check_errors,
    set_dr_check_results,
    transaction,
)

DR_CHECK_BATCH_SIZE = 1000
DR_CHECK_BATCHES_PER_CYCLE = 1
DR_CHECK_STATUS_PROCESSING = "processing"
DR_CHECK_STATUS_PASSED = "passed"
DR_CHECK_STATUS_FAILED = "failed"
FREE_DR_ENDPOINT = "public/domain-rating-free"
DEFAULT_STALE_PROCESSING_SECONDS = 120.0
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_HTTP_ATTEMPTS = 3
BACKOFF_SECONDS = 0.5

logger = logging.getLogger(__name__)

_DR_CANDIDATES_SQL = """
SELECT domain, COALESCE(dr_check_attempts, 0) AS attempts
FROM domains
WHERE blocked_category IS NULL
  AND COALESCE(prefilter_pass, 0) = 1
  AND COALESCE(keyword_check_pass, 0) = 1
  AND COALESCE(dr_check_attempts, 0) < ?
  AND (
      dr_check_status IS NULL
      OR (
          dr_check_status = 'processing'
          AND (dr_check_checked_at IS NULL OR dr_check_checked_at < ?)
      )
  )
ORDER BY domain
"""


class DomainRatingChecker(Protocol):
    """The small interface the worker needs from a free-DR client."""

    def check(self, domains: Sequence[str]) -> Mapping[str, DomainRatingVerdict]:
        """Return a parsed rating for every target the endpoint answered."""
        ...


@dataclass(frozen=True)
class DomainRatingCandidate:
    """One keyword-passed domain waiting for a free DR check."""

    domain: str
    attempts: int


def load_domain_rating_candidates(
    conn: sqlite3.Connection,
    *,
    max_attempts: int = DEFAULT_DR_CHECK_MAX_ATTEMPTS,
    stale_processing_seconds: float = DEFAULT_STALE_PROCESSING_SECONDS,
    now: datetime | None = None,
) -> list[DomainRatingCandidate]:
    """Return keyword-passed domains that need a DR verdict.

    A row is pending when it has no DR status, or it was left ``processing`` for
    longer than the stale timeout (for example after a worker restart). Domains
    that already used their attempt budget, or have a terminal verdict, are
    never returned.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(domains)")}
    required = {
        "prefilter_pass",
        "blocked_category",
        "keyword_check_pass",
        "dr_check_status",
        "dr_check_attempts",
        "dr_check_checked_at",
    }
    if not required <= columns:
        return []

    moment = _utc(now) or datetime.now(UTC)
    cutoff = (moment - timedelta(seconds=max(0.0, stale_processing_seconds))).isoformat(
        timespec="seconds"
    )
    rows = conn.execute(
        _DR_CANDIDATES_SQL,
        (max(1, int(max_attempts)), cutoff),
    ).fetchall()
    return [
        DomainRatingCandidate(domain=row["domain"], attempts=int(row["attempts"])) for row in rows
    ]


class AhrefsDomainRatingChecker:
    """Free Ahrefs ``public/domain-rating-free`` client."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key or ''}"},
        )

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> AhrefsDomainRatingChecker:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def check(self, domains: Sequence[str]) -> Mapping[str, DomainRatingVerdict]:
        """Ask the free endpoint for one batch, or return no verdicts."""
        targets = [str(domain) for domain in domains if str(domain).strip()]
        if not self._api_key or not targets:
            return {}

        payload = self._post({"targets": targets})
        return parse_domain_ratings(payload, targets)

    def _post(self, body: dict[str, Any]) -> Any:
        """POST with a small retry window for 429/5xx and transport errors."""
        delay = BACKOFF_SECONDS
        last_error: Exception | None = None
        for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
            try:
                response = self._http.post(FREE_DR_ENDPOINT, json=body)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt < MAX_HTTP_ATTEMPTS:
                    self._sleep(delay)
                    delay *= 2
                    continue
                raise

            if response.status_code == 429 or 500 <= response.status_code <= 599:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                if attempt < MAX_HTTP_ATTEMPTS:
                    self._sleep(delay)
                    delay *= 2
                    continue
            response.raise_for_status()
            return response.json()

        raise RuntimeError(f"DR check failed after {MAX_HTTP_ATTEMPTS} attempts: {last_error}")


def parse_domain_ratings(
    payload: Any,
    targets: Sequence[str],
) -> dict[str, DomainRatingVerdict]:
    """Validate a free-DR reply and keep ratings for the requested targets.

    The documented response wraps rows in a ``domain_ratings`` list, but the
    parser also accepts the common aliases used by other Ahrefs endpoints and a
    bare ``{target: rating}`` mapping. Unknown or malformed rows are ignored.
    """
    expected = {_normalise_target(target): target for target in targets}
    expected.pop("", None)
    if not expected:
        return {}

    default_license = _default_license(payload)

    results: dict[str, DomainRatingVerdict] = {}
    for item in _rating_items(payload, expected):
        if not isinstance(item, dict):
            continue
        target = item.get("target") or item.get("domain") or item.get("url")
        canonical = expected.get(_normalise_target(target))
        if canonical is None:
            continue

        rating = _rating_value(
            item.get("domain_rating", item.get("domainRating", item.get("rating")))
        )
        license_value = item.get("license") or default_license
        results[canonical] = DomainRatingVerdict(
            rating=rating,
            license=str(license_value).strip() if license_value else None,
        )

    return results


def dr_check_pending(
    db_path: Path,
    *,
    checker: DomainRatingChecker | None,
    dr_floor: int = DEFAULT_DR_FLOOR,
    max_attempts: int = DEFAULT_DR_CHECK_MAX_ATTEMPTS,
    batch_size: int = DR_CHECK_BATCH_SIZE,
    max_batches: int | None = None,
    stale_processing_seconds: float = DEFAULT_STALE_PROCESSING_SECONDS,
    now: datetime | None = None,
) -> int:
    """Check every queued keyword-passed domain and return terminal verdicts.

    Each batch is marked ``processing`` before the HTTP call. A transient
    failure returns the batch to pending and counts the attempt; once the
    per-domain attempt budget is exhausted the domain is stored as a terminal
    failure. Missing ratings in a successful reply are terminal failures too,
    so an unknown target cannot loop forever.
    """
    if checker is None:
        return 0

    moment = _utc(now) or datetime.now(UTC)
    conn = connect(db_path)
    try:
        candidates = load_domain_rating_candidates(
            conn,
            max_attempts=max_attempts,
            stale_processing_seconds=stale_processing_seconds,
            now=moment,
        )
    finally:
        conn.close()

    if not candidates:
        return 0

    pending_batches = _batches([candidate.domain for candidate in candidates], batch_size)
    if max_batches is not None:
        pending_batches = pending_batches[: max(0, max_batches)]

    checked = 0
    for batch in pending_batches:
        started_at = datetime.now(UTC).isoformat(timespec="seconds")

        conn = connect(db_path)
        try:
            with transaction(conn):
                mark_dr_checks_processing(conn, batch, started_at=started_at)
        finally:
            conn.close()

        try:
            verdicts = checker.check(batch)
        except Exception as exc:  # noqa: BLE001 - retry the batch next cycle
            logger.warning("DR check batch failed: %s", exc)
            finished_at = datetime.now(UTC).isoformat(timespec="seconds")
            conn = connect(db_path)
            try:
                with transaction(conn):
                    record_dr_check_errors(
                        conn,
                        batch,
                        error=str(exc),
                        checked_at=finished_at,
                        max_attempts=max_attempts,
                    )
            finally:
                conn.close()
            continue

        finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        accepted = {domain: verdict for domain, verdict in verdicts.items() if domain in batch}
        for domain in batch:
            accepted.setdefault(domain, DomainRatingVerdict(rating=None))

        conn = connect(db_path)
        try:
            with transaction(conn):
                checked += set_dr_check_results(
                    conn,
                    accepted,
                    dr_floor=dr_floor,
                    checked_at=finished_at,
                )
        finally:
            conn.close()

    return checked


def _batches(values: Sequence[str], size: int) -> list[Sequence[str]]:
    """Split values into consecutive batches of at most ``size``."""
    step = max(1, size)
    return [values[index : index + step] for index in range(0, len(values), step)]


def _rating_items(
    payload: Any,
    expected: Mapping[str, str],
) -> list[Any]:
    """Extract rating rows from the known free-DR response shapes."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    for key in (
        "domain_rating",
        "domain_ratings",
        "domainRatings",
        "results",
        "data",
        "ratings",
        "items",
        "targets",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _rating_items(value, expected)
            if nested:
                return nested

    rows: list[Any] = []
    for key, value in payload.items():
        if _normalise_target(key) not in expected:
            continue
        if isinstance(value, dict):
            rows.append({"target": key, **value})
        elif value is not None and not isinstance(value, bool):
            rows.append({"target": key, "domain_rating": value})
    return rows


def _default_license(payload: Any) -> str | None:
    """Return the first license URL found in the response wrapper."""
    if not isinstance(payload, dict):
        return None
    value = payload.get("license")
    if value:
        return str(value).strip()
    for key in ("domain_rating", "domain_ratings", "data", "results"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _default_license(nested)
            if found:
                return found
    return None


def _normalise_target(value: Any) -> str:
    """Normalise a target to a bare lowercase host for matching."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = urlsplit(text).hostname or text
    text = text.split("/", 1)[0].strip().rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def _rating_value(value: Any) -> float | None:
    """Parse a rating, treating unusable or missing values as ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc(value: datetime | None) -> datetime | None:
    """Return an aware UTC datetime, or ``None`` when no value was supplied."""
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
