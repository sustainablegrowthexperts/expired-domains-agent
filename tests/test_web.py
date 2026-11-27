"""FastAPI routes for the domain-discovery view."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import make_settings
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
    insert_run,
    set_domain_blocks,
    set_dr_check_results,
    set_keyword_check_results,
    set_live_check_results,
    set_prefilter_results,
    set_wayback_check_results,
    store_domain_observations,
)
from expired_domains.web.app import create_app


def _client(tmp_path: Path, tmp_db: Path) -> TestClient:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("example.com", "spamzilla", "2026-11-26", {}, {}),
                DomainObservation("other.com", "godaddy_auctions", "2026-11-26", {}, {}),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    return TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )


def test_index_serves_the_discovery_view(tmp_path: Path, tmp_db: Path) -> None:
    response = _client(tmp_path, tmp_db).get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "example.com" in response.text
    assert "Pending classification" in response.text
    assert "SpamZilla" in response.text


def test_legacy_index_without_niche_columns_still_serves_the_discovery_view(
    tmp_path: Path, tmp_db: Path
) -> None:
    conn = connect(tmp_db)
    try:
        conn.executescript(
            """
            CREATE TABLE domains (
                domain           TEXT PRIMARY KEY,
                first_seen_date  TEXT NOT NULL,
                last_seen_date   TEXT NOT NULL
            );
            CREATE TABLE domain_observations (
                domain          TEXT NOT NULL,
                source          TEXT NOT NULL,
                collected_date  TEXT NOT NULL,
                metrics_json    TEXT NOT NULL,
                raw_json        TEXT NOT NULL,
                PRIMARY KEY (domain, source, collected_date)
            );
            INSERT INTO domains VALUES ('example.com', '2026-11-26', '2026-11-26');
            INSERT INTO domain_observations VALUES (
                'example.com', 'spamzilla', '2026-11-26', '{}', '{}'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )

    page = client.get("/")
    api = client.get("/api/discovery")

    assert page.status_code == 200
    assert "Pending classification" in page.text
    assert api.status_code == 200
    assert api.json()["rows"][0]["niche"] == "pending"


def test_shortlist_route_aliases_the_index(tmp_path: Path, tmp_db: Path) -> None:
    client = _client(tmp_path, tmp_db)

    index = client.get("/")
    shortlist = client.get("/shortlist")

    assert shortlist.status_code == 200
    assert shortlist.text == index.text


def test_discovery_api_returns_rows_and_metadata(tmp_path: Path, tmp_db: Path) -> None:
    response = _client(tmp_path, tmp_db).get("/api/discovery")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert {row["domain"] for row in payload["rows"]} == {"example.com", "other.com"}
    assert [source["slug"] for source in payload["meta"]["sources"]] == [
        "spamzilla",
        "godaddy_auctions",
    ]
    assert [source["last_import"] for source in payload["meta"]["sources"]] == [
        "2026-11-26",
        "2026-11-26",
    ]


def test_discovery_api_filters_by_source_and_query(tmp_path: Path, tmp_db: Path) -> None:
    client = _client(tmp_path, tmp_db)

    by_source = client.get("/api/discovery", params={"source": "spamzilla"}).json()
    by_query = client.get("/api/discovery", params={"q": "EXAMPLE"}).json()

    assert by_source["count"] == 1
    assert by_source["rows"][0]["domain"] == "example.com"
    assert by_query["count"] == 1
    assert by_query["rows"][0]["domain"] == "example.com"


def test_missing_index_returns_503(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=missing,
            start_worker=False,
        )
    )

    page = client.get("/")
    api = client.get("/api/discovery")

    assert page.status_code == 503
    assert api.status_code == 503
    assert "index not found" in page.json()["detail"]


def test_healthz_reports_whether_the_index_exists(tmp_path: Path, tmp_db: Path) -> None:
    client = _client(tmp_path, tmp_db)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "db_path": str(tmp_db),
        "db_exists": True,
    }


def test_web_page_and_api_expose_prefilter_results(tmp_path: Path, tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation(
                    "example.com",
                    "spamzilla",
                    "2026-11-26",
                    {"price": 42.5, "auction_end_at": "2026-11-28T20:01:00Z"},
                    {},
                )
            ],
        )
        set_prefilter_results(
            conn,
            {
                "example.com": PrefilterResult(
                    False, "Hyphenated", {}, "v1", "2026-11-27T12:00:00+00:00"
                )
            },
        )
        conn.commit()
    finally:
        conn.close()

    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )

    page = client.get("/")
    payload = client.get("/api/discovery").json()

    assert page.status_code == 200
    assert 'data-view="prefilter"' in page.text
    assert 'data-view="keyword-check"' in page.text
    assert 'data-view="dr-check"' in page.text
    assert 'data-view="live-check"' in page.text
    assert 'data-view="wayback-check"' in page.text
    assert 'data-view="data-collection"' in page.text
    assert "Passed pre-filter" in page.text
    assert "Keyword check" in page.text
    assert "Free DR check" in page.text
    assert "Live check" in page.text
    assert "Wayback Machine Checks" in page.text
    assert "Data collection" in page.text
    assert '"writable":true' in page.text
    assert payload["rows"][0]["prefilter"] == {
        "pass": False,
        "reason": "Hyphenated",
        "manual": False,
    }
    assert payload["rows"][0]["keyword_check"] is None
    assert payload["rows"][0]["collection"]["status"] == "queued"
    assert payload["rows"][0]["collection"]["eligible"] is False
    assert payload["rows"][0]["price"] == 42.5
    assert payload["rows"][0]["bid_end_at"] == "2026-11-28T20:01:00Z"
    assert payload["rows"][0]["reached"] == 1
    assert payload["rows"][0]["progress_label"] == "Failed pre-filter"


def test_web_page_and_api_expose_collected_ahrefs_data(tmp_path: Path, tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation(
                    "example.com",
                    "spamzilla",
                    "2026-11-26",
                    {"price": 42.5},
                    {},
                )
            ],
        )
        set_prefilter_results(
            conn,
            {
                "example.com": PrefilterResult(
                    True, "Passed all rules", {}, "v1", "2026-11-27T12:00:00+00:00"
                )
            },
        )
        set_keyword_check_results(
            conn,
            {"example.com": KeywordVerdict(True, "reusable", 0.93)},
            model="test-model",
            checked_at="2026-11-27T12:00:00+00:00",
        )
        set_dr_check_results(
            conn,
            {"example.com": DomainRatingVerdict(42.0)},
            dr_floor=10,
            checked_at="2026-11-27T12:00:00+00:00",
        )
        insert_run(
            conn,
            run_id="example.com:20261127T130000Z:ahrefs",
            domain="example.com",
            mode="domain",
            tier="standard",
            prefilter_batch=None,
            drop_date=None,
            drop_date_source="unknown",
            fetched_at="2026-11-27T13:00:00+00:00",
            config_json="{}",
            coverage_json=json.dumps(
                {
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
            ),
            units_actual=3_400,
            error=None,
        )
        conn.commit()
    finally:
        conn.close()

    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )

    page = client.get("/")
    payload = client.get("/api/discovery").json()
    detail = client.get("/api/domains/example.com")
    missing = client.get("/api/domains/missing.com")

    assert page.status_code == 200
    assert 'data-view="data-collection"' in page.text
    assert "Data collection" in page.text
    assert 'id="detail-data"' in page.text
    assert '"domain":"example.com"' in page.text
    assert "__DETAILS_DATA__" not in page.text
    row = payload["rows"][0]
    assert row["collection"]["status"] == "collected"
    assert row["collection"]["eligible"] is True
    assert row["collection"]["live_links"] == 1159
    assert row["collection"]["live_refdomains"] == 992
    assert row["collection"]["units"] == 3_400
    assert payload["meta"]["data_collection"]["eligible"] == 1
    assert payload["meta"]["data_collection"]["collected"] == 1
    assert payload["meta"]["data_collection"]["queued"] == 0
    assert payload["meta"]["data_collection"]["spent"] == 3_400
    assert payload["meta"]["data_collection"]["monthly_cap"] == 800_000
    assert detail.status_code == 200
    assert detail.json()["run"]["run_id"] == "example.com:20261127T130000Z:ahrefs"
    assert detail.json()["stats"]["live"] == {"links": 1159, "refdomains": 992}
    assert missing.status_code == 404


def test_manual_pass_endpoint_updates_a_failed_prefilter_row(tmp_path: Path, tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("example.com", "spamzilla", "2026-11-26", {}, {})],
        )
        set_prefilter_results(
            conn,
            {
                "example.com": PrefilterResult(
                    False, "Hyphenated", {}, "v1", "2026-11-27T12:00:00+00:00"
                )
            },
        )
        conn.commit()
    finally:
        conn.close()

    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )

    response = client.post("/api/prefilter/example.com/pass")
    payload = client.get("/api/discovery").json()
    second = client.post("/api/prefilter/example.com/pass")

    assert response.status_code == 200
    assert response.json() == {
        "domain": "example.com",
        "prefilter_pass": True,
        "manual": True,
    }
    row = payload["rows"][0]
    assert row["prefilter"] == {
        "pass": True,
        "reason": "Manually passed",
        "manual": True,
    }
    assert row["progress_label"] == "Passed pre-filter (manual)"
    assert payload["meta"]["prefilter"]["manual"] == 1
    assert second.status_code == 404


def test_wayback_manual_pass_endpoint_updates_a_failed_zero_score_row(
    tmp_path: Path, tmp_db: Path
) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("example.com", "spamzilla", "2026-11-27", {}, {})],
        )
        set_prefilter_results(
            conn,
            {
                "example.com": PrefilterResult(
                    True, "Passed all rules", {}, "v1", "2026-11-27T12:00:00+00:00"
                )
            },
        )
        set_keyword_check_results(
            conn,
            {"example.com": KeywordVerdict(True, "reusable", 0.9)},
            model="test-model",
            checked_at="2026-11-27T12:00:00+00:00",
        )
        set_dr_check_results(
            conn,
            {"example.com": DomainRatingVerdict(30.0)},
            dr_floor=10,
            checked_at="2026-11-27T12:00:00+00:00",
        )
        set_wayback_check_results(
            conn,
            {
                "example.com": WaybackVerdict(
                    False,
                    0,
                    "error: archive offline",
                    "lexicon",
                    {
                        "domain": "example.com",
                        "status": "failed",
                        "score": 0,
                        "config_version": "v1",
                    },
                )
            },
            checked_at="2026-11-27T13:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()

    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )
    response = client.post("/api/wayback/example.com/pass")
    second = client.post("/api/wayback/example.com/pass")
    payload = client.get("/api/discovery").json()

    assert response.status_code == 200
    assert response.json() == {
        "domain": "example.com",
        "wayback_check_pass": True,
        "manual": True,
    }
    assert second.status_code == 404
    row = payload["rows"][0]["wayback_check"]
    assert row["status"] == "passed"
    assert row["passed"] is True
    assert row["manual"] is True
    assert row["source"] == "manual"
    assert row["reason"] == "Manually passed"
    assert payload["rows"][0]["progress_label"] == "Wayback passed (manual)"


def test_live_manual_pass_endpoint_updates_a_failed_row(tmp_path: Path, tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("example.com", "spamzilla", "2026-11-27", {}, {})],
        )
        set_live_check_results(
            conn,
            {
                "example.com": LiveCheckVerdict(
                    False,
                    "content",
                    "content: toxic title (casino)",
                    {
                        "domain": "example.com",
                        "status": "failed",
                        "class": "content",
                        "config_version": "v1",
                    },
                )
            },
            checked_at="2026-11-27T13:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()

    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )
    response = client.post("/api/live/example.com/pass")
    second = client.post("/api/live/example.com/pass")
    payload = client.get("/api/discovery").json()

    assert response.status_code == 200
    assert response.json() == {
        "domain": "example.com",
        "live_check_pass": True,
        "manual": True,
    }
    assert second.status_code == 404
    row = payload["rows"][0]["live_check"]
    assert row["status"] == "passed"
    assert row["passed"] is True
    assert row["manual"] is True
    assert row["class"] == "manual"
    assert row["reason"] == "Manually passed"
    assert payload["rows"][0]["progress_label"] == "Live passed (manual)"


def test_manual_pass_endpoint_rejects_an_unknown_domain(tmp_path: Path, tmp_db: Path) -> None:
    init_db(tmp_db)
    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )

    response = client.post("/api/prefilter/missing.com/pass")

    assert response.status_code == 404


def test_discovery_api_keeps_blocked_rows_out_of_the_main_list(
    tmp_path: Path, tmp_db: Path
) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("example.com", "spamzilla", "2026-11-26", {}, {}),
                DomainObservation("bestonlinecasino.com", "spamzilla", "2026-11-26", {}, {}),
            ],
        )
        set_domain_blocks(conn, {"bestonlinecasino.com": ("gambling", ("casino",))})
        conn.commit()
    finally:
        conn.close()

    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            db_path=tmp_db,
            start_worker=False,
        )
    )

    payload = client.get("/api/discovery").json()

    assert payload["count"] == 1
    assert {row["domain"] for row in payload["rows"]} == {"example.com"}
    assert payload["blocked_count"] == 1
    assert payload["blocked_rows"][0]["domain"] == "bestonlinecasino.com"
    assert payload["blocked_rows"][0]["blocked_category"] == "gambling"
    assert payload["meta"]["total"] == 1
    assert payload["meta"]["blocked_total"] == 1


def test_cli_exposes_the_serve_command() -> None:
    from expired_domains import cli

    args = cli.build_parser().parse_args(["serve", "--host", "0.0.0.0", "--port", "9001"])

    assert args.command == "serve"
    assert args.host == "0.0.0.0"
    assert args.port == 9001
