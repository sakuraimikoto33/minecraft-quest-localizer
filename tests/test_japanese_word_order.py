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
from mq_localizer.translation_quality import (
    JAPANESE_WORD_ORDER_INSTRUCTIONS,
    image_title_translation_issue,
    japanese_word_order_issue,
)
from mq_localizer.translator import (
    TranslationOptions, TranslationService, _existing_translation_is_safe,
    _make_prepared_part, _restore_bundle_response,
)


SOURCE = "Find a &6Honeycomb Brood Block&r and feed it some honey until the Bees like you!"
NAME = "幼虫入りの巣房ハニカムブロック"
BODY = "を見つけ、ハチがあなたを気に入るまで蜂蜜を与えましょう！"
BAD = BODY + "&6" + NAME + "&r"
GOOD = "&6" + NAME + "&r" + BODY


_REPORTED_WORD_ORDER_CASES = (
    (
        "With access to &bCelestigems&r, you can now create the Advanced versions of most machines!",
        "にアクセスできるようになったので、ほとんどの機械のアドバンスト版を作成できるようになります！&bセレスティジェム&r",
        "&bセレスティジェム&rにアクセスできるようになったので、ほとんどの機械のアドバンスト版を作成できるようになります！",
    ),
    (
        "Using &6Time Crystals&r, you can make the &6Time Wand&r.",
        "を使うと、&6タイムクリスタル&r&6Time Wand&rを作れます。",
        "&6タイムクリスタル&rを使うと、&6Time Wand&rを作れます。",
    ),
    (
        "&aMechanical Belts&r can be used to create conveyor-like systems to transfer items and/or &bRotational Force&r.",
        "は、アイテムや&a機械ベルト&rを運ぶベルトコンベアのようなシステムを作るのに使えます。&b回転力&r",
        "&a機械ベルト&rは、アイテムや&b回転力&rを運ぶベルトコンベアのようなシステムを作るのに使えます。",
    ),
    (
        "&6Create&r machines will automatically interact with items on &aBelts&r if they are able to.",
        "の機械は、可能であれば&6Create&r上のアイテムと自動的にやり取りします。&aベルト&r",
        "&6Create&rの機械は、可能であれば&aベルト&r上のアイテムと自動的にやり取りします。",
    ),
)


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

    def test_leading_styled_terms_are_not_left_after_japanese_particles(self):
        for source, bad, good in _REPORTED_WORD_ORDER_CASES:
            with self.subTest(source=source):
                self.assertIsNotNone(japanese_word_order_issue(source, bad, "en_us", "ja_jp"))
                self.assertIsNone(japanese_word_order_issue(source, good, "en_us", "ja_jp"))

    def test_modifier_before_styled_term_is_not_mistaken_for_a_shift(self):
        source = (
            "To progress into more late-game &6Create&r, you will need to craft "
            "different plates for different functions."
        )
        candidate = "より終盤の&6Create&rへ進むには、用途ごとに異なるプレートをクラフトする必要があります。"
        self.assertIsNone(japanese_word_order_issue(source, candidate, "en_us", "ja_jp"))
        self.assertIsNone(japanese_word_order_issue(
            "Yes, it does work with &eUltimine&r!",
            "はい、&eウルティマイン&rでも使えます！",
            "en_us", "ja_jp",
        ))
        self.assertIsNone(japanese_word_order_issue(
            "In order to build a more powerful altar you will need &2runes&r to upgrade it.",
            "より強力な祭壇を作るには、アップグレード用の&2ルーン&rが必要です。",
            "en_us", "ja_jp",
        ))
        self.assertIsNone(japanese_word_order_issue(
            "To craft more advanced items in &eDraconic Evolution&r, you will need a special setup.",
            "より高度なアイテムを&eDraconic Evolution&rでクラフトするには、特別な設備が必要です。",
            "en_us", "ja_jp",
        ))

    def test_additional_orphan_particles_and_unstyled_names_are_detected(self):
        cases = (
            (
                "Ferments &cRaw Ore Meat&r into fermented meat, multiplying it in the process.",
                "は&c生の鉱石肉&rを発酵肉へと発酵させ、その過程で増殖させます。",
            ),
            (
                "The Forge can be used to craft &6Elemental Prisms&r, but it requires a special relic for the recipe.",
                "は戦利品チェストから入手できますが、クラフトレシピもあります！&dElementarium Relic&r",
            ),
            (
                "It has a &bBase Modifier&r of 2, which means it will multiply both the energy storage and speed of the machine by 2.",
                "には&b基本補正値&rが2あり、機械のエネルギー貯蔵量と速度の両方が2倍になります。",
            ),
            (
                "Generates power by consuming Dragon's Breath.",
                "を消費して発電します。ドラゴンブレス。",
            ),
            (
                "Giving them a Honey Treat will lure in either a Yellow or Green Carpenter Bee.",
                "にHoney Treatを与えると、黄色またはGreen Carpenter Beeをおびき寄せられます。",
            ),
        )
        for source, candidate in cases:
            with self.subTest(source=source):
                self.assertIsNotNone(japanese_word_order_issue(source, candidate, "en_us", "ja_jp"))

    def test_introductory_clause_terms_are_not_detached(self):
        cases = (
            (
                "While intimidating, &aModern Industrialization&r has some super useful machines that can come in handy for generating lots of ore!",
                "は威圧的に見えますが、鉱石を大量に生成するのに役立つ非常に便利な機械をいくつか備えています。&aModern Industrialization&r",
            ),
            (
                "They also function similarly to &aAndesite Casings&r for decoration.",
                "は、装飾用として&a安山岩の外装&rと同様に機能します。",
            ),
            (
                "Once you've obtained &7Sulfuric Crude Oil&r, you can process it with &bHydrogen&r in a &3Chemical Reactor&r to obtain &eSulfuric Acid&r and &7Crude Oil&r.",
                "を入手したら、&7Sulfuric Crude Oil&rを&bHydrogen&rと&3Chemical Reactor&rで処理すると、&eSulfuric Acid&rと&7Crude Oil&rが得られます。",
            ),
            (
                "In order to complete the &6FTB Pyramid&r, it is recommended to automate &dWarden&r, &3Wither&r, and &5Ender Dragon &bPredictions&r.",
                "を完了するには&6FTBピラミッド&r、&dウォーデン&r、&3Wither&r、そして&5エンダードラゴン &b予測&rの自動化がおすすめです。",
            ),
        )
        for source, candidate in cases:
            with self.subTest(source=source):
                self.assertIsNotNone(japanese_word_order_issue(source, candidate, "en_us", "ja_jp"))

    def test_unstyled_names_before_styled_terms_are_not_detached(self):
        cases = (
            (
                "Using the power of Teleportation Cores, we can create our own &dPortals&r to quickly travel around our bases.",
                "の力を使えば、独自のTeleportation Coresを作成して、拠点間を素早く移動できるようになります！&dポータル&r",
            ),
            (
                "Using the power of Mekanism's &aLasers&r, you can generate ores over time.",
                "の力を利用したMekanismでエネルギーを充填し、そのエネルギーを&aレーザー&rに照射して鉱石を生成できます。",
            ),
            (
                "For those heading down the path of Mekanism, using the &dDigital Miner&r is a great idea.",
                "の道を進む人には、&aMekanism&rを使うと、採掘するのに最適です。&dデジタルマイナー&r",
            ),
        )
        for source, candidate in cases:
            with self.subTest(source=source):
                self.assertIsNotNone(japanese_word_order_issue(source, candidate, "en_us", "ja_jp"))

    def test_image_title_predicates_are_rejected_but_labels_are_kept(self):
        for source, candidate in (
            ("Deep Sea Drain", "Deep Sea Drainを設置します。"),
            ("Bio Reactor", "Bio Reactorです。"),
        ):
            with self.subTest(source=source):
                self.assertIsNotNone(image_title_translation_issue(
                    "image.3B3FFAA15928C9F6.title", source, candidate, "ja_jp",
                ))
                self.assertIsNone(image_title_translation_issue(
                    "image.3B3FFAA15928C9F6.title", source, source, "ja_jp",
                ))

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
        self.assertIsNone(japanese_word_order_issue(
            "The &6Example&r is shown here.",
            "は、ここに表示されます。\n&6例&r",
            "en_us", "ja_jp",
        ))

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
        self.assertIn(
            "JAPANESE WORD-ORDER RETRY",
            transport.calls[1]["payload"]["input"],
        )
        self.assertIn(
            "JAPANESE WORD-ORDER RETRY",
            transport.calls[1]["payload"]["instructions"],
        )

    def test_power_of_term_retry_moves_the_first_token_before_nos(self):
        source = (
            "Using the power of &6Entro Crystals&r, you can create the fastest "
            "autocrafting setups using &dQuantum Autocrafting&r."
        )

        def build_response(bad: bool) -> dict:
            root = {
                "fragments": (
                    {
                        "fragment_0000": "の力を使えば、",
                        "fragment_0001": "で最速の自動クラフト設備を作成できます。",
                        "fragment_0002": "",
                    }
                    if bad else {
                        "fragment_0000": "",
                        "fragment_0001": "の力を使えば、",
                        "fragment_0002": "を使って最速の自動クラフト設備を作成できます。",
                    }
                ),
                "token_positions": {"token_0000": 0, "token_0001": 1},
            }
            return _output_text({"translations": {
                "item_0000": {
                    "fragments": {"fragment_0000": "Quantum Autocrafting"},
                    "token_positions": {},
                },
                "item_0001": root,
            }})

        transport = SequenceTransport(build_response(True), build_response(False))
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(source, Path(directory))
            TranslationService(OpenAIClient(transport=transport)).translate(
                project, adapter, project.default_output, "test", "test",
                _glossary({"Entro Crystals": "エントロクリスタル"}), TranslationOptions(),
            )
        self.assertEqual(adapter.translations, [{
            "u": "&6エントロクリスタル&rの力を使えば、&dQuantum Autocrafting&rを使って最速の自動クラフト設備を作成できます。",
        }])
        self.assertEqual(len(transport.calls), 2)

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
