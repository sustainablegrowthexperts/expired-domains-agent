"""Automatic Ahrefs data collection and its page summary."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from expired_domains.collection import (
    collect_pending,
    collection_budget,
    load_collection_records,
)
from expired_domains.config import load_settings
from expired_domains.models import (
    DomainObservation,
    DomainRatingVerdict,
    KeywordVerdict,
    PrefilterResult,
    RunSummary,
    Units,
    WaybackVerdict,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    insert_run,
    list_collection_candidates,
    set_domain_blocks,
    set_dr_check_results,
    set_keyword_check_results,
    set_prefilter_results,
    set_wayback_check_results,
    store_domain_observations,
)
from expired_domains.storage.parsers import compute_coverage
from expired_domains.worker import NicheWorker

CHECKED_AT = "2026-11-27T10:00:00+00:00"
COVERAGE = {
    "mode": "top_n",
    "stats": {
        "live": 1159,
        "all_time": 5979,
        "live_refdomains": 992,
        "all_time_refdomains": 1599,
    },
    "rows": {"top_dr": 70, "latest": 30, "unique": 96},
    "top_anchors": 50,
    "lexicon_complete": True,
    "history_months": 48,
    "drop_date_source": "unknown",
    "prefilter": None,
}


def _seed(
    tmp_db: Path,
    domains: list[str],
    *,
    reusable: bool = True,
    confidence: float = 0.9,
    prices: dict[str, float] | None = None,
    dr_rating: float | None = 50.0,
) -> None:
    """Store keyword- and DR-passed, prefilter-passed candidates."""
    prices = prices or {}
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation(
                    domain,
                    "spamzilla",
                    "2026-11-27",
                    {"price": prices[domain]} if domain in prices else {},
                    {},
                )
                for domain in domains
            ],
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
            {domain: KeywordVerdict(reusable, "ok", confidence) for domain in domains},
            model="test-model",
            checked_at=CHECKED_AT,
        )
        if dr_rating is not None:
            set_dr_check_results(
                conn,
                {domain: DomainRatingVerdict(dr_rating) for domain in domains},
                dr_floor=10,
                checked_at=CHECKED_AT,
            )
            set_wayback_check_results(
                conn,
                {
                    domain: WaybackVerdict(
                        passed=True,
                        score=80,
                        reason="score 80/100 meets threshold 50",
                        source="lexicon",
                        details={
                            "domain": domain,
                            "status": "passed",
                            "score": 80,
                            "config_version": "v1",
                            "capture_count": 4,
                        },
                    )
                    for domain in domains
                },
                checked_at=CHECKED_AT,
            )
        conn.commit()
    finally:
        conn.close()


def _insert_run(
    conn,
    domain: str,
    *,
    run_id: str,
    fetched_at: str,
    units: int = 3400,
    error: str | None = None,
) -> None:
    insert_run(
        conn,
        run_id=run_id,
        domain=domain,
        mode="domain",
        tier="standard",
        prefilter_batch=None,
        drop_date=None,
        drop_date_source="unknown",
        fetched_at=fetched_at,
        config_json="{}",
        coverage_json=json.dumps(COVERAGE),
        units_actual=units,
        error=error,
    )


def _summary(domain: str, *, units: int = 3400, exit_code: int = 0) -> RunSummary:
    return RunSummary(
        run_id=f"{domain}:20261127T100000Z:ahrefs",
        domain=domain,
        raw_dir=Path("/tmp/raw") / domain,
        requests=[],
        coverage=compute_coverage([]),
        units=Units(actual=units, billed=units),
        error=None,
        exit_code=exit_code,
    )


def test_candidates_only_include_keyword_passed_unblocked_domains(tmp_db: Path) -> None:
    _seed(tmp_db, ["ready.com", "blocked.com", "keyword-failed.com"])
    conn = connect(tmp_db)
    try:
        set_domain_blocks(conn, {"blocked.com": ("gambling", ("casino",))})
        set_keyword_check_results(
            conn,
            {"keyword-failed.com": KeywordVerdict(False, "too specific", 0.8)},
            model="test-model",
            checked_at=CHECKED_AT,
        )
        conn.commit()
        candidates = list_collection_candidates(conn, limit=10, today="2026-11-27")
    finally:
        conn.close()

    assert [row["domain"] for row in candidates] == ["ready.com"]


def test_candidates_require_a_dr_pass(tmp_db: Path) -> None:
    _seed(tmp_db, ["dr-passed.com", "keyword-only.com"], dr_rating=None)
    conn = connect(tmp_db)
    try:
        set_dr_check_results(
            conn,
            {"dr-passed.com": DomainRatingVerdict(40.0)},
            dr_floor=10,
            checked_at=CHECKED_AT,
        )
        set_wayback_check_results(
            conn,
            {
                "dr-passed.com": WaybackVerdict(
                    True,
                    80,
                    "passed",
                    "lexicon",
                    {
                        "domain": "dr-passed.com",
                        "status": "passed",
                        "score": 80,
                        "config_version": "v1",
                        "capture_count": 4,
                    },
                )
            },
            checked_at=CHECKED_AT,
        )
        conn.commit()
        candidates = list_collection_candidates(conn, limit=10, today="2026-11-27")
    finally:
        conn.close()

    assert [row["domain"] for row in candidates] == ["dr-passed.com"]


def test_candidates_can_skip_the_wayback_gate_when_disabled(tmp_db: Path) -> None:
    _seed(tmp_db, ["ready.com"], dr_rating=50.0)
    conn = connect(tmp_db)
    try:
        conn.execute("UPDATE domains SET wayback_check_pass = NULL")
        conn.commit()
        assert list_collection_candidates(conn, limit=10, today="2026-11-27") == []
        candidates = list_collection_candidates(
            conn,
            limit=10,
            today="2026-11-27",
            require_wayback=False,
        )
    finally:
        conn.close()

    assert [row["domain"] for row in candidates] == ["ready.com"]


def test_candidates_skip_successful_runs_but_retry_old_failures(tmp_db: Path) -> None:
    _seed(tmp_db, ["done.com", "retry.com", "today-failed.com"])
    conn = connect(tmp_db)
    try:
        _insert_run(
            conn,
            "done.com",
            run_id="done.com:20261126T100000Z:ahrefs",
            fetched_at="2026-11-26T10:00:00+00:00",
        )
        _insert_run(
            conn,
            "retry.com",
            run_id="retry.com:20261126T100000Z:ahrefs",
            fetched_at="2026-11-26T10:00:00+00:00",
            error='{"stats": "timeout"}',
        )
        _insert_run(
            conn,
            "today-failed.com",
            run_id="today-failed.com:20261127T100000Z:ahrefs",
            fetched_at="2026-11-27T10:00:00+00:00",
            error='{"stats": "timeout"}',
        )
        conn.commit()
        candidates = list_collection_candidates(conn, limit=10, today="2026-11-27")
    finally:
        conn.close()

    assert [row["domain"] for row in candidates] == ["retry.com"]


def test_candidates_are_ranked_by_confidence_then_price(tmp_db: Path) -> None:
    _seed(
        tmp_db,
        ["alpha.com", "beta.com", "gamma.com"],
        prices={"alpha.com": 100, "beta.com": 200, "gamma.com": 50},
    )
    conn = connect(tmp_db)
    try:
        set_keyword_check_results(
            conn,
            {
                "alpha.com": KeywordVerdict(True, "ok", 0.5),
                "beta.com": KeywordVerdict(True, "ok", 0.9),
                "gamma.com": KeywordVerdict(True, "ok", 0.9),
            },
            model="test-model",
            checked_at=CHECKED_AT,
        )
        conn.commit()
        candidates = list_collection_candidates(conn, limit=10, today="2026-11-27")
    finally:
        conn.close()

    assert [row["domain"] for row in candidates] == ["gamma.com", "beta.com", "alpha.com"]


def test_collect_pending_honours_the_monthly_allowance(tmp_db: Path) -> None:
    _seed(tmp_db, ["one.com", "two.com", "three.com"])
    calls: list[str] = []

    def fake(domain: str) -> RunSummary:
        calls.append(domain)
        return _summary(domain)

    collected = collect_pending(
        tmp_db,
        collect=fake,
        monthly_cap=10_000,
        reserve_units=1_000,
        estimated_units=3_400,
        limit=3,
        now=datetime(2026, 11, 27, tzinfo=UTC),
    )

    assert collected == 2
    assert calls == ["one.com", "three.com"]


def test_collect_pending_stops_when_this_month_is_already_near_the_cap(tmp_db: Path) -> None:
    _seed(tmp_db, ["one.com"])
    conn = connect(tmp_db)
    try:
        _insert_run(
            conn,
            "one.com",
            run_id="one.com:20261120T100000Z:ahrefs",
            fetched_at="2026-11-20T10:00:00+00:00",
            units=8_000,
        )
        conn.commit()
    finally:
        conn.close()

    collected = collect_pending(
        tmp_db,
        collect=lambda domain: _summary(domain),
        monthly_cap=10_000,
        reserve_units=1_000,
        estimated_units=3_400,
        limit=1,
        now=datetime(2026, 11, 27, tzinfo=UTC),
    )

    assert collected == 0


def test_collect_pending_respects_the_per_cycle_limit(tmp_db: Path) -> None:
    _seed(tmp_db, ["one.com", "two.com"])
    calls: list[str] = []

    def fake(domain: str) -> RunSummary:
        calls.append(domain)
        return _summary(domain)

    collected = collect_pending(
        tmp_db,
        collect=fake,
        monthly_cap=800_000,
        reserve_units=50_000,
        estimated_units=3_400,
        limit=1,
        now=datetime(2026, 11, 27, tzinfo=UTC),
    )

    assert collected == 1
    assert calls == ["one.com"]


def test_collection_budget_reads_this_months_actual_spend(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        _insert_run(
            conn,
            "one.com",
            run_id="one.com:20261120T100000Z:ahrefs",
            fetched_at="2026-11-20T10:00:00+00:00",
            units=1_200,
        )
        _insert_run(
            conn,
            "old.com",
            run_id="old.com:20261020T100000Z:ahrefs",
            fetched_at="2026-10-20T10:00:00+00:00",
            units=9_000,
        )
        conn.commit()
        budget = collection_budget(
            conn,
            monthly_cap=10_000,
            reserve=1_000,
            now=datetime(2026, 11, 27, tzinfo=UTC),
        )
    finally:
        conn.close()

    assert budget.spent == 1_200
    assert budget.allowed == 9_000
    assert budget.remaining == 7_800


def test_load_collection_records_prefers_the_successful_run(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        _insert_run(
            conn,
            "example.com",
            run_id="example.com:20261126T100000Z:ahrefs",
            fetched_at="2026-11-26T10:00:00+00:00",
        )
        _insert_run(
            conn,
            "example.com",
            run_id="example.com:20261127T100000Z:ahrefs",
            fetched_at="2026-11-27T10:00:00+00:00",
            error='{"stats": "timeout"}',
        )
        conn.commit()
        records = load_collection_records(conn)
    finally:
        conn.close()

    record = records["example.com"]
    assert record.status == "collected"
    assert record.run_id == "example.com:20261126T100000Z:ahrefs"
    assert record.live_links == 1159
    assert record.live_refdomains == 992
    assert record.rows_unique == 96
    assert record.lexicon_complete is True


def test_niche_worker_collects_when_data_collection_is_enabled(tmp_db: Path) -> None:
    _seed(tmp_db, ["ready.com"])
    calls: list[str] = []

    def fake(domain: str) -> RunSummary:
        calls.append(domain)
        return _summary(domain)

    worker = NicheWorker(
        tmp_db,
        interval=0.1,
        collect_data=True,
        collect_fn=fake,
        collection_limit=1,
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not calls:
            time.sleep(0.02)
    finally:
        worker.stop()

    assert calls[0] == "ready.com"


def test_settings_read_data_collection_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_COLLECTION_ENABLED", "yes")
    monkeypatch.setenv("DATA_COLLECTION_MONTHLY_UNITS", "123456")
    monkeypatch.setenv("DATA_COLLECTION_RESERVE_UNITS", "7000")
    monkeypatch.setenv("DATA_COLLECTION_ESTIMATED_UNITS", "2500")
    monkeypatch.setenv("DATA_COLLECTION_MAX_PER_CYCLE", "2")

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.data_collection_enabled is True
    assert settings.data_collection_monthly_units == 123456
    assert settings.data_collection_reserve_units == 7000
    assert settings.data_collection_estimated_units == 2500
    assert settings.data_collection_max_per_cycle == 2
