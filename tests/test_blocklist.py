"""Deterministic blocklist matching and pre-classification filtering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_settings
from expired_domains import cli
from expired_domains.blocklist import (
    Blocklist,
    BlocklistMatch,
    load_blocklist,
    match_domain,
    split_blocked,
)
from expired_domains.models import DomainObservation
from expired_domains.niches import STUB_LLM_NICHE
from expired_domains.storage.db import (
    connect,
    init_db,
    list_pending_domains,
    set_domain_blocks,
    set_domain_niches,
    store_domain_observations,
)
from expired_domains.worker import classify_pending


@pytest.fixture
def blocklist() -> Blocklist:
    return load_blocklist()


def test_blocklist_loads_the_expected_categories(blocklist: Blocklist) -> None:
    assert blocklist.version == "v1"
    assert set(blocklist.categories) == {
        "adult",
        "gambling",
        "pharma",
        "payday",
        "crypto_scam",
        "counterfeit",
    }
    assert blocklist.label_for("pharma") == "Pharma spam"
    assert blocklist.label_for("crypto_scam") == "Crypto scam"
    assert all(category.terms for category in blocklist.categories.values())
    assert "essex" in blocklist.allow_terms


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("hotadultvideos.com", "adult"),
        ("bestsexcams.net", "adult"),
        ("p0rn.com", "adult"),
        ("bestonlinecasino.com", "gambling"),
        ("sportsbettingtips.org", "gambling"),
        ("c4sino.com", "gambling"),
        ("cheapviagraonline.com", "pharma"),
        ("online-pharmacy.net", "pharma"),
        ("v1agra.net", "pharma"),
        ("paydayloannow.com", "payday"),
        ("fast-cash-advance.com", "payday"),
        ("bitcoindoubler.com", "crypto_scam"),
        ("cryptosignals.net", "crypto_scam"),
        ("replicawatches.com", "counterfeit"),
        ("cheap-replicas.net", "counterfeit"),
    ],
)
def test_match_domain_flags_each_blocklist_category(
    blocklist: Blocklist, domain: str, expected: str
) -> None:
    match = match_domain(domain, blocklist)

    assert match is not None
    assert match.category == expected
    assert match.terms


@pytest.mark.parametrize(
    "domain",
    [
        "essex.com",
        "sussex.co.uk",
        "middlesex.net",
        "unisex-salon.com",
        "pillow.com",
        "replicable.com",
        "better.com",
        "analysis.com",
        "specialist.com",
        "ambiente.com",
    ],
)
def test_match_domain_ignores_allowlisted_words(blocklist: Blocklist, domain: str) -> None:
    assert match_domain(domain, blocklist) is None


def test_split_blocked_keeps_order_and_separates_blocked_domains(blocklist: Blocklist) -> None:
    allowed, blocked = split_blocked(
        ["goodtravel.com", "bestonlinecasino.com", "cheapviagra.com", "zooble.com"],
        blocklist,
    )

    assert allowed == ["goodtravel.com", "zooble.com"]
    assert list(blocked) == ["bestonlinecasino.com", "cheapviagra.com"]
    assert blocked["bestonlinecasino.com"].category == "gambling"
    assert blocked["cheapviagra.com"].category == "pharma"


def test_set_domain_blocks_persists_the_category_and_matched_terms(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("bestonlinecasino.com", "spamzilla", "2026-11-27", {}, {})],
        )
        updated = set_domain_blocks(
            conn,
            {"bestonlinecasino.com": ("gambling", ("casino", "onlinecasino"))},
        )
        conn.commit()
        stored = conn.execute("SELECT blocked_category, blocked_terms_json FROM domains").fetchone()
    finally:
        conn.close()

    assert updated == 1
    assert stored["blocked_category"] == "gambling"
    assert json.loads(stored["blocked_terms_json"]) == ["casino", "onlinecasino"]


def test_list_pending_domains_skips_blocked_domains(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("bestonlinecasino.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("zooble-brandable.com", "spamzilla", "2026-11-27", {}, {}),
            ],
        )
        set_domain_blocks(conn, {"bestonlinecasino.com": ("gambling", ("casino",))})
        conn.commit()

        pending = list_pending_domains(conn)
    finally:
        conn.close()

    assert pending == ["zooble-brandable.com"]


def test_classify_pending_blocks_before_niche_classification(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("bestonlinecasino.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("bestmedicalclinic.com", "spamzilla", "2026-11-27", {}, {}),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    processed = classify_pending(tmp_db)

    conn = connect(tmp_db)
    try:
        casino = conn.execute(
            "SELECT niche, blocked_category, blocked_terms_json "
            "FROM domains WHERE domain = 'bestonlinecasino.com'"
        ).fetchone()
        medical = conn.execute(
            "SELECT niche, blocked_category FROM domains WHERE domain = 'bestmedicalclinic.com'"
        ).fetchone()
        pending = list_pending_domains(conn)
    finally:
        conn.close()

    assert processed == 2
    assert casino["niche"] is None
    assert casino["blocked_category"] == "gambling"
    assert "casino" in json.loads(casino["blocked_terms_json"])
    assert medical["niche"] == "medical"
    assert medical["blocked_category"] is None
    assert pending == []


def test_classify_pending_never_sends_blocked_domains_to_the_llm(tmp_db: Path) -> None:
    class RecordingLLM:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def classify(self, domains, lexicon):
            self.calls.append(list(domains))
            return {}

    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("bestonlinecasino.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("zooble-brandable.com", "spamzilla", "2026-11-27", {}, {}),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    llm = RecordingLLM()
    classify_pending(tmp_db, llm=llm)

    assert llm.calls == [["zooble-brandable.com"]]


def test_blocklist_cli_marks_already_classified_domains(
    tmp_path: Path, tmp_db: Path, capsys
) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("bestonlinecasino.com", "spamzilla", "2026-11-27", {}, {})],
        )
        set_domain_niches(conn, {"bestonlinecasino.com": (STUB_LLM_NICHE, "stub_llm")})
        conn.commit()
    finally:
        conn.close()

    exit_code = cli.main(
        ["blocklist", "--db", str(tmp_db)],
        settings=make_settings(tmp_path),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "blocked=1" in captured.out

    conn = connect(tmp_db)
    try:
        stored = conn.execute(
            "SELECT blocked_category FROM domains WHERE domain = 'bestonlinecasino.com'"
        ).fetchone()
    finally:
        conn.close()

    assert stored["blocked_category"] == "gambling"


def test_import_domains_blocks_before_classification(tmp_path: Path, tmp_db: Path, capsys) -> None:
    path = tmp_path / "domains.csv"
    path.write_text(
        "Domain\nbestonlinecasino.com\nbestmedicalclinic.com\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "import-domains",
            str(path),
            "--source",
            "spamzilla",
            "--collected-date",
            "2026-11-27",
            "--db",
            str(tmp_db),
        ],
        settings=make_settings(tmp_path),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "blocked=1" in captured.out

    conn = connect(tmp_db)
    try:
        stored = {
            row["domain"]: (row["niche"], row["blocked_category"])
            for row in conn.execute("SELECT domain, niche, blocked_category FROM domains")
        }
    finally:
        conn.close()

    assert stored["bestonlinecasino.com"] == (None, "gambling")
    assert stored["bestmedicalclinic.com"] == ("medical", None)


def test_init_db_adds_blocked_columns_to_an_older_index(tmp_db: Path) -> None:
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
        stored = conn.execute("SELECT blocked_category, blocked_terms_json FROM domains").fetchone()
    finally:
        conn.close()

    assert {"blocked_category", "blocked_terms_json"} <= columns
    assert stored["blocked_category"] is None
    assert stored["blocked_terms_json"] is None


def test_blocklist_match_is_a_value_object() -> None:
    match = BlocklistMatch(category="gambling", terms=("casino",))

    assert match.category == "gambling"
    assert match.terms == ("casino",)
