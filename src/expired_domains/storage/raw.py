"""Run directory naming and raw payload persistence."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from expired_domains.config import PROJECT_ROOT
from expired_domains.models import RequestRecord


def make_run_id(domain: str, now_utc: datetime) -> str:
    """Return the immutable run identifier for ``domain``."""
    return f"{domain}:{now_utc.strftime('%Y%m%dT%H%M%SZ')}:ahrefs"


def run_dir_name(run_id: str) -> str:
    """Return the filesystem-safe directory name for ``run_id``."""
    return run_id.replace(":", "_")


def create_run_dir(raw_root: Path, domain: str, run_id: str) -> Path:
    """Create a fresh run directory, refusing to reuse an existing one."""
    run_dir = Path(raw_root) / domain / run_dir_name(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_request_files(run_dir: Path, record: RequestRecord) -> str:
    """Write the payload/header pair and return the project-relative payload path."""
    run_dir = Path(run_dir)
    stem = f"{record.query_id}.p{record.page}"
    payload_path = run_dir / f"{stem}.json"
    headers_path = run_dir / f"{stem}.headers.json"

    document = {
        "request": record.as_request_dict(),
        "payload": record.payload,
        "raw_body": None,
    }
    _write_exclusive(payload_path, json.dumps(document, ensure_ascii=False, indent=2))
    _write_exclusive(headers_path, json.dumps(record.headers, ensure_ascii=False, indent=2))

    relative = os.path.relpath(payload_path, PROJECT_ROOT)
    return Path(relative).as_posix()


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> Path:
    """Write a deterministic ``manifest.json`` into a fresh run directory."""
    path = Path(run_dir) / "manifest.json"
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    _write_exclusive(path, text)
    return path


def _write_exclusive(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` without ever overwriting existing content."""
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
