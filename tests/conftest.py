"""Shared offline fixtures for the Ahrefs test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from expired_domains.config import PROJECT_ROOT, Settings

FIXTURES_RUNS = PROJECT_ROOT / "tests" / "fixtures" / "runs"

ENV_VARS = (
    "AHREFS_KEY",
    "AHREFS_FREE_DR_CHECK",
    "DR_FLOOR",
    "DR_CHECK_MAX_ATTEMPTS",
    "WAYBACK_CHECKS_ENABLED",
    "WAYBACK_CHECK_MAX_ATTEMPTS",
    "WAYBACK_CHECKS_PER_CYCLE",
    "WAYBACK_RATE_PER_SEC",
    "WAYBACK_LLM_MODEL",
    "LIVE_CHECK_ENABLED",
    "LIVE_CHECK_MAX_ATTEMPTS",
    "LIVE_CHECKS_PER_CYCLE",
    "LIVE_CHECK_TIMEOUT_SECONDS",
    "LIVE_CHECK_MAX_REDIRECTS",
    "LIVE_CHECK_MAX_BODY_BYTES",
    "EXPIRED_DOMAINS_DATA_ROOT",
    "EXPIRED_DOMAINS_DB_PATH",
    "EXPIRED_DOMAINS_RAW_ROOT",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "NICHE_LLM_MODEL",
    "KEYWORD_CHECK_MODEL",
    "keyword_check_model",
    "EVALUATION_LLM_MODEL",
    "evaluation_llm_model",
    "EVALUATION_CHECKS_PER_CYCLE",
    "NICHE_WORKER_INTERVAL_SECONDS",
    "DATA_COLLECTION_ENABLED",
    "DATA_COLLECTION_MONTHLY_UNITS",
    "DATA_COLLECTION_RESERVE_UNITS",
    "DATA_COLLECTION_ESTIMATED_UNITS",
    "DATA_COLLECTION_MAX_PER_CYCLE",
)


@pytest.fixture(autouse=True)
def clean_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer machine's environment and ``.env`` out of tests.

    Live checks are the one exception: they need the real ``AHREFS_KEY`` from
    the process environment so the explicit ``-m live`` run can happen.
    """
    is_live = request.node.get_closest_marker("live") is not None
    for name in ENV_VARS:
        if is_live and name == "AHREFS_KEY":
            continue
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """A SQLite index path rooted in ``tmp_path``."""
    return tmp_path / "index.sqlite3"


@pytest.fixture
def tmp_raw_root(tmp_path: Path) -> Path:
    """A raw Ahrefs root rooted in ``tmp_path``."""
    return tmp_path / "raw" / "ahrefs"


def reference_run_dir(name: str) -> Path:
    """Return a reference run directory, or skip when the corpus is absent."""
    direct = FIXTURES_RUNS / name
    if (direct / "manifest.json").is_file():
        return direct

    matches = sorted(
        path for path in FIXTURES_RUNS.glob(f"*/{name}") if (path / "manifest.json").is_file()
    )
    if matches:
        return matches[0]

    pytest.skip("Ahrefs fixture corpus not available")


def run_dirs() -> list[Path]:
    """Return every reference run directory containing ``manifest.json``.

    The corpus also contains a non-run ``123eft.com/lexicon_probes`` directory;
    filtering on the manifest keeps that out of replay parametrisation.
    """
    if not FIXTURES_RUNS.is_dir():
        pytest.skip("Ahrefs fixture corpus not available")

    directories = sorted(
        path.parent for path in FIXTURES_RUNS.glob("*/*/manifest.json") if path.is_file()
    )
    if not directories:
        pytest.skip("Ahrefs fixture corpus has no manifest-bearing run directories")
    return directories


def make_settings(tmp_path: Path, *, ahrefs_key: str | None = "test-key") -> Settings:
    """Build explicit settings that never consult the local ``.env``."""
    data_root = tmp_path / "data"
    return Settings(
        ahrefs_key=ahrefs_key,
        data_root=data_root,
        db_path=tmp_path / "index.sqlite3",
        raw_root=data_root / "raw" / "ahrefs",
        wayback_checks_enabled=False,
        live_check_enabled=False,
    )


@pytest.fixture
def settings_factory(tmp_path: Path):
    """Return a factory for explicit settings objects."""

    def factory(*, ahrefs_key: str | None = "test-key") -> Settings:
        return make_settings(tmp_path, ahrefs_key=ahrefs_key)

    return factory
