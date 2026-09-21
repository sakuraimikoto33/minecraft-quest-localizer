from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import TranslationError, TranslationProject, TranslationUnit
from mq_localizer.glossary import GlossaryCatalog
from mq_localizer.translator import (
    TranslationOptions,
    TranslationService,
    _existing_translation_is_safe,
)


_TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")


class RecordingClient:
    def __init__(self, translate: Callable[[dict[str, Any]], str]) -> None:
        self.translate = translate
        self.calls: list[list[dict[str, Any]]] = []

    def translate_batch(
        self, api_key: str, model: str, items: list[dict[str, Any]],
        source_locale: str, target_locale: str, cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        return {item["id"]: self.translate(item) for item in items}


class RecordingAdapter:
    def __init__(self) -> None:
        self.translations: list[dict[str, str]] = []

    def write(
        self, project: TranslationProject, translations: dict[str, str],
        output_path: Path,
    ) -> None:
        self.translations.append(dict(translations))


def project_for(source: str, root: Path, existing: str = "") -> TranslationProject:
    return TranslationProject(
        adapter_id="test", adapter_label="Test", source_path=root / "en_us.snbt",
        default_output=root / "ja_jp.snbt", source_locale="en_us", target_locale="ja_jp",
        units=[TranslationUnit("u", "quest.test.quest_desc[0]", source)],
        existing={"u": existing} if existing else {},
    )


class TranslationWarningRegressionTests(unittest.TestCase):
    def test_reported_raw_json_list_connector_translates_to_comma_and_is_reused(self) -> None:
        component = [
            "You can now upgrade your existing ",
            {"text": "Pentacles", "color": "#55FFFF"},
            " with ", {"text": "Orange", "color": "#FCA645"},
            " and ", {"text": "Gray", "color": "#929292"},
            {"text": " Chalks", "color": "green"}, ", and ",
            {"text": "Spirit Attuned Crystals", "color": "#FF55FF"}, ".",
        ]
        translations = {
            "You can now upgrade your existing ": "既存のものをアップグレードできます：",
            "Pentacles": "五芒星", " with ": "を用いて", "Orange": "オレンジ",
            " and ": "、", "Gray": "灰色", " Chalks": "のチョーク",
            ", and ": "、", "Spirit Attuned Crystals": "霊魂に同調したクリスタル",
        }
        source = json.dumps(component)
        def translate(item: dict[str, Any]) -> str:
            if not item["id"].endswith("-json-stream"):
                return translations[item["text"]]
            # The ordinary list prose is one sentence, while coloured labels
            # remain protected component tokens restored to their original slots.
            candidate = item["text"]
            for source_text in sorted(translations, key=len, reverse=True):
                candidate = candidate.replace(source_text, translations[source_text])
            return candidate

        client = RecordingClient(translate)
        adapter = RecordingAdapter()
        progress: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(source, Path(directory))
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model", GlossaryCatalog(),
                TranslationOptions(), progress=lambda _done, _total, text: progress.append(text),
            )
            self.assertEqual(outcome.translated, 1)
            self.assertEqual(len(client.calls), 1)
            self.assertFalse(any("再試行" in text for text in progress))
            candidate = adapter.translations[0]["u"]
            translated = json.loads(candidate)
            self.assertEqual(translated[4], "、")
            self.assertEqual(translated[7], "、")
            self.assertEqual(translated[8]["color"], "#FF55FF")
            self.assertEqual(translated[-1], ".")

            reused_project = project_for(source, Path(directory), candidate)
            no_api = RecordingClient(lambda item: self.fail("既存の安全なJSON訳は再利用する"))
            reused = TranslationService(no_api).translate(
                reused_project, RecordingAdapter(), reused_project.default_output,
                "test", "model", GlossaryCatalog(), TranslationOptions(),
            )
            self.assertEqual(reused.reused, 1)
            self.assertEqual(no_api.calls, [])

    def test_connector_exception_does_not_accept_missing_body_or_change_prose(self) -> None:
        cases = [
            (json.dumps([", and "]), "", "en_us", "ja_jp"),
            (json.dumps([", and "]), " ", "en_us", "ja_jp"),
            (json.dumps([", and "]), ".", "en_us", "ja_jp"),
            (json.dumps([", and "]), "、\n", "en_us", "ja_jp"),
            (json.dumps([", and\n"]), "、", "en_us", "ja_jp"),
            (json.dumps(["Build this machine"]), "、", "en_us", "ja_jp"),
            (json.dumps(["and build"]), "、", "en_us", "ja_jp"),
            (json.dumps(["or"]), "、", "en_us", "ja_jp"),
            (", and ", "、", "en_us", "ja_jp"),
            (json.dumps([", and "]), "、", "de_de", "ja_jp"),
            (json.dumps([", and "]), "、", "en_us", "zh_cn"),
        ]
        for source, response, source_locale, target_locale in cases:
            with self.subTest(source=source, response=response, locales=(source_locale, target_locale)):
                with tempfile.TemporaryDirectory() as directory:
                    project = project_for(source, Path(directory))
                    project.source_locale = source_locale
                    project.target_locale = target_locale
                    adapter = RecordingAdapter()
                    client = RecordingClient(lambda item: response)
                    with self.assertRaises(TranslationError):
                        TranslationService(client).translate(
                            project, adapter, project.default_output, "test", "model",
                            GlossaryCatalog(), TranslationOptions(),
                        )
                    self.assertEqual(adapter.translations, [])
                    candidate = json.dumps([response]) if source.startswith("[") else response
                    self.assertFalse(_existing_translation_is_safe(
                        source, candidate, GlossaryCatalog(), source_locale, target_locale,
                    ))

    def test_reported_coloured_plus_is_one_immutable_symbol_while_prose_translates(self) -> None:
        source = (
            "You can still set the settings to change how fast you want the coal "
            "to be exported by click the &a+&r button beside the Variable Card."
        )

        def translate(item: dict[str, Any]) -> str:
            tokens = _TOKEN.findall(item["text"])
            self.assertEqual(len(tokens), 1)
            self.assertNotIn("+", item["text"])
            self.assertEqual(item["term_bindings"], [{
                "token": tokens[0], "source_term": "+", "approved_output": "+",
            }])
            return tokens[0] + "ボタンをクリックして石炭の搬出速度を変更できます。"

        client = RecordingClient(translate)
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as directory:
            project = project_for(source, Path(directory))
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model", GlossaryCatalog(),
                TranslationOptions(),
            )
        self.assertEqual(outcome.translated, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.translations[0]["u"],
                         "&a+&rボタンをクリックして石炭の搬出速度を変更できます。")
        self.assertTrue(_existing_translation_is_safe(source, adapter.translations[0]["u"]))

    def test_symbol_atom_cannot_be_deleted_by_provider(self) -> None:
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as directory:
            project = project_for("Click the &a+&r button.", Path(directory))
            client = RecordingClient(lambda item: "ボタンをクリックしてください。")
            with self.assertRaises(TranslationError):
                TranslationService(client).translate(
                    project, adapter, project.default_output, "test", "model",
                    GlossaryCatalog(), TranslationOptions(),
                )
        self.assertEqual(adapter.translations, [])
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
