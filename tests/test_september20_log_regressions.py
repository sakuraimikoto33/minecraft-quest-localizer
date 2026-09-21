from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tests.test_translation_warning_regressions import RecordingAdapter, RecordingClient, project_for
from tests.test_translator import _prefix_inside_protected_segment
from mq_localizer.domain import TranslationError
from mq_localizer.glossary import GlossaryCatalog
from mq_localizer.protection import TokenProtector, special_tokens
from mq_localizer.translator import TranslationOptions, TranslationService, _existing_translation_is_safe


TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")
LABEL_SOURCE = (
    r"- &eSave Offset \&\ Side&r: Does the same as the above, but also saves the side of the block you selected. "
    "This is particularly useful for blocks that have functionality on a specific side."
)


class September20LogTests(unittest.TestCase):
    def translate(self, source, callback, *, source_locale="en_us", unsafe=False):
        client = RecordingClient(callback)
        adapter = RecordingAdapter()
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(source, Path(directory))
            project.source_locale = source_locale
            def run():
                return TranslationService(client).translate(
                    project, adapter, project.default_output, "test", "test", GlossaryCatalog(),
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
        candidate = adapter.translations[0]["u"]
        self.assertTrue(_existing_translation_is_safe(source, candidate, GlossaryCatalog(), source_locale))
        return candidate

    def test_reported_pyramid_article_can_disappear_while_local_suffix_remains(self):
        def translate(item):
            if "::styled::" in item["id"]:
                self.assertEqual(item["text"], "FTB Pyramid")
                return "FTBピラミッド"
            self.assertEqual(item["text"], "The ")
            return ""
        self.assertEqual(self.translate("The &6FTB Pyramid", translate), "&6FTBピラミッド")

    def test_reported_guide_article_can_disappear_between_two_codes(self):
        for marker in ("&", "§"):
            for article in ("The", "An", "A"):
                with self.subTest(marker=marker, article=article):
                    source = f"{marker}f{article} {marker}bAE2 Guide"
                    result = self.translate(source, lambda item: item["text"].replace(article + " ", "").replace("AE2 Guide", "AE2ガイド"))
                    self.assertEqual(result, f"{marker}f{marker}bAE2ガイド")

    def test_guide_missing_body_codes_and_moved_body_are_not_accepted(self):
        protected = TokenProtector().protect("&fThe &bAE2 Guide")
        first, second = TOKEN.findall(protected.protected)
        for candidate in (first + second, first + "AE2ガイド" + second,
                          second + first + "AE2ガイド", "AE2ガイド"):
            with self.subTest(candidate=candidate), self.assertRaises(TranslationError):
                protected.restore(candidate, allow_omitted_determiners=True)
        # Colours are now restored locally. A child still cannot disappear,
        # move its body into the parent, or inject a code/another scope's token.
        for callback in (
            lambda _: "",
            lambda item: "AE2ガイド" if item["id"] == "u" else "",
            lambda _: "&bAE2ガイド",
            lambda _: first + "AE2ガイド",
        ):
            with self.subTest(callback=callback):
                self.translate("&fThe &bAE2 Guide", callback, unsafe=True)

    def test_article_exception_does_not_drop_actions_or_cross_newlines(self):
        for source in ("Make &bAE2 Guide", "&fMake &bAE2 Guide", "The\n&bAE2 Guide", "The &bAE2 Guide"):
            with self.subTest(source=source):
                def translate(item):
                    return item["text"].replace("The", "").replace("Make", "").replace("AE2 Guide", "AE2ガイド")
                self.translate(source, translate, source_locale="de_de" if source == "The &bAE2 Guide" else "en_us", unsafe=True)

    def test_comma_grouped_rates_are_protected_in_full(self):
        for value in ("25,000 RF/t", "1,234,567.5 FE/tick", "1000 EU/s", "0.01mB/tick"):
            with self.subTest(value=value):
                source = "Each port may output up to " + value + "."
                protected = TokenProtector().protect(source)
                self.assertIn(value, protected.special_values)
                def translate(item):
                    tokens = TOKEN.findall(item["text"])
                    self.assertEqual(len(tokens), 1)
                    self.assertFalse(any(c.isdigit() for c in TOKEN.sub("", item["text"])))
                    return "各ポートの最大出力は" + tokens[0] + "です。"
                self.assertEqual(self.translate(source, translate), "各ポートの最大出力は" + value + "です。")
                token = protected.special_placeholders[0]
                with self.assertRaises(TranslationError):
                    protected.restore(protected.protected.replace(token, value.replace("0", "1")))
        self.assertNotIn("00 RF/t", special_tokens("25,00 RF/t"))

    def test_escaped_label_is_a_separate_child_with_fixed_style(self):
        def translate(item):
            if "::styled::" in item["id"]:
                self.assertEqual(len(TOKEN.findall(item["text"])), 1)
                self.assertNotIn("Does the same", item["text"])
                return item["text"].replace("Save Offset", "オフセットを保存").replace("Side", "面")
            self.assertNotIn("Save Offset", item["text"])
            label = item["styled_bindings"][0]["token"]
            return "- " + label + "：上記と同じですが、選択したブロックの面も保存します。特定の面に機能があるブロックに便利です。"
        result = self.translate(LABEL_SOURCE, translate)
        self.assertIn(r"&eオフセットを保存 \&\ 面&r", result)
        self.assertEqual(special_tokens(LABEL_SOURCE), special_tokens(result))

    def test_escaped_label_cannot_lose_or_move_separator(self):
        for damage in ("missing", "leading", "trailing", "duplicate"):
            def translate(item):
                if "::styled::" not in item["id"]:
                    return item["text"]
                token = TOKEN.findall(item["text"])[0]
                body = item["text"].replace(token, "")
                return {"missing": body, "leading": token + body, "trailing": body + token, "duplicate": item["text"] + token}[damage]
            with self.subTest(damage=damage):
                self.translate(LABEL_SOURCE, translate, unsafe=True)

    def test_cyrillic_and_hebrew_contamination_still_retries_or_stops_without_writing(self):
        sources = (
            "The components will react and form a Fluix Crystal that is ready for refinement! You can do many of these at once to speed up the process, as well as eventually making machines that can automate the creation process for you.",
            "You will need 128 &aSails&r to max out the &bStress Capacity&r of the &aWindmill Bearing&r. Any &aSails&r above 128 do not count towards increasing your &bStress Capacity&r and are purely decorative.",
        )
        for source, character in zip(sources, ("\u0443", "\u05d1"), strict=True):
            for recover in (True, False):
                with self.subTest(character=character, recover=recover), tempfile.TemporaryDirectory() as directory:
                    project = project_for(source, Path(directory))
                    adapter = RecordingAdapter()
                    def translate(item):
                        self.assertEqual(adapter.translations, [])
                        response = _prefix_inside_protected_segment(item["text"])
                        return response + character if item["id"] == "u" and (not recover or len(client.calls) == 1) else response
                    client = RecordingClient(translate)
                    def run():
                        return TranslationService(client).translate(project, adapter, project.default_output, "test", "test", GlossaryCatalog(), TranslationOptions())
                    if recover:
                        run()
                        self.assertNotIn(character, adapter.translations[0]["u"])
                    else:
                        with self.assertRaises(TranslationError):
                            run()
                        self.assertEqual(adapter.translations, [])
                    self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
