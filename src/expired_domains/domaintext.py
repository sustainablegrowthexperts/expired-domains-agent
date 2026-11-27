"""Domain-name text helpers shared by the lexicon passes.

The niche classifier and the blocklist both need the registrable part of a
domain, a separator-free form for concatenated names, and token boundaries for
short terms. Keeping the helpers here makes both passes agree on how a domain
is read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import tldextract

_SUFFIXES = tldextract.TLDExtract(suffix_list_urls=())
_TOKEN_SPLIT = re.compile(r"[\W_]+", re.UNICODE)


def domain_body(domain: str) -> str:
    """Return the registrable domain without its public suffix."""
    text = domain.strip().lower()
    if not text:
        return ""
    extracted = _SUFFIXES(text)
    body = ".".join(part for part in (extracted.subdomain, extracted.domain) if part)
    if body:
        return body
    return text.rsplit(".", 1)[0]


def domain_suffix(domain: str) -> str:
    """Return the lowercase public suffix (``com``, ``co.uk``) or an empty string."""
    text = domain.strip().lower()
    if not text:
        return ""
    return _SUFFIXES(text).suffix.lower()


def compact(value: str) -> str:
    """Lowercase and remove separators so concatenated names still match."""
    return "".join(character for character in value.lower() if character.isalnum())


def tokens_for(body: str) -> tuple[str, ...]:
    """Split a domain body into lowercase alphanumeric tokens."""
    return tuple(token for token in _TOKEN_SPLIT.split(body.lower()) if token)


def term_matches(term: str, compact_body: str, tokens: Sequence[str]) -> bool:
    """Return True when ``term`` appears in the domain body.

    Longer terms are matched as substrings. Very short terms must line up with
    a token boundary, which avoids matching ``law`` inside ``delaware`` while
    still matching ``lawfirm`` and ``injurylaw``.
    """
    compact_term = compact(term)
    if not compact_term:
        return False
    if len(compact_term) >= 4:
        return compact_term in compact_body
    return any(
        token == compact_term or token.startswith(compact_term) or token.endswith(compact_term)
        for token in tokens
    )
