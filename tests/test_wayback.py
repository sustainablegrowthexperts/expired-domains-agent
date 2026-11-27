"""Wayback Machine checks, scoring, and the dedicated worker stage."""

from __future__ import annotations

import time
from pathlib import Path

from expired_domains.config import load_settings
from expired_domains.models import (
    DomainObservation,
    DomainRatingVerdict,
    KeywordVerdict,
    LiveCheckVerdict,
    PrefilterResult,
    WaybackVerdict,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    list_collection_candidates,
    set_dr_check_results,
    set_keyword_check_results,
    set_live_check_results,
    set_pipeline_meta,
    set_prefilter_results,
    set_wayback_check_results,
    set_wayback_manual_pass,
    store_domain_observations,
)
from expired_domains.wayback import (
    WaybackCapture,
    WaybackEvidence,
    WaybackLLMVerdict,
    WaybackSnapshot,
    evaluate_wayback,
    load_wayback_candidates,
    load_wayback_config,
    parse_cdx,
    parse_llm_verdict,
    parse_snapshot,
    select_snapshot_captures,
    wayback_check_pending,
)
from expired_domains.worker import WaybackCheckWorker

CHECKED_AT = "2026-11-27T12:00:00+00:00"
LONG_TEXT = " ".join(["dental clinic patient treatment health care"] * 25)
OTHER_TEXT = " ".join(["travel resort hotel destination booking vacation"] * 25)


class StubSource:
    """Deterministic stand-in for the Internet Archive client."""

    def __init__(self, evidence: WaybackEvidence, error: Exception | None = None) -> None:
        self.evidence = evidence
        self.error = error
        self.calls: list[str] = []

    def collect(self, domain, config):
        self.calls.append(domain)
        if self.error is not None:
            raise self.error
        return self.evidence


class StubLLM:
    def __init__(self, verdict: WaybackLLMVerdict) -> None:
        self.verdict = verdict
        self.calls = 0

    def evaluate(self, domain, snapshots, *, config):
        self.calls += 1
        return self.verdict


def _capture(timestamp: str, *, status: int = 200) -> WaybackCapture:
    return WaybackCapture(
        timestamp=timestamp,
        original=f"http://example.com/{timestamp}",
        status=status,
        mimetype="text/html",
        digest=f"digest-{timestamp}",
    )


def _snapshot(
    timestamp: str,
    text: str = LONG_TEXT,
    *,
    language: str = "en",
    niche: str = "medical",
    parking: bool = False,
    toxic: tuple[str, ...] = (),
) -> WaybackSnapshot:
    return WaybackSnapshot(
        capture=_capture(timestamp),
        http_status=200,
        title="Example page",
        text=text,
        language=language,
        word_count=len(text.split()),
        parking=parking,
        toxic_terms=toxic,
        toxic_categories=("gambling",) if toxic else (),
        niche=niche,
        meta_refresh=False,
        substantive=not parking and len(text.split()) >= 100,
        html="<html></html>",
    )


def _clean_evidence() -> WaybackEvidence:
    timestamps = [
        "20180101000000",
        "20190201000000",
        "20200601000000",
        "20210601000000",
        "20220601000000",
        "20230601000000",
    ]
    captures = [_capture(timestamp) for timestamp in timestamps]
    snapshots = [
        _snapshot(timestamp)
        for timestamp in ("20180101000000", "20190201000000", "20220601000000", "20230601000000")
    ]
    return WaybackEvidence("clean.com", captures, snapshots)


def _seed_dr_passed(db_path: Path, domains: list[str]) -> None:
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
            checked_at=CHECKED_AT,
        )
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
        set_pipeline_meta(conn, "wayback_checks_started_at", "2026-01-01T00:00:00+00:00")
        conn.commit()
    finally:
        conn.close()


def _row(db_path: Path, domain: str):
    conn = connect(db_path)
    try:
        return conn.execute(
            "SELECT wayback_check_status, wayback_check_pass, wayback_score, "
            "wayback_check_reason, wayback_check_source, wayback_check_attempts, "
            "wayback_check_manual_pass, wayback_check_details_json, "
            "wayback_first_capture, wayback_last_capture, "
            "wayback_age_years, wayback_capture_count "
            "FROM domains WHERE domain = ?",
            (domain,),
        ).fetchone()
    finally:
        conn.close()


def test_default_config_weights_total_one_hundred() -> None:
    config = load_wayback_config()

    assert sum(config.weights.values()) == 100
    assert "toxic_terms" in config.critical_checks
    assert "substantive_snapshots" in config.critical_checks


def test_select_snapshot_captures_uses_first_two_and_last_two() -> None:
    captures = [_capture(f"2020010100000{index}") for index in range(6)]

    selected = select_snapshot_captures(captures, max_snapshots=4)

    assert [capture.timestamp for capture in selected] == [
        captures[0].timestamp,
        captures[1].timestamp,
        captures[4].timestamp,
        captures[5].timestamp,
    ]


def test_parse_cdx_skips_the_header_and_ignores_bad_rows() -> None:
    payload = [
        ["timestamp", "original", "statuscode", "mimetype", "digest"],
        ["20200101000000", "http://example.com/", "200", "text/html", "abc"],
        ["bad-row"],
    ]

    captures = parse_cdx(payload)

    assert len(captures) == 1
    assert captures[0].timestamp == "20200101000000"
    assert captures[0].status == 200


def test_parse_snapshot_extracts_text_language_toxicity_and_niche() -> None:
    body = (
        "<html lang='en'><head><title>Example Dental Clinic</title>"
        "<meta http-equiv='refresh' content='0;url=/other'></head>"
        f"<body>{LONG_TEXT} casino</body></html>"
    )

    snapshot = parse_snapshot(
        _capture("20200101000000"),
        status=200,
        body=body,
        config=load_wayback_config(),
    )

    assert snapshot.title == "Example Dental Clinic"
    assert snapshot.language == "en"
    assert snapshot.word_count >= 100
    assert "casino" in snapshot.toxic_terms
    assert snapshot.niche == "medical"
    assert snapshot.meta_refresh is True
    assert snapshot.substantive is False


def test_clean_archive_passes_with_a_high_score() -> None:
    verdict = evaluate_wayback("clean.com", _clean_evidence())

    assert verdict.passed is True
    assert verdict.score >= 50
    assert verdict.source == "lexicon"
    assert verdict.details["capture_count"] == 6
    assert verdict.details["first_capture"] == "20180101000000"


def test_thin_snapshots_are_a_critical_failure() -> None:
    evidence = _clean_evidence()
    evidence.snapshots = [
        _snapshot(timestamp, text="short page")
        for timestamp in ("20180101000000", "20190201000000", "20220601000000", "20230601000000")
    ]

    verdict = evaluate_wayback("thin.com", evidence)

    assert verdict.passed is False
    assert "substantive_snapshots" in verdict.details["critical_failures"]
    assert "recent_snapshot_quality" in verdict.details["critical_failures"]


def test_toxic_terms_are_a_critical_failure_even_with_a_high_score() -> None:
    evidence = _clean_evidence()
    evidence.snapshots[-1] = _snapshot(
        "20230601000000",
        text=LONG_TEXT + " casino slots sportsbook",
        toxic=("casino", "slots", "sportsbook"),
    )

    verdict = evaluate_wayback("toxic.com", evidence)

    assert verdict.passed is False
    assert "toxic_terms" in verdict.details["critical_failures"]
    assert verdict.details["score"] > 50


def test_weak_toxic_signal_is_resolved_by_the_llm() -> None:
    evidence = _clean_evidence()
    evidence.snapshots[-1] = _snapshot(
        "20230601000000",
        text=LONG_TEXT + " casino",
        toxic=("casino",),
    )
    llm = StubLLM(WaybackLLMVerdict((), False, False, True, False, 0.9, "legitimate clinic"))

    verdict = evaluate_wayback("clean.com", evidence, llm=llm)

    assert llm.calls == 1
    assert verdict.passed is True
    assert verdict.source == "llm"
    assert verdict.details["llm"]["confidence"] == 0.9


def test_llm_confirmation_of_one_toxic_term_fails_the_critical_check() -> None:
    evidence = _clean_evidence()
    evidence.snapshots[-1] = _snapshot(
        "20230601000000",
        text=LONG_TEXT + " casino",
        toxic=("casino",),
    )
    llm = StubLLM(WaybackLLMVerdict(("casino",), False, False, True, False, 0.9, "gambling"))

    verdict = evaluate_wayback("clean.com", evidence, llm=llm)

    assert verdict.passed is False
    assert verdict.source == "llm"
    assert "toxic_terms" in verdict.details["critical_failures"]


def test_weak_toxic_signal_fails_when_no_llm_is_configured() -> None:
    evidence = _clean_evidence()
    evidence.snapshots[-1] = _snapshot(
        "20230601000000",
        text=LONG_TEXT + " casino",
        toxic=("casino",),
    )

    verdict = evaluate_wayback("clean.com", evidence, llm=None)

    assert verdict.passed is False
    assert "toxic_terms" in verdict.details["critical_failures"]
    assert "no LLM configured" in verdict.details["checks"][6]["reason"]


def test_parse_llm_verdict_accepts_fenced_json() -> None:
    verdict = parse_llm_verdict(
        """```json
        {"toxic_terms": [], "repurposed": false, "language_changed": false,
         "topic_stable": true, "parking": false, "confidence": 0.8,
         "reason": "consistent clinic", "risk_flags": []}
        ```"""
    )

    assert verdict.repurposed is False
    assert verdict.topic_stable is True
    assert verdict.confidence == 0.8


def test_load_wayback_candidates_requires_a_dr_pass(tmp_db: Path) -> None:
    _seed_dr_passed(tmp_db, ["ready.com"])
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
        conn.commit()
        candidates = load_wayback_candidates(conn)
    finally:
        conn.close()

    assert [domain for domain, _attempts in candidates] == ["ready.com"]


def test_wayback_check_pending_stores_a_terminal_verdict(tmp_db: Path, tmp_path: Path) -> None:
    _seed_dr_passed(tmp_db, ["clean.com"])
    source = StubSource(_clean_evidence())

    checked = wayback_check_pending(
        tmp_db,
        client=source,
        limit=1,
        raw_root=tmp_path / "raw" / "wayback",
    )

    row = _row(tmp_db, "clean.com")
    assert checked == 1
    assert row["wayback_check_status"] == "passed"
    assert row["wayback_check_pass"] == 1
    assert row["wayback_check_source"] == "lexicon"
    assert row["wayback_capture_count"] == 6
    assert list((tmp_path / "raw" / "wayback" / "clean.com").iterdir())


def test_wayback_worker_does_not_backfill_older_dr_checks(tmp_db: Path) -> None:
    _seed_dr_passed(tmp_db, ["old.com"])
    conn = connect(tmp_db)
    try:
        with conn:
            set_pipeline_meta(conn, "wayback_checks_started_at", "2027-01-01T00:00:00+00:00")
    finally:
        conn.close()

    checked = wayback_check_pending(tmp_db, client=StubSource(_clean_evidence()))

    assert checked == 0
    assert _row(tmp_db, "old.com")["wayback_check_status"] is None


def test_wayback_check_retries_and_then_records_terminal_errors(tmp_db: Path) -> None:
    _seed_dr_passed(tmp_db, ["broken.com"])
    source = StubSource(_clean_evidence(), error=RuntimeError("archive offline"))

    first = wayback_check_pending(tmp_db, client=source, max_attempts=2)
    assert first == 0
    assert _row(tmp_db, "broken.com")["wayback_check_status"] is None

    second = wayback_check_pending(tmp_db, client=source, max_attempts=2)
    row = _row(tmp_db, "broken.com")
    assert second == 0
    assert row["wayback_check_status"] == "failed"
    assert row["wayback_check_pass"] == 0
    assert row["wayback_check_attempts"] == 2
    assert "archive offline" in row["wayback_check_reason"]


def test_manual_pass_preserves_the_original_failure_and_allows_collection(
    tmp_db: Path,
) -> None:
    _seed_dr_passed(tmp_db, ["broken.com"])
    conn = connect(tmp_db)
    try:
        set_wayback_check_results(
            conn,
            {
                "broken.com": WaybackVerdict(
                    False,
                    0,
                    "error: archive offline",
                    "lexicon",
                    {
                        "domain": "broken.com",
                        "status": "failed",
                        "score": 0,
                        "config_version": "v1",
                    },
                )
            },
            checked_at=CHECKED_AT,
        )
        conn.commit()
        updated = set_wayback_manual_pass(conn, "broken.com", checked_at=CHECKED_AT)
        second = set_wayback_manual_pass(conn, "broken.com", checked_at=CHECKED_AT)
        conn.commit()
        row = _row(tmp_db, "broken.com")
        candidates = list_collection_candidates(conn, limit=10, today="2026-11-27")
    finally:
        conn.close()

    assert updated == 1
    assert second == 0
    assert row["wayback_check_status"] == "passed"
    assert row["wayback_check_pass"] == 1
    assert row["wayback_check_source"] == "manual"
    assert row["wayback_check_reason"] == "Manually passed"
    assert row["wayback_check_manual_pass"] == 1
    assert "manual_pass_original_reason" in row["wayback_check_details_json"]
    assert [candidate["domain"] for candidate in candidates] == ["broken.com"]


def test_collection_requires_a_wayback_pass(tmp_db: Path) -> None:
    _seed_dr_passed(tmp_db, ["clean.com"])
    conn = connect(tmp_db)
    try:
        from expired_domains.storage.db import list_collection_candidates

        assert list_collection_candidates(conn, limit=10, today="2026-11-27") == []
    finally:
        conn.close()

    wayback_check_pending(tmp_db, client=StubSource(_clean_evidence()), limit=1)

    conn = connect(tmp_db)
    try:
        candidates = list_collection_candidates(conn, limit=10, today="2026-11-27")
    finally:
        conn.close()

    assert [row["domain"] for row in candidates] == ["clean.com"]


def test_settings_read_wayback_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WAYBACK_CHECKS_ENABLED", "0")
    monkeypatch.setenv("WAYBACK_CHECK_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("WAYBACK_CHECKS_PER_CYCLE", "3")
    monkeypatch.setenv("WAYBACK_RATE_PER_SEC", "0.25")
    monkeypatch.setenv("WAYBACK_LLM_MODEL", "test/model")

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.wayback_checks_enabled is False
    assert settings.wayback_check_max_attempts == 5
    assert settings.wayback_checks_per_cycle == 3
    assert settings.wayback_rate_per_sec == 0.25
    assert settings.wayback_llm_model == "test/model"


def test_wayback_worker_runs_the_stage_in_the_background(tmp_db: Path, tmp_path: Path) -> None:
    _seed_dr_passed(tmp_db, ["clean.com"])
    worker = WaybackCheckWorker(
        tmp_db,
        client=StubSource(_clean_evidence()),
        interval=0.05,
        raw_root=tmp_path / "raw" / "wayback",
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if _row(tmp_db, "clean.com")["wayback_check_status"] == "passed":
                break
            time.sleep(0.02)
    finally:
        worker.stop()

    assert _row(tmp_db, "clean.com")["wayback_check_status"] == "passed"
