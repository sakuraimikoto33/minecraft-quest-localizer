from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import TranslationError
from mq_localizer.glossary import GlossaryCatalog
from mq_localizer.protection import TokenProtector
from mq_localizer.translator import (
    TranslationOptions, TranslationService, _existing_translation_is_safe, _prepare_unit,
    _provider_item, _part_location_summary,
)
from tests.test_translation_warning_regressions import RecordingAdapter, RecordingClient, project_for


_TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")
_REPORTED = [
    "With ", {"text": "Gray", "color": "#929292"}, " and ",
    {"text": "Orange", "color": "#FCA645"},
    {"text": " Chalks", "color": "green"}, ", you can now upgrade your ",
    {"text": "Ophynx' Calling", "color": "dark_aqua"}, " to ",
    {"text": "Kandar's Opened Conjure", "color": "#FF55FF"}, ".",
]


class JsonStreamTranslationTests(unittest.TestCase):
    def translate(self, component: Any, client: RecordingClient, glossary: GlossaryCatalog | None = None):
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(json.dumps(component), Path(directory))
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model",
                glossary or GlossaryCatalog(), TranslationOptions(),
            )
        return outcome, json.loads(adapter.translations[0]["u"])

    def test_reported_with_prefix_translates_as_one_sentence_and_reuses_empty_prefix(self) -> None:
        glossary = GlossaryCatalog().with_source_preserved_terms([
            "Gray", "Orange Chalks", "Ophynx' Calling", "Kandar's Opened Conjure",
        ])

        def translate(item):
            if not item["id"].endswith("-json-stream"):
                return item["text"]
            tokens = _TOKEN.findall(item["text"])
            self.assertEqual(len(tokens), 4)
            self.assertIn("With ", item["text"])
            self.assertIn("you can now upgrade your", item["text"])
            self.assertIn('"source_text": "Orange Chalks"', item["context"])
            return tokens[0] + "と" + tokens[1] + "を使って、" + tokens[2] + "を" + tokens[3] + "へアップグレードできます。"

        client = RecordingClient(translate)
        outcome, candidate = self.translate(_REPORTED, client, glossary)
        self.assertEqual(outcome.translated, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(candidate[0], "")
        self.assertEqual(len(candidate), len(_REPORTED))
        for index, source in enumerate(_REPORTED):
            if isinstance(source, dict):
                self.assertEqual(candidate[index], source)
        self.assertEqual(candidate[5], "を使って、")
        self.assertTrue(_existing_translation_is_safe(
            json.dumps(_REPORTED), json.dumps(candidate), glossary,
        ))
        no_api = RecordingClient(lambda item: self.fail("Safe full-stream translation should be reused"))
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(json.dumps(_REPORTED), Path(directory), json.dumps(candidate))
            reused = TranslationService(no_api).translate(
                project, RecordingAdapter(), project.default_output, "test", "model",
                glossary, TranslationOptions(),
            )
        self.assertEqual(reused.reused, 1)

    def test_the_and_using_are_not_translated_as_isolated_leaves(self) -> None:
        for prefix, trailing, japanese in [
            ("The ", " stores fluid.", "は液体を保管します。"),
            ("Using ", ", make chalk.", "を使ってチョークを作ります。"),
        ]:
            with self.subTest(prefix=prefix):
                component = [prefix, {"text": "Tank", "color": "green", "bold": True}, trailing]

                def translate(item):
                    if item["id"].endswith("-json-stream"):
                        return _TOKEN.findall(item["text"])[0] + japanese
                    return "タンク"

                _outcome, candidate = self.translate(component, RecordingClient(translate))
                self.assertEqual(candidate[0], "")
                self.assertEqual(candidate[1], {"text": "タンク", "color": "green", "bold": True})
                self.assertTrue(_existing_translation_is_safe(json.dumps(component), json.dumps(candidate)))

    def test_missing_body_missing_or_reordered_components_and_gap_injection_never_write(self) -> None:
        for kind in ("empty", "only_tokens", "missing", "reordered", "newline"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                project = project_for(json.dumps(_REPORTED), Path(directory))
                adapter = RecordingAdapter()

                def translate(item):
                    if not item["id"].endswith("-json-stream"):
                        return item["text"]
                    tokens = _TOKEN.findall(item["text"])
                    if kind == "empty":
                        return ""
                    if kind == "only_tokens":
                        return "".join(tokens)
                    if kind == "missing":
                        return "".join(tokens[:-1]) + "を使います。"
                    if kind == "reordered":
                        return "".join(reversed(tokens)) + "を使います。"
                    return "".join(tokens) + "を使います。\n"

                client = RecordingClient(translate)
                with self.assertRaises(TranslationError):
                    TranslationService(client).translate(
                        project, adapter, project.default_output, "test", "model",
                        GlossaryCatalog(), TranslationOptions(),
                    )
                self.assertEqual(adapter.translations, [])
                self.assertEqual(len(client.calls), 2)

    def test_adjacent_components_are_one_marker_with_independent_styles_and_texts(self) -> None:
        source = ["Use ", {"text": "Gray", "color": "gray"},
                  {"text": " Chalks", "color": "green"}, {"text": "!", "bold": True}]

        def translate(item):
            if item["id"].endswith("-json-stream"):
                tokens = _TOKEN.findall(item["text"])
                self.assertEqual(len(tokens), 1)
                self.assertIn('"source_text": "Gray Chalks!"', item["context"])
                return "使うもの：" + tokens[0]
            return {"Gray": "灰色", " Chalks": "のチョーク"}[item["text"]]

        _outcome, candidate = self.translate(source, RecordingClient(translate))
        self.assertEqual(candidate, ["使うもの：", {"text": "灰色", "color": "gray"},
                                    {"text": "のチョーク", "color": "green"}, {"text": "!", "bold": True}])
        self.assertTrue(_existing_translation_is_safe(json.dumps(source), json.dumps(candidate)))
        # A trailing slot still cannot be invented, even though the inner gaps
        # are now impossible to address through the provider protocol.
        with self.assertRaises(TranslationError):
            self.translate(source, RecordingClient(lambda item: (
                "使うもの：" + _TOKEN.findall(item["text"])[0] + "末尾"
                if item["id"].endswith("-json-stream") else item["text"]
            )))

    def test_log_context_excludes_provider_reference_data_even_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(json.dumps(_REPORTED), Path(directory))
            prepared = _prepare_unit(project.units[0], TokenProtector(), GlossaryCatalog())
        root = prepared.parts[-1]
        self.assertNotIn("Component reference", _part_location_summary(root))
        self.assertNotIn("Orange Chalks", _part_location_summary(root))
        self.assertIn("Component reference", _provider_item(root)["context"])
        retry = _provider_item(root, context="retry")
        self.assertTrue(retry["context"].startswith("retry"))
        self.assertIn('"source_text": "Orange Chalks"', retry["context"])

    def test_normal_gap_term_offsets_after_grouped_components_remain_correct(self) -> None:
        source = ["Use ", {"text": "Gray", "color": "gray"},
                  {"text": " Chalks", "color": "green"}, " with Spirit Attuned Crystals."]
        glossary = GlossaryCatalog().with_source_preserved_terms(["Spirit Attuned Crystals"])

        def translate(item):
            if not item["id"].endswith("-json-stream"):
                return item["text"]
            tokens = _TOKEN.findall(item["text"])
            self.assertEqual(len(tokens), 2)
            self.assertEqual(item["term_bindings"][0]["source_term"], "Spirit Attuned Crystals")
            return tokens[0] + "を" + tokens[1] + "と使います。"

        _outcome, candidate = self.translate(source, RecordingClient(translate), glossary)
        self.assertEqual(candidate[-1], "をSpirit Attuned Crystalsと使います。")
        self.assertTrue(_existing_translation_is_safe(json.dumps(source), json.dumps(candidate), glossary))

    def test_reuse_rejects_metadata_mutations_empty_body_and_new_component(self) -> None:
        source = ["With ", {"text": "Tank", "color": "green", "bold": True}, ", store water."]
        baseline = ["", {"text": "タンク", "color": "green", "bold": True}, "で水を保管します。"]
        self.assertTrue(_existing_translation_is_safe(json.dumps(source), json.dumps(baseline)))
        for mutation in ("color", "bold", "key", "length", "node_body", "body", "syntax"):
            candidate = copy.deepcopy(baseline)
            if mutation == "color":
                candidate[1]["color"] = "red"
            elif mutation == "bold":
                candidate[1]["bold"] = 1
            elif mutation == "key":
                candidate[1]["clickEvent"] = {"action": "run_command", "value": "/kill"}
            elif mutation == "length":
                candidate.append("text")
            elif mutation == "node_body":
                candidate[1]["text"] = ""
            elif mutation == "body":
                candidate[2] = ""
            else:
                candidate[2] += "{new}"
            with self.subTest(mutation=mutation):
                self.assertFalse(_existing_translation_is_safe(json.dumps(source), json.dumps(candidate)))

    def test_initial_empty_and_adjacent_string_slots_keep_exact_shape(self) -> None:
        source = ["", "Using ", {"text": "Tank", "color": "green"}, ", ", "store water."]

        def translate(item):
            if item["id"].endswith("-json-stream"):
                return _TOKEN.findall(item["text"])[0] + "で水を保管します。"
            return "タンク"

        _outcome, candidate = self.translate(source, RecordingClient(translate))
        self.assertEqual(candidate, ["", "", {"text": "タンク", "color": "green"}, "で水を保管します。", ""])
        self.assertTrue(_existing_translation_is_safe(json.dumps(source), json.dumps(candidate)))

    def test_dynamic_interactive_nested_layout_and_cross_gap_terms_keep_conservative_path(self) -> None:
        components = [
            ["With ", {"keybind": "key.jump"}, " jump."],
            ["With ", {"text": "Tank", "hoverEvent": {"action": "show_text", "contents": "Help"}}, " store."],
            ["With ", {"text": "Tank", "extra": [{"text": "Big"}]}, " store."],
            ["With\n", {"text": "Tank", "color": "green"}, " store."],
            ["Use {item}", {"text": "Tank", "color": "green"}, " now."],
        ]
        with tempfile.TemporaryDirectory() as directory:
            for component in components:
                project = project_for(json.dumps(component), Path(directory))
                prepared = _prepare_unit(project.units[0], TokenProtector(), GlossaryCatalog())
                self.assertIsNone(prepared.json_stream_plan)
            project = project_for(json.dumps(["Use Orange ", {"text": "Chalk", "color": "green"}, " now."]), Path(directory))
            glossary = GlossaryCatalog().with_source_preserved_terms(["Orange Chalk"])
            prepared = _prepare_unit(project.units[0], TokenProtector(), glossary)
            self.assertIsNone(prepared.json_stream_plan)


if __name__ == "__main__":
    unittest.main()
