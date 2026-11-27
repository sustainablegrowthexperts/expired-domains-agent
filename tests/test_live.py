"""Manual live verification against the real Ahrefs API.

The test is deselected by default (``-m "not live"``) and only runs in an
explicit ``pytest -m live`` invocation with ``AHREFS_KEY`` and
``EXPIRED_DOMAINS_LIVE_DOMAIN`` present in the process environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from expired_domains.cli import main
from expired_domains.config import load_settings
from expired_domains.storage.db import connect
from expired_domains.storage.raw import run_dir_name

pytestmark = pytest.mark.live

QUERY_IDS = (
    "limits_before",
    "stats",
    "refdomains_history",
    "metrics_history",
    "top_dr_backlinks",
    "latest_backlinks",
    "lexicon_anchors",
    "top_anchors",
    "limits_after",
)


def test_live_enrich_writes_raw_files_index_rows_and_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("AHREFS_KEY"):
        pytest.skip("AHREFS_KEY not set")
    domain = os.environ.get("EXPIRED_DOMAINS_LIVE_DOMAIN")
    if not domain:
        pytest.skip("EXPIRED_DOMAINS_LIVE_DOMAIN not set; choose a fresh domain")

    raw_root = tmp_path / "raw" / "ahrefs"
    db_path = tmp_path / "index.sqlite3"
    monkeypatch.setenv("EXPIRED_DOMAINS_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("EXPIRED_DOMAINS_DB_PATH", str(db_path))
    monkeypatch.setenv("EXPIRED_DOMAINS_RAW_ROOT", str(raw_root))
    settings = load_settings(env_file=tmp_path / "missing.env")

    exit_code = main(["enrich", domain], settings=settings)

    conn = connect(db_path)
    try:
        run = conn.execute("SELECT * FROM runs").fetchone()
        assert run is not None
        run_id = run["run_id"]
        error = run["error"]
        coverage = json.loads(run["coverage_json"])

        request_ids = {
            row["query_id"]
            for row in conn.execute("SELECT query_id FROM requests WHERE run_id = ?", (run_id,))
        }
        assert request_ids == set(QUERY_IDS)

        counts = {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            for table in ("stats", "history", "backlinks", "anchors")
        }
        assert counts["stats"] == 2
        assert counts["history"] > 0
        assert counts["backlinks"] > 0
        assert counts["anchors"] > 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM history WHERE run_id = ? AND series = 'refdomains'",
                (run_id,),
            ).fetchone()[0]
            > 0
        )
    finally:
        conn.close()

    run_dir = raw_root / domain / run_dir_name(run_id)
    assert (run_dir / "manifest.json").is_file()
    for query_id in QUERY_IDS:
        assert (run_dir / f"{query_id}.p0.json").is_file()
        assert (run_dir / f"{query_id}.p0.headers.json").is_file()

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert coverage["history_months"] == 48
    assert isinstance(coverage["lexicon_complete"], bool)

    assert exit_code in (0, 1)
    if exit_code == 1:
        assert error, "a non-zero exit must report the failed request in runs.error"

    actual = manifest["units_actual_total"]
    billed = manifest["units_billed_total"]
    cache_hits = [
        entry["query_id"] for entry in manifest["requests"] if entry.get("cache") == "hit"
    ]
    assert actual > 0
    assert billed >= actual
    assert 2700 <= billed <= 4100
    if not 2700 <= actual <= 4100:
        assert cache_hits, f"actual units {actual} outside 2700-4100 without cache hits"
    print(f"live units: actual={actual} billed={billed} cache_hits={cache_hits}")
