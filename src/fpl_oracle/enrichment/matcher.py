"""Name normalization and fuzzy matching for cross-source player lookup."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any


def normalize(name: str) -> str:
    """Normalize a player name for matching: strip accents, lowercase, remove punctuation."""
    # Decompose unicode and strip combining characters (accents)
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Lowercase, strip extra whitespace, remove punctuation
    clean = re.sub(r"[^a-z\s]", "", ascii_name.lower()).strip()
    return re.sub(r"\s+", " ", clean)


def fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def match_player(
    target_name: str,
    candidates: list[dict[str, Any]],
    *,
    name_keys: tuple[str, ...] = ("player_name", "name", "Player"),
    threshold: float = 0.75,
) -> dict[str, Any] | None:
    """Find the best matching player from candidates.

    Matching priority:
    1. Exact normalized match
    2. Substring containment
    3. Last-name match
    4. Fuzzy match above threshold
    """
    target_norm = normalize(target_name)
    target_last = target_norm.split()[-1] if target_norm else ""

    best: dict[str, Any] | None = None
    best_score = 0.0

    for candidate in candidates:
        # Try each possible name key
        cand_name = ""
        for key in name_keys:
            if key in candidate and candidate[key]:
                cand_name = str(candidate[key])
                break
        if not cand_name:
            continue

        cand_norm = normalize(cand_name)

        # Exact match
        if cand_norm == target_norm:
            return candidate

        # Substring
        if target_norm in cand_norm or cand_norm in target_norm:
            score = 0.90
        # Last name
        elif target_last and target_last == cand_norm.split()[-1]:
            score = 0.85
        else:
            score = SequenceMatcher(None, target_norm, cand_norm).ratio()

        if score > best_score:
            best_score = score
            best = candidate

    if best and best_score >= threshold:
        return best
    return None
