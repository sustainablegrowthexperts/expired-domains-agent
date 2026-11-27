"""Canonical domain normalisation, CSV ingestion, and storage plumbing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_settings
from expired_domains import cli
from expired_domains.domains import (
    METRIC_DEFAULTS,
    SOURCE_EXPIREDDOMAINS,
    SOURCE_GODADDY_AUCTIONS,
    SOURCE_SPAMZILLA,
    detect_source,
    empty_metrics,
    normalize_collected_date,
    normalize_domain,
    normalize_source,
    parse_domain_csv,
)
from expired_domains.models import DomainObservation
from expired_domains.storage.db import connect, init_db, store_domain_observations

EXPIRED_DOMAINS_CSV = """\
Domain,BL,DP,ABY,ACR,Dmoz,C,N,O,D,Reg,RDT,End Date
Example.COM,89,1.8 K,2003,68,-,registered,available,available,available,0,0,2026-11-26
www.Sub.Example.co.uk,1,1,2000,1,-,available,available,available,available,0,0,2026-11-26
,1,1,2000,1,-,available,available,available,available,0,0,2026-11-26
"""

GODADDY_CSV = (
    "ItemID,Domain Name,Traffic,Bids,Price,Estimated Value,Domain Age,Auction End Time,"
    "Sale Type,Majestic TF,Majestic CF,Majestic Backlinks,Majestic Referring Domains,"
    "Exact Match TLDs,Keyword Registrations,Developed TLDs,Semrush AS,"
    "Semrush Referring Domains,Semrush Backlinks,Semrush Indexed Pages,"
    "Semrush Top Referring Domains,Semrush Search Volume,Semrush CPC\n"
    "738962382,VNS.com,0,121,58888,917214,29,2026-11-26 17:44:00,Auction,0,10,15,11,"
    '109,2201,13,2,878,1051,1017,"nextdoor.com, yellowpages.com",33100,2.5\n'
)

SPAMZILLA_CSV = (
    "Domain,Source,TF,CF,Maj BL,Maj RD,Maj Topics,Maj Lang,Site Lang,Moz DA,Moz PA,Age,"
    "SZ Score,SZ Redirects,SZ Parked,SZ A/History,SZ Drops,Google Index,Out Links Internal,"
    "Out LinksExternal,Out DomainsExternal,SEMTraf,SEMRank,SEMKW,Date Added,Price,Expires\n"
    "Webmaster-Affiliates.NET,,23,16,16,13,12,EN,,,,,2,,,,,,,-,-,-,-,-,,,11/28/2026\n"
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Example.COM", "example.com"),
        ("  example.com.  ", "example.com"),
        ("https://Example.COM:443/Path?q=1#frag", "example.com"),
        ("http://user:pass@www.example.com/a", "example.com"),
        ("www.sub.example.co.uk", "example.co.uk"),
        ("BLOG.Example.com", "example.com"),
        ("foo.invalid", "foo.invalid"),
        ("example", ""),
        ("not a domain.com", ""),
        ("", ""),
        (None, ""),
        ("[::1]", ""),
    ],
)
def test_normalize_domain_returns_a_lowercase_registrable_domain(
    value: str | None, expected: str
) -> None:
    assert normalize_domain(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("SpamZilla", SOURCE_SPAMZILLA),
        ("spamzilla", SOURCE_SPAMZILLA),
        ("GoDaddy Auctions", SOURCE_GODADDY_AUCTIONS),
        ("godaddy_auctions", SOURCE_GODADDY_AUCTIONS),
        ("gd-auctions", SOURCE_GODADDY_AUCTIONS),
        ("ExpiredDomains.net", SOURCE_EXPIREDDOMAINS),
        ("expired domains", SOURCE_EXPIREDDOMAINS),
        ("Flippa", "flippa"),
        ("Some New Market", "some_new_market"),
    ],
)
def test_normalize_source_uses_aliases_and_slugs_unknown_labels(value: str, expected: str) -> None:
    assert normalize_source(value) == expected


def test_normalize_source_rejects_an_empty_label() -> None:
    with pytest.raises(ValueError, match="source is required"):
        normalize_source("  ")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("2026-11-27", "2026-11-27"),
        ("2026-11-27T10:11:12Z", "2026-11-27"),
    ],
)
def test_normalize_collected_date_accepts_iso_dates(value, expected) -> None:
    result = normalize_collected_date(value)
    if expected is None:
        assert len(result) == 10
    else:
        assert result == expected


def test_normalize_collected_date_rejects_a_bad_value() -> None:
    with pytest.raises(ValueError, match="invalid collection date"):
        normalize_collected_date("not-a-date")


def test_detect_source_recognises_each_supported_export() -> None:
    assert detect_source(["Domain", "BL", "End Date"]) == SOURCE_EXPIREDDOMAINS
    assert detect_source(["ItemID", "Domain Name", "Auction End Time"]) == SOURCE_GODADDY_AUCTIONS
    assert detect_source(["Domain", "SZ Score", "Date Added"]) == SOURCE_SPAMZILLA


def test_detect_source_rejects_unknown_headers() -> None:
    with pytest.raises(ValueError, match="--source"):
        detect_source(["Name", "Metric"])


def test_parse_expireddomains_csv_normalises_domains_and_keeps_other_columns(
    tmp_path: Path,
) -> None:
    parsed = parse_domain_csv(
        _write(tmp_path, "expireddomains.csv", EXPIRED_DOMAINS_CSV),
        collected_date="2026-11-27",
    )

    assert parsed.source == SOURCE_EXPIREDDOMAINS
    assert [row.domain for row in parsed.observations] == [
        "example.com",
        "example.co.uk",
    ]
    example = parsed.observations[0]
    assert example.collected_date == "2026-11-27"
    assert example.raw["BL"] == "89"
    assert example.raw["End Date"] == "2026-11-26"
    assert "Domain" not in example.raw
    assert example.metrics["backlinks"] == 89
    assert example.metrics["referring_domains"] == 1800
    assert example.metrics["archive_birth_year"] == 2003
    assert example.metrics["archive_captures"] == 68
    assert example.metrics["dmoz_listed"] is None
    assert example.metrics["registrations"] == 0
    assert example.metrics["drop_date"] == "2026-11-26T00:00:00Z"
    assert example.metrics["price"] is None


def test_parse_godaddy_csv_uses_domain_name_as_the_first_class_column(
    tmp_path: Path,
) -> None:
    parsed = parse_domain_csv(
        _write(tmp_path, "godaddy.csv", GODADDY_CSV),
        collected_date="2026-11-27",
    )

    assert parsed.source == SOURCE_GODADDY_AUCTIONS
    assert len(parsed.observations) == 1
    row = parsed.observations[0]
    assert row.domain == "vns.com"
    assert row.raw["ItemID"] == "738962382"
    assert row.raw["Auction End Time"] == "2026-11-26 17:44:00"
    assert row.raw["Semrush CPC"] == "2.5"
    assert "Domain Name" not in row.raw

    assert row.metrics["price"] == 58888.0
    assert row.metrics["estimated_value"] == 917214.0
    assert row.metrics["bid_count"] == 121
    assert row.metrics["auction_end_at"] == "2026-11-26T17:44:00Z"
    assert row.metrics["sale_type"] == "Auction"
    assert row.metrics["domain_age_years"] == 29.0
    assert row.metrics["traffic"] == 0
    assert row.metrics["majestic_trust_flow"] == 0
    assert row.metrics["majestic_citation_flow"] == 10
    assert row.metrics["majestic_backlinks"] == 15
    assert row.metrics["majestic_referring_domains"] == 11
    assert row.metrics["backlinks"] == 15
    assert row.metrics["referring_domains"] == 11
    assert row.metrics["semrush_authority_score"] == 2
    assert row.metrics["semrush_backlinks"] == 1051
    assert row.metrics["semrush_referring_domains"] == 878
    assert row.metrics["semrush_indexed_pages"] == 1017
    assert row.metrics["semrush_search_volume"] == 33100
    assert row.metrics["semrush_cpc"] == 2.5
    assert row.metrics["top_referring_domains"] == ["nextdoor.com", "yellowpages.com"]
    assert row.metrics["exact_match_tlds"] == 109
    assert row.metrics["keyword_registrations"] == 2201
    assert row.metrics["developed_tlds"] == 13
    assert row.metrics["listed_at"] is None
    assert row.metrics["expires_at"] is None
    assert row.metrics["drop_date"] is None


def test_parse_spamzilla_csv_maps_metrics_and_keeps_the_source_column(
    tmp_path: Path,
) -> None:
    parsed = parse_domain_csv(
        _write(tmp_path, "spamzilla.csv", SPAMZILLA_CSV),
        collected_date="2026-11-27",
    )

    assert parsed.source == SOURCE_SPAMZILLA
    assert len(parsed.observations) == 1
    row = parsed.observations[0]
    assert row.domain == "webmaster-affiliates.net"
    assert row.raw["SZ Score"] == "2"
    assert row.raw["Expires"] == "11/28/2026"
    assert "Domain" not in row.raw

    assert row.metrics["majestic_trust_flow"] == 23
    assert row.metrics["majestic_citation_flow"] == 16
    assert row.metrics["majestic_backlinks"] == 16
    assert row.metrics["majestic_referring_domains"] == 13
    assert row.metrics["backlinks"] == 16
    assert row.metrics["referring_domains"] == 13
    assert row.metrics["majestic_topics"] == [12]
    assert row.metrics["majestic_language"] == "EN"
    assert row.metrics["site_language"] is None
    assert row.metrics["language"] == "EN"
    assert row.metrics["spamzilla_score"] == 2
    assert row.metrics["expires_at"] == "2026-11-28T00:00:00Z"
    assert row.metrics["listed_at"] is None
    assert row.metrics["price"] is None


def test_every_source_exposes_the_same_metric_keys(tmp_path: Path) -> None:
    parsed = [
        parse_domain_csv(
            _write(tmp_path, "expireddomains.csv", EXPIRED_DOMAINS_CSV),
            collected_date="2026-11-27",
        ),
        parse_domain_csv(
            _write(tmp_path, "godaddy.csv", GODADDY_CSV),
            collected_date="2026-11-27",
        ),
        parse_domain_csv(
            _write(tmp_path, "spamzilla.csv", SPAMZILLA_CSV),
            collected_date="2026-11-27",
        ),
    ]

    for entry in parsed:
        assert set(entry.observations[0].metrics) == set(METRIC_DEFAULTS)


def test_parse_domain_csv_accepts_an_explicit_source_override(tmp_path: Path) -> None:
    path = _write(tmp_path, "custom.csv", "Domain,Metric\nExample.com,7\n")

    parsed = parse_domain_csv(path, source="Flippa", collected_date="2026-11-27")

    assert parsed.source == "flippa"
    assert parsed.observations[0].domain == "example.com"
    assert parsed.observations[0].raw == {"Metric": "7"}
    assert parsed.observations[0].metrics == empty_metrics()


def test_init_db_creates_the_domain_tables(tmp_db: Path) -> None:
    init_db(tmp_db)

    conn = connect(tmp_db)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"domains", "domain_observations"} <= tables
        assert [row["name"] for row in conn.execute("PRAGMA table_info(domains)") if row["pk"]] == [
            "domain"
        ]
        assert [
            row["name"]
            for row in conn.execute("PRAGMA table_info(domain_observations)")
            if row["pk"]
        ] == ["domain", "source", "collected_date"]
    finally:
        conn.close()


def test_init_db_upgrades_the_previous_payload_json_schema(tmp_db: Path) -> None:
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
                payload_json    TEXT NOT NULL,
                PRIMARY KEY (domain, source, collected_date),
                FOREIGN KEY (domain) REFERENCES domains (domain) ON DELETE CASCADE
            );
            INSERT INTO domains VALUES ('example.com', '2026-11-27', '2026-11-27');
            INSERT INTO domain_observations VALUES (
                'example.com', 'spamzilla', '2026-11-27', '{"SZ Score": "2"}'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    init_db(tmp_db)

    conn = connect(tmp_db)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(domain_observations)")}
        assert {"metrics_json", "raw_json"} <= columns
        assert "payload_json" not in columns
        stored = conn.execute("SELECT metrics_json, raw_json FROM domain_observations").fetchone()
        assert json.loads(stored["metrics_json"]) == {}
        assert json.loads(stored["raw_json"]) == {"SZ Score": "2"}
    finally:
        conn.close()


def test_store_domain_observations_spans_sources_and_dates(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        stored = store_domain_observations(
            conn,
            [
                DomainObservation(
                    "example.com",
                    "spamzilla",
                    "2026-11-27",
                    {"spamzilla_score": 2},
                    {"SZ Score": "2"},
                ),
                DomainObservation(
                    "example.com",
                    "godaddy_auctions",
                    "2026-11-29",
                    {"bid_count": 3},
                    {"Bids": "3"},
                ),
                DomainObservation("other.com", "spamzilla", "2026-11-27", {}, {}),
            ],
        )
        conn.commit()

        assert stored == 3
        assert conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM domain_observations").fetchone()[0] == 3

        domain = conn.execute("SELECT * FROM domains WHERE domain = 'example.com'").fetchone()
        assert domain["first_seen_date"] == "2026-11-27"
        assert domain["last_seen_date"] == "2026-11-29"

        stored_observation = conn.execute(
            "SELECT metrics_json, raw_json FROM domain_observations "
            "WHERE domain = 'example.com' AND source = 'godaddy_auctions'"
        ).fetchone()
        assert json.loads(stored_observation["metrics_json"]) == {"bid_count": 3}
        assert json.loads(stored_observation["raw_json"]) == {"Bids": "3"}
    finally:
        conn.close()


def test_reimporting_the_same_observation_replaces_both_blobs(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation(
                    "example.com", "spamzilla", "2026-11-27", {"price": 1.0}, {"Price": "1"}
                )
            ],
        )
        store_domain_observations(
            conn,
            [
                DomainObservation(
                    "example.com", "spamzilla", "2026-11-27", {"price": 2.0}, {"Price": "2"}
                )
            ],
        )
        conn.commit()

        assert conn.execute("SELECT COUNT(*) FROM domain_observations").fetchone()[0] == 1
        stored = conn.execute("SELECT metrics_json, raw_json FROM domain_observations").fetchone()
        assert json.loads(stored["metrics_json"]) == {"price": 2.0}
        assert json.loads(stored["raw_json"]) == {"Price": "2"}
    finally:
        conn.close()


def test_import_domains_cli_imports_all_three_exports(tmp_path: Path, tmp_db: Path, capsys) -> None:
    paths = [
        _write(tmp_path, "expireddomains.csv", EXPIRED_DOMAINS_CSV),
        _write(tmp_path, "godaddy.csv", GODADDY_CSV),
        _write(tmp_path, "spamzilla.csv", SPAMZILLA_CSV),
    ]
    exit_code = cli.main(
        [
            "import-domains",
            *(str(path) for path in paths),
            "--collected-date",
            "2026-11-27",
            "--db",
            str(tmp_db),
        ],
        settings=make_settings(tmp_path),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "source=expireddomains" in captured.out
    assert "source=godaddy_auctions" in captured.out
    assert "source=spamzilla" in captured.out

    conn = connect(tmp_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM domain_observations").fetchone()[0] == 4
        sources = {row["source"] for row in conn.execute("SELECT source FROM domain_observations")}
        assert sources == {
            SOURCE_EXPIREDDOMAINS,
            SOURCE_GODADDY_AUCTIONS,
            SOURCE_SPAMZILLA,
        }
    finally:
        conn.close()


def test_import_domains_cli_is_idempotent_per_collection_date(tmp_path: Path, tmp_db: Path) -> None:
    path = _write(tmp_path, "spamzilla.csv", SPAMZILLA_CSV)
    settings = make_settings(tmp_path)

    def import_on(day: str) -> int:
        return cli.main(
            ["import-domains", str(path), "--collected-date", day, "--db", str(tmp_db)],
            settings=settings,
        )

    assert import_on("2026-11-27") == 0
    assert import_on("2026-11-27") == 0
    assert import_on("2026-11-28") == 0

    conn = connect(tmp_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM domain_observations").fetchone()[0] == 2
        domain = conn.execute("SELECT * FROM domains").fetchone()
        assert domain["first_seen_date"] == "2026-11-27"
        assert domain["last_seen_date"] == "2026-11-28"
    finally:
        conn.close()


def test_import_domains_cli_rejects_an_invalid_date(tmp_path: Path, tmp_db: Path, capsys) -> None:
    path = _write(tmp_path, "spamzilla.csv", SPAMZILLA_CSV)

    exit_code = cli.main(
        ["import-domains", str(path), "--collected-date", "yesterday", "--db", str(tmp_db)],
        settings=make_settings(tmp_path),
    )

    assert exit_code == 2
    assert "invalid collection date" in capsys.readouterr().err
    assert not tmp_db.exists()
