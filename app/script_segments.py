"""Source boundaries shared by generation, review, and spoken-text validation."""

import re
from collections import Counter


_PREFIX = re.compile(r"(?m)^[ \t]*(?P<name>[\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z ·・]{0,19})[：:][ \t]*(?=\S)")
_PAIRS = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"'}


def dialogue_prefixes(text):
    candidates = list(_PREFIX.finditer(text))
    counts = Counter(match['name'].strip() for match in candidates
                     if match['name'].strip().lower() not in {'http', 'https', 'ftp', '作者', '译者', '书名', '时间', '日期'}
                     and not re.search(r'(?:说|说道|问|问道|回答|答道|主张|表示|写道)$', match['name'].strip()))
    names = {name for name, count in counts.items() if count >= 2}
    if len(names) < 2:
        return []
    return [match for match in candidates if match['name'].strip() in names]


def spoken_source(text):
    """Exclude only the exact label spans confirmed in the complete source."""
    parts = []
    end = 0
    for match in dialogue_prefixes(text):
        parts.append(text[end:match.start()])
        end = match.end()
    parts.append(text[end:])
    return ''.join(parts)


def _bounded_spans(text, start, end, max_chars):
    while end - start > max_chars:
        boundary = start + max_chars
        stops = list(re.finditer(r'[。！？!?；;]|\.(?=\s|$)', text[start:boundary]))
        if stops:
            boundary = start + stops[-1].end()
        yield start, boundary
        start = boundary
    if start < end:
        yield start, end


def _prose_spans(text, start, end):
    """Quotes can enclose speech only within this line, never later paragraphs."""
    cursor = start
    index = start
    while index < end:
        char = text[index]
        if char in _PAIRS:
            closing = text.find(_PAIRS[char], index + 1, end)
            if closing >= 0:
                if cursor < index:
                    yield cursor, index
                yield index, closing + 1
                cursor = index = closing + 1
                continue
        if char in '。！？!?；;' or (char == '.' and (index + 1 == end or text[index + 1].isspace())):
            yield cursor, index + 1
            cursor = index + 1
        index += 1
    if cursor < end:
        yield cursor, end


def split_annotation_segments(text, max_chars=500):
    prefixes = {match.start(): match for match in dialogue_prefixes(text)}
    segments = []
    active_speaker = None
    turn = 0
    pending = ''
    for paragraph, line in enumerate(re.finditer(r'[^\n]+(?:\n|$)|\n', text)):
        start, end = line.span()
        prefix = prefixes.get(start)
        if prefix:
            active_speaker = prefix['name'].strip()
            turn += 1
            start = prefix.end()
        spans = [(start, end)] if prefixes else _prose_spans(text, start, end)
        for span_start, span_end in spans:
            # Layout whitespace may be shortened, but may not exceed the TTS text cap.
            pending = pending[-(max_chars - 1):] if max_chars > 1 else ''
            for left, right in _bounded_spans(text, span_start, span_end, max(1, max_chars - len(pending))):
                value = text[left:right]
                if not value.strip():
                    pending += value
                    continue
                row = {
                    'segment_id': f's{len(segments) + 1:04d}',
                    'text': pending + value, 'source_start': left, 'source_end': right,
                    'paragraph_id': paragraph, 'turn_id': turn,
                }
                pending = ''
                if prefix:
                    row['expected_speaker'] = active_speaker
                    row['source_label_start'] = prefix.start()
                    row['source_label_end'] = prefix.end()
                elif prefixes and active_speaker is None:
                    row['expected_speaker'] = 'NARRATOR'
                if active_speaker:
                    row['speaker_hint'] = active_speaker
                segments.append(row)
    if pending and segments:
        segments[-1]['text'] += pending[:max_chars - len(segments[-1]['text'])]
    turns = [0, *prefixes, len(text)]
    for row in segments:
        start, end = turns[row['turn_id']], turns[row['turn_id'] + 1]
        context = text[start:end]
        if len(context) > 2000:
            label = f"{row['speaker_hint']}：\n…\n" if row.get('speaker_hint') else ''
            budget = 2000 - len(label)
            left = max(start, min(row['source_start'] - budget // 2, end - budget))
            context = label + text[left:left + budget]
        row['source_context'] = context
    return segments


def annotation_batches(segments, max_size, max_segments=30):
    """Batch existing source records without reparsing away speaker constraints."""
    batches = []
    current = []
    size = 0
    for segment in segments:
        value = segment['text']
        for offset in range(0, len(value), max_size):
            part = dict(segment, text=value[offset:offset + max_size])
            if current and (size + len(part['text']) > max_size or len(current) >= max_segments):
                batches.append(current)
                current, size = [], 0
            current.append(part)
            size += len(part['text'])
    if current:
        batches.append(current)
    for batch in batches:
        for index, segment in enumerate(batch, 1):
            segment['segment_id'] = f's{index:04d}'
    return batches
