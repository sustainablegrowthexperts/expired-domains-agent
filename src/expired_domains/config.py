"""Application configuration.

Values come from the process environment first, then from a local ``.env``
file. Secret values are never printed or logged here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
DEFAULT_API_BASE = "https://api.ahrefs.com/v3"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_NICHE_LLM_MODEL = "google/gemini-3.5-flash"
DEFAULT_KEYWORD_CHECK_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_EVALUATION_LLM_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_EVALUATION_CHECKS_PER_CYCLE = 5
DEFAULT_NICHE_WORKER_INTERVAL_SECONDS = 10.0
DEFAULT_DR_FLOOR = 10
DEFAULT_DR_CHECK_MAX_ATTEMPTS = 3
DEFAULT_WAYBACK_CHECKS_ENABLED = True
DEFAULT_WAYBACK_CHECK_MAX_ATTEMPTS = 3
DEFAULT_WAYBACK_CHECKS_PER_CYCLE = 1
DEFAULT_WAYBACK_RATE_PER_SEC = 1.0
DEFAULT_WAYBACK_LLM_MODEL = DEFAULT_NICHE_LLM_MODEL
DEFAULT_LIVE_CHECK_ENABLED = True
DEFAULT_LIVE_CHECK_MAX_ATTEMPTS = 3
DEFAULT_LIVE_CHECKS_PER_CYCLE = 1
DEFAULT_LIVE_CHECK_TIMEOUT_SECONDS = 10.0
DEFAULT_LIVE_CHECK_MAX_REDIRECTS = 10
DEFAULT_LIVE_CHECK_MAX_BODY_BYTES = 50_000
DEFAULT_DATA_COLLECTION_MONTHLY_UNITS = 800_000
DEFAULT_DATA_COLLECTION_RESERVE_UNITS = 50_000
DEFAULT_DATA_COLLECTION_ESTIMATED_UNITS = 3_400
DEFAULT_DATA_COLLECTION_MAX_PER_CYCLE = 1


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings."""

    ahrefs_key: str | None
    data_root: Path
    db_path: Path
    raw_root: Path
    api_base: str = DEFAULT_API_BASE
    openrouter_key: str | None = None
    openrouter_base_url: str = DEFAULT_OPENROUTER_BASE_URL
    niche_llm_model: str = DEFAULT_NICHE_LLM_MODEL
    keyword_check_model: str = DEFAULT_KEYWORD_CHECK_MODEL
    evaluation_llm_model: str = DEFAULT_EVALUATION_LLM_MODEL
    evaluation_checks_per_cycle: int = DEFAULT_EVALUATION_CHECKS_PER_CYCLE
    niche_worker_interval: float = DEFAULT_NICHE_WORKER_INTERVAL_SECONDS
    ahrefs_free_dr_check: str | None = None
    dr_floor: int = DEFAULT_DR_FLOOR
    dr_check_max_attempts: int = DEFAULT_DR_CHECK_MAX_ATTEMPTS
    wayback_checks_enabled: bool = DEFAULT_WAYBACK_CHECKS_ENABLED
    wayback_check_max_attempts: int = DEFAULT_WAYBACK_CHECK_MAX_ATTEMPTS
    wayback_checks_per_cycle: int = DEFAULT_WAYBACK_CHECKS_PER_CYCLE
    wayback_rate_per_sec: float = DEFAULT_WAYBACK_RATE_PER_SEC
    wayback_llm_model: str = DEFAULT_WAYBACK_LLM_MODEL
    live_check_enabled: bool = DEFAULT_LIVE_CHECK_ENABLED
    live_check_max_attempts: int = DEFAULT_LIVE_CHECK_MAX_ATTEMPTS
    live_checks_per_cycle: int = DEFAULT_LIVE_CHECKS_PER_CYCLE
    live_check_timeout_seconds: float = DEFAULT_LIVE_CHECK_TIMEOUT_SECONDS
    live_check_max_redirects: int = DEFAULT_LIVE_CHECK_MAX_REDIRECTS
    live_check_max_body_bytes: int = DEFAULT_LIVE_CHECK_MAX_BODY_BYTES
    data_collection_enabled: bool = False
    data_collection_monthly_units: int = DEFAULT_DATA_COLLECTION_MONTHLY_UNITS
    data_collection_reserve_units: int = DEFAULT_DATA_COLLECTION_RESERVE_UNITS
    data_collection_estimated_units: int = DEFAULT_DATA_COLLECTION_ESTIMATED_UNITS
    data_collection_max_per_cycle: int = DEFAULT_DATA_COLLECTION_MAX_PER_CYCLE

    def require_ahrefs_key(self) -> str:
        """Return the Ahrefs key or raise a secret-free configuration error."""
        if not self.ahrefs_key:
            raise ConfigError("AHREFS_KEY is required for Ahrefs retrieval")
        return self.ahrefs_key


def _path_from_env(name: str, default: Path) -> Path:
    """Return an absolute path from ``name``, or ``default`` when unset."""
    raw = os.getenv(name)
    if not raw:
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _float_from_env(name: str, default: float) -> float:
    """Return a float from ``name``, or ``default`` when unset or invalid."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_from_env(name: str, default: int) -> int:
    """Return a non-negative integer from ``name``, or ``default``."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def _bool_from_env(name: str, default: bool = False) -> bool:
    """Return a boolean from ``name``, accepting common truthy spellings."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from the environment, falling back to ``.env``.

    ``env_file`` defaults to the project-root ``.env``. Existing environment
    variables take precedence over values in the file.
    """
    load_dotenv(dotenv_path=env_file or PROJECT_ROOT / ".env", override=False)

    data_root = _path_from_env("EXPIRED_DOMAINS_DATA_ROOT", DEFAULT_DATA_ROOT)

    return Settings(
        ahrefs_key=os.getenv("AHREFS_KEY") or None,
        data_root=data_root,
        db_path=_path_from_env("EXPIRED_DOMAINS_DB_PATH", data_root / "index.sqlite3"),
        raw_root=_path_from_env("EXPIRED_DOMAINS_RAW_ROOT", data_root / "raw" / "ahrefs"),
        openrouter_key=os.getenv("OPENROUTER_API_KEY") or None,
        openrouter_base_url=(os.getenv("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL),
        niche_llm_model=os.getenv("NICHE_LLM_MODEL") or DEFAULT_NICHE_LLM_MODEL,
        keyword_check_model=(
            os.getenv("KEYWORD_CHECK_MODEL")
            or os.getenv("keyword_check_model")
            or DEFAULT_KEYWORD_CHECK_MODEL
        ),
        evaluation_llm_model=(
            os.getenv("EVALUATION_LLM_MODEL")
            or os.getenv("evaluation_llm_model")
            or DEFAULT_EVALUATION_LLM_MODEL
        ),
        evaluation_checks_per_cycle=max(
            1,
            _int_from_env(
                "EVALUATION_CHECKS_PER_CYCLE",
                DEFAULT_EVALUATION_CHECKS_PER_CYCLE,
            ),
        ),
        niche_worker_interval=_float_from_env(
            "NICHE_WORKER_INTERVAL_SECONDS",
            DEFAULT_NICHE_WORKER_INTERVAL_SECONDS,
        ),
        ahrefs_free_dr_check=os.getenv("AHREFS_FREE_DR_CHECK") or None,
        dr_floor=min(100, _int_from_env("DR_FLOOR", DEFAULT_DR_FLOOR)),
        dr_check_max_attempts=max(
            1,
            _int_from_env("DR_CHECK_MAX_ATTEMPTS", DEFAULT_DR_CHECK_MAX_ATTEMPTS),
        ),
        wayback_checks_enabled=_bool_from_env(
            "WAYBACK_CHECKS_ENABLED",
            DEFAULT_WAYBACK_CHECKS_ENABLED,
        ),
        wayback_check_max_attempts=max(
            1,
            _int_from_env(
                "WAYBACK_CHECK_MAX_ATTEMPTS",
                DEFAULT_WAYBACK_CHECK_MAX_ATTEMPTS,
            ),
        ),
        wayback_checks_per_cycle=max(
            1,
            _int_from_env("WAYBACK_CHECKS_PER_CYCLE", DEFAULT_WAYBACK_CHECKS_PER_CYCLE),
        ),
        wayback_rate_per_sec=max(
            0.1,
            _float_from_env("WAYBACK_RATE_PER_SEC", DEFAULT_WAYBACK_RATE_PER_SEC),
        ),
        wayback_llm_model=(
            os.getenv("WAYBACK_LLM_MODEL")
            or os.getenv("wayback_llm_model")
            or DEFAULT_WAYBACK_LLM_MODEL
        ),
        live_check_enabled=_bool_from_env(
            "LIVE_CHECK_ENABLED",
            DEFAULT_LIVE_CHECK_ENABLED,
        ),
        live_check_max_attempts=max(
            1,
            _int_from_env("LIVE_CHECK_MAX_ATTEMPTS", DEFAULT_LIVE_CHECK_MAX_ATTEMPTS),
        ),
        live_checks_per_cycle=max(
            1,
            _int_from_env("LIVE_CHECKS_PER_CYCLE", DEFAULT_LIVE_CHECKS_PER_CYCLE),
        ),
        live_check_timeout_seconds=max(
            0.1,
            _float_from_env("LIVE_CHECK_TIMEOUT_SECONDS", DEFAULT_LIVE_CHECK_TIMEOUT_SECONDS),
        ),
        live_check_max_redirects=_int_from_env(
            "LIVE_CHECK_MAX_REDIRECTS",
            DEFAULT_LIVE_CHECK_MAX_REDIRECTS,
        ),
        live_check_max_body_bytes=max(
            1,
            _int_from_env("LIVE_CHECK_MAX_BODY_BYTES", DEFAULT_LIVE_CHECK_MAX_BODY_BYTES),
        ),
        data_collection_enabled=_bool_from_env("DATA_COLLECTION_ENABLED", False),
        data_collection_monthly_units=_int_from_env(
            "DATA_COLLECTION_MONTHLY_UNITS",
            DEFAULT_DATA_COLLECTION_MONTHLY_UNITS,
        ),
        data_collection_reserve_units=_int_from_env(
            "DATA_COLLECTION_RESERVE_UNITS",
            DEFAULT_DATA_COLLECTION_RESERVE_UNITS,
        ),
        data_collection_estimated_units=_int_from_env(
            "DATA_COLLECTION_ESTIMATED_UNITS",
            DEFAULT_DATA_COLLECTION_ESTIMATED_UNITS,
        ),
        data_collection_max_per_cycle=_int_from_env(
            "DATA_COLLECTION_MAX_PER_CYCLE",
            DEFAULT_DATA_COLLECTION_MAX_PER_CYCLE,
        ),
    )
