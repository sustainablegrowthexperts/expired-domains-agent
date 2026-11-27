"""Deterministic Live check stage for DR-passed candidate domains.

The stage sits between the free Domain Rating check and the Wayback Machine
stage. It resolves A/AAAA records, fetches the landing page over HTTPS with an
HTTP fallback, follows a bounded redirect chain, and inspects the final page.
Domains that no longer resolve, have no usable HTTP response, or serve a parked
page are neutral passes: they may still be available to buy. Off-root redirects
and toxic landing content fail.

The worker stores a terminal verdict and full evidence. Re-runs stop once the
per-domain attempt budget is spent, so a permanently broken target cannot loop.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

import httpx
import tldextract

from expired_domains.blocklist import Blocklist, load_blocklist
from expired_domains.config import PROJECT_ROOT
from expired_domains.models import LiveCheckVerdict
from expired_domains.storage.db import (
    connect,
    get_pipeline_meta,
    mark_live_checks_processing,
    record_live_check_errors,
    set_live_check_results,
    set_pipeline_meta,
    transaction,
)
from expired_domains.wayback import ContentLexicon, load_content_lexicon

DEFAULT_LIVE_CONFIG_PATH = PROJECT_ROOT / "config" / "live" / "v1.json"
DEFAULT_STALE_PROCESSING_SECONDS = 120.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_REDIRECTS = 10
DEFAULT_MAX_BODY_BYTES = 50_000
LIVE_EXCERPT_CHARS = 600
USER_AGENT = "expired-domains-agent/0.1 (+live check)"

logger = logging.getLogger(__name__)

_SUFFIXES = tldextract.TLDExtract(suffix_list_urls=())


@dataclass(frozen=True)
class LiveCheckConfig:
    """A versioned Live check configuration."""

    version: str
    enabled: bool
    timeout_seconds: float
    max_redirects: int
    max_body_bytes: int
    content_min_term_hits: int
    toxic_categories: frozenset[str]
    parking_terms: tuple[str, ...]


@dataclass(frozen=True)
class LiveDns:
    """One deterministic A/AAAA lookup result."""

    ok: bool
    addresses: tuple[str, ...] = ()
    error: str | None = None
    nxdomain: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly evidence shape."""
        return {
            "ok": self.ok,
            "addresses": list(self.addresses),
            "error": self.error,
            "nxdomain": self.nxdomain,
        }


@dataclass(frozen=True)
class LiveAttempt:
    """One HTTPS or HTTP probe attempt."""

    url: str
    status: int | None = None
    final_url: str | None = None
    redirects: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly attempt shape."""
        return {
            "url": self.url,
            "status": self.status,
            "final_url": self.final_url,
            "redirects": list(self.redirects),
            "error": self.error,
        }


@dataclass
class LiveProbe:
    """Raw DNS and HTTP evidence for one domain."""

    domain: str
    start_host: str
    dns: LiveDns
    attempts: list[LiveAttempt] = field(default_factory=list)
    final_url: str | None = None
    final_host: str | None = None
    status: int | None = None
    redirects: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    text: str = ""
    html: str = ""
    error: str | None = None


class LiveProber(Protocol):
    """The probing interface the worker needs from a Live check client."""

    def probe(self, domain: str, config: LiveCheckConfig) -> LiveProbe:
        """Return DNS and HTTP evidence for one domain."""
        ...


def load_live_config(path: Path | None = None) -> LiveCheckConfig:
    """Load ``config/live/v1.json`` once per process."""
    return _load_live_config(str(path or DEFAULT_LIVE_CONFIG_PATH))


@cache
def _load_live_config(path: str) -> LiveCheckConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("live config must be a JSON object")

    return LiveCheckConfig(
        version=str(data.get("version") or "v1"),
        enabled=_as_bool(data.get("enabled"), True),
        timeout_seconds=max(0.1, _as_float(data.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS)),
        max_redirects=max(0, _as_int(data.get("max_redirects"), DEFAULT_MAX_REDIRECTS)),
        max_body_bytes=max(1, _as_int(data.get("max_body_bytes"), DEFAULT_MAX_BODY_BYTES)),
        content_min_term_hits=max(1, _as_int(data.get("content_min_term_hits"), 2)),
        toxic_categories=frozenset(str(key) for key in data.get("toxic_categories", [])),
        parking_terms=tuple(str(term) for term in data.get("parking_terms", [])),
    )


def resolve_host(host: str) -> LiveDns:
    """Resolve A/AAAA addresses for ``host``; MX records are deliberately excluded."""
    name = _normalise_host(host)
    if not name:
        return LiveDns(ok=False, error="empty host", nxdomain=True)
    try:
        infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        code = getattr(exc, "errno", None)
        nxdomain = code in {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)}
        return LiveDns(ok=False, error=str(exc) or "DNS lookup failed", nxdomain=nxdomain)
    except OSError as exc:
        return LiveDns(ok=False, error=str(exc) or "DNS lookup failed", nxdomain=False)

    addresses = sorted(
        {
            str(info[4][0])
            for info in infos
            if info[0] in {socket.AF_INET, socket.AF_INET6} and info[4]
        }
    )
    if not addresses:
        return LiveDns(ok=False, error="no A/AAAA records", nxdomain=True)
    return LiveDns(ok=True, addresses=tuple(addresses))


class LiveClient:
    """Small DNS and plain-HTTP client for one Live check probe."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float | None = None,
        max_redirects: int | None = None,
        max_body_bytes: int | None = None,
        resolver: Callable[[str], LiveDns] = resolve_host,
    ) -> None:
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._max_redirects = max_redirects
        self._max_body_bytes = max_body_bytes
        self._resolver = resolver

    def close(self) -> None:
        """Keep the client interface uniform; probes open short-lived clients."""
        return None

    def __enter__(self) -> LiveClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def probe(self, domain: str, config: LiveCheckConfig) -> LiveProbe:
        """Resolve DNS, try HTTPS then HTTP, and return the selected response."""
        start_host = _normalise_host(domain)
        dns = self._resolver(start_host)
        probe = LiveProbe(domain=domain, start_host=start_host, dns=dns)

        for scheme in ("https", "http"):
            url = f"{scheme}://{start_host}/"
            try:
                response = self._get(url, config)
            except Exception as exc:  # noqa: BLE001 - fall back or record no_http
                probe.attempts.append(LiveAttempt(url=url, error=str(exc) or type(exc).__name__))
                probe.error = str(exc) or type(exc).__name__
                continue

            redirects = _redirect_chain(response)
            final_url = str(response.url)
            probe.attempts.append(
                LiveAttempt(
                    url=url,
                    status=response.status_code,
                    final_url=final_url,
                    redirects=tuple(redirects),
                )
            )
            if not _usable_status(response.status_code):
                probe.error = f"HTTP {response.status_code}"
                continue

            body = _response_text(response, self._body_limit(config))
            title, text = parse_live_page(body)
            probe.final_url = final_url
            probe.final_host = _normalise_host(final_url)
            probe.status = response.status_code
            probe.redirects = redirects
            probe.title = title[:500]
            probe.text = text
            probe.html = body
            probe.error = None
            return probe

        return probe

    def _get(self, url: str, config: LiveCheckConfig) -> httpx.Response:
        timeout = self._timeout_seconds
        if timeout is None:
            timeout = config.timeout_seconds
        redirects = self._max_redirects
        if redirects is None:
            redirects = config.max_redirects
        with httpx.Client(
            follow_redirects=True,
            max_redirects=redirects,
            timeout=timeout,
            transport=self._transport,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            },
        ) as client:
            return client.get(url)

    def _body_limit(self, config: LiveCheckConfig) -> int:
        return self._max_body_bytes or config.max_body_bytes


def parse_live_page(body: str) -> tuple[str, str]:
    """Extract the ``<title>`` and visible text from a landing page."""
    parser = _LivePageParser()
    try:
        parser.feed(body)
    except Exception:  # noqa: BLE001 - malformed HTML is still evidence
        return "", ""
    title = _normalise_text(" ".join(parser.title_parts))
    text = _normalise_text(" ".join(parser.text_parts))
    return title, text


def evaluate_live_check(
    domain: str,
    probe: LiveProbe,
    *,
    config: LiveCheckConfig | None = None,
    blocklist: Blocklist | None = None,
    lexicon: ContentLexicon | None = None,
) -> LiveCheckVerdict:
    """Classify one probe and return a terminal pass/fail verdict."""
    resolved = config or load_live_config()
    blocked = blocklist or load_blocklist()
    content = lexicon or load_content_lexicon()

    if probe.dns.nxdomain:
        return _verdict(
            domain,
            probe,
            resolved,
            passed=True,
            class_="nxdomain",
            reason="nxdomain: no A/AAAA records",
            neutral=True,
            toxic_terms=(),
            toxic_categories=(),
            parking=False,
        )

    if probe.final_url is None or probe.status is None:
        return _verdict(
            domain,
            probe,
            resolved,
            passed=True,
            class_="no_http",
            reason="no_http: HTTPS and HTTP probes failed",
            neutral=True,
            toxic_terms=(),
            toxic_categories=(),
            parking=False,
        )

    final_host = probe.final_host or _normalise_host(probe.final_url)
    if _root_domain(final_host) != _root_domain(probe.start_host):
        return _verdict(
            domain,
            probe,
            resolved,
            passed=False,
            class_="redirect_offsite",
            reason=f"redirect_offsite: {probe.start_host} redirected to {final_host}",
            neutral=False,
            toxic_terms=(),
            toxic_categories=(),
            parking=False,
        )

    page_text = f"{probe.title} {probe.text}"
    parking = _contains_any(resolved.parking_terms, page_text)
    if parking:
        return _verdict(
            domain,
            probe,
            resolved,
            passed=True,
            class_="parked",
            reason="parked: parking or for-sale signature found",
            neutral=True,
            toxic_terms=(),
            toxic_categories=(),
            parking=True,
        )

    toxic_host = _find_terms(
        text=final_host,
        blocklist=blocked,
        lexicon=content,
        categories=resolved.toxic_categories,
    )
    toxic_title = _find_terms(
        text=probe.title,
        blocklist=blocked,
        lexicon=content,
        categories=resolved.toxic_categories,
    )
    toxic_body = _find_terms(
        text=probe.text,
        blocklist=blocked,
        lexicon=content,
        categories=resolved.toxic_categories,
    )
    body_terms = {term for _category, term in toxic_body}
    title_terms = {term for _category, term in toxic_title}
    host_terms = {term for _category, term in toxic_host}
    toxic_terms = tuple(sorted(host_terms | title_terms | body_terms))
    toxic_categories = tuple(
        sorted({category for category, _term in [*toxic_host, *toxic_title, *toxic_body]})
    )

    if host_terms:
        return _verdict(
            domain,
            probe,
            resolved,
            passed=False,
            class_="content",
            reason=f"content: toxic final host ({', '.join(sorted(host_terms))})",
            neutral=False,
            toxic_terms=toxic_terms,
            toxic_categories=toxic_categories,
            parking=False,
        )
    if title_terms:
        return _verdict(
            domain,
            probe,
            resolved,
            passed=False,
            class_="content",
            reason=f"content: toxic title ({', '.join(sorted(title_terms))})",
            neutral=False,
            toxic_terms=toxic_terms,
            toxic_categories=toxic_categories,
            parking=False,
        )
    if len(body_terms) >= resolved.content_min_term_hits:
        return _verdict(
            domain,
            probe,
            resolved,
            passed=False,
            class_="content",
            reason=(
                "content: toxic body terms "
                f"({', '.join(sorted(body_terms))}); need "
                f"{resolved.content_min_term_hits}"
            ),
            neutral=False,
            toxic_terms=toxic_terms,
            toxic_categories=toxic_categories,
            parking=False,
        )

    return _verdict(
        domain,
        probe,
        resolved,
        passed=True,
        class_="content",
        reason="content: live page passed",
        neutral=False,
        toxic_terms=toxic_terms,
        toxic_categories=toxic_categories,
        parking=False,
    )


def _verdict(
    domain: str,
    probe: LiveProbe,
    config: LiveCheckConfig,
    *,
    passed: bool,
    class_: str,
    reason: str,
    neutral: bool,
    toxic_terms: tuple[str, ...],
    toxic_categories: tuple[str, ...],
    parking: bool,
) -> LiveCheckVerdict:
    final_host = probe.final_host or (_normalise_host(probe.final_url) if probe.final_url else None)
    details = {
        "domain": domain,
        "status": "passed" if passed else "failed",
        "class": class_,
        "reason": reason,
        "neutral": neutral,
        "config_version": config.version,
        "dns": probe.dns.as_dict(),
        "http": {
            "attempts": [attempt.as_dict() for attempt in probe.attempts],
            "final_url": probe.final_url,
            "final_host": final_host,
            "status": probe.status,
            "redirects": list(probe.redirects),
            "title": probe.title,
            "excerpt": probe.text[:LIVE_EXCERPT_CHARS],
            "error": probe.error,
        },
        "final_host": final_host,
        "http_status": probe.status,
        "title": probe.title,
        "excerpt": probe.text[:LIVE_EXCERPT_CHARS],
        "toxic_terms": list(toxic_terms),
        "toxic_categories": list(toxic_categories),
        "parking": parking,
    }
    return LiveCheckVerdict(
        passed=passed,
        class_=class_,
        reason=reason,
        details=details,
    )


def write_live_evidence(
    raw_root: Path,
    probe: LiveProbe,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Write DNS, HTTP, redirect, and body evidence for one probe."""
    moment = now or datetime.now(UTC)
    stamp = run_id or f"live_{moment.strftime('%Y%m%dT%H%M%S%fZ')}"
    run_dir = _unique_dir(Path(raw_root) / probe.domain / stamp)
    run_dir.mkdir(parents=True, exist_ok=False)

    (run_dir / "dns.json").write_text(
        json.dumps(probe.dns.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "http.json").write_text(
        json.dumps(
            {
                "domain": probe.domain,
                "start_host": probe.start_host,
                "attempts": [attempt.as_dict() for attempt in probe.attempts],
                "final_url": probe.final_url,
                "final_host": probe.final_host,
                "status": probe.status,
                "title": probe.title,
                "excerpt": probe.text[:LIVE_EXCERPT_CHARS],
                "error": probe.error,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "redirects.json").write_text(
        json.dumps(list(probe.redirects), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "body.html").write_text(probe.html, encoding="utf-8")
    return run_dir


def load_live_check_candidates(
    conn: sqlite3.Connection,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    stale_processing_seconds: float = DEFAULT_STALE_PROCESSING_SECONDS,
    since: str | None = None,
    now: datetime | None = None,
) -> list[tuple[str, int]]:
    """Return DR-passed domains that still need a Live check verdict.

    ``since`` is the first-run checkpoint. Only domains whose DR check happened
    on or after the Live worker started are queued, so deployment does not
    backfill the existing DR-passed list.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(domains)")}
    required = {
        "blocked_category",
        "prefilter_pass",
        "keyword_check_pass",
        "dr_check_pass",
        "dr_check_checked_at",
        "live_check_status",
        "live_check_attempts",
        "live_check_checked_at",
        "live_check_manual_pass",
    }
    if not required <= columns:
        return []

    moment = _utc(now) or datetime.now(UTC)
    cutoff = (moment - timedelta(seconds=max(0.0, stale_processing_seconds))).isoformat(
        timespec="seconds"
    )
    rows = conn.execute(
        """
        SELECT domain, COALESCE(live_check_attempts, 0) AS attempts
        FROM domains
        WHERE blocked_category IS NULL
          AND COALESCE(prefilter_pass, 0) = 1
          AND COALESCE(keyword_check_pass, 0) = 1
          AND COALESCE(dr_check_pass, 0) = 1
          AND COALESCE(live_check_manual_pass, 0) = 0
          AND (
              ? IS NULL
              OR (
                  dr_check_checked_at IS NOT NULL
                  AND dr_check_checked_at >= ?
              )
          )
          AND COALESCE(live_check_attempts, 0) < ?
          AND (
              live_check_status IS NULL
              OR (
                  live_check_status = 'processing'
                  AND (live_check_checked_at IS NULL OR live_check_checked_at < ?)
              )
          )
        ORDER BY domain
        """,
        (since, since, max(1, int(max_attempts)), cutoff),
    ).fetchall()
    return [(str(row["domain"]), int(row["attempts"])) for row in rows]


def live_check_pending(
    db_path: Path,
    *,
    client: LiveProber | None,
    config: LiveCheckConfig | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    limit: int = 1,
    stale_processing_seconds: float = DEFAULT_STALE_PROCESSING_SECONDS,
    raw_root: Path | None = None,
    since: str | None = None,
    now: datetime | None = None,
) -> int:
    """Check up to ``limit`` queued DR-passed domains and store Live verdicts.

    The first enabled run records ``live_checks_started_at``. Only domains whose
    DR check happened on or after that checkpoint are queued.
    """
    resolved = config or load_live_config()
    if client is None or not resolved.enabled or limit <= 0:
        return 0

    moment = _utc(now) or datetime.now(UTC)
    conn = connect(db_path)
    try:
        cutoff = since
        if cutoff is None:
            cutoff = get_pipeline_meta(conn, "live_checks_started_at")
            if cutoff is None:
                cutoff = moment.isoformat(timespec="seconds")
                with transaction(conn):
                    set_pipeline_meta(conn, "live_checks_started_at", cutoff)
        candidates = load_live_check_candidates(
            conn,
            max_attempts=max_attempts,
            stale_processing_seconds=stale_processing_seconds,
            since=cutoff,
            now=moment,
        )
    finally:
        conn.close()

    if not candidates:
        return 0

    checked = 0
    for domain, _attempts in candidates[: max(0, limit)]:
        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        conn = connect(db_path)
        try:
            with transaction(conn):
                mark_live_checks_processing(conn, [domain], started_at=started_at)
        finally:
            conn.close()

        try:
            probe = client.probe(domain, resolved)
            if raw_root is not None:
                write_live_evidence(raw_root, probe, now=moment)
            verdict = evaluate_live_check(domain, probe, config=resolved)
        except Exception as exc:  # noqa: BLE001 - retry the domain next cycle
            logger.warning("live check failed for %s: %s", domain, exc)
            finished_at = datetime.now(UTC).isoformat(timespec="seconds")
            conn = connect(db_path)
            try:
                with transaction(conn):
                    record_live_check_errors(
                        conn,
                        [domain],
                        error=str(exc),
                        checked_at=finished_at,
                        max_attempts=max_attempts,
                    )
            finally:
                conn.close()
            continue

        finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        conn = connect(db_path)
        try:
            with transaction(conn):
                checked += set_live_check_results(
                    conn,
                    {domain: verdict},
                    checked_at=finished_at,
                )
        finally:
            conn.close()

    return checked


class _LivePageParser(HTMLParser):
    """Extract title and visible text, ignoring scripts and styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._title_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "title":
            self._title_depth += 1
        elif name in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "title" and self._title_depth:
            self._title_depth -= 1
        elif name in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._title_depth:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


def registrable_root(host: str) -> str:
    """Return the registrable root domain for a host (``www.example.co.uk`` → ``example.co.uk``)."""
    text = _normalise_host(host)
    if not text:
        return ""
    extracted = _SUFFIXES(text)
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}".lower()
    return text


def _root_domain(host: str) -> str:
    return registrable_root(host)


def _normalise_host(value: str | None) -> str:
    """Return a bare lowercase host without scheme, port, path, or trailing dot."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if text.startswith("["):
        end = text.find("]")
        text = text[1:end] if end >= 0 else text
    elif ":" in text:
        text = text.rsplit(":", 1)[0]
    return text.strip().rstrip(".")


def _redirect_chain(response: httpx.Response) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    for item in response.history:
        chain.append(
            {
                "url": str(item.url),
                "status": item.status_code,
                "location": item.headers.get("location", ""),
            }
        )
    if response.history:
        chain.append(
            {
                "url": str(response.url),
                "status": response.status_code,
                "location": "",
            }
        )
    return chain


def _usable_status(status: int) -> bool:
    return 200 <= status < 400


def _response_text(response: httpx.Response, limit: int) -> str:
    content = response.content[: max(1, int(limit))]
    encoding = response.encoding or "utf-8"
    try:
        return content.decode(encoding, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _find_terms(
    *,
    text: str,
    blocklist: Blocklist,
    lexicon: ContentLexicon,
    categories: frozenset[str],
) -> list[tuple[str, str]]:
    lowered = text.lower()
    found: list[tuple[str, str]] = []
    for category, block_category in blocklist.categories.items():
        if category not in categories:
            continue
        for term in block_category.terms:
            if _term_in_text(term, lowered):
                found.append((category, term))
    for category, terms in lexicon.categories.items():
        if category not in categories:
            continue
        for term in terms:
            if _term_in_text(term, lowered):
                found.append((category, term))
    return found


def _contains_any(terms: Sequence[str], text: str) -> bool:
    lowered = text.lower()
    return any(_term_in_text(term, lowered) for term in terms)


@cache
def _term_pattern(term: str) -> re.Pattern[str]:
    pieces = [re.escape(piece) for piece in term.strip().lower().split()]
    phrase = r"\s+".join(pieces)
    return re.compile(rf"(?<!\w){phrase}(?!\w)", re.IGNORECASE | re.UNICODE)


def _term_in_text(term: str, lowered_text: str) -> bool:
    if not term.strip():
        return False
    return bool(_term_pattern(term).search(lowered_text))


def _unique_dir(path: Path) -> Path:
    """Return a run directory path that does not overwrite earlier evidence."""
    candidate = path
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}_{index:02d}")
        index += 1
    return candidate


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
