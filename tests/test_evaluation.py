"""Offline tests for the deterministic purchase-evaluation stage."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from conftest import make_settings
from expired_domains.ahrefs.lexicon import load_lexicon
from expired_domains.evaluation import (
    OpenRouterRelevanceLLM,
    _classify_anchor_text,
    _classify_referring_domain,
    _score_components,
    evaluate_domain,
    evaluate_pending,
    evaluation_pending,
    load_evaluation_config,
    parse_relevance_assessment,
    store_evaluation,
)
from expired_domains.models import (
    AnchorRow,
    BacklinkRow,
    DomainObservation,
    DomainRatingVerdict,
    WaybackVerdict,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    insert_run,
    set_domain_niches,
    set_dr_check_results,
    set_wayback_check_results,
    store_domain_observations,
    upsert_anchors,
    upsert_backlinks,
    upsert_stats,
)
from expired_domains.web.app import create_app
from expired_domains.worker import EvaluationWorker

RUN_ID = "clinic.com:20261127T000000Z:ahrefs"
CHECKED_AT = "2026-11-27T00:00:00+00:00"


def _wayback_details(
    *,
    age_years: float = 12.0,
    live_months: int = 60,
    topic_result: str = "pass",
    toxic_categories: list[str] | None = None,
) -> dict:
    checks = [
        {"check": "archive_exists", "result": "pass", "reason": "120 captures"},
        {"check": "archive_span", "result": "pass", "reason": "12.0 years"},
        {
            "check": "live_html_months",
            "result": "pass",
            "reason": f"{live_months} months with live HTML",
        },
        {"check": "substantive_snapshots", "result": "pass", "reason": "2 substantive"},
        {"check": "content_depth", "result": "pass", "reason": "median 500 words"},
        {"check": "toxic_terms", "result": "pass", "reason": "no toxic terms"},
        {"check": "repurposing", "result": "pass", "reason": "not repurposed"},
        {"check": "language_stability", "result": "pass", "reason": "stable (en)"},
        {"check": "topic_stability", "result": topic_result, "reason": "topic stable"},
        {"check": "parking_history", "result": "pass", "reason": "no parking"},
        {"check": "redirect_history", "result": "pass", "reason": "no redirects"},
    ]
    snapshots = [
        {
            "timestamp": "20180101000000",
            "title": "Clinic",
            "excerpt": "A local medical clinic.",
            "niche": "medical",
            "language": "en",
            "substantive": True,
            "parking": False,
            "toxic_categories": toxic_categories or [],
            "word_count": 500,
        },
        {
            "timestamp": "20240101000000",
            "title": "Clinic",
            "excerpt": "A local medical clinic.",
            "niche": "medical",
            "language": "en",
            "substantive": True,
            "parking": False,
            "toxic_categories": toxic_categories or [],
            "word_count": 500,
        },
    ]
    return {
        "first_capture": "20140101000000",
        "last_capture": "20260101000000",
        "age_years": age_years,
        "capture_count": 120,
        "checks": checks,
        "snapshots": snapshots,
        "llm": None,
    }


def _seed_domain(
    tmp_db: Path,
    *,
    domain: str = "clinic.com",
    niche: str = "medical",
    dr: float = 20.0,
    price: float | None = 1.0,
    anchors: list[AnchorRow] | None = None,
    backlinks: list[BacklinkRow] | None = None,
    wayback: bool = True,
    toxic_categories: list[str] | None = None,
) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        metrics = {"price": price} if price is not None else {}
        store_domain_observations(
            conn,
            [DomainObservation(domain, "godaddy_auctions", "2026-11-26", metrics, {})],
        )
        set_domain_niches(conn, {domain: (niche, "lexicon")})
        set_dr_check_results(
            conn,
            {domain: DomainRatingVerdict(rating=dr)},
            dr_floor=10,
            checked_at=CHECKED_AT,
        )
        insert_run(
            conn,
            run_id=RUN_ID,
            domain=domain,
            mode="domain",
            tier="standard",
            prefilter_batch=None,
            drop_date=None,
            drop_date_source="unknown",
            fetched_at="2026-11-27T00:00:00+00:00",
            config_json=json.dumps({"history_months": 48}),
            coverage_json=json.dumps({"mode": "top_n"}),
            units_actual=3_000,
            error=None,
        )
        upsert_stats(conn, run_id=RUN_ID, snapshot="live", links=100, refdomains=50)
        upsert_anchors(
            conn,
            RUN_ID,
            anchors
            or [
                AnchorRow(
                    source_query="top_anchors",
                    anchor_raw="clinic",
                    anchor_norm="clinic",
                    links_to_target=10,
                    dofollow_links=10,
                    refdomains=None,
                    first_seen=None,
                    last_seen=None,
                    lexicon_terms=[],
                    lexicon_categories=[],
                    raw_json="{}",
                )
            ],
        )
        upsert_backlinks(
            conn,
            RUN_ID,
            backlinks
            or [
                BacklinkRow(
                    link_hash="hash-1",
                    source_queries=["top_dr_backlinks"],
                    url_from="https://github.com/example",
                    url_to="https://clinic.com/",
                    domain_from="github.com",
                    root_domain_from="github.com",
                    anchor_raw="clinic",
                    anchor_norm="clinic",
                    is_dofollow=True,
                    domain_rating=90.0,
                    first_seen_link="2025-01-01T00:00:00Z",
                    era=None,
                    raw_json="{}",
                ),
                BacklinkRow(
                    link_hash="hash-2",
                    source_queries=["top_dr_backlinks"],
                    url_from="https://www.harvard.edu/example",
                    url_to="https://clinic.com/",
                    domain_from="www.harvard.edu",
                    root_domain_from="harvard.edu",
                    anchor_raw="clinic",
                    anchor_norm="clinic",
                    is_dofollow=True,
                    domain_rating=80.0,
                    first_seen_link="2025-01-01T00:00:00Z",
                    era=None,
                    raw_json="{}",
                ),
                BacklinkRow(
                    link_hash="hash-3",
                    source_queries=["top_dr_backlinks"],
                    url_from="https://www.mdpi.com/example",
                    url_to="https://clinic.com/",
                    domain_from="www.mdpi.com",
                    root_domain_from="mdpi.com",
                    anchor_raw="clinic",
                    anchor_norm="clinic",
                    is_dofollow=True,
                    domain_rating=70.0,
                    first_seen_link="2025-01-01T00:00:00Z",
                    era=None,
                    raw_json="{}",
                ),
            ],
        )
        if wayback:
            set_wayback_check_results(
                conn,
                {
                    domain: WaybackVerdict(
                        True,
                        100,
                        "score 100/100 meets threshold 50",
                        "lexicon",
                        _wayback_details(toxic_categories=toxic_categories),
                    )
                },
                checked_at=CHECKED_AT,
            )
        conn.commit()
    finally:
        conn.close()


def test_config_loads_the_documented_weights() -> None:
    config = load_evaluation_config()
    assert config.version == "v1"
    assert config.weights() == {
        "authority": 25.0,
        "history": 25.0,
        "links": 20.0,
        "relevance": 20.0,
        "price": 10.0,
    }
    assert config.section("thresholds")["buy_min_score"] == 62


def test_anchor_classification_uses_word_boundaries() -> None:
    lexicon = load_lexicon()
    terms, categories = _classify_anchor_text("specialist Seoul", lexicon)
    assert terms == []
    assert categories == []

    terms, categories = _classify_anchor_text("cialis and casino", lexicon)
    assert "cialis" in terms
    assert "casino" in terms
    assert "pharma" in categories
    assert "gambling" in categories


def test_referring_domain_classifier() -> None:
    config = load_evaluation_config()
    section = config.section("links")
    assert _classify_referring_domain("github.com", section) == "trusted"
    assert _classify_referring_domain("www.mdpi.com", section) == "trusted"
    assert _classify_referring_domain("yellowpages.com", section) == "citation"
    assert _classify_referring_domain("seoexpress.store", section) == "spam"
    assert _classify_referring_domain("example.net", section) == "neutral"


def test_happy_path_scores_as_buy(tmp_db: Path) -> None:
    _seed_domain(tmp_db)
    conn = connect(tmp_db)
    try:
        verdict = evaluate_domain(conn, "clinic.com")
    finally:
        conn.close()

    assert verdict is not None
    assert verdict.score == 89.0
    assert verdict.evidence_points == 79.0
    assert verdict.verdict == "Buy"
    assert verdict.recommendation == "Buy"
    assert verdict.components["authority"].value == 0.8
    assert verdict.components["history"].value == 1.0
    assert verdict.components["links"].value == 0.7
    assert verdict.components["relevance"].value == 1.0
    assert [signal["key"] for signal in verdict.signals] == ["live_missing"]


def test_seo_spam_caps_a_high_score_at_maybe(tmp_db: Path) -> None:
    _seed_domain(
        tmp_db,
        anchors=[
            AnchorRow(
                source_query="top_anchors",
                anchor_raw="clinic.com Premium Backlinks SEOExpress.org",
                anchor_norm="clinic.com premium backlinks seoexpress.org",
                links_to_target=100,
                dofollow_links=100,
                refdomains=None,
                first_seen=None,
                last_seen=None,
                lexicon_terms=[],
                lexicon_categories=[],
                raw_json="{}",
            )
        ],
    )
    conn = connect(tmp_db)
    try:
        verdict = evaluate_domain(conn, "clinic.com")
    finally:
        conn.close()

    assert verdict is not None
    assert verdict.score >= 62
    assert verdict.verdict == "Maybe"
    assert any(signal["key"] == "seo_spam" for signal in verdict.signals)
    assert "seo_spam" in verdict.reason


def test_hard_reject_flag_wins_over_a_high_score(tmp_db: Path) -> None:
    _seed_domain(tmp_db, toxic_categories=["gambling"])
    conn = connect(tmp_db)
    try:
        verdict = evaluate_domain(conn, "clinic.com")
    finally:
        conn.close()

    assert verdict is not None
    assert verdict.score >= 62
    assert verdict.verdict == "Reject"
    assert any(signal["key"] == "gambling" for signal in verdict.signals)


def test_missing_price_is_not_scored_as_zero_and_caps_at_maybe(tmp_db: Path) -> None:
    _seed_domain(tmp_db, price=None)
    conn = connect(tmp_db)
    try:
        verdict = evaluate_domain(conn, "clinic.com")
    finally:
        conn.close()

    assert verdict is not None
    assert verdict.components["price"].value is None
    assert verdict.components["price"].points is None
    assert verdict.verdict == "Maybe"
    assert any(signal["key"] == "price_missing" for signal in verdict.signals)


def test_missing_wayback_records_history_as_missing(tmp_db: Path) -> None:
    _seed_domain(tmp_db, wayback=False)
    conn = connect(tmp_db)
    try:
        verdict = evaluate_domain(conn, "clinic.com")
    finally:
        conn.close()

    assert verdict is not None
    assert verdict.components["history"].status == "missing"
    assert "history" in verdict.coverage["missing_components"]
    assert any(signal["key"] == "wayback_unavailable" for signal in verdict.signals)


def test_price_carried_score_uses_evidence_out_of_ninety() -> None:
    config = load_evaluation_config()
    from expired_domains.evaluation import Component

    components = {
        "authority": Component("authority", 0.8, 25, "ok", "DR 20"),
        "history": Component("history", 0.6, 25, "ok", "6 yrs"),
        "links": Component("links", 0.55, 20, "ok", "neutral"),
        "relevance": Component("relevance", 0.35, 20, "ok", "generic"),
        "price": Component("price", 1.0, 10, "ok", "$1"),
    }
    score, evidence, available = _score_components(components, config)
    assert available == 100.0
    assert score == 63.0
    assert evidence == 53.0


def test_store_evaluation_updates_the_domain_row(tmp_db: Path) -> None:
    _seed_domain(tmp_db)
    conn = connect(tmp_db)
    try:
        verdict = evaluate_domain(conn, "clinic.com")
        assert verdict is not None
        assert store_evaluation(conn, verdict)
        conn.commit()
        row = conn.execute(
            "SELECT evaluation_verdict, evaluation_score, evaluation_status "
            "FROM domains WHERE domain = 'clinic.com'"
        ).fetchone()
    finally:
        conn.close()

    assert row["evaluation_verdict"] == "Buy"
    assert row["evaluation_score"] == 89.0
    assert row["evaluation_status"] == "complete"


def test_evaluate_pending_processes_each_collected_domain_once(tmp_db: Path) -> None:
    _seed_domain(tmp_db)

    assert evaluate_pending(tmp_db, limit=5) == 1
    assert evaluate_pending(tmp_db, limit=5) == 0

    conn = connect(tmp_db)
    try:
        row = conn.execute(
            "SELECT evaluation_verdict, evaluation_score FROM domains WHERE domain = 'clinic.com'"
        ).fetchone()
    finally:
        conn.close()
    assert row["evaluation_verdict"] == "Buy"
    assert row["evaluation_score"] == 89.0


def test_a_newer_collection_run_requeues_the_domain(tmp_db: Path) -> None:
    _seed_domain(tmp_db)
    assert evaluate_pending(tmp_db, limit=5) == 1

    conn = connect(tmp_db)
    try:
        insert_run(
            conn,
            run_id="clinic.com:20270101T000000Z:ahrefs",
            domain="clinic.com",
            mode="domain",
            tier="standard",
            prefilter_batch=None,
            drop_date=None,
            drop_date_source="unknown",
            fetched_at="2027-01-01T00:00:00+00:00",
            config_json=json.dumps({"history_months": 48}),
            coverage_json=json.dumps({"mode": "top_n"}),
            units_actual=3_000,
            error=None,
        )
        conn.commit()
        pending = evaluation_pending(
            conn,
            config_version=load_evaluation_config().version,
        )
    finally:
        conn.close()

    assert pending == ["clinic.com"]


def test_evaluation_worker_runs_in_the_background(tmp_db: Path) -> None:
    _seed_domain(tmp_db)
    worker = EvaluationWorker(tmp_db, interval=0.1)
    worker.start()
    try:
        deadline = time.monotonic() + 3.0
        verdict = None
        while time.monotonic() < deadline:
            conn = connect(tmp_db)
            try:
                row = conn.execute(
                    "SELECT evaluation_verdict FROM domains WHERE domain = 'clinic.com'"
                ).fetchone()
            finally:
                conn.close()
            verdict = row["evaluation_verdict"] if row is not None else None
            if verdict:
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    assert verdict == "Buy"


def test_evaluation_api_returns_the_verdict(tmp_path: Path, tmp_db: Path) -> None:
    _seed_domain(tmp_db)
    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )
    response = client.get("/api/evaluation/clinic.com")
    assert response.status_code == 200
    payload = response.json()
    assert payload["verdict"] == "Buy"
    assert payload["components"]["relevance"]["value"] == 1.0


def test_relevance_llm_sends_only_the_ambiguous_name_and_snapshots() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        content = json.dumps(
            {
                "topic": "medical clinic",
                "name_class": "primary",
                "snapshots_on_topic": [True],
                "confidence": 0.9,
                "reason": "name and archive agree",
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    llm = OpenRouterRelevanceLLM(
        api_key="test-key",
        model="test-model",
        base_url="https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        assessment = llm.assess(
            "clinic.com",
            "unknown",
            [{"title": "Clinic", "excerpt": "A local medical clinic."}],
        )
    finally:
        llm.close()

    assert assessment is not None
    assert assessment.name_base == 0.55
    assert assessment.on_topic == (True,)
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["temperature"] == 0
    prompt = json.dumps(body["messages"])
    assert "clinic.com" in prompt
    assert "A local medical clinic." in prompt
    assert "authority" in prompt
    assert "backlinks" in prompt


def test_parse_relevance_assessment_validates_shape_and_confidence() -> None:
    payload = json.dumps(
        {
            "topic": "medical clinic",
            "name_class": "primary",
            "snapshots_on_topic": [True, True],
            "confidence": 0.91,
            "reason": "name and archive agree",
        }
    )
    parsed = parse_relevance_assessment(payload, 2)
    assert parsed is not None
    assert parsed.name_base == 0.55
    assert parsed.on_topic == (True, True)

    assert parse_relevance_assessment(payload, 1) is None
    low_confidence = json.dumps(
        {
            "topic": "medical",
            "name_class": "primary",
            "snapshots_on_topic": [True],
            "confidence": 0.2,
        }
    )
    assert parse_relevance_assessment(low_confidence, 1) is None
