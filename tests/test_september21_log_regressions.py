from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.test_translation_warning_regressions import RecordingAdapter, RecordingClient, project_for
from tests.test_terminal_style_regressions import _glossary
from mq_localizer.domain import TranslationError
from mq_localizer.glossary import GlossaryCatalog
from mq_localizer.protection import TokenProtector, special_tokens
from mq_localizer.translator import TranslationOptions, TranslationService, _existing_translation_is_safe


SOURCE = "&f1 Skill Point in &dMagic"
TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")


class September21LogTests(unittest.TestCase):
    def translate(self, source, callback, *, glossary=None, unsafe=False):
        glossary = glossary or GlossaryCatalog()
        client, adapter = RecordingClient(callback), RecordingAdapter()
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(source, Path(directory))
            project.units[0] = replace(project.units[0], key="reward.2292CE0AA6C85837.title", category="reward_title")
            def run():
                return TranslationService(client).translate(
                    project, adapter, project.default_output, "test", "test", glossary,
                    TranslationOptions(), progress=lambda _done, _total, message: messages.append(message),
                )
            if unsafe:
                with self.assertRaises(TranslationError):
                    run()
                self.assertEqual(adapter.translations, [])
                return
            self.assertEqual(run().translated, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(any("再試行" in message for message in messages))
        result = adapter.translations[0]["u"]
        self.assertEqual(special_tokens(source), special_tokens(result))
        self.assertTrue(_existing_translation_is_safe(source, result, glossary, "en_us"))
        return result

    def test_reported_reward_uses_separate_bodies_without_movable_colours(self):
        seen = []
        def translate(item):
            seen.append(item["text"])
            self.assertFalse(TOKEN.search(item["text"]))
            return {"1 Skill Point in ": "スキルポイント1点：", "Magic": "魔法"}[item["text"]]
        self.assertEqual(self.translate(SOURCE, translate), "&fスキルポイント1点：&d魔法")
        self.assertCountEqual(seen, ["1 Skill Point in ", "Magic"])

    def test_multiple_switches_and_inherited_modifiers_keep_original_order(self):
        for marker in ("&", "§"):
            for source in (
                f"{marker}fFirst {marker}dSecond {marker}lThird {marker}oLast",
                f"First {marker}dSecond {marker}lThird {marker}oLast",
            ):
                with self.subTest(source=source):
                    def translate(item):
                        self.assertFalse(TOKEN.search(item["text"]))
                        return {"First ": "最初", "Second ": "次", "Third ": "三番目", "Last": "最後"}[item["text"]]
                    expected = source.replace("First ", "最初").replace("Second ", "次").replace("Third ", "三番目").replace("Last", "最後")
                    self.assertEqual(self.translate(source, translate), expected)

    def test_official_or_untranslated_names_stay_in_their_original_scope(self):
        for target in ("秘術魔法", "Arcane Magic"):
            with self.subTest(target=target):
                glossary = _glossary({"Arcane Magic": target})
                def translate(item):
                    if item["text"] == "1 Skill Point in ":
                        return "スキルポイント1点："
                    # The actual parent occurrence, not a model-generated name.
                    self.assertEqual(len(item["term_bindings"]), 1)
                    return item["text"]
                self.assertEqual(self.translate(SOURCE.replace("Magic", "Arcane Magic"), translate, glossary=glossary), "&fスキルポイント1点：&d" + target)

    def test_missing_body_injected_code_or_foreign_token_never_writes(self):
        for bad_value in ("", "魔法&r", "&f魔法", "魔法\n", "魔法__MQP_0000__", "魔法\u0443"):
            for damaged_body in ("1 Skill Point in ", "Magic"):
                with self.subTest(bad_value=bad_value, damaged_body=damaged_body):
                    def translate(item):
                        if item["text"] == damaged_body:
                            return bad_value
                        return {"1 Skill Point in ": "スキルポイント1点：", "Magic": "魔法"}[item["text"]]
                    self.translate(SOURCE, translate, unsafe=True)

    def test_swapped_protected_names_and_missing_child_do_not_pass(self):
        glossary = _glossary({"Arcane Magic": "秘術魔法", "Melee Combat": "近接戦闘"})
        source = "&fArcane Magic skill &dMelee Combat skill"
        def swapped(item):
            token = item["term_bindings"][0]["token"]
            return item["text"].replace(token, "近接" if "Magic" in item["_source_text"] else "魔法")
        self.translate(source, swapped, glossary=glossary, unsafe=True)
        def missing(item):
            return None if "::styled::" in item["id"] else "スキルポイント1点："
        self.translate(SOURCE, missing, unsafe=True)

    def test_full_layout_validator_still_rejects_crossing_colours(self):
        protected = TokenProtector().protect(SOURCE)
        white, magenta = protected.special_placeholders
        for result in (magenta + "魔法の" + white + "スキルポイント1点", white + magenta + "魔法のスキルポイント1点"):
            with self.subTest(result=result), self.assertRaises(TranslationError):
                protected.restore(result)


if __name__ == "__main__":
    unittest.main()
