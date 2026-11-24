"""Offline client tests using ``httpx.MockTransport``."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from expired_domains.ahrefs.client import AhrefsClient
from expired_domains.models import QuerySpec, RequestRecord, Units

STATS_PAYLOAD = {
    "metrics": {
        "live": 1159,
        "all_time": 5979,
        "live_refdomains": 992,
        "all_time_refdomains": 1599,
    }
}

CACHE_HIT_HEADERS = {
    "content-type": "application/json",
    "x-api-cache": "hit",
    "x-api-units-cost-total": "50",
    "x-api-units-cost-total-actual": "0",
    "x-api-units-cost-row": "12",
    "x-api-rows": "1",
}

CACHE_MISS_HEADERS = {
    "content-type": "application/json",
    "x-api-cache": "miss",
    "x-api-units-cost-total": "50",
    "x-api-units-cost-total-actual": "50",
    "x-api-units-cost-row": "12",
    "x-api-rows": "1",
}


def _stats_spec() -> QuerySpec:
    return QuerySpec(
        query_id="stats",
        endpoint="/site-explorer/backlinks-stats",
        params={"target": "smoke-fire.com", "mode": "domain", "date": "2026-11-24"},
    )


def _client(settings_factory, handler, *, sleeps: list[float] | None = None) -> AhrefsClient:
    recorded = sleeps if sleeps is not None else []
    return AhrefsClient(
        settings_factory(),
        transport=httpx.MockTransport(handler),
        sleep=recorded.append,
    )


def test_successful_response_writes_raw_pair_and_captures_units(
    tmp_path: Path, settings_factory
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STATS_PAYLOAD, headers=CACHE_HIT_HEADERS)

    record = _client(settings_factory, handler).execute(_stats_spec(), run_dir)

    assert isinstance(record, RequestRecord)
    assert record.status == 200
    assert record.attempts == 1
    assert record.error is None
    assert record.parse_error is None
    assert record.cache == "hit"
    assert record.headers["x-api-units-cost-total"] == "50"
    assert record.units == Units(actual=0, billed=50)

    payload_path = run_dir / "stats.p0.json"
    headers_path = run_dir / "stats.p0.headers.json"
    assert payload_path.is_file()
    assert headers_path.is_file()

    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    assert set(raw) == {"request", "payload", "raw_body"}
    assert raw["request"]["query_id"] == "stats"
    assert raw["request"]["status"] == 200
    assert raw["payload"] == STATS_PAYLOAD
    assert raw["raw_body"] is None

    saved_headers = json.loads(headers_path.read_text(encoding="utf-8"))
    assert saved_headers["x-api-units-cost-total"] == "50"


def test_cache_hit_charges_zero_actual_and_fifty_billed(
    tmp_path: Path, settings_factory
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STATS_PAYLOAD, headers=CACHE_HIT_HEADERS)

    record = _client(settings_factory, handler).execute(_stats_spec(), run_dir)

    assert record.units == Units(actual=0, billed=50)


def test_cache_miss_charges_actual_equal_to_billed(
    tmp_path: Path, settings_factory
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STATS_PAYLOAD, headers=CACHE_MISS_HEADERS)

    record = _client(settings_factory, handler).execute(_stats_spec(), run_dir)

    assert record.units == Units(actual=50, billed=50)


def test_429_then_200_retries_once_with_backoff(
    tmp_path: Path, settings_factory
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if len(sleeps) == 0:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=STATS_PAYLOAD, headers=CACHE_HIT_HEADERS)

    record = _client(settings_factory, handler, sleeps=sleeps).execute(
        _stats_spec(), run_dir
    )

    assert record.status == 200
    assert record.attempts == 2
    assert sleeps == [0.5]


def test_six_consecutive_500s_are_recorded_without_raising(
    tmp_path: Path, settings_factory
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    record = _client(settings_factory, handler).execute(_stats_spec(), run_dir)

    assert record.status == 500
    assert record.attempts == 6
    assert record.error is not None
    assert record.raw_path is not None

    payload_path = run_dir / "stats.p0.json"
    headers_path = run_dir / "stats.p0.headers.json"
    assert payload_path.is_file()
    assert headers_path.is_file()

    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    assert raw["payload"] is None
    assert raw["request"]["query_id"] == "stats"
    assert raw["request"]["status"] == 500
    assert raw["request"]["attempts"] == 6
    assert raw["request"]["error"] == record.error

    saved_headers = json.loads(headers_path.read_text(encoding="utf-8"))
    assert "content-type" in saved_headers


def test_401_is_not_retried(tmp_path: Path, settings_factory) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    record = _client(settings_factory, handler, sleeps=sleeps).execute(
        _stats_spec(), run_dir
    )

    assert record.status == 401
    assert record.attempts == 1
    assert sleeps == []


def test_malformed_json_sets_parse_error(tmp_path: Path, settings_factory) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{not valid json",
            headers={"content-type": "application/json", "x-api-rows": "1"},
        )

    record = _client(settings_factory, handler).execute(_stats_spec(), run_dir)

    assert record.status == 200
    assert record.attempts == 1
    assert record.parse_error is not None
    assert record.payload is None
