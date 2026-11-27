"""Lexicon-first niche categorisation and the LLM fallback pass."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import make_settings
from expired_domains import cli
from expired_domains.models import DomainObservation
from expired_domains.niches import (
    LLM_UNKNOWN_NICHE,
    STUB_LLM_NICHE,
    UNKNOWN_NICHE,
    NicheLexicon,
    OpenRouterNicheClassifier,
    StubNicheClassifier,
    categorize_domains,
    classify_domain,
    load_niche_lexicon,
    parse_llm_classifications,
)
from expired_domains.shortlist import load_discovery_records
from expired_domains.storage.db import (
    connect,
    init_db,
    list_pending_domains,
    set_domain_niches,
    store_domain_observations,
)


class StubLLM:
    """Deterministic stand-in for the OpenRouter client."""

    def __init__(
        self,
        answers: dict[str, str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.answers = answers or {}
        self.error = error
        self.calls: list[tuple[list[str], tuple[str, ...]]] = []

    def classify(self, domains, lexicon: NicheLexicon) -> dict[str, str]:
        self.calls.append((list(domains), tuple(lexicon.labels)))
        if self.error is not None:
            raise self.error
        return {domain: self.answers[domain] for domain in domains if domain in self.answers}


@pytest.fixture
def lexicon() -> NicheLexicon:
    return load_niche_lexicon()


def test_lexicon_loads_the_sample_niches(lexicon: NicheLexicon) -> None:
    assert lexicon.version == "v1"
    assert lexicon.labels == ("medical", "travel", "legal", "hospitality")
    assert lexicon.label_for("legal") == "Legal / Law"
    assert lexicon.label_for("hospitality") == "Hospitality"
    assert lexicon.label_for("missing") == "missing"
    assert all(niche.terms for niche in lexicon.niches.values())


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("bestmedicalclinic.com", "medical"),
        ("online-pharmacy.net", "medical"),
        ("cheapflightsandhotels.com", "travel"),
        ("personalinjurylawyer.com", "legal"),
        ("divorce-attorney.org", "legal"),
        ("hospitalitydesignshow.com", "hospitality"),
        ("restaurant-supply.com", "hospitality"),
    ],
)
def test_classify_domain_matches_each_dictionary(
    lexicon: NicheLexicon, domain: str, expected: str
) -> None:
    match = classify_domain(domain, lexicon)

    assert match is not None
    assert match.niche == expected
    assert match.terms


def test_classify_domain_returns_none_when_nothing_matches(lexicon: NicheLexicon) -> None:
    assert classify_domain("zooble-brandable.com", lexicon) is None


def test_classify_domain_prefers_the_longest_specific_match(lexicon: NicheLexicon) -> None:
    # "hospitality" is longer than "hospital", so the hospitality design show is
    # not accidentally labelled medical.
    match = classify_domain("hospitalitydesignshow.com", lexicon)

    assert match is not None
    assert match.niche == "hospitality"
    assert "hospitality" in match.terms


def test_categorize_domains_uses_lexicon_first_and_batches_unknowns_in_tens(
    lexicon: NicheLexicon,
) -> None:
    unknown = [f"zzz{index}.com" for index in range(1, 22)]
    llm = StubLLM({domain: "travel" for domain in unknown[:11]})
    domains = ["bestmedicalclinic.com", *unknown]

    assignments = categorize_domains(domains, lexicon, llm=llm)

    assert [assignment.niche for assignment in assignments] == [
        "medical",
        *(["travel"] * 11),
        *([LLM_UNKNOWN_NICHE] * 10),
    ]
    assert [len(call[0]) for call in llm.calls] == [10, 10, 1]
    assert all(set(labels) == set(lexicon.labels) for _, labels in llm.calls)
    assert "bestmedicalclinic.com" not in {domain for batch, _ in llm.calls for domain in batch}


def test_categorize_domains_without_llm_keeps_plain_unknown(lexicon: NicheLexicon) -> None:
    assignments = categorize_domains(["zooble-brandable.com"], lexicon)

    assert assignments[0].niche == UNKNOWN_NICHE
    assert assignments[0].source == "unknown"


def test_stub_llm_labels_lexicon_misses_with_the_fixed_niche(lexicon: NicheLexicon) -> None:
    assignments = categorize_domains(
        ["bestmedicalclinic.com", "zooble-brandable.com"],
        lexicon,
        llm=StubNicheClassifier(),
    )

    assert assignments[0].niche == "medical"
    assert assignments[0].source == "lexicon"
    assert assignments[1].niche == STUB_LLM_NICHE
    assert assignments[1].source == "stub_llm"


def test_categorize_domains_decays_invalid_or_missing_llm_answers_to_llm_unknown(
    lexicon: NicheLexicon,
) -> None:
    llm = StubLLM({"one-unknown.com": "not-a-niche"})

    assignments = categorize_domains(
        ["one-unknown.com", "two-unknown.com"],
        lexicon,
        llm=llm,
    )

    assert [assignment.niche for assignment in assignments] == [
        LLM_UNKNOWN_NICHE,
        LLM_UNKNOWN_NICHE,
    ]
    assert [assignment.source for assignment in assignments] == ["llm_unknown", "llm_unknown"]


def test_categorize_domains_survives_an_llm_failure(lexicon: NicheLexicon) -> None:
    llm = StubLLM(error=RuntimeError("provider down"))

    assignments = categorize_domains(["zooble-brandable.com"], lexicon, llm=llm)

    assert assignments[0].niche == LLM_UNKNOWN_NICHE
    assert assignments[0].source == "llm_unknown"


def test_parse_llm_classifications_requires_a_high_confidence_known_label(
    lexicon: NicheLexicon,
) -> None:
    content = json.dumps(
        {
            "classifications": [
                {"domain": "good.com", "niche": "travel", "confidence": 0.95},
                {"domain": "low.com", "niche": "medical", "confidence": 0.4},
                {"domain": "bad-label.com", "niche": "gambling", "confidence": 0.99},
                {"domain": "not-asked.com", "niche": "legal", "confidence": 0.99},
                {"domain": "no-confidence.com", "niche": "legal"},
                {"domain": "choose-unknown.com", "niche": LLM_UNKNOWN_NICHE, "confidence": 0.99},
            ]
        }
    )

    accepted = parse_llm_classifications(
        content,
        ["good.com", "low.com", "bad-label.com", "no-confidence.com", "choose-unknown.com"],
        lexicon,
    )

    assert accepted == {"good.com": "travel"}


def test_openrouter_classifier_sends_one_batch_and_parses_the_reply(
    lexicon: NicheLexicon,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        content = json.dumps(
            {
                "classifications": [
                    {"domain": "besttraveldeals.com", "niche": "travel", "confidence": 0.97}
                ]
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    classifier = OpenRouterNicheClassifier(
        api_key="test-key",
        model="test-model",
        base_url="https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        accepted = classifier.classify(["besttraveldeals.com"], lexicon)
    finally:
        classifier.close()

    assert accepted == {"besttraveldeals.com": "travel"}
    assert str(captured["url"]).endswith("/chat/completions")
    assert captured["authorization"] == "Bearer test-key"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "test-model"
    assert body["temperature"] == 0
    prompt = json.dumps(body["messages"])
    assert "medical" in prompt
    assert "travel" in prompt
    assert "legal" in prompt
    assert "hospitality" in prompt
    assert LLM_UNKNOWN_NICHE in prompt


def test_openrouter_classifier_without_a_key_makes_no_request(lexicon: NicheLexicon) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made without an API key")

    classifier = OpenRouterNicheClassifier(
        api_key=None,
        model="test-model",
        base_url="https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert classifier.classify(["besttraveldeals.com"], lexicon) == {}
    finally:
        classifier.close()


def test_set_domain_niches_persists_to_the_domain_and_discovery_rows(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("bestmedicalclinic.com", "spamzilla", "2026-11-26", {}, {}),
                DomainObservation("zooble-brandable.com", "spamzilla", "2026-11-26", {}, {}),
            ],
        )
        updated = set_domain_niches(
            conn,
            {
                "bestmedicalclinic.com": ("medical", "lexicon"),
                "zooble-brandable.com": (LLM_UNKNOWN_NICHE, "llm_unknown"),
            },
        )
        conn.commit()

        records = {record.domain: record for record in load_discovery_records(conn)}
    finally:
        conn.close()

    assert updated == 2
    assert records["bestmedicalclinic.com"].niche == "medical"
    assert records["bestmedicalclinic.com"].niche_source == "lexicon"
    assert records["zooble-brandable.com"].niche == LLM_UNKNOWN_NICHE
    assert records["zooble-brandable.com"].niche_source == "llm_unknown"


def test_init_db_adds_niche_columns_to_an_older_index(tmp_db: Path) -> None:
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
        stored = conn.execute("SELECT niche, niche_source FROM domains").fetchone()
    finally:
        conn.close()

    assert {"niche", "niche_source"} <= columns
    assert stored["niche"] is None
    assert stored["niche_source"] is None


def test_list_pending_domains_returns_only_unclassified_rows(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("classified.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("llm-unknown.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("pending.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("legacy-unknown.com", "spamzilla", "2026-11-27", {}, {}),
            ],
        )
        set_domain_niches(
            conn,
            {
                "classified.com": ("medical", "lexicon"),
                "llm-unknown.com": (LLM_UNKNOWN_NICHE, "llm_unknown"),
                "legacy-unknown.com": (UNKNOWN_NICHE, "unknown"),
            },
        )
        conn.commit()

        pending = list_pending_domains(conn)
    finally:
        conn.close()

    assert pending == ["legacy-unknown.com", "pending.com"]


def test_import_domains_cli_persists_lexicon_and_stub_llm_niches(
    tmp_path: Path, tmp_db: Path, capsys
) -> None:
    path = tmp_path / "domains.csv"
    path.write_text(
        "Domain\nbestmedicalclinic.com\nzooble-brandable.com\n",
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
    assert "medical=1" in captured.out
    assert f"{STUB_LLM_NICHE}=1" in captured.out

    conn = connect(tmp_db)
    try:
        stored = {
            row["domain"]: (row["niche"], row["niche_source"])
            for row in conn.execute("SELECT domain, niche, niche_source FROM domains")
        }
    finally:
        conn.close()

    assert stored["bestmedicalclinic.com"] == ("medical", "lexicon")
    assert stored["zooble-brandable.com"] == (STUB_LLM_NICHE, "stub_llm")


def test_import_domains_only_classifies_domains_with_a_null_niche(
    tmp_path: Path, tmp_db: Path, monkeypatch
) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("already-classified.com", "spamzilla", "2026-11-26", {}, {})],
        )
        set_domain_niches(conn, {"already-classified.com": ("medical", "lexicon")})
        conn.commit()
    finally:
        conn.close()

    class FakeClassifier:
        calls: list[list[str]] = []

        def __init__(self) -> None:
            pass

        def classify(self, domains, lexicon):
            self.calls.append(list(domains))
            return {"zooble-brandable.com": "travel"}

    monkeypatch.setattr(cli, "StubNicheClassifier", FakeClassifier)

    path = tmp_path / "domains.csv"
    path.write_text(
        "Domain\nalready-classified.com\nzooble-brandable.com\n",
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

    conn = connect(tmp_db)
    try:
        stored = {
            row["domain"]: row["niche"] for row in conn.execute("SELECT domain, niche FROM domains")
        }
    finally:
        conn.close()

    assert exit_code == 0
    assert FakeClassifier.calls == [["zooble-brandable.com"]]
    assert stored == {
        "already-classified.com": "medical",
        "zooble-brandable.com": "travel",
    }
