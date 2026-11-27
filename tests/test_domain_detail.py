"""Per-domain Ahrefs evidence for the data-collection detail modal."""

from __future__ import annotations

import json
from pathlib import Path

from expired_domains.domain_detail import load_domain_detail, load_domain_details
from expired_domains.models import (
    AnchorRow,
    BacklinkRow,
    DomainObservation,
    HistoryRow,
    LiveCheckVerdict,
    WaybackVerdict,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    insert_run,
    set_live_check_results,
    set_wayback_check_results,
    store_domain_observations,
    upsert_anchors,
    upsert_backlinks,
    upsert_history_rows,
    upsert_request,
    upsert_stats,
)

RUN_ID = "clicker.org:20261127T060536Z:ahrefs"


def _store_full_run(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation(
                    "clicker.org",
                    "godaddy_auctions",
                    "2026-11-26",
                    {"price": 227.0, "bid_count": 27, "top_referring_domains": ["a.com"]},
                    {},
                )
            ],
        )
        insert_run(
            conn,
            run_id=RUN_ID,
            domain="clicker.org",
            mode="domain",
            tier="standard",
            prefilter_batch=None,
            drop_date=None,
            drop_date_source="unknown",
            fetched_at="2026-11-27T06:05:36.020429+00:00",
            config_json=json.dumps({"history_months": 48, "top_dr_rows": 70}),
            coverage_json=json.dumps(
                {
                    "mode": "top_n",
                    "rows": {"unique": 100},
                    "top_anchors": 50,
                    "lexicon_complete": True,
                    "stats": {"live": 692, "all_time": 1028},
                }
            ),
            units_actual=2_804,
            error=None,
        )
        upsert_request(
            conn,
            run_id=RUN_ID,
            query_id="stats",
            page=0,
            endpoint="/site-explorer/backlinks-stats",
            params_json="{}",
            row_count=1,
            units_actual=50,
            raw_path=None,
            status=200,
            error=None,
        )
        upsert_request(
            conn,
            run_id=RUN_ID,
            query_id="top_anchors",
            page=0,
            endpoint="/site-explorer/anchors",
            params_json="{}",
            row_count=0,
            units_actual=200,
            raw_path=None,
            status=500,
            error="upstream failure",
        )
        upsert_stats(conn, run_id=RUN_ID, snapshot="live", links=692, refdomains=652)
        upsert_stats(conn, run_id=RUN_ID, snapshot="all_time", links=1_028, refdomains=929)
        upsert_history_rows(
            conn,
            RUN_ID,
            [
                HistoryRow("refdomains", "2026-10-01", 660),
                HistoryRow("refdomains", "2026-11-01", 652),
            ],
        )
        upsert_anchors(
            conn,
            RUN_ID,
            [
                AnchorRow(
                    source_query="top_anchors",
                    anchor_raw="clicker.org",
                    anchor_norm="clicker.org",
                    links_to_target=57,
                    dofollow_links=28,
                    refdomains=None,
                    first_seen=None,
                    last_seen=None,
                    lexicon_terms=["seo"],
                    lexicon_categories=["link_selling_seo"],
                    raw_json="{}",
                )
            ],
        )
        upsert_backlinks(
            conn,
            RUN_ID,
            [
                BacklinkRow(
                    link_hash="hash-1",
                    source_queries=["top_dr_backlinks"],
                    url_from="https://factmags.com/post",
                    url_to="https://clicker.org/",
                    domain_from="factmags.com",
                    root_domain_from="factmags.com",
                    anchor_raw="clicker.org",
                    anchor_norm="clicker.org",
                    is_dofollow=False,
                    domain_rating=73.0,
                    first_seen_link="2026-05-11T16:38:11Z",
                    era=None,
                    raw_json="{}",
                ),
                BacklinkRow(
                    link_hash="hash-2",
                    source_queries=["latest_backlinks"],
                    url_from="https://example.com/post",
                    url_to="https://clicker.org/",
                    domain_from="example.com",
                    root_domain_from="example.com",
                    anchor_raw="clicker",
                    anchor_norm="clicker",
                    is_dofollow=True,
                    domain_rating=20.0,
                    first_seen_link="2026-06-01T00:00:00Z",
                    era=None,
                    raw_json="{}",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_load_domain_detail_returns_the_collected_evidence(tmp_db: Path) -> None:
    _store_full_run(tmp_db)
    conn = connect(tmp_db)
    try:
        detail = load_domain_detail(conn, "clicker.org")
    finally:
        conn.close()

    assert detail is not None
    assert detail["observation"]["source"] == "godaddy_auctions"
    assert detail["observation"]["metrics"]["price"] == 227.0
    assert detail["run"]["run_id"] == RUN_ID
    assert detail["run"]["units"] == 2_804
    assert detail["run"]["requests_ok"] == 1
    assert detail["run"]["requests_total"] == 2
    assert detail["stats"] == {
        "all_time": {"links": 1_028, "refdomains": 929},
        "live": {"links": 692, "refdomains": 652},
    }
    assert detail["history"] == [
        {
            "series": "refdomains",
            "points": [
                {"date": "2026-10-01", "value": 660},
                {"date": "2026-11-01", "value": 652},
            ],
        }
    ]
    assert detail["anchors"] == [
        {
            "source": "top_anchors",
            "anchor": "clicker.org",
            "links": 57,
            "dofollow": 28,
            "refdomains": None,
            "terms": ["seo"],
            "categories": ["link_selling_seo"],
        }
    ]
    assert detail["backlinks"]["total"] == 2
    assert detail["backlinks"]["dofollow"] == 1
    assert detail["backlinks"]["nofollow"] == 1
    assert detail["backlinks"]["unique_root_domains"] == 2
    assert detail["backlinks"]["max_domain_rating"] == 73.0
    assert [item["domain_from"] for item in detail["backlinks"]["items"]] == [
        "factmags.com",
        "example.com",
    ]
    assert detail["requests"][0]["query_id"] == "stats"
    assert detail["requests"][1]["error"] == "upstream failure"


def test_load_domain_detail_without_a_run_keeps_the_observation(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation(
                    "queued.org",
                    "spamzilla",
                    "2026-11-26",
                    {"price": 10.0},
                    {},
                )
            ],
        )
        conn.commit()
        detail = load_domain_detail(conn, "queued.org")
    finally:
        conn.close()

    assert detail is not None
    assert detail["run"] is None
    assert detail["requests"] == []
    assert detail["stats"] == {}
    assert detail["backlinks"]["items"] == []
    assert detail["wayback"] is None


def test_domain_detail_exposes_the_wayback_verdict(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("archived.com", "spamzilla", "2026-11-26", {}, {})],
        )
        set_wayback_check_results(
            conn,
            {
                "archived.com": WaybackVerdict(
                    True,
                    85,
                    "score 85/100 meets threshold 50",
                    "lexicon",
                    {
                        "domain": "archived.com",
                        "status": "passed",
                        "score": 85,
                        "config_version": "v1",
                        "first_capture": "20180101000000",
                        "last_capture": "20230601000000",
                        "age_years": 5.4,
                        "capture_count": 6,
                        "checks": [{"check": "archive_exists", "result": "pass"}],
                        "snapshots": [{"timestamp": "20230601000000", "title": "Clinic"}],
                        "llm": None,
                    },
                )
            },
            checked_at="2026-11-27T13:00:00+00:00",
        )
        conn.commit()
        detail = load_domain_detail(conn, "archived.com")
    finally:
        conn.close()

    assert detail is not None
    assert detail["wayback"]["status"] == "passed"
    assert detail["wayback"]["score"] == 85
    assert detail["wayback"]["first_capture"] == "20180101000000"
    assert detail["wayback"]["checks"][0]["check"] == "archive_exists"
    assert detail["wayback"]["snapshots"][0]["title"] == "Clinic"


def test_domain_detail_exposes_the_live_check_verdict(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("live.com", "spamzilla", "2026-11-26", {}, {})],
        )
        set_live_check_results(
            conn,
            {
                "live.com": LiveCheckVerdict(
                    False,
                    "redirect_offsite",
                    "redirect_offsite: live.com redirected to other.com",
                    {
                        "domain": "live.com",
                        "status": "failed",
                        "class": "redirect_offsite",
                        "neutral": False,
                        "config_version": "v1",
                        "final_host": "other.com",
                        "http_status": 200,
                        "title": "Other",
                        "excerpt": "moved",
                        "toxic_terms": ["casino"],
                        "toxic_categories": ["gambling"],
                        "parking": False,
                        "dns": {
                            "ok": True,
                            "addresses": ["1.2.3.4"],
                            "error": None,
                            "nxdomain": False,
                        },
                        "http": {
                            "attempts": [],
                            "final_url": "https://other.com/",
                            "final_host": "other.com",
                            "status": 200,
                            "redirects": [
                                {
                                    "url": "https://live.com/",
                                    "status": 301,
                                    "location": "https://other.com/",
                                }
                            ],
                            "title": "Other",
                            "excerpt": "moved",
                            "error": None,
                        },
                    },
                )
            },
            checked_at="2026-11-27T13:00:00+00:00",
        )
        conn.commit()
        detail = load_domain_detail(conn, "live.com")
    finally:
        conn.close()

    assert detail is not None
    assert detail["live"]["status"] == "failed"
    assert detail["live"]["class"] == "redirect_offsite"
    assert detail["live"]["final_host"] == "other.com"
    assert detail["live"]["http_status"] == 200
    assert detail["live"]["dns"]["addresses"] == ["1.2.3.4"]
    assert detail["live"]["redirects"][0]["status"] == 301
    assert detail["live"]["title"] == "Other"
    assert detail["live"]["toxic_terms"] == ["casino"]


def test_load_domain_details_skips_unknown_domains(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("clicker.org", "spamzilla", "2026-11-26", {}, {})],
        )
        conn.commit()
        details = load_domain_details(conn, ["clicker.org", "missing.com", "clicker.org"])
        missing = load_domain_detail(conn, "missing.com")
    finally:
        conn.close()

    assert list(details) == ["clicker.org"]
    assert missing is None
