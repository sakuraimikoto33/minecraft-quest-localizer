from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tests.test_mixed_style_regressions import _glossary
from tests.test_translation_warning_regressions import RecordingAdapter, RecordingClient, project_for
from mq_localizer.domain import TranslationError
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry
from mq_localizer.protection import TokenProtector, special_tokens
from mq_localizer.translator import TranslationOptions, TranslationService, _existing_translation_is_safe


TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")


def mekanism_glossary():
    return GlossaryCatalog(entries={"Mekanism": GlossaryEntry(
        source="Mekanism", target="Mekanism", key="mod.display_name.mekanism", mod_id="mekanism",
        translated=False, provenance="mekanism.jar!/META-INF/neoforge.mods.toml",
    )})


class SeptemberLogRegressions(unittest.TestCase):
    def translate(self, source, translate, glossary=None):
        glossary = glossary or GlossaryCatalog()
        client = RecordingClient(translate)
        adapter = RecordingAdapter()
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(source, Path(directory))
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test", "test", glossary,
                TranslationOptions(), progress=lambda _done, _total, message: messages.append(message),
            )
        self.assertEqual(outcome.translated, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(any("再試行" in message for message in messages))
        result = adapter.translations[0]["u"]
        self.assertTrue(_existing_translation_is_safe(source, result, glossary))
        return result

    def test_weapon_heading_can_omit_article_without_losing_coloured_name(self):
        for name in ("Edge of Deliverance", "Weight of Worlds", "Erosion Scepter"):
            with self.subTest(name=name):
                source = f"&5The {name}&r:"
                def translate(item):
                    if "::styled::" in item["id"]:
                        return item["term_bindings"][0]["token"]
                    return item["text"]
                self.assertEqual(self.translate(source, translate, _glossary((name, name))),
                                 f"&5{name}&r:")

    def test_omission_never_allows_prose_or_names_to_cross_style_boundaries(self):
        for source in ("&5The Sword&r", "&5Strong Sword&r", "&5Sword's&r"):
            protected = TokenProtector().protect(source, {"Sword": "剣"})
            term = protected.term_placeholders[0]
            formats = protected.special_placeholders
            correct = formats[0] + term + formats[1]
            if "The " in source:
                self.assertEqual(protected.restore(correct, allow_omitted_determiners=True), "&5剣&r")
                with self.assertRaises(TranslationError):
                    protected.restore(correct)  # Non-English callers remain strict.
            else:
                with self.assertRaises(TranslationError):
                    protected.restore(correct, allow_omitted_determiners=True)
            for candidate in (formats[0] + formats[1] + term, formats[0] + "訳" + formats[1]):
                with self.assertRaises(TranslationError):
                    protected.restore(candidate, allow_omitted_determiners=True)

    def test_article_omission_also_preserves_newlines_and_terminal_reset(self):
        glossary = _glossary(("Edge of Deliverance", "Edge of Deliverance"))
        for source in (
            "&5The Edge of Deliverance&r:\nNext",
            "Use &5The Edge of Deliverance&r here.",
            "&5The Edge of Deliverance&r:&r",
        ):
            with self.subTest(source=source):
                def translate(item):
                    return item["text"].replace("The ", "").replace("Next", "次").replace("Use ", "使う：").replace(" here.", "ここで。")
                result = self.translate(source, translate, glossary)
                self.assertNotIn("The ", result)
                self.assertEqual(special_tokens(source), special_tokens(result))

    def test_terminal_redundant_reset_is_kept_local(self):
        source = "Any player not sneaking inside a field block will take instantaneous &cinfinite damage&r.&r"
        def translate(item):
            if "::styled::" in item["id"]:
                self.assertEqual(item["text"], "infinite damage")
                return "無限のダメージ"
            tokens = TOKEN.findall(item["text"])
            self.assertEqual(len(tokens), 1)  # No editable orphan reset.
            return "フィールド内でスニークしていないプレイヤーは即座に" + tokens[0] + "を受けます。"
        result = self.translate(source, translate)
        self.assertEqual(result, "フィールド内でスニークしていないプレイヤーは即座に&c無限のダメージ&rを受けます。&r")

    def test_redundant_reset_cannot_move_or_disappear(self):
        protected = TokenProtector().protect("Use &cName&r.&r", {"Name": "名前"})
        first, close, tail = protected.special_placeholders
        for candidate in (
            protected.protected.replace(tail, ""),
            tail + protected.protected.replace(tail, ""),
            protected.protected.replace(first, "SWAP").replace(close, first).replace("SWAP", close),
        ):
            with self.assertRaises(TranslationError):
                protected.restore(candidate)

    def test_rainbow_is_an_explicit_colour_after_a_white_boundary(self):
        source = "The &6Data Center&f multiblock is a 7x7x7 structure that runs up to 25 &zSelf Aware&r data models at the same time."
        def translate(item):
            if "::styled::" in item["id"]:
                self.assertEqual(item["text"], "Self Aware")
                return "自己認識"
            term = item["term_bindings"][0]["token"]
            style = item["styled_bindings"][0]["token"]
            return term + "は7x7x7のマルチブロックで、最大25個の" + style + "データモデルを同時に動作させます。"
        result = self.translate(source, translate, _glossary(("Data Center", "データセンター")))
        self.assertIn("&6データセンター&f", result)
        self.assertIn("&z自己認識&r", result)

    def test_exact_reference_name_echo_is_removed_before_possessive_restoration(self):
        source = "The &dQuantum Item Orchestration&r (&dQIO&r) system is &eMekanism's&r wireless, power-free, and expandable digital storage solution."
        def translate(item):
            if "::styled::" in item["id"]:
                if "term_bindings" in item:
                    return "Mekanism" + item["term_bindings"][0]["token"] + "の"
                return {"Quantum Item Orchestration": "量子アイテム管理", "QIO": "QIO"}[item["text"]]
            a, b, c = (binding["token"] for binding in item["styled_bindings"])
            return a + "（" + b + "）は" + c + "無線で電力不要の拡張可能なデジタルストレージです。"
        result = self.translate(source, translate, mekanism_glossary())
        self.assertIn("&eMekanismの&r", result)
        self.assertEqual(result.count("Mekanism"), 1)

    def test_echo_repair_does_not_accept_unknown_affixes_or_remove_possessive(self):
        protected = TokenProtector().protect("Mekanism's", {"Mekanism": "Mekanism"})
        token = protected.term_placeholders[0]
        self.assertEqual(protected.remove_adjacent_term_echoes(token + "Mekanismの"), token + "の")
        for candidate in ("SuperMekanism" + token + "の", token + "MekanismPlusの", "Mekanism " + token + "の", token + token):
            self.assertEqual(protected.remove_adjacent_term_echoes(candidate), candidate)
        with tempfile.TemporaryDirectory() as directory:
            project = project_for("Mekanism's", Path(directory))
            client = RecordingClient(lambda item: "Mekanism" + item["term_bindings"][0]["token"])
            adapter = RecordingAdapter()
            with self.assertRaises(TranslationError):
                TranslationService(client).translate(
                    project, adapter, project.default_output, "test", "test",
                    mekanism_glossary(), TranslationOptions(),
                )
            self.assertEqual(adapter.translations, [])

    def test_numeric_rate_is_one_literal_not_a_new_slash_path(self):
        source = "&dReactors&r can burn fuel at a max rate of 1mB * (number of &3Fuel Assemblies&r) per tick, down to 0.01mB/tick."
        self.assertIn("0.01mB/tick", special_tokens(source))
        def translate(item):
            if "::styled::" in item["id"]:
                return {"Reactors": "原子炉", "Fuel Assemblies": "燃料集合体"}[item["text"]]
            self.assertNotIn("/tick", item["text"])
            a, b = (binding["token"] for binding in item["styled_bindings"])
            rate = next(token for token in TOKEN.findall(item["text"]) if token not in {a, b})
            return a + "の燃料消費速度は、最大1mB×" + b + "の数毎ティック、最小" + rate + "です。"
        result = self.translate(source, translate)
        self.assertIn("0.01mB/tick", result)
        protected = TokenProtector().protect(source)
        rate = next(token for token, value in protected.replacements.items() if value == "0.01mB/tick")
        with self.assertRaises(TranslationError):
            protected.restore(protected.protected.replace(rate, "0.1mB/tick"))


if __name__ == "__main__":
    unittest.main()
