"""Versioned anchor lexicon: loading, query building, and local classification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from expired_domains.config import PROJECT_ROOT

DEFAULT_LEXICON_PATH = PROJECT_ROOT / "config" / "lexicon" / "v1.json"


@dataclass(frozen=True)
class Lexicon:
    """A loaded lexicon version with ordered categories and terms."""

    version: str
    categories: dict[str, tuple[str, ...]]

    @property
    def terms(self) -> tuple[str, ...]:
        """Return every term once, preserving the configured category order."""
        seen: dict[str, None] = {}
        for terms in self.categories.values():
            for term in terms:
                seen.setdefault(term, None)
        return tuple(seen)


def load_lexicon(path: Path | None = None) -> Lexicon:
    """Load ``config/lexicon/v1.json`` once per process."""
    return _load_lexicon(str(path or DEFAULT_LEXICON_PATH))


@cache
def _load_lexicon(path: str) -> Lexicon:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    categories = {name: tuple(terms) for name, terms in data["categories"].items()}
    return Lexicon(version=data["version"], categories=categories)


def build_where(lexicon: Lexicon) -> str:
    """Return the compact JSON ``or`` clause over every lexicon term."""
    clause = {
        "or": [
            {"field": "anchor", "is": ["isubstring", term]}
            for term in lexicon.terms
        ]
    }
    return json.dumps(clause, separators=(",", ":"), ensure_ascii=False)


def classify(anchor_norm: str | None, lexicon: Lexicon) -> tuple[list[str], list[str]]:
    """Return matched terms and sorted unique categories for one anchor."""
    if not anchor_norm:
        return [], []

    haystack = anchor_norm.lower()
    matches = [
        (term, category)
        for category, terms in lexicon.categories.items()
        for term in terms
        if term.lower() in haystack
    ]
    if not matches:
        return [], []

    terms = list(dict.fromkeys(term for term, _ in matches))
    categories = sorted({category for _, category in matches})
    return terms, categories
