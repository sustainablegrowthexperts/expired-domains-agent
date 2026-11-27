"""Free Ahrefs Domain Rating checks and the worker stage that applies them."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from expired_domains.config import (
    DEFAULT_DR_CHECK_MAX_ATTEMPTS,
    DEFAULT_DR_FLOOR,
    load_settings,
)
from expired_domains.dr_check import (
    AhrefsDomainRatingChecker,
    dr_check_pending,
    load_domain_rating_candidates,
    parse_domain_ratings,
)
from expired_domains.models import (
    DomainObservation,
    DomainRatingVerdict,
    KeywordVerdict,
    PrefilterResult,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    set_keyword_check_results,
    set_prefilter_results,
    store_domain_observations,
)
from expired_domains.worker import FreeDRCheckWorker

CHECKED_AT = "2026-11-27T12:00:00+00:00"


class StubDomainRatingChecker:
    """Deterministic stand-in for the free Ahrefs DR endpoint."""

    def __init__(
        self,
        ratings: dict[str, float | None] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.ratings = ratings or {}
        self.error = error
        self.calls: list[list[str]] = []

    def check(self, domains):
        self.calls.append(list(domains))
        if self.error is not None:
            raise self.error
        return {
            domain: DomainRatingVerdict(rating=self.ratings.get(domain), license="test")
            for domain in domains
        }


def _store_keyword_passed(tmp_db: Path, domains: list[str]) -> None:
    """Store domains that already passed the prefilter and keyword check."""
    init_db(tmp_db)
    conn = connect(tmp_db)
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
        conn.commit()
    finally:
        conn.close()


def _row(tmp_db: Path, domain: str):
    conn = connect(tmp_db)
    try:
        return conn.execute(
            "SELECT dr_check_status, dr_check_pass, dr_rating, dr_check_reason, "
            "dr_check_attempts, dr_check_details_json "
            "FROM domains WHERE domain = ?",
            (domain,),
        ).fetchone()
    finally:
        conn.close()


def test_parse_domain_ratings_maps_the_documented_shape() -> None:
    payload = {
        "domain_ratings": [
            {
                "target": "https://www.smoke-fire.com/path",
                "domain_rating": 42,
                "license": "https://ahrefs.com/license",
            },
            {"target": "example.net", "domain_rating": "7.5"},
            {"target": "unknown.org", "domain_rating": 90},
        ]
    }

    verdicts = parse_domain_ratings(payload, ["smoke-fire.com", "example.net"])

    assert verdicts["smoke-fire.com"].rating == 42.0
    assert verdicts["smoke-fire.com"].license == "https://ahrefs.com/license"
    assert verdicts["example.net"].rating == 7.5
    assert "unknown.org" not in verdicts


def test_parse_domain_ratings_accepts_a_bare_target_mapping() -> None:
    verdicts = parse_domain_ratings(
        {"a.com": 12, "b.com": {"domain_rating": 3}},
        ["a.com", "b.com"],
    )

    assert verdicts["a.com"].rating == 12.0
    assert verdicts["b.com"].rating == 3.0


def test_ahrefs_checker_posts_targets_to_the_free_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "domain_rating": {
                    "targets": [
                        {"target": "a.com/", "domain_rating": 30.0},
                        {"target": "b.com/", "domain_rating": 4.0},
                    ],
                    "license": "http://ahrefs.com/legal/domain-rating-license",
                }
            },
        )

    checker = AhrefsDomainRatingChecker(
        api_key="free-key",
        base_url="https://api.ahrefs.com/v3",
        transport=httpx.MockTransport(handler),
    )

    verdicts = checker.check(["a.com", "b.com"])

    assert seen == {
        "method": "POST",
        "path": "/v3/public/domain-rating-free",
        "body": {"targets": ["a.com", "b.com"]},
        "auth": "Bearer free-key",
    }
    assert verdicts["a.com"].rating == 30.0
    assert verdicts["b.com"].rating == 4.0
    assert verdicts["a.com"].license == "http://ahrefs.com/legal/domain-rating-license"


def test_parse_domain_ratings_maps_the_live_nested_shape() -> None:
    payload = {
        "domain_rating": {
            "targets": [
                {"target": "smoke-fire.com/", "domain_rating": 33.0},
                {"target": "example.com/", "domain_rating": 94.0},
            ],
            "license": "http://ahrefs.com/legal/domain-rating-license",
        }
    }

    verdicts = parse_domain_ratings(payload, ["smoke-fire.com", "example.com"])

    assert verdicts["smoke-fire.com"].rating == 33.0
    assert verdicts["example.com"].rating == 94.0
    assert verdicts["smoke-fire.com"].license == "http://ahrefs.com/legal/domain-rating-license"


def test_ahrefs_checker_without_a_key_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("request should not be sent without a key")

    checker = AhrefsDomainRatingChecker(
        api_key=None,
        base_url="https://api.ahrefs.com/v3",
        transport=httpx.MockTransport(handler),
    )

    assert checker.check(["a.com"]) == {}


def test_dr_check_pending_applies_the_floor_and_stores_terminal_verdicts(
    tmp_db: Path,
) -> None:
    _store_keyword_passed(tmp_db, ["pass.com", "fail.com"])
    checker = StubDomainRatingChecker({"pass.com": 25, "fail.com": 4})

    checked = dr_check_pending(tmp_db, checker=checker, dr_floor=10)

    assert checked == 2
    assert [sorted(call) for call in checker.calls] == [["fail.com", "pass.com"]]

    passed = _row(tmp_db, "pass.com")
    assert passed["dr_check_status"] == "passed"
    assert passed["dr_check_pass"] == 1
    assert passed["dr_rating"] == 25
    assert passed["dr_check_reason"] == "DR 25 meets floor 10"
    assert passed["dr_check_attempts"] == 1

    failed = _row(tmp_db, "fail.com")
    assert failed["dr_check_status"] == "failed"
    assert failed["dr_check_pass"] == 0
    assert failed["dr_rating"] == 4
    assert failed["dr_check_reason"] == "value_dr: DR 4 below floor 10"


def test_dr_check_missing_rating_is_a_terminal_failure(tmp_db: Path) -> None:
    _store_keyword_passed(tmp_db, ["unknown.com"])
    checker = StubDomainRatingChecker()

    assert dr_check_pending(tmp_db, checker=checker, dr_floor=10) == 1

    row = _row(tmp_db, "unknown.com")
    assert row["dr_check_status"] == "failed"
    assert row["dr_check_pass"] == 0
    assert row["dr_check_reason"] == "value_dr: no domain rating returned"


def test_dr_check_retries_errors_until_the_attempt_budget_is_spent(tmp_db: Path) -> None:
    _store_keyword_passed(tmp_db, ["flaky.com"])
    checker = StubDomainRatingChecker(error=RuntimeError("provider unavailable"))

    assert dr_check_pending(tmp_db, checker=checker, max_attempts=2) == 0
    first = _row(tmp_db, "flaky.com")
    assert first["dr_check_status"] is None
    assert first["dr_check_attempts"] == 1

    assert dr_check_pending(tmp_db, checker=checker, max_attempts=2) == 0
    second = _row(tmp_db, "flaky.com")
    assert second["dr_check_status"] == "failed"
    assert second["dr_check_pass"] == 0
    assert second["dr_check_attempts"] == 2
    assert "error: provider unavailable" in second["dr_check_reason"]
    details = json.loads(second["dr_check_details_json"])
    assert details["exhausted"] is True

    # A terminal failure is never claimed again.
    assert dr_check_pending(tmp_db, checker=checker, max_attempts=2) == 0
    assert len(checker.calls) == 2


def test_dr_check_reclaims_a_stale_processing_row(tmp_db: Path) -> None:
    _store_keyword_passed(tmp_db, ["stale.com"])
    stale_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="seconds")
    conn = connect(tmp_db)
    try:
        conn.execute(
            "UPDATE domains SET dr_check_status = 'processing', dr_check_checked_at = ? "
            "WHERE domain = 'stale.com'",
            (stale_at,),
        )
        conn.commit()
    finally:
        conn.close()

    checker = StubDomainRatingChecker({"stale.com": 15})
    assert dr_check_pending(tmp_db, checker=checker, dr_floor=10) == 1

    row = _row(tmp_db, "stale.com")
    assert row["dr_check_status"] == "passed"
    assert row["dr_check_attempts"] == 1


def test_dr_candidates_only_include_keyword_passed_domains(tmp_db: Path) -> None:
    _store_keyword_passed(tmp_db, ["ready.com"])
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("pending.com", "spamzilla", "2026-11-27", {}, {})],
        )
        conn.commit()
    finally:
        conn.close()

    conn = connect(tmp_db)
    try:
        candidates = load_domain_rating_candidates(conn)
    finally:
        conn.close()

    assert [candidate.domain for candidate in candidates] == ["ready.com"]


def test_free_dr_worker_runs_only_the_dr_check_stage(tmp_db: Path) -> None:
    _store_keyword_passed(tmp_db, ["ready.com"])
    checker = StubDomainRatingChecker({"ready.com": 22})

    worker = FreeDRCheckWorker(
        tmp_db,
        checker=checker,
        interval=0.1,
        dr_floor=10,
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            row = _row(tmp_db, "ready.com")
            if row["dr_check_status"] == "passed":
                break
            time.sleep(0.02)
    finally:
        worker.stop()

    row = _row(tmp_db, "ready.com")
    assert row["dr_check_status"] == "passed"
    assert row["dr_rating"] == 22
    assert [sorted(call) for call in checker.calls] == [["ready.com"]]


def test_settings_read_dr_check_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AHREFS_FREE_DR_CHECK", "free-key")
    monkeypatch.setenv("DR_FLOOR", "17")
    monkeypatch.setenv("DR_CHECK_MAX_ATTEMPTS", "4")

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.ahrefs_free_dr_check == "free-key"
    assert settings.dr_floor == 17
    assert settings.dr_check_max_attempts == 4


def test_settings_use_dr_defaults(tmp_path: Path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.ahrefs_free_dr_check is None
    assert settings.dr_floor == DEFAULT_DR_FLOOR
    assert settings.dr_check_max_attempts == DEFAULT_DR_CHECK_MAX_ATTEMPTS
