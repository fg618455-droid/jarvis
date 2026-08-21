"""Turn a token stream into sentences the speech path can start on.

A reply is written faster than it is spoken, so waiting for the last token
before making any sound spends the whole generation in silence. The segmenter
watches the stream and releases each sentence the moment it is finished,
which is the earliest point speech can begin without cutting a clause in
half.

Sentence ends are recognised by their punctuation. That is a property of
writing systems rather than of any one language: the set covers the Latin
stops alongside the ideographic, Devanagari and Arabic ones, so a reply in
Chinese, Hindi or Urdu segments the same way a German one does. A stream
whose punctuation is unknown to us is not lost, it simply arrives as one
segment at the end, which is exactly today's behaviour.
"""

from __future__ import annotations

import re
from typing import List

from ..debug import debug_log

# Characters that end a sentence across the writing systems Jarvis may be
# asked to speak. Latin (. ! ?), full-width and ideographic (。！？), the
# Devanagari danda (।॥), the Arabic full stop (۔) and the Armenian and Greek
# question marks (՞ ;) are all terminal in their own scripts.
_TERMINATORS = ".!?。！？…।॥۔՞;"

# A terminator ends a sentence when it is followed by whitespace or by the
# end of what has arrived so far. Requiring the space is what keeps "21.5"
# and "youtube.com" from being cut in the middle.
_SENTENCE_END_RE = re.compile(
    rf"[{re.escape(_TERMINATORS)}]+(?=\s|$)"
)

# Openings that mean the model is emitting structure for a parser rather than
# prose for a person: a JSON object or array, or a fenced code block. Reading
# those aloud is worse than saying nothing, so a stream that starts this way
# is never spoken. Checked against the head of the stream only — a brace in
# the middle of a sentence is just a brace.
_STRUCTURED_OPENINGS = ("{", "[", "```", "<")


class SpeechSegmenter:
    """Release finished sentences from a stream of partial text."""

    def __init__(self) -> None:
        self._pending = ""
        self._seen_any = False
        self._speakable = True

    @property
    def is_speakable(self) -> bool:
        """Whether this stream is prose rather than output for a parser."""
        return self._speakable

    def feed(self, chunk: str) -> List[str]:
        """Add a chunk and return every sentence it completed."""
        if not chunk:
            return []
        self._pending += chunk
        if not self._seen_any:
            head = self._pending.lstrip()
            if head:
                self._seen_any = True
                if head.startswith(_STRUCTURED_OPENINGS):
                    self._speakable = False
                    debug_log(
                        "speech stream withheld: reply opens as structured output",
                        "tts",
                    )
        if not self._speakable:
            return []

        released: List[str] = []
        while True:
            match = _SENTENCE_END_RE.search(self._pending)
            if match is None:
                break
            sentence = self._pending[: match.end()].strip()
            self._pending = self._pending[match.end():]
            if sentence:
                released.append(sentence)
        return released

    def flush(self) -> List[str]:
        """Release the unfinished tail once the stream has ended."""
        if not self._speakable:
            self._pending = ""
            return []
        tail = self._pending.strip()
        self._pending = ""
        return [tail] if tail else []
