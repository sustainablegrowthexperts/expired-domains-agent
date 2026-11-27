"""Domain-discovery shortlist data loading and HTML rendering."""

from __future__ import annotations

from pathlib import Path

from conftest import make_settings
from expired_domains import cli
from expired_domains.models import (
    DomainObservation,
    DomainRatingVerdict,
    KeywordVerdict,
    PrefilterResult,
)
from expired_domains.prefilter import load_prefilter_config
from expired_domains.shortlist import (
    DiscoveryRecord,
    build_page_data,
    load_discovery_records,
    load_source_imports,
    render_shortlist_html,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    set_domain_blocks,
    set_dr_check_results,
    set_keyword_check_results,
    set_prefilter_manual_pass,
    set_prefilter_results,
    store_domain_observations,
)


def _store(tmp_db: Path, observations: list[DomainObservation]) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(conn, observations)
        conn.commit()
    finally:
        conn.close()


def test_load_discovery_records_keeps_the_latest_observation_per_domain(tmp_db: Path) -> None:
    _store(
        tmp_db,
        [
            DomainObservation("example.com", "spamzilla", "2026-11-26", {}, {}),
            DomainObservation("example.com", "godaddy_auctions", "2026-11-27", {}, {}),
            DomainObservation("example.com", "expireddomains", "2026-11-27", {}, {}),
            DomainObservation("other.com", "expireddomains", "2026-11-25", {}, {}),
        ],
    )
    conn = connect(tmp_db)
    try:
        records = load_discovery_records(conn)
    finally:
        conn.close()

    assert [(record.domain, record.source, record.collected_date) for record in records] == [
        ("example.com", "godaddy_auctions", "2026-11-27"),
        ("other.com", "expireddomains", "2026-11-25"),
    ]


def test_load_discovery_records_reads_an_index_without_niche_columns(tmp_db: Path) -> None:
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

        records = load_discovery_records(conn)
    finally:
        conn.close()

    assert len(records) == 1
    assert records[0].domain == "example.com"
    assert records[0].niche is None
    assert records[0].niche_source is None
    assert records[0].as_row()["niche"] == "pending"
    assert records[0].as_row()["progress_label"] == "Pending classification"


def test_load_source_imports_returns_the_latest_date_per_source(tmp_db: Path) -> None:
    _store(
        tmp_db,
        [
            DomainObservation("alpha.com", "spamzilla", "2026-11-25", {}, {}),
            DomainObservation("beta.com", "spamzilla", "2026-11-27", {}, {}),
            DomainObservation("gamma.com", "godaddy_auctions", "2026-11-26", {}, {}),
        ],
    )
    conn = connect(tmp_db)
    try:
        imports = load_source_imports(conn)
    finally:
        conn.close()

    assert imports == {
        "spamzilla": "2026-11-27",
        "godaddy_auctions": "2026-11-26",
    }


def test_build_page_data_orders_sources_and_marks_unclassified_domains_pending() -> None:
    records = [
        DiscoveryRecord("gamma.com", "godaddy_auctions", "2026-11-26"),
        DiscoveryRecord("alpha.com", "spamzilla", "2026-11-26"),
        DiscoveryRecord("delta.com", "flippa", "2026-11-26"),
        DiscoveryRecord("beta.com", "expireddomains", "2026-11-26"),
    ]

    rows, meta = build_page_data(
        records,
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
        source_last_import={
            "spamzilla": "2026-11-28",
            "godaddy_auctions": "2026-11-27",
        },
    )

    assert [source["slug"] for source in meta["sources"]] == [
        "spamzilla",
        "expireddomains",
        "godaddy_auctions",
        "flippa",
    ]
    assert [source["count"] for source in meta["sources"]] == [1, 1, 1, 1]
    assert [source["last_import"] for source in meta["sources"]] == [
        "2026-11-28",
        "2026-11-26",
        "2026-11-27",
        "2026-11-26",
    ]
    assert meta["total"] == 4
    assert meta["generated_at"] == "2026-11-27T10:53:00Z"
    assert all(
        row["niche"] == "pending"
        and row["niche_label"] == "Pending classification"
        and row["progress_label"] == "Pending classification"
        for row in rows
    )
    assert all(row["reached"] == 0 for row in rows)
    flippa = next(row for row in rows if row["source"] == "flippa")
    assert flippa["source_label"] == "Flippa"
    assert flippa["source_short"] == "Flippa"
    assert flippa["source_icon"] == "FL"


def test_build_page_data_uses_the_stored_niche_and_display_label() -> None:
    rows, meta = build_page_data(
        [DiscoveryRecord("bestmedicalclinic.com", "spamzilla", "2026-11-26", "medical", "lexicon")],
        db_path="data/index.sqlite3",
    )

    assert rows[0]["niche"] == "medical"
    assert rows[0]["niche_label"] == "Medical"
    assert rows[0]["niche_source"] == "lexicon"
    assert {"value": "medical", "label": "Medical"} in meta["niches"]


def test_build_page_data_hides_blocked_domains_from_the_main_view() -> None:
    records = [
        DiscoveryRecord("goodtravel.com", "spamzilla", "2026-11-26", "travel", "lexicon"),
        DiscoveryRecord(
            "bestonlinecasino.com",
            "spamzilla",
            "2026-11-26",
            blocked_category="gambling",
            blocked_terms=("casino", "onlinecasino"),
        ),
    ]

    rows, meta = build_page_data(records, db_path="data/index.sqlite3")

    main_rows = [row for row in rows if not row["blocked"]]
    blocked_rows = [row for row in rows if row["blocked"]]
    assert [row["domain"] for row in main_rows] == ["goodtravel.com"]
    assert meta["total"] == 1
    assert meta["blocked_total"] == 1
    assert [source["count"] for source in meta["sources"]] == [1]
    assert blocked_rows[0]["niche"] == "blocked"
    assert blocked_rows[0]["niche_label"] == "Blocked · Gambling"
    assert blocked_rows[0]["niche_source"] == "blocklist"
    assert blocked_rows[0]["blocked_category"] == "gambling"
    assert blocked_rows[0]["blocked_terms"] == ["casino", "onlinecasino"]
    assert blocked_rows[0]["progress_label"] == "Blocked"
    assert blocked_rows[0]["reached"] == 0


def test_build_page_data_marks_prefiltered_domains_and_counts_results() -> None:
    records = [
        DiscoveryRecord(
            "passed.com",
            "spamzilla",
            "2026-11-26",
            "medical",
            "lexicon",
            prefilter_pass=True,
            prefilter_reason="Passed all rules",
            price=1250.5,
            bid_end_at="2026-11-28T20:01:00Z",
        ),
        DiscoveryRecord(
            "failed.com",
            "spamzilla",
            "2026-11-26",
            "travel",
            "lexicon",
            prefilter_pass=False,
            prefilter_reason="Hyphenated",
        ),
        DiscoveryRecord("pending.com", "spamzilla", "2026-11-26", "legal", "lexicon"),
    ]

    rows, meta = build_page_data(records, db_path="data/index.sqlite3")

    assert rows[0]["prefilter"] == {
        "pass": True,
        "reason": "Passed all rules",
        "manual": False,
    }
    assert rows[0]["reached"] == 1
    assert rows[0]["progress_label"] == "Passed pre-filter"
    assert rows[0]["price"] == 1250.5
    assert rows[0]["bid_end_at"] == "2026-11-28T20:01:00Z"
    assert rows[1]["prefilter"] == {
        "pass": False,
        "reason": "Hyphenated",
        "manual": False,
    }
    assert rows[1]["price"] is None
    assert rows[1]["reached"] == 1
    assert rows[1]["progress_label"] == "Failed pre-filter"
    assert rows[2]["prefilter"] is None
    assert rows[2]["reached"] == 0
    assert meta["prefilter"] == {
        "passed": 1,
        "failed": 1,
        "pending": 1,
        "manual": 0,
        "queue": 1,
    }


def test_build_page_data_counts_the_prefilter_queue() -> None:
    current_version = load_prefilter_config().version
    records = [
        DiscoveryRecord(
            "pending.com",
            "spamzilla",
            "2026-11-26",
            "medical",
            "lexicon",
        ),
        DiscoveryRecord(
            "stale.com",
            "spamzilla",
            "2026-11-26",
            "medical",
            "lexicon",
            prefilter_pass=True,
            prefilter_config_version="old-version",
        ),
        DiscoveryRecord(
            "current.com",
            "spamzilla",
            "2026-11-26",
            "medical",
            "lexicon",
            prefilter_pass=True,
            prefilter_config_version=current_version,
        ),
        DiscoveryRecord("unclassified.com", "spamzilla", "2026-11-26"),
    ]

    _, meta = build_page_data(records, db_path="data/index.sqlite3")

    assert meta["prefilter"]["queue"] == 2


def test_build_page_data_marks_a_manual_prefilter_pass() -> None:
    rows, meta = build_page_data(
        [
            DiscoveryRecord(
                "manually-passed.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=True,
                prefilter_reason="Manually passed",
                prefilter_manual_pass=True,
            )
        ],
        db_path="data/index.sqlite3",
    )

    assert rows[0]["prefilter"] == {
        "pass": True,
        "reason": "Manually passed",
        "manual": True,
    }
    assert rows[0]["progress_label"] == "Passed pre-filter (manual)"
    assert meta["prefilter"] == {
        "passed": 1,
        "failed": 0,
        "pending": 0,
        "manual": 1,
        "queue": 0,
    }


def test_load_discovery_records_reads_manual_prefilter_pass(tmp_db: Path) -> None:
    _store(
        tmp_db,
        [DomainObservation("failed.com", "spamzilla", "2026-11-27", {}, {})],
    )
    conn = connect(tmp_db)
    try:
        with conn:
            set_prefilter_results(
                conn,
                {
                    "failed.com": PrefilterResult(
                        False, "Hyphenated", {}, "v1", "2026-11-27T12:00:00+00:00"
                    )
                },
            )
        with conn:
            set_prefilter_manual_pass(conn, "failed.com")
        records = {record.domain: record for record in load_discovery_records(conn)}
    finally:
        conn.close()

    assert records["failed.com"].prefilter_pass is True
    assert records["failed.com"].prefilter_reason == "Manually passed"
    assert records["failed.com"].prefilter_manual_pass is True


def test_load_discovery_records_reads_price_and_bid_end_from_the_latest_observation(
    tmp_db: Path,
) -> None:
    _store(
        tmp_db,
        [
            DomainObservation(
                "example.com",
                "spamzilla",
                "2026-11-26",
                {"price": 100, "auction_end_at": "2026-11-27T10:00:00Z"},
                {},
            ),
            DomainObservation(
                "example.com",
                "godaddy_auctions",
                "2026-11-27",
                {"price": 1250.5, "auction_end_at": "2026-11-28T20:01:00Z"},
                {},
            ),
            DomainObservation("missing.com", "expireddomains", "2026-11-27", {}, {}),
        ],
    )
    conn = connect(tmp_db)
    try:
        records = {record.domain: record for record in load_discovery_records(conn)}
    finally:
        conn.close()

    assert records["example.com"].price == 1250.5
    assert records["example.com"].bid_end_at == "2026-11-28T20:01:00Z"
    assert records["missing.com"].price is None
    assert records["missing.com"].bid_end_at is None


def test_load_discovery_records_reads_prefilter_columns(tmp_db: Path) -> None:
    _store(
        tmp_db,
        [
            DomainObservation("passed.com", "spamzilla", "2026-11-27", {}, {}),
            DomainObservation("failed.com", "spamzilla", "2026-11-27", {}, {}),
        ],
    )
    conn = connect(tmp_db)
    try:
        with conn:
            set_prefilter_results(
                conn,
                {
                    "passed.com": PrefilterResult(
                        True, "Passed all rules", {}, "v1", "2026-11-27T12:00:00+00:00"
                    ),
                    "failed.com": PrefilterResult(
                        False, "Has digits", {}, "v1", "2026-11-27T12:00:00+00:00"
                    ),
                },
            )
        records = {record.domain: record for record in load_discovery_records(conn)}
    finally:
        conn.close()

    assert records["passed.com"].prefilter_pass is True
    assert records["passed.com"].prefilter_reason == "Passed all rules"
    assert records["failed.com"].prefilter_pass is False
    assert records["failed.com"].prefilter_reason == "Has digits"


def test_build_page_data_marks_keyword_check_results() -> None:
    records = [
        DiscoveryRecord(
            "reusable.com",
            "spamzilla",
            "2026-11-26",
            "medical",
            "lexicon",
            prefilter_pass=True,
            prefilter_reason="Passed all rules",
            keyword_check_pass=True,
            keyword_check_reason="Broad medical term",
            keyword_check_confidence=0.92,
            keyword_check_model="test-model",
            keyword_check_checked_at="2026-11-27T12:00:00+00:00",
        ),
        DiscoveryRecord(
            "brand.com",
            "spamzilla",
            "2026-11-26",
            "travel",
            "lexicon",
            prefilter_pass=True,
            prefilter_reason="Passed all rules",
            keyword_check_pass=False,
            keyword_check_reason="Likely trademark",
            keyword_check_confidence=0.99,
            keyword_check_model="test-model",
            keyword_check_checked_at="2026-11-27T12:00:00+00:00",
        ),
        DiscoveryRecord(
            "waiting.com",
            "spamzilla",
            "2026-11-26",
            "legal",
            "lexicon",
            prefilter_pass=True,
            prefilter_reason="Passed all rules",
        ),
    ]

    rows, meta = build_page_data(records, db_path="data/index.sqlite3")

    assert rows[0]["keyword_check"] == {
        "status": "reusable",
        "reusable": True,
        "reason": "Broad medical term",
        "confidence": 0.92,
        "model": "test-model",
        "checked_at": "2026-11-27T12:00:00+00:00",
    }
    assert rows[0]["reached"] == 2
    assert rows[0]["progress_label"] == "Keyword passed"
    assert rows[1]["keyword_check"]["status"] == "not_reusable"
    assert rows[1]["keyword_check"]["reusable"] is False
    assert rows[1]["progress_label"] == "Keyword failed"
    assert rows[2]["keyword_check"] is None
    assert rows[2]["progress_label"] == "Passed pre-filter"
    assert meta["keyword_check"] == {
        "reusable": 1,
        "not_reusable": 1,
        "processing": 0,
        "pending": 1,
    }


def test_build_page_data_marks_an_in_flight_keyword_check() -> None:
    rows, meta = build_page_data(
        [
            DiscoveryRecord(
                "processing.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=True,
                prefilter_reason="Passed all rules",
                keyword_check_status="processing",
                keyword_check_model="test-model",
                keyword_check_checked_at="2026-11-27T12:00:00+00:00",
            )
        ],
        db_path="data/index.sqlite3",
    )

    assert rows[0]["keyword_check"]["status"] == "processing"
    assert rows[0]["keyword_check"]["reusable"] is None
    assert rows[0]["progress_label"] == "Keyword check processing"
    assert rows[0]["reached"] == 2
    assert meta["keyword_check"]["processing"] == 1
    assert meta["keyword_check"]["pending"] == 0


def test_load_discovery_records_reads_keyword_check_columns(tmp_db: Path) -> None:
    _store(
        tmp_db,
        [DomainObservation("reusable.com", "spamzilla", "2026-11-27", {}, {})],
    )
    conn = connect(tmp_db)
    try:
        with conn:
            set_prefilter_results(
                conn,
                {
                    "reusable.com": PrefilterResult(
                        True, "Passed all rules", {}, "v1", "2026-11-27T12:00:00+00:00"
                    )
                },
            )
        with conn:
            set_keyword_check_results(
                conn,
                {
                    "reusable.com": KeywordVerdict(
                        True,
                        "Broad medical term",
                        0.92,
                        ("brand",),
                    )
                },
                model="test-model",
                checked_at="2026-11-27T12:00:00+00:00",
            )
        records = {record.domain: record for record in load_discovery_records(conn)}
    finally:
        conn.close()

    record = records["reusable.com"]
    assert record.keyword_check_status == "reusable"
    assert record.keyword_check_pass is True
    assert record.keyword_check_reason == "Broad medical term"
    assert record.keyword_check_confidence == 0.92
    assert record.keyword_check_model == "test-model"
    assert record.keyword_check_checked_at == "2026-11-27T12:00:00+00:00"


def test_build_page_data_marks_dr_check_results() -> None:
    records = [
        DiscoveryRecord(
            "passed.com",
            "spamzilla",
            "2026-11-26",
            "medical",
            "lexicon",
            prefilter_pass=True,
            prefilter_reason="Passed all rules",
            keyword_check_pass=True,
            keyword_check_reason="Broad medical term",
            keyword_check_confidence=0.92,
            dr_check_status="passed",
            dr_check_pass=True,
            dr_rating=32.0,
            dr_check_reason="DR 32 meets floor 10",
            dr_check_attempts=1,
            dr_check_checked_at="2026-11-27T12:00:00+00:00",
            live_check_status="passed",
            live_check_pass=True,
            live_check_class="content",
            live_check_reason="content: live page passed",
            live_check_attempts=1,
            live_check_checked_at="2026-11-27T12:30:00+00:00",
            wayback_check_status="passed",
            wayback_check_pass=True,
            wayback_score=85,
            wayback_check_reason="score 85/100 meets threshold 50",
            wayback_check_source="lexicon",
            wayback_check_attempts=1,
            wayback_check_checked_at="2026-11-27T13:00:00+00:00",
        ),
        DiscoveryRecord(
            "failed.com",
            "spamzilla",
            "2026-11-26",
            "travel",
            "lexicon",
            prefilter_pass=True,
            prefilter_reason="Passed all rules",
            keyword_check_pass=True,
            dr_check_status="failed",
            dr_check_pass=False,
            dr_rating=4.0,
            dr_check_reason="value_dr: DR 4 below floor 10",
            dr_check_attempts=1,
        ),
        DiscoveryRecord(
            "waiting.com",
            "spamzilla",
            "2026-11-26",
            "legal",
            "lexicon",
            prefilter_pass=True,
            prefilter_reason="Passed all rules",
            keyword_check_pass=True,
        ),
    ]

    rows, meta = build_page_data(records, db_path="data/index.sqlite3")

    assert rows[0]["dr_check"] == {
        "status": "passed",
        "passed": True,
        "rating": 32.0,
        "reason": "DR 32 meets floor 10",
        "attempts": 1,
        "checked_at": "2026-11-27T12:00:00+00:00",
    }
    assert rows[0]["wayback_check"] == {
        "status": "passed",
        "passed": True,
        "score": 85,
        "reason": "score 85/100 meets threshold 50",
        "source": "lexicon",
        "attempts": 1,
        "checked_at": "2026-11-27T13:00:00+00:00",
        "manual": False,
        "first_capture": "",
        "last_capture": "",
        "age_years": None,
        "capture_count": None,
    }
    assert rows[0]["reached"] == 5
    assert rows[0]["progress_label"] == "Wayback passed"
    assert rows[0]["collection"]["eligible"] is True
    assert rows[1]["dr_check"]["status"] == "failed"
    assert rows[1]["dr_check"]["passed"] is False
    assert rows[1]["progress_label"] == "DR failed"
    assert rows[1]["collection"]["eligible"] is False
    assert rows[2]["dr_check"] is None
    assert rows[2]["progress_label"] == "Keyword passed"
    assert rows[2]["collection"]["eligible"] is False
    assert meta["dr_check"] == {
        "passed": 1,
        "failed": 1,
        "processing": 0,
        "pending": 1,
    }
    assert meta["live_check"] == {
        "passed": 1,
        "failed": 0,
        "processing": 0,
        "pending": 0,
        "enabled": True,
    }
    assert meta["wayback_check"] == {
        "passed": 1,
        "failed": 0,
        "connection_failed": 0,
        "processing": 0,
        "pending": 0,
    }


def test_build_page_data_excludes_prefilter_failed_domains_from_dr_counts() -> None:
    rows, meta = build_page_data(
        [
            DiscoveryRecord(
                "tld.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=False,
                prefilter_reason="TLD not allowed",
                keyword_check_pass=True,
                keyword_check_reason="Broad term",
            )
        ],
        db_path="data/index.sqlite3",
    )

    assert rows[0]["prefilter"]["pass"] is False
    assert rows[0]["keyword_check"]["reusable"] is True
    assert meta["dr_check"] == {
        "passed": 0,
        "failed": 0,
        "processing": 0,
        "pending": 0,
    }


def test_build_page_data_marks_an_in_flight_dr_check() -> None:
    rows, meta = build_page_data(
        [
            DiscoveryRecord(
                "processing.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=True,
                prefilter_reason="Passed all rules",
                keyword_check_pass=True,
                dr_check_status="processing",
                dr_check_attempts=1,
                dr_check_checked_at="2026-11-27T12:00:00+00:00",
            )
        ],
        db_path="data/index.sqlite3",
    )

    assert rows[0]["dr_check"]["status"] == "processing"
    assert rows[0]["dr_check"]["passed"] is None
    assert rows[0]["progress_label"] == "DR check processing"
    assert rows[0]["reached"] == 3
    assert meta["dr_check"]["processing"] == 1
    assert meta["dr_check"]["pending"] == 0


def test_load_discovery_records_reads_dr_check_columns(tmp_db: Path) -> None:
    _store(
        tmp_db,
        [DomainObservation("rated.com", "spamzilla", "2026-11-27", {}, {})],
    )
    conn = connect(tmp_db)
    try:
        with conn:
            set_dr_check_results(
                conn,
                {"rated.com": DomainRatingVerdict(28.0, "https://ahrefs.com/license")},
                dr_floor=10,
                checked_at="2026-11-27T12:00:00+00:00",
            )
        records = {record.domain: record for record in load_discovery_records(conn)}
    finally:
        conn.close()

    record = records["rated.com"]
    assert record.dr_check_status == "passed"
    assert record.dr_check_pass is True
    assert record.dr_rating == 28.0
    assert record.dr_check_reason == "DR 28 meets floor 10"
    assert record.dr_check_attempts == 0
    assert record.dr_check_checked_at == "2026-11-27T12:00:00+00:00"


def test_load_discovery_records_reads_blocked_columns(tmp_db: Path) -> None:
    _store(
        tmp_db,
        [
            DomainObservation("goodtravel.com", "spamzilla", "2026-11-27", {}, {}),
            DomainObservation("bestonlinecasino.com", "spamzilla", "2026-11-27", {}, {}),
        ],
    )
    conn = connect(tmp_db)
    try:
        set_domain_blocks(conn, {"bestonlinecasino.com": ("gambling", ("casino",))})
        conn.commit()
        records = {record.domain: record for record in load_discovery_records(conn)}
    finally:
        conn.close()

    assert records["goodtravel.com"].blocked_category is None
    assert records["bestonlinecasino.com"].blocked_category == "gambling"
    assert records["bestonlinecasino.com"].blocked_terms == ("casino",)


def test_render_shortlist_html_embeds_blocked_rows_for_the_blocked_view() -> None:
    html = render_shortlist_html(
        [
            DiscoveryRecord(
                "bestonlinecasino.com",
                "spamzilla",
                "2026-11-26",
                blocked_category="gambling",
                blocked_terms=("casino",),
            )
        ],
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
    )

    assert '"blocked":true' in html
    assert '"blocked_category":"gambling"' in html
    assert "Blocked · Gambling" in html
    assert "__ROWS_DATA__" not in html


def test_render_shortlist_html_includes_the_prefilter_view_and_reasons() -> None:
    html = render_shortlist_html(
        [
            DiscoveryRecord(
                "failed.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=False,
                prefilter_reason="Hyphenated",
                price=42.5,
                bid_end_at="2026-11-28T20:01:00Z",
            )
        ],
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
    )

    assert 'data-view="prefilter"' in html
    assert '"prefilter":{"pass":false,"reason":"Hyphenated","manual":false}' in html
    assert '"price":42.5' in html
    assert '"bid_end_at":"2026-11-28T20:01:00Z"' in html
    assert '"prefilter":{"passed":0,"failed":1,"pending":0,"manual":0,"queue":0}' in html
    assert '"writable":false' in html
    assert "Passed pre-filter" in html
    assert "Failed pre-filter" in html
    assert 'id="prefilter-reasons"' in html


def test_render_shortlist_html_includes_the_keyword_check_view() -> None:
    html = render_shortlist_html(
        [
            DiscoveryRecord(
                "reusable.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=True,
                prefilter_reason="Passed all rules",
                keyword_check_pass=True,
                keyword_check_reason="Broad medical term",
                keyword_check_confidence=0.92,
                keyword_check_model="test-model",
                keyword_check_checked_at="2026-11-27T12:00:00+00:00",
            )
        ],
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
    )

    assert 'data-view="keyword-check"' in html
    assert "Keyword check" in html
    assert (
        '"keyword_check":{"status":"reusable","reusable":true,"reason":"Broad medical term"' in html
    )
    assert '"keyword_check":{"reusable":1,"not_reusable":0,"processing":0,"pending":0}' in html
    assert "Keyword passed" in html
    assert "… Processing" in html
    assert "data-kres" in html
    assert "KEYWORD_FILTERS" in html
    assert 'id="keyword-live"' in html
    assert 'id="prefilter-live"' in html
    assert 'data-tip="${esc(check.reason)}"' in html
    assert 'id="tip"' in html
    assert "keyword_reason" not in html
    assert '"queue":0' in html


def test_render_shortlist_html_includes_the_dr_check_view() -> None:
    html = render_shortlist_html(
        [
            DiscoveryRecord(
                "rated.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=True,
                prefilter_reason="Passed all rules",
                keyword_check_pass=True,
                keyword_check_reason="Broad medical term",
                keyword_check_confidence=0.92,
                dr_check_status="passed",
                dr_check_pass=True,
                dr_rating=32.0,
                dr_check_reason="DR 32 meets floor 10",
                dr_check_attempts=1,
                dr_check_checked_at="2026-11-27T12:00:00+00:00",
            )
        ],
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
    )

    assert 'data-view="dr-check"' in html
    assert "Free DR check" in html
    assert '"dr_check":{"status":"passed","passed":true,"rating":32.0' in html
    assert '"dr_check":{"passed":1,"failed":0,"processing":0,"pending":0}' in html
    assert "DR passed" in html
    assert "data-dres" in html
    assert "DR_FILTERS" in html
    assert 'id="dr-live"' in html
    assert 'id="dr-count"' in html


def test_render_shortlist_html_includes_the_wayback_check_view() -> None:
    html = render_shortlist_html(
        [
            DiscoveryRecord(
                "archived.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=True,
                prefilter_reason="Passed all rules",
                keyword_check_pass=True,
                dr_check_status="passed",
                dr_check_pass=True,
                live_check_status="passed",
                live_check_pass=True,
                live_check_class="content",
                live_check_reason="content: live page passed",
                live_check_attempts=1,
                live_check_checked_at="2026-11-27T12:30:00+00:00",
                wayback_check_status="passed",
                wayback_check_pass=True,
                wayback_score=85,
                wayback_check_reason="score 85/100 meets threshold 50",
                wayback_check_source="lexicon",
                wayback_check_attempts=1,
                wayback_check_checked_at="2026-11-27T13:00:00+00:00",
            )
        ],
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
    )

    assert 'data-view="wayback-check"' in html
    assert "Wayback Machine Checks" in html
    assert '"wayback_check":{"status":"passed","passed":true,"score":85' in html
    assert (
        '"wayback_check":{"passed":1,"failed":0,"connection_failed":0,"processing":0,"pending":0}'
    ) in html
    assert "Wayback passed" in html
    assert "data-wres" in html
    assert "WAYBACK_FILTERS" in html
    assert 'id="wayback-live"' in html
    assert 'id="wayback-count"' in html


def test_render_shortlist_html_includes_the_live_check_view() -> None:
    html = render_shortlist_html(
        [
            DiscoveryRecord(
                "live.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=True,
                prefilter_reason="Passed all rules",
                keyword_check_pass=True,
                dr_check_status="passed",
                dr_check_pass=True,
                live_check_status="passed",
                live_check_pass=True,
                live_check_class="content",
                live_check_reason="content: live page passed",
                live_check_attempts=1,
                live_check_checked_at="2026-11-27T12:30:00+00:00",
                live_check_final_host="www.live.com",
                live_check_http_status=200,
            )
        ],
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
    )

    assert 'data-view="live-check"' in html
    assert '<b class="step">4</b>Live check' in html
    assert '<b class="step">5</b>Wayback Machine Checks' in html
    assert '<b class="step">6</b>Data collection' in html
    assert '"live_check":{"status":"passed","passed":true,"class":"content"' in html
    assert (
        '"live_check":{"passed":1,"failed":0,"processing":0,"pending":0,"enabled":true}'
    ) in html
    assert "Live passed" in html
    assert "data-lres" in html
    assert "LIVE_FILTERS" in html
    assert 'id="live-live"' in html
    assert 'id="live-count"' in html
    assert "live_check" in html


def test_build_page_data_marks_evaluated_domains_and_counts_passed_failed() -> None:
    records = [
        DiscoveryRecord(
            domain="buy.com",
            source="spamzilla",
            collected_date="2026-11-27",
            niche="medical",
            evaluation_status="complete",
            evaluation_score=80.0,
            evaluation_evidence_points=70.0,
            evaluation_verdict="Buy",
            evaluation_recommendation="Buy",
            evaluation_reason="Buy (80.0)",
        ),
        DiscoveryRecord(
            domain="reject.com",
            source="spamzilla",
            collected_date="2026-11-27",
            niche="medical",
            evaluation_status="complete",
            evaluation_score=20.0,
            evaluation_evidence_points=10.0,
            evaluation_verdict="Reject",
            evaluation_recommendation="Reject",
            evaluation_reason="Reject (20.0)",
        ),
        DiscoveryRecord(
            domain="maybe.com",
            source="spamzilla",
            collected_date="2026-11-27",
            niche="medical",
            evaluation_status="complete",
            evaluation_score=70.0,
            evaluation_evidence_points=60.0,
            evaluation_verdict="Maybe",
            evaluation_recommendation="Manual review",
            evaluation_reason="Maybe (70.0)",
        ),
    ]

    rows, meta = build_page_data(records, db_path="/tmp/index.sqlite3")

    assert rows[0]["evaluation"]["verdict"] == "Buy"
    assert rows[0]["reached"] == 7
    assert rows[0]["progress_label"] == "Evaluated: Buy"
    assert meta["evaluation"] == {"passed": 2, "failed": 1, "total": 3, "pending": 0}


def test_load_discovery_records_reads_evaluation_columns(tmp_db: Path) -> None:
    _store(
        tmp_db,
        [DomainObservation("evaluated.com", "spamzilla", "2026-11-27", {}, {})],
    )
    conn = connect(tmp_db)
    try:
        conn.execute(
            "UPDATE domains SET evaluation_status = 'complete', evaluation_score = 77.5, "
            "evaluation_evidence_points = 67.5, evaluation_verdict = 'Buy', "
            "evaluation_recommendation = 'Buy', evaluation_reason = 'Buy (77.5)', "
            "evaluation_config_version = 'v1', evaluation_checked_at = "
            "'2026-11-27T00:00:00+00:00' WHERE domain = 'evaluated.com'"
        )
        conn.commit()
        records = load_discovery_records(conn)
    finally:
        conn.close()

    assert records[0].evaluation_verdict == "Buy"
    assert records[0].evaluation_score == 77.5
    assert records[0]._evaluation_row()["recommendation"] == "Buy"


def test_build_page_data_marks_an_in_flight_live_check() -> None:
    rows, meta = build_page_data(
        [
            DiscoveryRecord(
                "processing.com",
                "spamzilla",
                "2026-11-26",
                "medical",
                "lexicon",
                prefilter_pass=True,
                prefilter_reason="Passed all rules",
                keyword_check_pass=True,
                dr_check_pass=True,
                live_check_status="processing",
                live_check_attempts=1,
                live_check_checked_at="2026-11-27T12:30:00+00:00",
            )
        ],
        db_path="data/index.sqlite3",
    )

    assert rows[0]["live_check"]["status"] == "processing"
    assert rows[0]["live_check"]["passed"] is None
    assert rows[0]["progress_label"] == "Live check processing"
    assert rows[0]["reached"] == 4
    assert meta["live_check"]["processing"] == 1
    assert meta["live_check"]["pending"] == 0


def test_render_shortlist_html_includes_the_evaluated_domains_view() -> None:
    records = [
        DiscoveryRecord(
            domain="buy.com",
            source="spamzilla",
            collected_date="2026-11-27",
            niche="medical",
            evaluation_status="complete",
            evaluation_score=80.0,
            evaluation_evidence_points=70.0,
            evaluation_verdict="Buy",
            evaluation_recommendation="Buy",
            evaluation_reason="Buy (80.0)",
        )
    ]
    html = render_shortlist_html(records, db_path="/tmp/index.sqlite3")

    assert "Evaluated Domains" in html
    assert 'data-view="evaluated-domains"' in html
    assert "data-eres" in html
    assert "EVALUATION_FILTERS" in html
    assert '"verdict":"Buy"' in html


def test_render_shortlist_html_makes_evaluated_domains_open_the_detail_modal() -> None:
    """Evaluated-domain rows should use the same clickable modal affordance."""
    records = [
        DiscoveryRecord(
            domain="buy.com",
            source="spamzilla",
            collected_date="2026-11-27",
            niche="medical",
            evaluation_status="complete",
            evaluation_score=80.0,
            evaluation_evidence_points=70.0,
            evaluation_verdict="Buy",
            evaluation_recommendation="Buy",
            evaluation_reason="Buy (80.0)",
        )
    ]
    html = render_shortlist_html(records, db_path="/tmp/index.sqlite3")

    body_render = html.split(
        "document.querySelector('#tbl tbody').innerHTML = page.map(row => {", 1
    )[1]
    evaluation_branch = body_render.split("if (collectionView) {", 1)[0]

    assert 'class="clickable"' in evaluation_branch
    assert 'class="dom-link" data-detail="${esc(row.domain)}"' in evaluation_branch
    assert "row.classList.contains('clickable')" in html


def test_render_shortlist_html_uses_a_tidy_collapsible_detail_modal() -> None:
    html = render_shortlist_html(
        [DiscoveryRecord("buy.com", "spamzilla", "2026-11-27")],
        db_path="/tmp/index.sqlite3",
    )

    assert 'id="detail-badges"' in html
    assert "function detailStats(" in html
    assert "function detailPipeline(" in html
    assert "function detailFold(" in html
    assert ".detail-body>*{flex:none}" in html
    assert "detailBody.scrollTop = 0;" in html


def test_render_shortlist_html_embeds_rows_and_placeholders_are_gone() -> None:
    html = render_shortlist_html(
        [DiscoveryRecord("example.com", "spamzilla", "2026-11-26")],
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
        source_last_import={"spamzilla": "2026-11-28"},
    )

    assert '"domain":"example.com"' in html
    assert '"source_label":"SpamZilla"' in html
    assert '"last_import":"2026-11-28"' in html
    assert '"niche":"pending"' in html
    assert '"progress_label":"Pending classification"' in html
    assert "__ROWS_DATA__" not in html
    assert "__META_DATA__" not in html


def test_render_shortlist_html_embeds_domain_details_for_the_modal() -> None:
    html = render_shortlist_html(
        [DiscoveryRecord("clicker.org", "godaddy_auctions", "2026-11-26")],
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
        details={
            "clicker.org": {
                "domain": "clicker.org",
                "run": {"run_id": "clicker.org:test", "units": 2_804},
                "stats": {"live": {"links": 692, "refdomains": 652}},
            }
        },
    )

    assert 'id="detail-data"' in html
    assert 'id="detail"' in html
    assert "Domain detail" in html
    assert '"clicker.org":{"domain":"clicker.org"' in html
    assert '"run":{"run_id":"clicker.org:test"' in html
    assert "__DETAILS_DATA__" not in html


def test_render_shortlist_html_integrates_the_sge_global_rail() -> None:
    html = render_shortlist_html(
        [DiscoveryRecord("example.com", "spamzilla", "2026-11-26")],
        db_path="data/index.sqlite3",
        generated_at="2026-11-27T10:53:00Z",
    )

    assert (
        '<script type="module" '
        'src="https://sidebar.sustainable-growth-experts.com/sge-sidebar.js"></script>' in html
    )
    assert '<sge-sidebar src="https://sidebar.sustainable-growth-experts.com/menu.json"' in html
    assert '<body class="desk">' in html
    assert "sge-sidebar{width:64px}" in html
    assert "body.desk>sge-sidebar{position:sticky" in html
    assert "body.desk{padding-bottom:calc(64px + env(safe-area-inset-bottom))}" in html
    assert "rail.setAttribute('theme'" in html


def test_shortlist_cli_writes_the_page_and_prints_its_path(
    tmp_path: Path, tmp_db: Path, capsys
) -> None:
    _store(tmp_db, [DomainObservation("example.com", "spamzilla", "2026-11-26", {}, {})])
    output = tmp_path / "site" / "shortlist.html"

    exit_code = cli.main(
        ["shortlist", "--db", str(tmp_db), "--output", str(output)],
        settings=make_settings(tmp_path),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert output.is_file()
    rendered = output.read_text(encoding="utf-8")
    assert "example.com" in rendered
    assert '"last_import":"2026-11-26"' in rendered
    assert "domains=1" in captured.out
    assert "sources=1" in captured.out


def test_shortlist_cli_defaults_output_next_to_the_data_root(tmp_path: Path, tmp_db: Path) -> None:
    settings = make_settings(tmp_path)
    _store(tmp_db, [DomainObservation("example.com", "spamzilla", "2026-11-26", {}, {})])

    exit_code = cli.main(["shortlist", "--db", str(tmp_db)], settings=settings)

    assert exit_code == 0
    assert (tmp_path / "output" / "shortlist.html").is_file()


def test_shortlist_cli_reports_a_missing_index(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.sqlite3"

    exit_code = cli.main(
        ["shortlist", "--db", str(missing)],
        settings=make_settings(tmp_path),
    )

    assert exit_code == 2
    assert "index not found" in capsys.readouterr().err
    assert not missing.exists()
