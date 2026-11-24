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
    )
