"""Smoke tests for the project scaffold."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import expired_domains
from expired_domains.config import PROJECT_ROOT, load_settings

PATHS_ENV_VARS = (
    "AHREFS_KEY",
    "EXPIRED_DOMAINS_DATA_ROOT",
    "EXPIRED_DOMAINS_DB_PATH",
    "EXPIRED_DOMAINS_RAW_ROOT",
)


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "expired_domains", *args],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
    )


def test_package_imports_and_exposes_version() -> None:
    assert expired_domains.__version__ == "0.1.0"


def test_load_settings_reads_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AHREFS_KEY", "test-key")
    monkeypatch.setenv("EXPIRED_DOMAINS_DATA_ROOT", "/tmp/example-data")

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.ahrefs_key == "test-key"
    assert settings.data_root == Path("/tmp/example-data")
    assert settings.db_path == Path("/tmp/example-data/index.sqlite3")
    assert settings.raw_root == Path("/tmp/example-data/raw/ahrefs")


def test_load_settings_falls_back_to_dotenv_and_defaults(monkeypatch, tmp_path: Path) -> None:
    for name in PATHS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("AHREFS_KEY=from-dotenv\n", encoding="utf-8")

    settings = load_settings(env_file=env_file)

    assert settings.ahrefs_key == "from-dotenv"
    assert settings.data_root == PROJECT_ROOT / "data"
    assert settings.db_path == PROJECT_ROOT / "data" / "index.sqlite3"
    assert settings.raw_root == PROJECT_ROOT / "data" / "raw" / "ahrefs"


def test_load_settings_without_key_is_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AHREFS_KEY", raising=False)

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.ahrefs_key is None


def test_cli_help_exits_zero() -> None:
    result = _run_cli("--help")

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_cli_version_exits_zero() -> None:
    result = _run_cli("--version")

    assert result.returncode == 0
    assert expired_domains.__version__ in result.stdout
