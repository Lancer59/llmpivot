"""Lightweight, tokenizer-free estimates for prompt token counts."""

import math
from typing import Union


def estimate_tokens(text_or_character_count: Union[str, int]) -> int:
    """Estimate tokens with a rough four-characters-per-token heuristic.

    This intentionally avoids tokenizer dependencies. The result is a planning
    estimate only; actual counts depend on the model and its tokenizer.
    """
    if isinstance(text_or_character_count, int):
        character_count = max(0, text_or_character_count)
    else:
        character_count = len(text_or_character_count or "")
    return math.ceil(character_count / 4)
