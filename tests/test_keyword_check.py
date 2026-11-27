"""LLM keyword-reusability checks and the worker stage that applies them."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from expired_domains.config import DEFAULT_KEYWORD_CHECK_MODEL, load_settings
from expired_domains.keyword_check import (
    OpenRouterKeywordChecker,
    batches,
    load_keyword_candidates,
    parse_keyword_checks,
)
from expired_domains.models import DomainObservation, KeywordVerdict, PrefilterResult
from expired_domains.storage.db import (
    connect,
    init_db,
    mark_keyword_checks_processing,
    set_domain_blocks,
    set_domain_niches,
    set_keyword_check_results,
    set_prefilter_results,
    store_domain_observations,
)
from expired_domains.worker import NicheWorker, keyword_check_pending


def _store_prefiltered(tmp_db: Path, domains: list[str]) -> None:
    """Store domains that already passed the local prefilter."""
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation(domain, "spamzilla", "2026-11-27", {}, {}) for domain in domains],
        )
        set_domain_niches(conn, {domain: ("medical", "lexicon") for domain in domains})
        set_prefilter_results(
            conn,
            {
                domain: PrefilterResult(
                    True, "Passed all rules", {}, "v1", "2026-11-27T12:00:00+00:00"
                )
                for domain in domains
            },
        )
        conn.commit()
    finally:
        conn.close()


class StubKeywordChecker:
    """Deterministic stand-in for the OpenRouter keyword checker."""

    def __init__(
        self,
        *,
        reusable: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.reusable = reusable
        self.error = error
        self.calls: list[list[str]] = []

    def check(self, keywords):
        self.calls.append(list(keywords))
        if self.error is not None:
            raise self.error
        return {
            keyword: KeywordVerdict(
                reusable=self.reusable,
                reason="Generic and brand-free",
                confidence=0.95,
            )
            for keyword in keywords
        }


def test_keyword_check_model_defaults_to_gemini_flash_lite(tmp_path: Path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.keyword_check_model == DEFAULT_KEYWORD_CHECK_MODEL
    assert settings.keyword_check_model == "google/gemini-2.5-flash-lite"


def test_keyword_check_model_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYWORD_CHECK_MODEL", "test/keyword-model")

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.keyword_check_model == "test/keyword-model"


def test_load_candidates_only_returns_prefilter_passed_unblocked_domains(
    tmp_db: Path,
) -> None:
    _store_prefiltered(tmp_db, ["ready.com", "blocked.com", "stale.com", "current.com"])
    conn = connect(tmp_db)
    try:
        set_domain_blocks(conn, {"blocked.com": ("gambling", ("casino",))})
        set_prefilter_results(
            conn,
            {
                "blocked.com": PrefilterResult(
                    False, "Hyphenated", {}, "v1", "2026-11-27T12:00:00+00:00"
                )
            },
        )
        set_keyword_check_results(
            conn,
            {"stale.com": KeywordVerdict(True, "Old model", 0.9)},
            model="old-model",
            checked_at="2026-11-27T12:00:00+00:00",
        )
        set_keyword_check_results(
            conn,
            {"current.com": KeywordVerdict(True, "Current model", 0.9)},
            model="test-model",
            checked_at="2026-11-27T12:00:00+00:00",
        )
        conn.commit()
        candidates = load_keyword_candidates(conn, "test-model")
    finally:
        conn.close()

    assert [candidate.domain for candidate in candidates] == ["ready.com", "stale.com"]


def test_batches_split_keywords_into_ten() -> None:
    assert [len(batch) for batch in batches([str(index) for index in range(25)])] == [10, 10, 5]


def test_parse_keyword_checks_accepts_valid_verdicts_and_ignores_malformed_items() -> None:
    content = json.dumps(
        {
            "checks": [
                {
                    "keyword": "BestTravelDeals.com",
                    "reusable": True,
                    "confidence": 1.4,
                    "reason": "Broad travel term",
                    "flags": ["brand"],
                },
                {
                    "keyword": "nike.com",
                    "reusable": False,
                    "confidence": 0.98,
                    "reason": "Trademark",
                    "flags": ["trademark"],
                },
                {"keyword": "unknown.com", "reusable": "maybe", "confidence": 0.5},
                {"keyword": "not-in-batch.com", "reusable": True, "confidence": 0.9},
            ]
        }
    )

    verdicts = parse_keyword_checks(content, ["besttraveldeals.com", "nike.com", "unknown.com"])

    assert verdicts["besttraveldeals.com"].reusable is True
    assert verdicts["besttraveldeals.com"].confidence == 1.0
    assert verdicts["besttraveldeals.com"].flags == ("brand",)
    assert verdicts["nike.com"].reusable is False
    assert verdicts["nike.com"].reason == "Trademark"
    assert "unknown.com" not in verdicts
    assert "not-in-batch.com" not in verdicts


def test_openrouter_keyword_checker_sends_one_batch_and_parses_the_reply() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        content = json.dumps(
            {
                "checks": [
                    {
                        "keyword": "besttraveldeals.com",
                        "reusable": True,
                        "confidence": 0.97,
                        "reason": "Broad travel term",
                        "flags": [],
                    }
                ]
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    checker = OpenRouterKeywordChecker(
        api_key="test-key",
        model="test-model",
        base_url="https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        verdicts = checker.check(["besttraveldeals.com"])
    finally:
        checker.close()

    assert verdicts["besttraveldeals.com"].reusable is True
    assert verdicts["besttraveldeals.com"].confidence == 0.97
    assert str(captured["url"]).endswith("/chat/completions")
    assert captured["authorization"] == "Bearer test-key"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "test-model"
    assert body["temperature"] == 0
    prompt = json.dumps(body["messages"])
    assert "reusable" in prompt
    assert "trademark" in prompt
    assert "besttraveldeals.com" in prompt


def test_openrouter_keyword_checker_without_a_key_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made without an API key")

    checker = OpenRouterKeywordChecker(
        api_key=None,
        model="test-model",
        base_url="https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert checker.check(["besttraveldeals.com"]) == {}
    finally:
        checker.close()


def test_keyword_check_pending_stores_verdicts_and_batches_in_tens(tmp_db: Path) -> None:
    domains = [f"ready{index:02d}.com" for index in range(21)]
    _store_prefiltered(tmp_db, domains)
    checker = StubKeywordChecker()

    updated = keyword_check_pending(tmp_db, checker=checker, model="test-model")

    assert updated == 21
    assert [len(call) for call in checker.calls] == [10, 10, 1]

    conn = connect(tmp_db)
    try:
        stored = {
            row["domain"]: (
                row["keyword_check_status"],
                row["keyword_check_pass"],
                row["keyword_check_model"],
                row["keyword_check_reason"],
                row["keyword_check_confidence"],
            )
            for row in conn.execute(
                "SELECT domain, keyword_check_status, keyword_check_pass, "
                "keyword_check_model, keyword_check_reason, "
                "keyword_check_confidence FROM domains"
            )
        }
    finally:
        conn.close()

    assert stored["ready00.com"] == (
        "reusable",
        1,
        "test-model",
        "Generic and brand-free",
        0.95,
    )
    assert len(stored) == 21


def test_keyword_check_can_limit_the_batches_per_cycle(tmp_db: Path) -> None:
    domains = [f"ready{index:02d}.com" for index in range(21)]
    _store_prefiltered(tmp_db, domains)
    checker = StubKeywordChecker()

    updated = keyword_check_pending(
        tmp_db,
        checker=checker,
        model="test-model",
        max_batches=1,
    )

    conn = connect(tmp_db)
    try:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM domains WHERE keyword_check_status IS NULL"
        ).fetchone()["n"]
    finally:
        conn.close()

    assert updated == 10
    assert pending == 11
    assert [len(call) for call in checker.calls] == [10]


def test_keyword_check_pending_retries_a_failed_batch(tmp_db: Path) -> None:
    _store_prefiltered(tmp_db, ["ready.com"])
    checker = StubKeywordChecker(error=RuntimeError("provider unavailable"))

    assert keyword_check_pending(tmp_db, checker=checker, model="test-model") == 0

    conn = connect(tmp_db)
    try:
        row = conn.execute(
            "SELECT keyword_check_status, keyword_check_pass "
            "FROM domains WHERE domain = 'ready.com'"
        ).fetchone()
    finally:
        conn.close()
    assert row["keyword_check_status"] is None
    assert row["keyword_check_pass"] is None


def test_keyword_check_persists_successful_batches_when_a_later_batch_fails(
    tmp_db: Path,
) -> None:
    domains = [f"ready{index:02d}.com" for index in range(21)]
    _store_prefiltered(tmp_db, domains)

    class FailingSecondBatch(StubKeywordChecker):
        def check(self, keywords):
            if len(self.calls) == 1:
                self.calls.append(list(keywords))
                raise RuntimeError("provider unavailable")
            return super().check(keywords)

    checker = FailingSecondBatch()

    assert keyword_check_pending(tmp_db, checker=checker, model="test-model") == 11

    conn = connect(tmp_db)
    try:
        checked = conn.execute(
            "SELECT COUNT(*) AS n FROM domains WHERE keyword_check_status = 'reusable'"
        ).fetchone()["n"]
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM domains WHERE keyword_check_status IS NULL"
        ).fetchone()["n"]
    finally:
        conn.close()

    assert checked == 11
    assert pending == 10


def test_stale_processing_rows_are_recovered_but_recent_ones_are_skipped(
    tmp_db: Path,
) -> None:
    _store_prefiltered(tmp_db, ["stale.com", "recent.com"])
    conn = connect(tmp_db)
    try:
        mark_keyword_checks_processing(
            conn,
            ["stale.com"],
            model="test-model",
            started_at="2026-11-27T11:00:00+00:00",
        )
        mark_keyword_checks_processing(
            conn,
            ["recent.com"],
            model="test-model",
            started_at="2026-11-27T11:59:00+00:00",
        )
        conn.commit()
        candidates = load_keyword_candidates(
            conn,
            "test-model",
            now=datetime(2026, 11, 27, 12, 0, tzinfo=UTC),
            stale_processing_seconds=600,
        )
    finally:
        conn.close()

    assert [candidate.domain for candidate in candidates] == ["stale.com"]


def test_niche_worker_checks_keywords_after_the_prefilter(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("bestmedicalclinic.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("cheaptraveldeals.com", "spamzilla", "2026-11-27", {}, {}),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    checker = StubKeywordChecker()
    worker = NicheWorker(
        tmp_db,
        interval=0.1,
        keyword_checker=checker,
        keyword_check_model="test-model",
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            conn = connect(tmp_db)
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) AS n FROM domains WHERE keyword_check_pass IS NULL"
                ).fetchone()["n"]
            finally:
                conn.close()
            if remaining == 0:
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    conn = connect(tmp_db)
    try:
        stored = {
            row["domain"]: (row["prefilter_pass"], row["keyword_check_pass"])
            for row in conn.execute(
                "SELECT domain, prefilter_pass, keyword_check_pass FROM domains"
            )
        }
    finally:
        conn.close()

    assert stored == {
        "bestmedicalclinic.com": (1, 1),
        "cheaptraveldeals.com": (1, 1),
    }
    assert checker.calls


def test_init_db_adds_keyword_check_columns_to_an_older_index(tmp_db: Path) -> None:
    conn = connect(tmp_db)
    try:
        conn.execute(
            "CREATE TABLE domains ("
            "domain TEXT PRIMARY KEY, "
            "first_seen_date TEXT NOT NULL, "
            "last_seen_date TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO domains VALUES ('example.com', '2026-11-27', '2026-11-27')")
        conn.commit()
    finally:
        conn.close()

    init_db(tmp_db)

    conn = connect(tmp_db)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(domains)")}
        stored = conn.execute(
            "SELECT keyword_check_status, keyword_check_pass, keyword_check_reason, "
            "keyword_check_details_json, keyword_check_confidence, "
            "keyword_check_model, keyword_check_checked_at FROM domains"
        ).fetchone()
    finally:
        conn.close()

    assert {
        "keyword_check_status",
        "keyword_check_pass",
        "keyword_check_reason",
        "keyword_check_details_json",
        "keyword_check_confidence",
        "keyword_check_model",
        "keyword_check_checked_at",
    } <= columns
    assert tuple(stored) == (None, None, None, None, None, None, None)
