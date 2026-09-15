"""Convert Alexandria voice directions into Fish S2 control cues."""

from __future__ import annotations

import re


_WHITESPACE_RE = re.compile(r"\s+")


def clean_instruct(instruct: str) -> str:
    """Collapse instruction whitespace without changing its wording."""
    if not isinstance(instruct, str):
        raise TypeError("instruct must be a string")
    return _WHITESPACE_RE.sub(" ", instruct.strip())


def build_fish_text(text: str, instruct: str) -> str:
    """Return Fish S2 input with an optional natural-language control cue.

    The spoken text is kept byte-for-byte unchanged. Only the instruction is
    trimmed and its line breaks are collapsed so it cannot accidentally create
    multiple cue blocks. No keyword-based emotion classification is applied.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    cue = clean_instruct(instruct)
    if not cue:
        return text
    return f"[{cue}]\n{text}"
