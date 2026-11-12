"""Deterministic candidate retrieval. Matching a name is not permission to execute a metric."""
from __future__ import annotations

import re
from collections.abc import Iterable

from app.ledger.semantic import mention


def score(question: str, name: str, aliases: Iterable[str] = ()) -> int:
    terms = [name, *aliases]
    exact = [len(term) for term in terms if isinstance(term, str) and mention(question, term)]
    if exact:
        return 100 + max(exact)
    # OEE/RTY/TEEP are discoverable even when the old metric only has a qualified display name.
    # This only produces candidates; it never selects between their different business formulas.
    tokens = set(re.findall(r"(?<![A-Za-z])[A-Z][A-Z0-9]{1,9}(?![A-Za-z])", name))
    return 1 if any(mention(question, token) for token in tokens) else 0
