"""Deterministic blocklist for disallowed domain categories.

The blocklist runs before niche classification. It is intentionally
dictionary-only: no model is involved, so the result is stable and a blocked
domain never reaches the LLM pass. Matching is deliberately generous on
concatenated names, while ``allow_terms`` suppresses known non-blocked words
such as ``essex`` and ``replicable``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from expired_domains.config import PROJECT_ROOT
from expired_domains.domaintext import compact, domain_body

DEFAULT_BLOCKLIST_PATH = PROJECT_ROOT / "config" / "blocklist" / "v1.json"


@dataclass(frozen=True)
class BlockCategory:
    """One disallowed category and the terms that identify it."""

    key: str
    label: str
    description: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class Blocklist:
    """A loaded blocklist version with ordered categories and exemptions."""

    version: str
    categories: dict[str, BlockCategory]
    allow_terms: tuple[str, ...] = ()

    def label_for(self, key: str) -> str:
        """Return a display label, falling back to the key itself."""
        category = self.categories.get(key)
        return category.label if category is not None else key


@dataclass(frozen=True)
class BlocklistMatch:
    """One blocklist category that matched a domain."""

    category: str
    terms: tuple[str, ...]


def load_blocklist(path: Path | None = None) -> Blocklist:
    """Load ``config/blocklist/v1.json`` once per process."""
    return _load_blocklist(str(path or DEFAULT_BLOCKLIST_PATH))


@cache
def _load_blocklist(path: str) -> Blocklist:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    categories = {
        key: BlockCategory(
            key=key,
            label=str(value.get("label") or key),
            description=str(value.get("description") or ""),
            terms=tuple(str(term) for term in value.get("terms", [])),
        )
        for key, value in data["categories"].items()
    }
    return Blocklist(
        version=str(data["version"]),
        categories=categories,
        allow_terms=tuple(str(term) for term in data.get("allow_terms", [])),
    )


def match_domain(domain: str, blocklist: Blocklist) -> BlocklistMatch | None:
    """Return the best blocklist match for ``domain``, or ``None``.

    A name that matches several categories is assigned the category with the
    longest matched term, then the most matched terms. Matched terms are kept
    for audit output and for the blocked-domains view.
    """
    body = domain_body(domain)
    compact_body = compact(body)
    if not compact_body:
        return None

    for allow_term in blocklist.allow_terms:
        compact_allow = compact(allow_term)
        if compact_allow:
            compact_body = compact_body.replace(compact_allow, "")
    if not compact_body:
        return None

    best_key: tuple[int, int] | None = None
    best: BlocklistMatch | None = None

    for category in blocklist.categories.values():
        matched = tuple(
            term
            for term in category.terms
            if (compact_term := compact(term)) and compact_term in compact_body
        )
        if not matched:
            continue

        score = (
            max(len(compact(term)) for term in matched),
            len(matched),
        )
        if best_key is None or score > best_key:
            best_key = score
            best = BlocklistMatch(category=category.key, terms=matched)

    return best


def split_blocked(
    domains: Sequence[str], blocklist: Blocklist
) -> tuple[list[str], dict[str, BlocklistMatch]]:
    """Separate ``domains`` into allowed names and blocked matches.

    The allowed list preserves the input order. Blocked matches are keyed by
    domain so callers can persist the category and matched terms.
    """
    allowed: list[str] = []
    blocked: dict[str, BlocklistMatch] = {}
    for domain in domains:
        match = match_domain(domain, blocklist)
        if match is None:
            allowed.append(domain)
        else:
            blocked[domain] = match
    return allowed, blocked


def block_mapping(
    blocked: Mapping[str, BlocklistMatch],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Convert matches to the ``(category, terms)`` shape used by storage."""
    return {domain: (match.category, match.terms) for domain, match in blocked.items()}
