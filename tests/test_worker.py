"""Automatic pending-niche worker."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import make_settings
from expired_domains import cli
from expired_domains.models import DomainObservation
from expired_domains.niches import LLM_UNKNOWN_NICHE, STUB_LLM_NICHE
from expired_domains.storage.db import (
    connect,
    init_db,
    list_pending_domains,
    set_domain_blocks,
    set_domain_niches,
    store_domain_observations,
)
from expired_domains.web.app import create_app
from expired_domains.worker import NicheWorker, classify_pending, prefilter_pending


def _store_pending(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("bestmedicalclinic.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("zooble-brandable.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("already-classified.com", "spamzilla", "2026-11-27", {}, {}),
            ],
        )
        set_domain_niches(conn, {"already-classified.com": ("medical", "lexicon")})
        conn.commit()
    finally:
        conn.close()


def test_classify_pending_processes_only_null_niches(tmp_db: Path) -> None:
    _store_pending(tmp_db)

    processed = classify_pending(tmp_db)

    conn = connect(tmp_db)
    try:
        stored = {
            row["domain"]: (row["niche"], row["niche_source"])
            for row in conn.execute("SELECT domain, niche, niche_source FROM domains")
        }
        pending = list_pending_domains(conn)
    finally:
        conn.close()

    assert processed == 2
    assert pending == []
    assert stored["already-classified.com"] == ("medical", "lexicon")
    assert stored["bestmedicalclinic.com"] == ("medical", "lexicon")
    assert stored["zooble-brandable.com"] == (STUB_LLM_NICHE, "stub_llm")


def test_niche_worker_classifies_pending_domains_in_the_background(tmp_db: Path) -> None:
    _store_pending(tmp_db)
    worker = NicheWorker(tmp_db, interval=0.1)
    worker.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            conn = connect(tmp_db)
            try:
                pending = list_pending_domains(conn)
            finally:
                conn.close()
            if not pending:
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    conn = connect(tmp_db)
    try:
        pending = list_pending_domains(conn)
        zooble = conn.execute(
            "SELECT niche FROM domains WHERE domain = 'zooble-brandable.com'"
        ).fetchone()
    finally:
        conn.close()

    assert pending == []
    assert zooble["niche"] == STUB_LLM_NICHE


def test_niche_worker_prefilters_after_classification(tmp_db: Path) -> None:
    _store_pending(tmp_db)
    worker = NicheWorker(tmp_db, interval=0.1)
    worker.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            conn = connect(tmp_db)
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) AS n FROM domains WHERE prefilter_pass IS NULL"
                ).fetchone()["n"]
            finally:
                conn.close()
            if remaining == 0:
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    conn = connect(tmp_db)
    try:
        stored = {
            row["domain"]: row["prefilter_pass"]
            for row in conn.execute("SELECT domain, prefilter_pass FROM domains")
        }
    finally:
        conn.close()

    assert stored == {
        "already-classified.com": 1,
        "bestmedicalclinic.com": 1,
        "zooble-brandable.com": 1,
    }


def test_web_app_startup_starts_the_worker_automatically(tmp_path: Path, tmp_db: Path) -> None:
    _store_pending(tmp_db)
    app = create_app(settings=make_settings(tmp_path), db_path=tmp_db)

    with TestClient(app):
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            conn = connect(tmp_db)
            try:
                pending = list_pending_domains(conn)
            finally:
                conn.close()
            if not pending:
                break
            time.sleep(0.05)

    assert pending == []


def test_prefilter_pending_processes_only_classified_unblocked_domains(
    tmp_db: Path,
) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [
                DomainObservation("bestmedicalclinic.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("metro-health-clinic.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("pending.com", "spamzilla", "2026-11-27", {}, {}),
                DomainObservation("blocked.com", "spamzilla", "2026-11-27", {}, {}),
            ],
        )
        set_domain_niches(
            conn,
            {
                "bestmedicalclinic.com": ("medical", "lexicon"),
                "metro-health-clinic.com": ("medical", "lexicon"),
                "blocked.com": ("medical", "lexicon"),
            },
        )
        set_domain_blocks(conn, {"blocked.com": ("gambling", ("casino",))})
        conn.commit()
    finally:
        conn.close()

    updated = prefilter_pending(tmp_db)

    conn = connect(tmp_db)
    try:
        stored = {
            row["domain"]: (row["prefilter_pass"], row["prefilter_reason"])
            for row in conn.execute("SELECT domain, prefilter_pass, prefilter_reason FROM domains")
        }
    finally:
        conn.close()

    assert updated == 2
    assert stored["bestmedicalclinic.com"] == (1, "Passed all rules")
    assert stored["metro-health-clinic.com"] == (0, "Hyphenated")
    assert stored["pending.com"] == (None, None)
    assert stored["blocked.com"] == (None, None)


def test_worker_cli_once_processes_the_current_backlog(
    tmp_path: Path, tmp_db: Path, capsys
) -> None:
    _store_pending(tmp_db)

    exit_code = cli.main(
        ["worker", "--once", "--db", str(tmp_db)],
        settings=make_settings(tmp_path),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "worker: classified=2 prefilted=3" in captured.out
    assert "live_checked=0" in captured.out


def test_worker_cli_reports_a_missing_index(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.sqlite3"

    exit_code = cli.main(
        ["worker", "--once", "--db", str(missing)],
        settings=make_settings(tmp_path),
    )

    assert exit_code == 2
    assert "index not found" in capsys.readouterr().err


def test_serve_command_can_disable_the_worker() -> None:
    args = cli.build_parser().parse_args(["serve", "--no-worker"])

    assert args.no_worker is True


def test_llm_unknown_is_not_treated_as_pending(tmp_db: Path) -> None:
    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        store_domain_observations(
            conn,
            [DomainObservation("terminal.com", "spamzilla", "2026-11-27", {}, {})],
        )
        set_domain_niches(conn, {"terminal.com": (LLM_UNKNOWN_NICHE, "llm_unknown")})
        conn.commit()
    finally:
        conn.close()

    assert classify_pending(tmp_db) == 0
