"""Synchronous Ahrefs HTTP client with bounded retries and raw capture."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx

from expired_domains.config import Settings
from expired_domains.models import QuerySpec, RequestRecord
from expired_domains.storage.raw import write_request_files

MAX_ATTEMPTS = 6
BACKOFF_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 120.0


class AhrefsClient:
    """Offline-testable Ahrefs API client."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=settings.api_base,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {settings.ahrefs_key or ''}"},
            transport=transport,
        )

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._http.close()

    def execute(self, spec: QuerySpec, run_dir: Path) -> RequestRecord:
        """Execute one query and always return a record instead of raising."""
        target = httpx.URL(
            f"{self._settings.api_base.rstrip('/')}{spec.endpoint}",
            params=spec.params or None,
        )
        started_at = _utc_timestamp()

        response: httpx.Response | None = None
        attempts = 0
        error: str | None = None
        delay = BACKOFF_SECONDS

        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts = attempt
            try:
                response = self._http.get(target)
            except httpx.TransportError as exc:
                response = None
                error = f"{type(exc).__name__}: {exc}"
                if attempt < MAX_ATTEMPTS:
                    self._sleep(delay)
                    delay *= 2
                    continue
                break

            status = response.status_code
            if status == 429 or 500 <= status <= 599:
                error = f"HTTP {status} after {attempt} attempt(s)"
                if attempt < MAX_ATTEMPTS:
                    self._sleep(delay)
                    delay *= 2
                    continue
                break

            error = f"HTTP {status}" if status >= 400 else None
            break

        finished_at = _utc_timestamp()
        headers = dict(response.headers) if response is not None else {}
        payload = None
        parse_error = None
        if response is not None and response.status_code < 400:
            try:
                payload = response.json()
            except (ValueError, UnicodeDecodeError) as exc:
                parse_error = f"{type(exc).__name__}: {exc}"

        record = RequestRecord(
            query_id=spec.query_id,
            endpoint=spec.endpoint,
            page=spec.page,
            params=dict(spec.params),
            url=str(response.request.url) if response is not None else str(target),
            status=response.status_code if response is not None else None,
            started_at=started_at,
            finished_at=finished_at,
            attempts=attempts,
            error=error,
            parse_error=parse_error,
            rows=headers.get("x-api-rows"),
            units_cost_total=headers.get("x-api-units-cost-total"),
            units_cost_total_actual=headers.get("x-api-units-cost-total-actual"),
            units_cost_row=headers.get("x-api-units-cost-row"),
            cache=headers.get("x-api-cache"),
            headers=headers,
            payload=payload,
        )

        if record.status is not None and record.status < 400 and payload is not None:
            raw_path = write_request_files(run_dir, record)
            record = replace(record, raw_path=raw_path)

        return record


def _utc_timestamp() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()
