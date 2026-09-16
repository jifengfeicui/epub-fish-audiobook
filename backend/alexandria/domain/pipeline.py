from __future__ import annotations

from collections.abc import Iterable

AFTER_REVIEW_BATCH = "after_review_batch"
AFTER_ALL_REVIEWS = "after_all_reviews"
RENDER_START_MODES = {AFTER_REVIEW_BATCH, AFTER_ALL_REVIEWS}


def build_release_batches(
    chapter_ids: Iterable[int], mode: str = AFTER_REVIEW_BATCH, batch_size: int = 3
) -> list[list[int]]:
    chapters = list(chapter_ids)
    if mode not in RENDER_START_MODES:
        raise ValueError(f"Unknown render start mode: {mode}")
    if not 1 <= batch_size <= 20:
        raise ValueError("release batch size must be between 1 and 20")
    if not chapters:
        return []
    if mode == AFTER_ALL_REVIEWS:
        return [chapters]
    return [chapters[index:index + batch_size] for index in range(0, len(chapters), batch_size)]
