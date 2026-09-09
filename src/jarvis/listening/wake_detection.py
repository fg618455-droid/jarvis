"""Wake-word detection and query extraction."""

from typing import List, Optional
import difflib
import string

from ..debug import debug_log


def is_wake_word_detected(text_lower: str, wake_word: str, aliases: List[str], fuzzy_ratio: float = 0.78) -> bool:
    """
    Check if text contains wake word using exact and fuzzy matching.
    
    Args:
        text_lower: Lowercase text to check
        wake_word: Primary wake word
        aliases: List of wake word aliases
        fuzzy_ratio: Threshold for fuzzy matching (0.0-1.0)
    
    Returns:
        True if wake word detected
    """
    if not text_lower or not text_lower.strip():
        return False
    
    # Combine wake word and aliases
    all_aliases = set(aliases) | {wake_word}
    
    # Check exact match first
    if wake_word in text_lower:
        return True
    
    # Check aliases exact match
    for alias in aliases:
        if alias in text_lower:
            return True
    
    # Fuzzy matching for close variations
    try:
        heard_tokens = [t.strip(".,!?;:()[]{}\"'`).-_/") for t in text_lower.split() if t.strip()]
        for token in heard_tokens:
            for alias in all_aliases:
                ratio = difflib.SequenceMatcher(a=alias, b=token).ratio()
                if ratio >= fuzzy_ratio:
                    debug_log(f"wake word fuzzy match: '{alias}' ~ '{token}' (ratio: {ratio:.3f})", "wake")
                    return True
    except Exception:
        pass
    
    return False


def extract_query_after_wake(text_lower: str, wake_word: str, aliases: List[str]) -> str:
    """
    Extract the query portion after removing wake word.
    
    Args:
        text_lower: Lowercase text containing wake word
        wake_word: Primary wake word
        aliases: List of wake word aliases
    
    Returns:
        Query text with wake word removed
    """
    if not text_lower:
        return ""
    
    all_aliases = set(aliases) | {wake_word}
    fragment = text_lower
    
    # Remove all aliases from the text
    for alias in all_aliases:
        fragment = fragment.replace(alias, " ")
    
    # Clean up punctuation that might be left after wake word removal
    fragment = fragment.strip().lstrip(",.!?;:")
    fragment = fragment.strip()
    
    return fragment if fragment else ""


def extract_query_from_edge_wake(
    text_lower: str,
    wake_word: str,
    aliases: List[str],
    fuzzy_ratio: float = 0.78,
) -> Optional[str]:
    """Return the query when a wake name is the first or last spoken token.

    Edge position is a language-independent signal that the name is being used
    as an address. A name inside the utterance remains ambiguous and is left to
    the contextual intent judge, for example a person's name containing the
    assistant's name.

    ``None`` means no unambiguous edge address was found. An empty string means
    the utterance contained only the wake name.
    """
    if not text_lower or not text_lower.strip():
        return None

    raw_tokens = text_lower.split()
    if not raw_tokens:
        return None

    edge_punctuation = string.punctuation + "“”‘’«»"
    clean_tokens = [token.strip(edge_punctuation).lower() for token in raw_tokens]
    candidates = {
        alias.strip().lower()
        for alias in (set(aliases) | {wake_word})
        if alias and alias.strip()
    }

    for candidate in sorted(candidates, key=lambda value: len(value.split()), reverse=True):
        candidate_tokens = [part.strip(edge_punctuation) for part in candidate.split()]
        width = len(candidate_tokens)
        if not width or width > len(clean_tokens):
            continue

        starts = clean_tokens[:width] == candidate_tokens
        ends = clean_tokens[-width:] == candidate_tokens
        if width == 1 and not starts and not ends:
            starts = (
                difflib.SequenceMatcher(a=candidate_tokens[0], b=clean_tokens[0]).ratio()
                >= fuzzy_ratio
            )
            ends = (
                difflib.SequenceMatcher(a=candidate_tokens[0], b=clean_tokens[-1]).ratio()
                >= fuzzy_ratio
            )

        if starts:
            return " ".join(raw_tokens[width:]).strip().lstrip(",.!?;:").strip()
        if ends:
            return " ".join(raw_tokens[:-width]).strip().rstrip(",.!?;:").strip()

    return None
