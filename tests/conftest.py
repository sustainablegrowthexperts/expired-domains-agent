"""Shared offline fixtures for the Ahrefs test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from expired_domains.config import PROJECT_ROOT, Settings

FIXTURES_RUNS = PROJECT_ROOT / "tests" / "fixtures" / "runs"

ENV_VARS = (
    "AHREFS_KEY",
    "EXPIRED_DOMAINS_DATA_ROOT",
    "EXPIRED_DOMAINS_DB_PATH",
    "EXPIRED_DOMAINS_RAW_ROOT",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer machine's environment and ``.env`` out of tests."""
    for name in ENV_VARS:
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
        path
        for path in FIXTURES_RUNS.glob(f"*/{name}")
        if (path / "manifest.json").is_file()
    )
    if matches:
        return matches[0]

    pytest.skip("Ahrefs fixture corpus not available")


def make_settings(tmp_path: Path, *, ahrefs_key: str | None = "test-key") -> Settings:
    """Build explicit settings that never consult the local ``.env``."""
    data_root = tmp_path / "data"
    return Settings(
        ahrefs_key=ahrefs_key,
        data_root=data_root,
        db_path=tmp_path / "index.sqlite3",
        raw_root=data_root / "raw" / "ahrefs",
    )


@pytest.fixture
def settings_factory(tmp_path: Path):
    """Return a factory for explicit settings objects."""

    def factory(*, ahrefs_key: str | None = "test-key") -> Settings:
        return make_settings(tmp_path, ahrefs_key=ahrefs_key)

    return factory
