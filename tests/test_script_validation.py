import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from generate_script import annotation_response_format, annotations_to_entries, build_lossless_fallback_entries, split_annotation_chunks, split_annotation_segments, split_into_chunks  # noqa: E402
from review_script import check_text_loss  # noqa: E402
from script_validation import (  # noqa: E402
    compare_text_fidelity,
    normalize_unsafe_speakers,
    normalize_fidelity_text,
    validate_script_entries,
    validate_speakers_for_source,
)


class ScriptValidationTests(unittest.TestCase):
    def test_segment_annotations_preserve_text_and_reject_missing_or_rewritten_segments(self):
        source = "旁白。\n“你好！”她回答。"
        segments = split_annotation_segments(source)
        annotations = [
            {"segment_id": row["segment_id"], "speaker": "角色" if "你好" in row["text"] else "NARRATOR", "instruct": ""}
            for row in segments
        ]
        entries = annotations_to_entries(segments, annotations)
        self.assertEqual("".join(row["text"] for row in entries), source)
        with self.assertRaisesRegex(ValueError, "complete and ordered"):
            annotations_to_entries(segments, annotations[:-1])
        rewritten = [dict(row) for row in annotations]
        rewritten[0]["text"] = "改写"
        with self.assertRaisesRegex(ValueError, "must not contain text"):
            annotations_to_entries(segments, rewritten)

        schema = annotation_response_format(segments)["json_schema"]["schema"]
        self.assertEqual(schema["minItems"], len(segments))
        self.assertEqual(
            [item["properties"]["segment_id"]["const"] for item in schema["prefixItems"]],
            [row["segment_id"] for row in segments],
        )
        self.assertTrue(all(item["additionalProperties"] is False for item in schema["prefixItems"]))
        self.assertEqual(
            schema["prefixItems"][0]["properties"]["speaker"]["pattern"],
            r"^(?:NARRATOR|[^A-Za-z]*[\u3400-\u9fff][^A-Za-z]*)$",
        )

        english_schema = annotation_response_format([
            {"segment_id": "s0001", "text": "Hello."},
        ])["json_schema"]["schema"]
        self.assertEqual(
            english_schema["prefixItems"][0]["properties"]["speaker"],
            {"type": "string", "minLength": 1},
        )

        self.assertEqual("".join(split_annotation_chunks(source, 5)), source)

        dense_source = "。".join(f"短句{i}" for i in range(120)) + "。"
        dense_chunks = split_annotation_chunks(dense_source, 10_000)
        self.assertEqual("".join(dense_chunks), dense_source)
        self.assertTrue(all(len(split_annotation_segments(chunk)) <= 30 for chunk in dense_chunks))

    def test_chinese_dialogue_wrappers_and_layout_are_allowed(self):
        source = "林轩皱眉。\n\n“你到底想干什么？”\n他说：“别走。”"
        entries = [
            {"speaker": "NARRATOR", "text": "林轩皱眉。", "instruct": "Tense narration."},
            {"speaker": "林轩", "text": "你到底想干什么？", "instruct": "Angry voice."},
            {"speaker": "NARRATOR", "text": "他说：", "instruct": "Tense narration."},
            {"speaker": "林轩", "text": "别走。", "instruct": "Low voice."},
        ]

        self.assertTrue(compare_text_fidelity(source, entries).exact)

    def test_missing_chinese_sentence_is_rejected(self):
        source = "第一句。第二句。第三句。"
        entries = [
            {"speaker": "NARRATOR", "text": "第一句。第三句。", "instruct": "Neutral narration."},
        ]

        result = compare_text_fidelity(source, entries)

        self.assertFalse(result.exact)
        self.assertEqual(result.mismatch_index, 5)

    def test_reordered_or_rewritten_text_is_rejected(self):
        source = "他说：别走。"
        entries = [
            {"speaker": "NARRATOR", "text": "林轩说：别走。", "instruct": "Neutral narration."},
        ]

        self.assertFalse(compare_text_fidelity(source, entries).exact)

    def test_review_check_is_character_exact_not_word_count_based(self):
        original = [
            {"speaker": "NARRATOR", "text": "第一句。第二句。第三句。", "instruct": "Neutral narration."},
        ]
        corrected = [
            {"speaker": "NARRATOR", "text": "第一句。第三句。", "instruct": "Neutral narration."},
        ]

        passed, _, _, ratio = check_text_loss(original, corrected)

        self.assertFalse(passed)
        self.assertLess(ratio, 1.0)

    def test_resegmentation_without_text_changes_is_allowed(self):
        original = [
            {"speaker": "NARRATOR", "text": "第一句。第二句。", "instruct": "Neutral narration."},
        ]
        corrected = [
            {"speaker": "NARRATOR", "text": "第一句。", "instruct": "Neutral narration."},
            {"speaker": "地下人", "text": "第二句。", "instruct": "Measured voice."},
        ]

        passed, _, _, ratio = check_text_loss(original, corrected)

        self.assertTrue(passed)
        self.assertEqual(ratio, 1.0)

    def test_placeholder_and_pronoun_speakers_are_rejected(self):
        for speaker in ("CHARACTER", "VOICE 2", "角色3", "UNKNOWN VOICE", "未知角色", "你们", "我"):
            with self.subTest(speaker=speaker):
                errors = validate_script_entries([
                    {"speaker": speaker, "text": "正文。", "instruct": "Neutral voice."},
                ])
                self.assertTrue(any("invalid speaker" in error for error in errors))

    def test_named_chinese_speaker_and_narrator_are_valid(self):
        errors = validate_script_entries([
            {"speaker": "NARRATOR", "text": "旁白。", "instruct": "Neutral narration."},
            {"speaker": "地下人", "text": "台词。", "instruct": "Measured voice."},
        ])

        self.assertEqual(errors, [])
        self.assertEqual(validate_speakers_for_source(errors, "English only."), [])
        self.assertTrue(validate_speakers_for_source([
            {"speaker": "CHEN JI", "text": "你好。", "instruct": ""},
        ], "陳跡說。"))
        self.assertTrue(validate_speakers_for_source([
            {"speaker": "C 陳跡", "text": "你好。", "instruct": ""},
        ], "陳跡說。"))

    def test_explicit_first_person_identity_normalizes_unsafe_labels(self):
        entries = [
            {"speaker": "我", "text": "我在说话。", "instruct": "Measured voice."},
            {"speaker": "你们", "text": "你们听着。", "instruct": "Firm voice."},
            {"speaker": "NARRATOR", "text": "第一章", "instruct": "Neutral narration."},
        ]

        normalized, changes = normalize_unsafe_speakers(entries, "地下人")

        self.assertEqual(changes, 2)
        self.assertEqual([entry["speaker"] for entry in normalized], ["地下人", "地下人", "NARRATOR"])
        self.assertEqual(entries[0]["speaker"], "我")

    def test_lossless_fallback_preserves_text_and_structural_heading(self):
        source = "第一章 地下室[1]\n\n我病了。\n\n二\n\n我继续说。"

        entries = build_lossless_fallback_entries(source, "地下人")

        self.assertTrue(compare_text_fidelity(source, entries).exact)
        self.assertEqual(entries[0]["speaker"], "NARRATOR")
        self.assertEqual(entries[1]["speaker"], "地下人")
        self.assertEqual(entries[2]["speaker"], "NARRATOR")
        self.assertEqual(entries[3]["speaker"], "地下人")

    def test_lossless_fallback_supports_regular_narration(self):
        source = "第一章\n\n模型删改失败时，正文仍应完整保留。"

        entries = build_lossless_fallback_entries(source, "NARRATOR")

        self.assertTrue(compare_text_fidelity(source, entries).exact)
        self.assertTrue(all(entry["speaker"] == "NARRATOR" for entry in entries))

    def test_chinese_long_paragraph_splits_within_limit(self):
        source = "第一句很长。第二句也很长！第三句仍然很长？" * 30

        chunks = split_into_chunks(source, max_size=100)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))
        self.assertEqual(
            normalize_fidelity_text("".join(chunks)),
            normalize_fidelity_text(source),
        )


if __name__ == "__main__":
    unittest.main()
