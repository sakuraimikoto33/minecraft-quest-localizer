from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_translation_warning_regressions import RecordingAdapter, project_for
from tests.test_terminal_style_regressions import _glossary
from tests.test_openai_client import SequenceTransport, _output_text
from mq_localizer.domain import TranslationError
from mq_localizer.openai_client import IMMUTABLE_TRANSLATION_PROTOCOL, OpenAIClient
from mq_localizer.protection import TokenProtector
from mq_localizer.translation_quality import JAPANESE_WORD_ORDER_INSTRUCTIONS, japanese_word_order_issue
from mq_localizer.translator import (
    TranslationOptions, TranslationService, _existing_translation_is_safe,
    _make_prepared_part, _restore_bundle_response,
)


SOURCE = "Find a &6Honeycomb Brood Block&r and feed it some honey until the Bees like you!"
NAME = "幼虫入りの巣房ハニカムブロック"
BODY = "を見つけ、ハチがあなたを気に入るまで蜂蜜を与えましょう！"
BAD = BODY + "&6" + NAME + "&r"
GOOD = "&6" + NAME + "&r" + BODY


def response(*, bad=False):
    return _output_text({"translations": {"item_0000": {
        "fragments": {"fragment_0000": BODY if bad else "", "fragment_0001": "" if bad else BODY},
        "token_positions": {"token_0000": 0},
    }}})


class JapaneseWordOrderTests(unittest.TestCase):
    def test_reported_output_is_detected_without_changing_it(self):
        issue = japanese_word_order_issue(SOURCE, BAD, "en_us", "ja_jp")
        self.assertIn("文頭の「を」", issue)
        self.assertIsNone(japanese_word_order_issue(SOURCE, GOOD, "en_us", "ja_jp"))

    def test_good_end_labels_quotes_other_locales_and_multiline_are_accepted(self):
        for candidate in (
            GOOD, "探すもの：&6" + NAME + "&r", "見つけましょう！&6" + NAME + "&r",
            "「を」を使いましょう！&6例文&r", "を含む言葉：&6名称&r",
            "を見つけましょう！\n&6" + NAME + "&r",
            "を見つけましょう！\\n&6" + NAME + "&r",
            "を見つけましょう！&6例文を使いましょう！&r",
        ):
            with self.subTest(candidate=candidate):
                self.assertIsNone(japanese_word_order_issue(SOURCE, candidate, "en_us", "ja_jp"))
        for source_locale, target_locale in (("ja_jp", "ja_jp"), ("en_us", "en_us")):
            self.assertIsNone(japanese_word_order_issue(SOURCE, BAD, source_locale, target_locale))
        self.assertIsNone(japanese_word_order_issue(BAD, BAD, "en_us", "ja_jp"))

    def test_reported_response_retries_whole_sentence_and_writes_only_good_result(self):
        transport = SequenceTransport(response(bad=True), response())
        adapter = RecordingAdapter()
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(SOURCE, Path(directory))
            outcome = TranslationService(OpenAIClient(transport=transport)).translate(
                project, adapter, project.default_output, "test", "test", _glossary({"Honeycomb Brood Block": NAME}),
                TranslationOptions(), progress=lambda _done, _total, message: messages.append(message),
            )
        self.assertEqual(outcome.translated, 1)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(adapter.translations, [{"u": GOOD}])
        self.assertTrue(any("日本語の語順" in message for message in messages))
        self.assertIn("NOT translations of the matching source fragment numbers", IMMUTABLE_TRANSLATION_PROTOCOL)
        self.assertIn("fragment_0000=''", transport.calls[0]["payload"]["instructions"])

    def test_repeated_bad_order_stops_without_write_or_partial_confirmation(self):
        transport = SequenceTransport(response(bad=True), response(bad=True))
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(SOURCE, Path(directory))
            with self.assertRaisesRegex(TranslationError, "日本語の語順"):
                TranslationService(OpenAIClient(transport=transport)).translate(
                    project, adapter, project.default_output, "test", "test", _glossary({"Honeycomb Brood Block": NAME}),
                    TranslationOptions(), confirm_partial=lambda _: self.fail("Unexpected partial save"),
                )
        self.assertEqual(adapter.translations, [])
        self.assertEqual(len(transport.calls), 2)

    def test_multiple_terms_with_shifted_prose_are_retried_together(self):
        source = (
            "Once you have your hands on some &dStainless Steel Dust&r, you can run it "
            "through an &3Electric Blast Furnace&r to create a &bStainless Steel Hot Ingot&r!"
        )
        terms = {"Stainless Steel Dust": "ステンレス鋼の粉", "Electric Blast Furnace": "電気高炉",
                 "Stainless Steel Hot Ingot": "ステンレス鋼高温インゴット"}
        good_fragments = ["", "を手に入れたら、", "で加工して、", "を作れます！"]
        def build(fragments):
            return _output_text({"translations": {"item_0000": {
                "fragments": {f"fragment_{i:04d}": value for i, value in enumerate(fragments)},
                "token_positions": {f"token_{i:04d}": i for i in range(3)},
            }}})
        transport = SequenceTransport(build(good_fragments[1:] + [""]), build(good_fragments))
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(source, Path(directory))
            TranslationService(OpenAIClient(transport=transport)).translate(
                project, adapter, project.default_output, "test", "test", _glossary(terms), TranslationOptions(),
            )
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(adapter.translations, [{"u": "&dステンレス鋼の粉&rを手に入れたら、&3電気高炉&rで加工して、&bステンレス鋼高温インゴット&rを作れます！"}])

    def test_independent_json_fragment_is_not_treated_as_complete_prose(self):
        protector = TokenProtector()
        root = _make_prepared_part(
            part_id="fragment", protected=protector.protect(SOURCE, {"Honeycomb Brood Block": NAME}),
            context="JSON text part", unit_key="test", source_path="test.snbt",
            protector=protector, raw_json_fragment=True,
        )
        token = root.provider_projection.expansions[0][0]
        result, failure = _restore_bundle_response(root, {root.id: BODY + token}, "en_us", "ja_jp", {})
        self.assertIsNone(failure)
        self.assertEqual(result[root.id], BAD)

    def test_japanese_example_is_not_sent_for_other_locales(self):
        transport = SequenceTransport(_output_text({"translations": {"item_0000": {
            "fragments": {"fragment_0000": "Trouver un bloc."}, "token_positions": {},
        }}}))
        OpenAIClient(transport=transport).translate_batch(
            "test", "test", [{"id": "u", "text": "Find a block."}], "en_us", "fr_fr",
        )
        self.assertNotIn(JAPANESE_WORD_ORDER_INSTRUCTIONS, transport.calls[0]["payload"]["instructions"])

    def test_existing_bad_translation_is_retranslated_and_good_one_is_reused(self):
        glossary = _glossary({"Honeycomb Brood Block": NAME})
        self.assertFalse(_existing_translation_is_safe(SOURCE, BAD, glossary))
        self.assertTrue(_existing_translation_is_safe(SOURCE, GOOD, glossary))
        for existing, expected_calls in ((BAD, 1), (GOOD, 0)):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                transport = SequenceTransport(response())
                adapter = RecordingAdapter()
                messages = []
                project = project_for(SOURCE, Path(directory), existing=existing)
                outcome = TranslationService(OpenAIClient(transport=transport)).translate(
                    project, adapter, project.default_output, "test", "test", glossary, TranslationOptions(),
                    progress=lambda _done, _total, message: messages.append(message),
                )
                self.assertEqual(len(transport.calls), expected_calls)
                self.assertEqual(outcome.reused, 1 - expected_calls)
                self.assertEqual(adapter.translations, [{"u": GOOD}])
                if expected_calls:
                    self.assertTrue(any("既存訳を再翻訳" in message and "語順" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
