from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tests.test_translation_warning_regressions import RecordingAdapter, RecordingClient, project_for
from mq_localizer.domain import TranslationError
from mq_localizer.glossary import GlossaryCatalog
from mq_localizer.openai_client import _prepare_structured_translation_items
from mq_localizer.protection import TokenProtector, protected_layout_signature
from mq_localizer.translator import (
    TranslationOptions, TranslationService, _bundle_parts, _make_prepared_part,
    _restore_bundle_response, _existing_translation_is_safe,
)

SOURCE = (
    "&cStrings&r: These are words or sentences containing text, which can include numbers. "
    "&oEverything you can read on this page is a &cString&f!&r"
)


class CompoundClosingSymbolTests(unittest.TestCase):
    def test_reported_full_sentence_uses_local_closing_symbols_and_is_reusable(self) -> None:
        for marker in ("&", "§"):
            source = SOURCE.replace("&", marker)
            glossary = GlossaryCatalog().with_source_preserved_terms(["String"])

            def translate(item):
                original = item["_source_text"]
                if original == "Strings":
                    return "文字列"
                if original.startswith("Everything"):
                    self.assertNotIn("!", item["text"])
                    self.assertNotIn("__MQP_", item["text"])
                    return "このページで読めるものはすべて"
                tokens = re.findall(r"__MQP_[0-9A-F]{4}__", item["text"])
                self.assertEqual(len(tokens), 2)
                return tokens[0] + "：数字も含められる単語や文です。" + tokens[1]

            class ProtocolClient(RecordingClient):
                def translate_batch(self, *args, **kwargs):
                    _prepare_structured_translation_items(args[2])
                    return super().translate_batch(*args, **kwargs)

            client = ProtocolClient(translate)
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                project = project_for(source, Path(directory))
                adapter = RecordingAdapter()
                TranslationService(client).translate(
                    project, adapter, project.default_output, "offline", "model", glossary, TranslationOptions(),
                )
                expected = (
                    "&c文字列&r：数字も含められる単語や文です。"
                    "&oこのページで読めるものはすべて&cString&f!&r"
                ).replace("&", marker)
                self.assertEqual(adapter.translations[0]["u"], expected)
                self.assertEqual(len(client.calls), 1)
                self.assertTrue(_existing_translation_is_safe(source, expected, glossary))

    def test_multiple_symbol_only_colors_keep_exact_suffix_and_do_not_gain_body(self) -> None:
        source = "Use &aExplain &bName&e!&d?&r now."
        protector = TokenProtector()
        root = _make_prepared_part(
            part_id="u", protected=protector.protect(source, {"Name": "Name"}),
            context="description", unit_key="u", source_path="en_us.snbt", protector=protector,
        )
        parts = _bundle_parts(root)
        child = next(part for part in parts if part.id != root.id)
        self.assertEqual(child.provider_projection.text, "Explain ")
        response = {part.id: part.provider_projection.text for part in parts}
        response[child.id] = "説明："
        restored, failure = _restore_bundle_response(root, response, "en_us", "ja_jp", {})
        self.assertIsNone(failure)
        self.assertEqual(restored[root.id], "Use &a説明：&bName&e!&d?&r now.")
        self.assertEqual(protected_layout_signature(source, {"Name": "Name"}),
                         protected_layout_signature(restored[root.id], {"Name": "Name"}))
        # The strict parent contract still rejects a changed symbol or any
        # linguistic text injected into the symbol-only scope.
        for suffix in ("!added", "！", ""):
            candidate = root.protected.protected.replace("!", suffix)
            with self.subTest(suffix=suffix), self.assertRaises(TranslationError):
                root.protected.restore(candidate)


if __name__ == "__main__":
    unittest.main()
