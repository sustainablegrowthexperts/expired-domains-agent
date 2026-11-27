"""Live check probing, classification, queueing, and Wayback gating."""

from __future__ import annotations

import socket
import time
from pathlib import Path

import httpx

from expired_domains.config import load_settings
from expired_domains.live_check import (
    LiveAttempt,
    LiveCheckConfig,
    LiveClient,
    LiveDns,
    LiveProbe,
    evaluate_live_check,
    live_check_pending,
    load_live_check_candidates,
    load_live_config,
    parse_live_page,
    registrable_root,
    resolve_host,
)
from expired_domains.models import (
    DomainObservation,
    DomainRatingVerdict,
    KeywordVerdict,
    LiveCheckVerdict,
    PrefilterResult,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    list_collection_candidates,
    set_dr_check_results,
    set_keyword_check_results,
    set_live_check_manual_pass,
    set_live_check_results,
    set_pipeline_meta,
    set_prefilter_results,
    store_domain_observations,
)
from expired_domains.wayback import (
    WaybackEvidence,
    load_wayback_candidates,
    wayback_check_pending,
)
from expired_domains.worker import LiveCheckWorker

CHECKED_AT = "2026-11-27T12:00:00+00:00"


class StubClient:
    """Deterministic stand-in for the DNS/HTTP Live client."""

    def __init__(self, probe: LiveProbe, error: Exception | None = None) -> None:
        self.probe_result = probe
        self.error = error
        self.calls: list[str] = []

    def probe(self, domain: str, config: LiveCheckConfig) -> LiveProbe:
        self.calls.append(domain)
        if self.error is not None:
            raise self.error
        return self.probe_result


def _config(**overrides) -> LiveCheckConfig:
    base = dict(
        version="v1",
        enabled=True,
        timeout_seconds=10.0,
        max_redirects=10,
        max_body_bytes=50_000,
        content_min_term_hits=2,
        toxic_categories=frozenset({"adult", "gambling", "pharma"}),
        parking_terms=("domain for sale", "buy this domain", "parked domain"),
    )
    base.update(overrides)
    return LiveCheckConfig(**base)


def _probe(
    domain: str = "example.com",
    *,
    final_url: str | None = "https://example.com/",
    status: int | None = 200,
    title: str = "Example",
    text: str = "welcome to a normal website",
    dns: LiveDns | None = None,
) -> LiveProbe:
    resolved = dns or LiveDns(ok=True, addresses=("93.184.216.34",))
    return LiveProbe(
        domain=domain,
        start_host=domain,
        dns=resolved,
        attempts=[LiveAttempt(url=f"https://{domain}/", status=status, final_url=final_url)],
        final_url=final_url,
        final_host=_host(final_url) if final_url else None,
        status=status,
        title=title,
        text=text,
        html=f"<html><head><title>{title}</title></head><body>{text}</body></html>",
    )


def _host(url: str | None) -> str | None:
    if not url:
        return None
    return url.split("://", 1)[-1].split("/", 1)[0]


def _seed_dr_passed(
    db_path: Path,
    domains: list[str],
    *,
    dr_checked_at: str = CHECKED_AT,
    live: bool = False,
) -> None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        store_domain_observations(
            conn,
            [DomainObservation(domain, "spamzilla", "2026-11-27", {}, {}) for domain in domains],
        )
        set_prefilter_results(
            conn,
            {
                domain: PrefilterResult(True, "Passed all rules", {}, "v1", CHECKED_AT)
                for domain in domains
            },
        )
        set_keyword_check_results(
            conn,
            {domain: KeywordVerdict(True, "ok", 0.9) for domain in domains},
            model="test-model",
            checked_at=CHECKED_AT,
        )
        set_dr_check_results(
            conn,
            {domain: DomainRatingVerdict(30.0) for domain in domains},
            dr_floor=10,
            checked_at=dr_checked_at,
        )
        if live:
            set_live_check_results(
                conn,
                {
                    domain: LiveCheckVerdict(
                        True,
                        "content",
                        "content: live page passed",
                        {"domain": domain, "status": "passed", "config_version": "v1"},
                    )
                    for domain in domains
                },
                checked_at=CHECKED_AT,
            )
        set_pipeline_meta(conn, "live_checks_started_at", "2026-01-01T00:00:00+00:00")
        set_pipeline_meta(conn, "wayback_checks_started_at", "2026-01-01T00:00:00+00:00")
        conn.commit()
    finally:
        conn.close()


def _live_row(db_path: Path, domain: str):
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT live_check_status, live_check_pass, live_check_class, "
            "live_check_reason, live_check_attempts, live_check_manual_pass, "
            "live_check_details_json, live_check_final_host, live_check_http_status "
            "FROM domains WHERE domain = ?",
            (domain,),
        ).fetchone()
    finally:
        conn.close()


def test_default_config_is_versioned_and_bounded() -> None:
    config = load_live_config()

    assert config.version == "v1"
    assert config.enabled is True
    assert config.max_redirects == 10
    assert config.max_body_bytes == 50_000
    assert config.content_min_term_hits >= 1
    assert "gambling" in config.toxic_categories


def test_registrable_root_treats_www_and_subdomains_as_the_same_root() -> None:
    assert registrable_root("example.com") == "example.com"
    assert registrable_root("www.example.com") == "example.com"
    assert registrable_root("shop.example.com") == "example.com"
    assert registrable_root("https://www.example.co.uk/path") == "example.co.uk"
    assert registrable_root("example.co.uk") != registrable_root("example.com")


def test_parse_live_page_extracts_title_and_visible_text() -> None:
    title, text = parse_live_page(
        "<html><head><title>Hello World</title><style>bad</style></head>"
        "<body><script>ignored()</script><p>Visible body</p></body></html>"
    )

    assert title == "Hello World"
    assert text == "Visible body"


def test_same_root_redirect_passes() -> None:
    probe = _probe("example.com", final_url="https://www.example.com/landing")

    verdict = evaluate_live_check("example.com", probe, config=_config())

    assert verdict.passed is True
    assert verdict.class_ == "content"
    assert verdict.details["final_host"] == "www.example.com"


def test_off_root_redirect_fails_unconditionally() -> None:
    probe = _probe("example.com", final_url="https://example.co.uk/")

    verdict = evaluate_live_check("example.com", probe, config=_config())

    assert verdict.passed is False
    assert verdict.class_ == "redirect_offsite"
    assert "example.co.uk" in verdict.reason


def test_nxdomain_is_a_neutral_pass() -> None:
    probe = _probe(
        "gone.com", final_url=None, status=None, dns=LiveDns(False, error="no host", nxdomain=True)
    )

    verdict = evaluate_live_check("gone.com", probe, config=_config())

    assert verdict.passed is True
    assert verdict.class_ == "nxdomain"
    assert verdict.details["neutral"] is True


def test_no_http_is_a_neutral_pass() -> None:
    probe = _probe("offline.com", final_url=None, status=None)

    verdict = evaluate_live_check("offline.com", probe, config=_config())

    assert verdict.passed is True
    assert verdict.class_ == "no_http"
    assert verdict.details["neutral"] is True


def test_parked_page_is_a_neutral_pass() -> None:
    probe = _probe(
        "parked.com",
        final_url="https://parked.com/",
        title="Parked Domain",
        text="This domain is for sale",
    )

    verdict = evaluate_live_check("parked.com", probe, config=_config())

    assert verdict.passed is True
    assert verdict.class_ == "parked"
    assert verdict.details["parking"] is True


def test_toxic_final_host_fails_the_content_check() -> None:
    probe = _probe("example.com", final_url="https://casino.example.com/")

    verdict = evaluate_live_check("example.com", probe, config=_config())

    assert verdict.passed is False
    assert verdict.class_ == "content"
    assert "toxic final host" in verdict.reason


def test_toxic_title_fails_the_content_check() -> None:
    probe = _probe("example.com", title="Best Casino Bonuses")

    verdict = evaluate_live_check("example.com", probe, config=_config())

    assert verdict.passed is False
    assert verdict.class_ == "content"
    assert "toxic title" in verdict.reason


def test_one_body_term_passes_but_the_threshold_terms_fail() -> None:
    one = _probe("example.com", text="casino reviews")
    two = _probe("example.com", text="casino poker betting")

    assert evaluate_live_check("example.com", one, config=_config()).passed is True
    verdict = evaluate_live_check("example.com", two, config=_config())
    assert verdict.passed is False
    assert "toxic body terms" in verdict.reason


def test_malformed_or_empty_page_is_still_content_evidence() -> None:
    probe = _probe("example.com", title="", text="")
    probe.html = "<html><body><p"

    assert evaluate_live_check("example.com", probe, config=_config()).passed is True


def test_live_client_follows_redirects_and_records_the_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(
                301,
                headers={"location": "https://www.example.com/"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><head><title>Hi</title></head><body>Hello</body></html>",
            request=request,
        )

    client = LiveClient(
        transport=httpx.MockTransport(handler),
        resolver=lambda host: LiveDns(ok=True, addresses=("1.2.3.4",)),
    )

    probe = client.probe("example.com", _config())

    assert probe.status == 200
    assert probe.final_host == "www.example.com"
    assert probe.title == "Hi"
    assert probe.redirects[0]["status"] == 301


def test_live_client_falls_back_from_https_to_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            raise httpx.ConnectError("tls failed", request=request)
        return httpx.Response(
            200,
            text="<html><head><title>Plain</title></head><body>works</body></html>",
            request=request,
        )

    client = LiveClient(
        transport=httpx.MockTransport(handler),
        resolver=lambda host: LiveDns(ok=True, addresses=("1.2.3.4",)),
    )

    probe = client.probe("example.com", _config())

    assert probe.status == 200
    assert [attempt.url for attempt in probe.attempts] == [
        "https://example.com/",
        "http://example.com/",
    ]
    assert probe.attempts[0].error


def test_live_client_caps_the_body_size() -> None:
    body = "<html><head><title>T</title></head><body>" + ("x" * 200) + "</body></html>"
    client = LiveClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body, request=request)
        ),
        resolver=lambda host: LiveDns(ok=True, addresses=("1.2.3.4",)),
    )

    probe = client.probe("example.com", _config(max_body_bytes=20))

    assert len(probe.html.encode()) <= 20


def test_resolve_host_reports_nxdomain(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)

    result = resolve_host("missing.example")

    assert result.ok is False
    assert result.nxdomain is True


def test_live_queue_requires_a_dr_pass_and_does_not_backfill(tmp_db: Path) -> None:
    _seed_dr_passed(tmp_db, ["ready.com"], dr_checked_at="2025-01-01T00:00:00+00:00")
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("keyword-only.com", "spamzilla", "2026-11-27", {}, {})],
        )
        set_prefilter_results(
            conn,
            {"keyword-only.com": PrefilterResult(True, "ok", {}, "v1", CHECKED_AT)},
        )
        set_keyword_check_results(
            conn,
            {"keyword-only.com": KeywordVerdict(True, "ok", 0.9)},
            model="test-model",
            checked_at=CHECKED_AT,
        )
        set_pipeline_meta(conn, "live_checks_started_at", "2026-01-01T00:00:00+00:00")
        conn.commit()
        candidates = load_live_check_candidates(
            conn,
            since="2026-01-01T00:00:00+00:00",
        )
    finally:
        conn.close()

    assert candidates == []


def test_live_check_pending_stores_a_terminal_verdict_and_evidence(
    tmp_db: Path, tmp_path: Path
) -> None:
    _seed_dr_passed(tmp_db, ["example.com"], dr_checked_at="2026-11-27T00:00:00+00:00")
    client = StubClient(_probe("example.com"))

    checked = live_check_pending(
        tmp_db,
        client=client,
        limit=1,
        raw_root=tmp_path / "raw" / "live",
    )

    row = _live_row(tmp_db, "example.com")
    assert checked == 1
    assert row["live_check_status"] == "passed"
    assert row["live_check_pass"] == 1
    assert row["live_check_class"] == "content"
    assert row["live_check_final_host"] == "example.com"
    assert row["live_check_http_status"] == 200
    assert client.calls == ["example.com"]
    assert list((tmp_path / "raw" / "live" / "example.com").iterdir())


def test_live_check_retries_then_records_terminal_errors(tmp_db: Path) -> None:
    _seed_dr_passed(tmp_db, ["broken.com"], dr_checked_at="2026-11-27T00:00:00+00:00")
    client = StubClient(_probe("broken.com"), error=RuntimeError("dns timeout"))

    first = live_check_pending(tmp_db, client=client, max_attempts=2)
    assert first == 0
    assert _live_row(tmp_db, "broken.com")["live_check_status"] is None

    second = live_check_pending(tmp_db, client=client, max_attempts=2)
    row = _live_row(tmp_db, "broken.com")
    assert second == 0
    assert row["live_check_status"] == "failed"
    assert row["live_check_pass"] == 0
    assert row["live_check_attempts"] == 2
    assert "dns timeout" in row["live_check_reason"]


def test_manual_pass_preserves_failure_and_unblocks_wayback(tmp_db: Path) -> None:
    _seed_dr_passed(tmp_db, ["blocked.com"], dr_checked_at="2026-11-27T00:00:00+00:00")
    conn = connect(tmp_db)
    try:
        set_live_check_results(
            conn,
            {
                "blocked.com": LiveCheckVerdict(
                    False,
                    "content",
                    "content: toxic title (casino)",
                    {"domain": "blocked.com", "status": "failed", "config_version": "v1"},
                )
            },
            checked_at=CHECKED_AT,
        )
        conn.commit()
        assert (
            load_wayback_candidates(
                conn,
                since="2026-01-01T00:00:00+00:00",
            )
            == []
        )
        updated = set_live_check_manual_pass(conn, "blocked.com", checked_at=CHECKED_AT)
        second = set_live_check_manual_pass(conn, "blocked.com", checked_at=CHECKED_AT)
        conn.commit()
        row = _live_row(tmp_db, "blocked.com")
        candidates = load_wayback_candidates(
            conn,
            since="2026-01-01T00:00:00+00:00",
        )
    finally:
        conn.close()

    assert updated == 1
    assert second == 0
    assert row["live_check_status"] == "passed"
    assert row["live_check_class"] == "manual"
    assert row["live_check_manual_pass"] == 1
    assert "manual_pass_original_reason" in row["live_check_details_json"]
    assert [domain for domain, _attempts in candidates] == ["blocked.com"]


def test_wayback_requires_a_live_pass_when_enabled(tmp_db: Path) -> None:
    _seed_dr_passed(tmp_db, ["no-live.com"], dr_checked_at="2026-11-27T00:00:00+00:00")
    conn = connect(tmp_db)
    try:
        assert load_wayback_candidates(conn, since="2026-01-01T00:00:00+00:00") == []
        assert load_wayback_candidates(
            conn,
            since="2026-01-01T00:00:00+00:00",
            require_live=False,
        ) == [("no-live.com", 0)]
    finally:
        conn.close()


def test_wayback_checkpoint_uses_live_checked_at_when_enabled(tmp_db: Path) -> None:
    _seed_dr_passed(
        tmp_db,
        ["old-live.com"],
        dr_checked_at="2026-11-27T00:00:00+00:00",
        live=True,
    )
    conn = connect(tmp_db)
    try:
        conn.execute(
            "UPDATE domains SET live_check_checked_at = ? WHERE domain = ?",
            ("2026-01-01T00:00:00+00:00", "old-live.com"),
        )
        conn.commit()
        candidates = load_wayback_candidates(
            conn,
            since="2026-06-01T00:00:00+00:00",
        )
    finally:
        conn.close()

    assert candidates == []


def test_disabling_live_restores_the_dr_wayback_fallback(tmp_db: Path, tmp_path: Path) -> None:
    _seed_dr_passed(tmp_db, ["fallback.com"], dr_checked_at="2026-11-27T00:00:00+00:00")
    checked = wayback_check_pending(
        tmp_db,
        client=_StubWaybackSource(),
        since="2026-01-01T00:00:00+00:00",
        require_live=False,
        raw_root=tmp_path / "raw" / "wayback",
    )

    assert checked == 1


class _StubWaybackSource:
    def collect(self, domain, config):
        return WaybackEvidence(domain=domain, captures=[], snapshots=[])


def test_live_settings_read_the_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIVE_CHECK_ENABLED", "0")
    monkeypatch.setenv("LIVE_CHECK_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("LIVE_CHECKS_PER_CYCLE", "4")
    monkeypatch.setenv("LIVE_CHECK_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("LIVE_CHECK_MAX_REDIRECTS", "7")
    monkeypatch.setenv("LIVE_CHECK_MAX_BODY_BYTES", "1234")

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.live_check_enabled is False
    assert settings.live_check_max_attempts == 5
    assert settings.live_checks_per_cycle == 4
    assert settings.live_check_timeout_seconds == 3.5
    assert settings.live_check_max_redirects == 7
    assert settings.live_check_max_body_bytes == 1234


def test_live_worker_runs_the_stage_in_the_background(tmp_db: Path, tmp_path: Path) -> None:
    _seed_dr_passed(tmp_db, ["example.com"], dr_checked_at="2026-11-27T00:00:00+00:00")
    worker = LiveCheckWorker(
        tmp_db,
        client=StubClient(_probe("example.com")),
        interval=0.05,
        raw_root=tmp_path / "raw" / "live",
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _live_row(tmp_db, "example.com")["live_check_status"] == "passed":
                break
            time.sleep(0.02)
    finally:
        worker.stop()

    assert _live_row(tmp_db, "example.com")["live_check_status"] == "passed"


def test_collection_still_requires_wayback_after_a_live_pass(tmp_db: Path) -> None:
    _seed_dr_passed(tmp_db, ["example.com"], dr_checked_at="2026-11-27T00:00:00+00:00", live=True)
    conn = connect(tmp_db)
    try:
        assert list_collection_candidates(conn, limit=10, today="2026-11-27") == []
    finally:
        conn.close()
