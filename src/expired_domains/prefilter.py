"""Deterministic local prefilter for niche-classified candidate domains.

The local prefilter is the second evaluation stage, after discovery and niche
classification. It only uses data already stored with an observation, so it
never spends provider units. The gates are intentionally conservative and
config-driven; a gate that cannot be evaluated from the available data is
skipped rather than failed.

Domain age, Wayback checks, and provider authority metrics belong to later
stages and are deliberately not part of this module.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from expired_domains.config import PROJECT_ROOT
from expired_domains.domaintext import domain_body, domain_suffix
from expired_domains.models import PrefilterCandidate, PrefilterResult

DEFAULT_PREFILTER_PATH = PROJECT_ROOT / "config" / "prefilter" / "v3.json"

PASS_REASON = "Passed all rules"
DISABLED_REASON = "Prefilter disabled"

# One observation per domain: the latest collection date wins; known sources
# break same-day ties in the same order as the discovery view.
_PREFILTER_CANDIDATES_SQL = """
WITH ranked AS (
    SELECT
        domain,
        metrics_json,
        ROW_NUMBER() OVER (
            PARTITION BY domain
            ORDER BY
                collected_date DESC,
                CASE source
                    WHEN 'godaddy_auctions' THEN 0
                    WHEN 'spamzilla' THEN 1
                    WHEN 'expireddomains' THEN 2
                    ELSE 3
                END,
                source
        ) AS row_number
    FROM domain_observations
)
SELECT
    domains.domain AS domain,
    domains.niche AS niche,
    ranked.metrics_json AS metrics_json
FROM domains
LEFT JOIN ranked ON ranked.domain = domains.domain AND ranked.row_number = 1
WHERE domains.niche IS NOT NULL
  AND domains.niche != 'unknown'
  AND domains.blocked_category IS NULL
  AND COALESCE(domains.prefilter_manual_pass, 0) = 0
  AND (
      domains.prefilter_pass IS NULL
      OR domains.prefilter_config_version IS NULL
      OR domains.prefilter_config_version != ?
  )
ORDER BY domains.domain
"""


@dataclass(frozen=True)
class LengthRule:
    """Registrable-name length bounds, excluding the public suffix."""

    enabled: bool = True
    minimum: int = 3
    maximum: int = 35


@dataclass(frozen=True)
class HyphenRule:
    """Maximum hyphen count in the registrable name."""

    enabled: bool = True
    maximum: int = 1


@dataclass(frozen=True)
class NumberRule:
    """Maximum digit count in the registrable name."""

    enabled: bool = True
    maximum: int = 1


@dataclass(frozen=True)
class TldRule:
    """Optional public-suffix allowlist."""

    enabled: bool = False
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriceRule:
    """Maximum known listing price in US dollars."""

    enabled: bool = True
    maximum_usd: float = 500.0


@dataclass(frozen=True)
class TimeRule:
    """Minimum known hours remaining before an auction closes."""

    enabled: bool = True
    minimum_hours: float = 6.0


@dataclass(frozen=True)
class GateSet:
    """The resolved gate values for one domain's niche."""

    length: LengthRule
    hyphens: HyphenRule
    numbers: NumberRule
    tld: TldRule
    price: PriceRule
    time_to_close: TimeRule


@dataclass(frozen=True)
class PrefilterConfig:
    """A versioned prefilter config with optional per-niche gate overrides."""

    version: str
    enabled: bool
    raw: Mapping[str, Any]
    niche_overrides: Mapping[str, Mapping[str, Any]]

    def gates_for(self, niche: str | None) -> GateSet:
        """Return the gate set for ``niche``, applying its overrides."""
        data: Mapping[str, Any] = self.raw
        if niche and niche in self.niche_overrides:
            data = _merge_gate_data(self.raw, self.niche_overrides[niche])
        return _parse_gate_set(data)


def load_prefilter_config(path: Path | None = None) -> PrefilterConfig:
    """Load ``config/prefilter/v3.json`` once per process."""
    return _load_prefilter_config(str(path or DEFAULT_PREFILTER_PATH))


@cache
def _load_prefilter_config(path: str) -> PrefilterConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("prefilter config must be a JSON object")

    overrides = data.get("niche_overrides") or {}
    if not isinstance(overrides, dict):
        raise ValueError("prefilter niche_overrides must be a JSON object")

    return PrefilterConfig(
        version=str(data.get("version") or "v1"),
        enabled=_as_bool(data.get("enabled"), True),
        raw=data,
        niche_overrides={
            str(niche): dict(value) for niche, value in overrides.items() if isinstance(value, dict)
        },
    )


def evaluate_domain(
    domain: str,
    *,
    niche: str | None,
    metrics: Mapping[str, Any] | None,
    config: PrefilterConfig,
    now: datetime | None = None,
    checked_at: str | None = None,
) -> PrefilterResult:
    """Run the enabled local gates for one domain and return the stored result.

    Gates run in a fixed order: length, hyphens, numbers, TLD, price, time to
    close. The first failure is the human-readable reason; every gate result is
    kept in the details payload for audit.
    """
    moment = _as_utc(now) or datetime.now(UTC)
    timestamp = checked_at or moment.isoformat(timespec="seconds")
    details: dict[str, Any] = {
        "domain": domain,
        "niche": niche,
        "config_version": config.version,
        "checks": [],
    }

    if not config.enabled:
        details["disabled"] = True
        return PrefilterResult(
            passed=True,
            reason=DISABLED_REASON,
            details=details,
            config_version=config.version,
            checked_at=timestamp,
        )

    gates = config.gates_for(niche)
    values = metrics or {}
    body = domain_body(domain)
    suffix = domain_suffix(domain)
    checks: list[dict[str, Any]] = details["checks"]
    failures: list[str] = []

    def add(
        gate: str,
        status: str,
        *,
        reason: str | None = None,
        observed: Any = None,
        limit: Any = None,
    ) -> None:
        check: dict[str, Any] = {"gate": gate, "status": status}
        if reason is not None:
            check["reason"] = reason
        if observed is not None:
            check["observed"] = observed
        if limit is not None:
            check["limit"] = limit
        checks.append(check)
        if status == "fail":
            failures.append(reason or gate)

    # Name length, excluding the public suffix.
    observed_length = len(body)
    length_limit = {"min": gates.length.minimum, "max": gates.length.maximum}
    if not gates.length.enabled:
        add("length", "skip", reason="disabled", observed=observed_length)
    elif observed_length < gates.length.minimum:
        add("length", "fail", reason="Too short", observed=observed_length, limit=length_limit)
    elif observed_length > gates.length.maximum:
        add("length", "fail", reason="Too long", observed=observed_length, limit=length_limit)
    else:
        add("length", "pass", observed=observed_length, limit=length_limit)

    # Hyphens in the registrable name.
    observed_hyphens = body.count("-")
    hyphen_limit = {"max": gates.hyphens.maximum}
    if not gates.hyphens.enabled:
        add("hyphens", "skip", reason="disabled", observed=observed_hyphens)
    elif observed_hyphens > gates.hyphens.maximum:
        add("hyphens", "fail", reason="Hyphenated", observed=observed_hyphens, limit=hyphen_limit)
    else:
        add("hyphens", "pass", observed=observed_hyphens, limit=hyphen_limit)

    # Digit characters in the registrable name.
    observed_numbers = sum(character.isdigit() for character in body)
    number_limit = {"max": gates.numbers.maximum}
    if not gates.numbers.enabled:
        add("numbers", "skip", reason="disabled", observed=observed_numbers)
    elif observed_numbers > gates.numbers.maximum:
        add("numbers", "fail", reason="Has digits", observed=observed_numbers, limit=number_limit)
    else:
        add("numbers", "pass", observed=observed_numbers, limit=number_limit)

    # Optional public-suffix allowlist.
    if not gates.tld.enabled:
        add("tld_allow", "skip", reason="disabled", observed=suffix or None)
    elif not suffix:
        add("tld_allow", "skip", reason="no tld")
    elif not gates.tld.values:
        add("tld_allow", "skip", reason="empty allowlist", observed=suffix)
    elif suffix in gates.tld.values:
        add("tld_allow", "pass", observed=suffix, limit={"values": list(gates.tld.values)})
    else:
        add(
            "tld_allow",
            "fail",
            reason="TLD not allowed",
            observed=suffix,
            limit={"values": list(gates.tld.values)},
        )

    # Known listing price. Sources without a price skip this gate.
    price = _as_float(values.get("price"))
    price_limit = {"max": gates.price.maximum_usd}
    if not gates.price.enabled:
        add("max_price_usd", "skip", reason="disabled", observed=price)
    elif price is None:
        add("max_price_usd", "skip", reason="no data")
    elif price > gates.price.maximum_usd:
        add("max_price_usd", "fail", reason="Over budget", observed=price, limit=price_limit)
    else:
        add("max_price_usd", "pass", observed=price, limit=price_limit)

    # Known auction deadline. Sources without a deadline skip this gate.
    deadline = _as_utc(values.get("auction_end_at"))
    time_limit = {"min_hours": gates.time_to_close.minimum_hours}
    if not gates.time_to_close.enabled:
        add("min_hours_to_close", "skip", reason="disabled")
    elif deadline is None:
        add("min_hours_to_close", "skip", reason="no data")
    else:
        hours = (deadline - moment).total_seconds() / 3600
        observed_hours = round(hours, 2)
        if hours <= 0:
            add(
                "min_hours_to_close",
                "fail",
                reason="Ended",
                observed=observed_hours,
                limit=time_limit,
            )
        elif hours < gates.time_to_close.minimum_hours:
            add(
                "min_hours_to_close",
                "fail",
                reason="Ending soon",
                observed=observed_hours,
                limit=time_limit,
            )
        else:
            add("min_hours_to_close", "pass", observed=observed_hours, limit=time_limit)

    passed = not failures
    details["failed"] = failures
    return PrefilterResult(
        passed=passed,
        reason=PASS_REASON if passed else failures[0],
        details=details,
        config_version=config.version,
        checked_at=timestamp,
    )


def load_prefilter_candidates(
    conn: sqlite3.Connection,
    config_version: str,
) -> list[PrefilterCandidate]:
    """Return classified, unblocked domains that need a prefilter for ``config_version``."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(domains)")}
    if not {"prefilter_pass", "prefilter_config_version", "prefilter_manual_pass"} <= columns:
        return []

    rows = conn.execute(_PREFILTER_CANDIDATES_SQL, (config_version,)).fetchall()
    return [
        PrefilterCandidate(
            domain=row["domain"],
            niche=row["niche"],
            metrics=_load_metrics(row["metrics_json"]),
        )
        for row in rows
    ]


def _load_metrics(value: str | None) -> dict[str, Any]:
    """Decode a stored metrics blob, treating missing or bad JSON as empty."""
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_gate_set(data: Mapping[str, Any]) -> GateSet:
    """Build a typed gate set from one config layer."""
    length = _gate(data, "length")
    hyphens = _gate(data, "hyphens")
    numbers = _gate(data, "numbers")
    tld = _gate(data, "tld_allow")
    price = _gate(data, "max_price_usd")
    time_to_close = _gate(data, "min_hours_to_close")

    return GateSet(
        length=LengthRule(
            enabled=_as_bool(length.get("enabled"), True),
            minimum=_as_int(length.get("min"), 3),
            maximum=_as_int(length.get("max"), 35),
        ),
        hyphens=HyphenRule(
            enabled=_as_bool(hyphens.get("enabled"), True),
            maximum=_as_int(hyphens.get("max"), 1),
        ),
        numbers=NumberRule(
            enabled=_as_bool(numbers.get("enabled"), True),
            maximum=_as_int(numbers.get("max"), 1),
        ),
        tld=TldRule(
            enabled=_as_bool(tld.get("enabled"), False),
            values=tuple(
                suffix for value in tld.get("values", []) if (suffix := _normalise_tld(value))
            ),
        ),
        price=PriceRule(
            enabled=_as_bool(price.get("enabled"), True),
            maximum_usd=_as_float(price.get("value"), 500.0),
        ),
        time_to_close=TimeRule(
            enabled=_as_bool(time_to_close.get("enabled"), True),
            minimum_hours=_as_float(time_to_close.get("value"), 6.0),
        ),
    )


def _gate(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return one gate object, treating a missing or disabled value as an empty layer."""
    value = data.get(key)
    if value is None or value is False:
        return {"enabled": False}
    return value if isinstance(value, dict) else {}


def _merge_gate_data(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge a per-niche override onto the base config one gate at a time."""
    merged: dict[str, Any] = {
        key: dict(value) if isinstance(value, dict) else value for key, value in base.items()
    }
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = {**current, **value}
        else:
            merged[key] = value
    return merged


def _normalise_tld(value: Any) -> str:
    """Normalise a configured suffix by trimming whitespace, dots, and case."""
    return str(value).strip().lower().lstrip(".")


def _as_utc(value: Any) -> datetime | None:
    """Parse an ISO timestamp or datetime into an aware UTC datetime."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _as_bool(value: Any, default: bool) -> bool:
    """Parse a JSON boolean, falling back to ``default``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    """Parse a JSON integer, falling back to ``default``."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    """Parse a JSON number, preserving ``None`` for missing values."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
