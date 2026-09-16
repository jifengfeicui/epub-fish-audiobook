"""Text-fidelity and schema checks for generated audiobook scripts."""

import re
from dataclasses import dataclass


# Script entries store spoken text without the source's outer dialogue marks.
_IGNORED_QUOTE_MARKS = frozenset('"“”‘’「」『』')
_INVALID_SPEAKERS = frozenset({
    "CHARACTER", "SPEAKER", "UNKNOWN", "人物", "角色", "说话人",
    "我", "你", "你们", "他", "她", "他们", "她们",
})
_GENERIC_SPEAKER = re.compile(r"^(?:VOICE|CHARACTER|SPEAKER|人物|角色|说话人)\s*\d*$", re.IGNORECASE)


@dataclass(frozen=True)
class FidelityResult:
    exact: bool
    source_length: int
    script_length: int
    mismatch_index: int | None
    source_excerpt: str
    script_excerpt: str


def normalize_fidelity_text(text):
    """Remove layout whitespace and dialogue wrappers, preserving all wording."""
    return "".join(
        char for char in str(text)
        if not char.isspace() and char not in _IGNORED_QUOTE_MARKS
    )


def combined_script_text(entries):
    return "".join(str(entry.get("text", "")) for entry in entries)


def compare_text_fidelity(source_text, entries, excerpt_radius=24):
    source = normalize_fidelity_text(source_text)
    script = normalize_fidelity_text(combined_script_text(entries))

    mismatch = None
    for index, (source_char, script_char) in enumerate(zip(source, script)):
        if source_char != script_char:
            mismatch = index
            break
    if mismatch is None and len(source) != len(script):
        mismatch = min(len(source), len(script))

    if mismatch is None:
        return FidelityResult(True, len(source), len(script), None, "", "")

    start = max(0, mismatch - excerpt_radius)
    source_end = min(len(source), mismatch + excerpt_radius)
    script_end = min(len(script), mismatch + excerpt_radius)
    return FidelityResult(
        False,
        len(source),
        len(script),
        mismatch,
        source[start:source_end],
        script[start:script_end],
    )


def validate_script_entries(entries):
    """Return schema and unsafe-speaker errors without modifying model output."""
    errors = []
    if not isinstance(entries, list) or not entries:
        return ["script must be a non-empty JSON array"]

    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            errors.append(f"entry {index} must be an object")
            continue
        missing = [field for field in ("speaker", "text", "instruct") if field not in entry]
        if missing:
            errors.append(f"entry {index} is missing: {', '.join(missing)}")
            continue
        for field in ("speaker", "text", "instruct"):
            if not isinstance(entry[field], str):
                errors.append(f"entry {index}.{field} must be a string")
        speaker = entry.get("speaker")
        if isinstance(speaker, str):
            normalized_speaker = speaker.strip()
            if not normalized_speaker:
                errors.append(f"entry {index}.speaker must not be empty")
            elif (
                normalized_speaker.upper() in _INVALID_SPEAKERS
                or normalized_speaker.upper().startswith("UNKNOWN")
                or normalized_speaker.startswith("未知")
                or _GENERIC_SPEAKER.fullmatch(normalized_speaker)
            ):
                errors.append(f"entry {index} uses invalid speaker label: {speaker!r}")
        text = entry.get("text")
        if isinstance(text, str) and not text.strip():
            errors.append(f"entry {index}.text must not be empty")
    return errors


def validate_speakers_for_source(entries, source_text):
    """Keep role labels in the source language so one character is not split into transliterated aliases."""
    if not re.search(r"[\u3400-\u9fff]", source_text):
        return []
    return [
        f"entry {index} uses a non-Chinese speaker label for Chinese source: {entry.get('speaker')!r}"
        for index, entry in enumerate(entries, 1)
        if isinstance(entry, dict)
        and isinstance(entry.get("speaker"), str)
        and entry["speaker"] != "NARRATOR"
        and (
            not re.search(r"[\u3400-\u9fff]", entry["speaker"])
            or (re.search(r"[A-Za-z]", entry["speaker"]) and entry["speaker"] not in source_text)
        )
    ]


def normalize_unsafe_speakers(entries, first_person_speaker):
    """Map unsafe placeholder/pronoun labels to an explicitly configured identity."""
    if not first_person_speaker:
        return entries, 0

    normalized_entries = []
    changes = 0
    for entry in entries:
        normalized = dict(entry) if isinstance(entry, dict) else entry
        if isinstance(normalized, dict):
            speaker = normalized.get("speaker")
            if isinstance(speaker, str) and speaker.strip().upper() in _INVALID_SPEAKERS:
                normalized["speaker"] = first_person_speaker
                changes += 1
        normalized_entries.append(normalized)
    return normalized_entries, changes


def format_fidelity_error(result):
    if result.exact:
        return "text is fully preserved"
    return (
        f"first mismatch at normalized character {result.mismatch_index}; "
        f"source={result.source_length} chars, script={result.script_length} chars; "
        f"source excerpt={result.source_excerpt!r}; script excerpt={result.script_excerpt!r}"
    )
