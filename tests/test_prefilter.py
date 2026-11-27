"""Deterministic local prefilter for classified candidate domains."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest

from expired_domains.models import DomainObservation
from expired_domains.prefilter import (
    DISABLED_REASON,
    PASS_REASON,
    PrefilterConfig,
    evaluate_domain,
    load_prefilter_candidates,
    load_prefilter_config,
)
from expired_domains.storage.db import (
    connect,
    init_db,
    set_domain_blocks,
    set_domain_niches,
    set_prefilter_manual_pass,
    set_prefilter_results,
    store_domain_observations,
    transaction,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
_CONFIG_COUNTER = count()


def _config(tmp_path: Path, **overrides: object) -> PrefilterConfig:
    """Load the shipped config with selected top-level overrides."""
    source = Path(__file__).resolve().parents[1] / "config" / "prefilter" / "v3.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data.update(overrides)
    path = tmp_path / f"prefilter-{next(_CONFIG_COUNTER)}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_prefilter_config(path)


def _checks(result) -> dict[str, dict[str, object]]:
    return {check["gate"]: check for check in result.details["checks"]}


def test_default_config_uses_conservative_local_gates(tmp_path: Path) -> None:
    config = load_prefilter_config()

    gates = config.gates_for("medical")

    assert config.version == "v3"
    assert config.enabled is True
    assert gates.length.enabled is True
    assert (gates.length.minimum, gates.length.maximum) == (3, 35)
    assert gates.hyphens.enabled is True
    assert gates.hyphens.maximum == 1
    assert gates.numbers.enabled is True
    assert gates.numbers.maximum == 1
    assert gates.tld.enabled is True
    assert gates.tld.values == ("com", "com.sg", "sg", "org")
    assert gates.price.enabled is True
    assert gates.price.maximum_usd == 500
    assert gates.time_to_close.enabled is True
    assert gates.time_to_close.minimum_hours == 6


def test_clean_domain_passes_and_missing_metrics_are_skipped(tmp_path: Path) -> None:
    result = evaluate_domain(
        "bestmedicalclinic.com",
        niche="medical",
        metrics={},
        config=_config(tmp_path),
        now=NOW,
    )

    checks = _checks(result)
    assert result.passed is True
    assert result.reason == PASS_REASON
    assert result.config_version == "v3"
    assert result.checked_at.startswith("2026-11-27T12:00:00")
    assert [check["gate"] for check in result.details["checks"]] == [
        "length",
        "hyphens",
        "numbers",
        "tld_allow",
        "max_price_usd",
        "min_hours_to_close",
    ]
    assert checks["length"]["status"] == "pass"
    assert checks["hyphens"]["status"] == "pass"
    assert checks["numbers"]["status"] == "pass"
    assert checks["tld_allow"]["status"] == "pass"
    assert checks["max_price_usd"]["status"] == "skip"
    assert checks["min_hours_to_close"]["status"] == "skip"


@pytest.mark.parametrize(
    ("domain", "reason"),
    [
        ("ab.com", "Too short"),
        ("averyveryverylongdomainnamethatkeepsgoingandgoing.com", "Too long"),
        ("metro-health-clinic.com", "Hyphenated"),
        ("clinic42.com", "Has digits"),
    ],
)
def test_name_gates_reject_with_the_first_reason(tmp_path: Path, domain: str, reason: str) -> None:
    result = evaluate_domain(
        domain,
        niche="medical",
        metrics={},
        config=_config(tmp_path),
        now=NOW,
    )

    assert result.passed is False
    assert result.reason == reason


def test_the_first_failed_gate_is_the_reported_reason(tmp_path: Path) -> None:
    result = evaluate_domain(
        "a--22.com",
        niche="medical",
        metrics={},
        config=_config(tmp_path),
        now=NOW,
    )

    assert result.passed is False
    assert result.reason == "Hyphenated"
    assert result.details["failed"] == ["Hyphenated", "Has digits"]


@pytest.mark.parametrize(
    "domain",
    [
        "example.com",
        "example.com.sg",
        "example.sg",
        "example.org",
        "subdomain.example.com.sg",
    ],
)
def test_default_tld_gate_allows_com_singapore_and_org_suffixes(
    tmp_path: Path, domain: str
) -> None:
    result = evaluate_domain(
        domain,
        niche="medical",
        metrics={},
        config=_config(tmp_path),
        now=NOW,
    )

    assert result.passed is True
    assert _checks(result)["tld_allow"]["status"] == "pass"


@pytest.mark.parametrize(
    "domain",
    [
        "example.net",
        "example.io",
        "example.co.uk",
        "example.edu.sg",
    ],
)
def test_default_tld_gate_rejects_other_suffixes(tmp_path: Path, domain: str) -> None:
    result = evaluate_domain(
        domain,
        niche="medical",
        metrics={},
        config=_config(tmp_path),
        now=NOW,
    )

    assert result.passed is False
    assert result.reason == "TLD not allowed"
    assert _checks(result)["tld_allow"]["status"] == "fail"


def test_price_gate_rejects_over_budget_and_skips_missing_values(tmp_path: Path) -> None:
    config = _config(tmp_path)

    over_budget = evaluate_domain(
        "bestmedicalclinic.com",
        niche="medical",
        metrics={"price": 600},
        config=config,
        now=NOW,
    )
    missing = evaluate_domain(
        "bestmedicalclinic.com",
        niche="medical",
        metrics={"price": None},
        config=config,
        now=NOW,
    )

    assert over_budget.passed is False
    assert over_budget.reason == "Over budget"
    assert missing.passed is True
    assert _checks(missing)["max_price_usd"]["status"] == "skip"


def test_time_gate_rejects_ended_and_ending_soon(tmp_path: Path) -> None:
    config = _config(tmp_path)

    ended = evaluate_domain(
        "bestmedicalclinic.com",
        niche="medical",
        metrics={"auction_end_at": (NOW - timedelta(hours=1)).isoformat()},
        config=config,
        now=NOW,
    )
    soon = evaluate_domain(
        "bestmedicalclinic.com",
        niche="medical",
        metrics={"auction_end_at": (NOW + timedelta(hours=2)).isoformat()},
        config=config,
        now=NOW,
    )
    later = evaluate_domain(
        "bestmedicalclinic.com",
        niche="medical",
        metrics={"auction_end_at": (NOW + timedelta(hours=12)).isoformat()},
        config=config,
        now=NOW,
    )

    assert ended.passed is False
    assert ended.reason == "Ended"
    assert soon.passed is False
    assert soon.reason == "Ending soon"
    assert later.passed is True


def test_disabled_gate_is_skipped_not_failed(tmp_path: Path) -> None:
    result = evaluate_domain(
        "metro-health-clinic.com",
        niche="medical",
        metrics={},
        config=_config(tmp_path, hyphens={"enabled": False, "max": 1}),
        now=NOW,
    )

    assert result.passed is True
    assert _checks(result)["hyphens"]["status"] == "skip"
    assert _checks(result)["hyphens"]["reason"] == "disabled"


def test_niche_overrides_replace_the_matching_gate(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        niche_overrides={
            "legal": {"max_price_usd": {"enabled": True, "value": 1000}},
        },
    )

    medical = evaluate_domain(
        "bestmedicalclinic.com",
        niche="medical",
        metrics={"price": 700},
        config=config,
        now=NOW,
    )
    legal = evaluate_domain(
        "bestmedicalclinic.com",
        niche="legal",
        metrics={"price": 700},
        config=config,
        now=NOW,
    )

    assert medical.passed is False
    assert medical.reason == "Over budget"
    assert legal.passed is True


def test_global_disable_passes_every_domain(tmp_path: Path) -> None:
    result = evaluate_domain(
        "a--22.com",
        niche="medical",
        metrics={},
        config=_config(tmp_path, enabled=False),
        now=NOW,
    )

    assert result.passed is True
    assert result.reason == DISABLED_REASON


def test_init_db_adds_prefilter_columns_to_an_older_index(tmp_db: Path) -> None:
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
            "SELECT prefilter_pass, prefilter_reason, prefilter_details_json, "
            "prefilter_config_version, prefilter_checked_at, "
            "prefilter_manual_pass, prefilter_manual_pass_at FROM domains"
        ).fetchone()
    finally:
        conn.close()

    assert {
        "prefilter_pass",
        "prefilter_reason",
        "prefilter_details_json",
        "prefilter_config_version",
        "prefilter_checked_at",
        "prefilter_manual_pass",
        "prefilter_manual_pass_at",
    } <= columns
    assert tuple(stored) == (None, None, None, None, None, 0, None)


def test_load_candidates_only_returns_classified_unblocked_pending_domains(
    tmp_db: Path,
) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("ready.com", "spamzilla", "2026-11-27", {"price": 100}, {}),
                DomainObservation("blocked.com", "spamzilla", "2026-11-27", {"price": 100}, {}),
                DomainObservation("pending.com", "spamzilla", "2026-11-27", {"price": 100}, {}),
            ],
        )
        set_domain_niches(
            conn,
            {
                "ready.com": ("medical", "lexicon"),
                "blocked.com": ("medical", "lexicon"),
            },
        )
        set_domain_blocks(conn, {"blocked.com": ("gambling", ("casino",))})
        conn.commit()

        candidates = load_prefilter_candidates(conn, load_prefilter_config().version)
    finally:
        conn.close()

    assert [candidate.domain for candidate in candidates] == ["ready.com"]
    assert candidates[0].niche == "medical"
    assert candidates[0].metrics == {"price": 100}


def test_candidates_use_the_latest_observation_metrics(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("ready.com", "spamzilla", "2026-11-26", {"price": 100}, {}),
                DomainObservation(
                    "ready.com", "godaddy_auctions", "2026-11-27", {"price": 900}, {}
                ),
            ],
        )
        set_domain_niches(conn, {"ready.com": ("medical", "lexicon")})
        conn.commit()

        candidates = load_prefilter_candidates(conn, load_prefilter_config().version)
    finally:
        conn.close()

    assert [candidate.metrics for candidate in candidates] == [{"price": 900}]


def test_candidates_are_reprocessed_when_the_config_version_changes(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("ready.com", "spamzilla", "2026-11-27", {}, {})],
        )
        set_domain_niches(conn, {"ready.com": ("medical", "lexicon")})
        with transaction(conn):
            set_prefilter_results(
                conn,
                {
                    "ready.com": evaluate_domain(
                        "ready.com",
                        niche="medical",
                        metrics={},
                        config=load_prefilter_config(),
                        now=NOW,
                    )
                },
            )
        same_version = load_prefilter_candidates(conn, load_prefilter_config().version)
        next_version = load_prefilter_candidates(conn, "next-version")
    finally:
        conn.close()

    assert same_version == []
    assert [candidate.domain for candidate in next_version] == ["ready.com"]


def test_manual_pass_marks_the_domain_and_survives_future_runs(tmp_db: Path) -> None:
    init_db(tmp_db)
    config = load_prefilter_config()
    failed = evaluate_domain(
        "metro-health-clinic.com",
        niche="medical",
        metrics={},
        config=config,
        now=NOW,
    )
    assert failed.passed is False

    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("metro-health-clinic.com", "spamzilla", "2026-11-27", {}, {})],
        )
        set_domain_niches(conn, {"metro-health-clinic.com": ("medical", "lexicon")})
        with transaction(conn):
            set_prefilter_results(conn, {"metro-health-clinic.com": failed})
        with transaction(conn):
            updated = set_prefilter_manual_pass(
                conn,
                "metro-health-clinic.com",
                checked_at="2026-11-27T12:30:00+00:00",
            )

        stored = conn.execute(
            "SELECT prefilter_pass, prefilter_reason, prefilter_details_json, "
            "prefilter_manual_pass, prefilter_manual_pass_at FROM domains "
            "WHERE domain = 'metro-health-clinic.com'"
        ).fetchone()
        same_version = load_prefilter_candidates(conn, load_prefilter_config().version)
        next_version = load_prefilter_candidates(conn, "next-version")
    finally:
        conn.close()

    details = json.loads(stored["prefilter_details_json"])
    assert updated == 1
    assert stored["prefilter_pass"] == 1
    assert stored["prefilter_reason"] == "Manually passed"
    assert stored["prefilter_manual_pass"] == 1
    assert stored["prefilter_manual_pass_at"] == "2026-11-27T12:30:00+00:00"
    assert details["manual_pass"] is True
    assert details["manual_pass_original_reason"] == "Hyphenated"
    assert same_version == []
    assert next_version == []


def test_manual_pass_only_applies_to_failed_domains(tmp_db: Path) -> None:
    init_db(tmp_db)
    config = load_prefilter_config()
    passed = evaluate_domain(
        "bestmedicalclinic.com",
        niche="medical",
        metrics={},
        config=config,
        now=NOW,
    )
    assert passed.passed is True

    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("bestmedicalclinic.com", "spamzilla", "2026-11-27", {}, {})],
        )
        set_domain_niches(conn, {"bestmedicalclinic.com": ("medical", "lexicon")})
        with transaction(conn):
            set_prefilter_results(conn, {"bestmedicalclinic.com": passed})
        with transaction(conn):
            already_passed = set_prefilter_manual_pass(conn, "bestmedicalclinic.com")
            missing = set_prefilter_manual_pass(conn, "missing.com")
    finally:
        conn.close()

    assert already_passed == 0
    assert missing == 0


def test_automated_results_cannot_overwrite_a_manual_pass(tmp_db: Path) -> None:
    init_db(tmp_db)
    config = load_prefilter_config()
    failed = evaluate_domain(
        "metro-health-clinic.com",
        niche="medical",
        metrics={},
        config=config,
        now=NOW,
    )

    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("metro-health-clinic.com", "spamzilla", "2026-11-27", {}, {})],
        )
        set_domain_niches(conn, {"metro-health-clinic.com": ("medical", "lexicon")})
        with transaction(conn):
            set_prefilter_results(conn, {"metro-health-clinic.com": failed})
        with transaction(conn):
            set_prefilter_manual_pass(conn, "metro-health-clinic.com")
        with transaction(conn):
            overwritten = set_prefilter_results(conn, {"metro-health-clinic.com": failed})

        stored = conn.execute(
            "SELECT prefilter_pass, prefilter_reason, prefilter_manual_pass FROM domains "
            "WHERE domain = 'metro-health-clinic.com'"
        ).fetchone()
    finally:
        conn.close()

    assert overwritten == 0
    assert stored["prefilter_pass"] == 1
    assert stored["prefilter_reason"] == "Manually passed"
    assert stored["prefilter_manual_pass"] == 1
