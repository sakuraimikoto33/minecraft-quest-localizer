from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import (  # noqa: E402
    TranslationError,
    TranslationProject,
    TranslationUnit,
)
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry  # noqa: E402
from mq_localizer.translator import TranslationOptions, TranslationService  # noqa: E402


_TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")


class _ScriptedClient:
    def __init__(
        self,
        responder: Callable[[int, list[dict[str, object]]], dict[str, str]],
    ) -> None:
        self.responder = responder
        self.calls: list[list[dict[str, object]]] = []

    def translate_batch(
        self,
        _api_key: str,
        _model: str,
        items: list[dict[str, object]],
        _source_locale: str,
        _target_locale: str,
        _cancel: object = None,
    ) -> dict[str, str]:
        copied = [dict(item) for item in items]
        self.calls.append(copied)
        return self.responder(len(self.calls), copied)


class _RecordingAdapter:
    def __init__(self, *, write_file: bool = False) -> None:
        self.calls: list[dict[str, str]] = []
        self.write_file = write_file

    def write(
        self,
        _project: TranslationProject,
        translations: dict[str, str],
        output_path: Path,
        selected_unit_ids: frozenset[str] | None = None,
    ) -> None:
        del selected_unit_ids
        self.calls.append(dict(translations))
        if self.write_file:
            output_path.write_text(str(translations), encoding="utf-8")


def _project(
    root: Path,
    sources: tuple[str, ...],
    *,
    target_locale: str = "ja_jp",
    existing: dict[str, str] | None = None,
) -> TranslationProject:
    return TranslationProject(
        adapter_id="test",
        adapter_label="Test",
        source_path=root / "source.snbt",
        default_output=root / f"{target_locale}.snbt",
        source_locale="en_us",
        target_locale=target_locale,
        units=[
            TranslationUnit(
                id=f"unit-{index}",
                key=f"quest.test.{index}.title",
                source=source,
                context="Quest title",
                source_path=str(root / "source.snbt"),
            )
            for index, source in enumerate(sources, start=1)
        ],
        existing=existing or {},
    )


class JapaneseUnicodeTranslationTests(unittest.TestCase):
    def test_foreign_script_retries_only_the_bad_item_and_recovers(self) -> None:
        def respond(call: int, items: list[dict[str, object]]) -> dict[str, str]:
            if call == 1:
                return {
                    "unit-1": "変成の հնարみ",
                    "unit-2": "メニューを開く",
                }
            return {"unit-1": "変成の技巧"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = _ScriptedClient(respond)
            adapter = _RecordingAdapter()
            progress: list[str] = []

            outcome = TranslationService(client).translate(
                _project(root, ("transmutation tricks", "Open menu")),
                adapter,
                root / "ja_jp.snbt",
                "key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
                progress=lambda _done, _total, message: progress.append(message),
            )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual([item["id"] for item in client.calls[1]], ["unit-1"])
        self.assertIn("異種文字", str(client.calls[1][0]["context"]))
        self.assertEqual(adapter.calls[0]["unit-1"], "変成の技巧")
        self.assertEqual(adapter.calls[0]["unit-2"], "メニューを開く")
        self.assertEqual(outcome.translated, 2)
        self.assertTrue(any("個別再試行" in message for message in progress))

    def test_persistent_zero_width_spaces_never_reach_writer(self) -> None:
        def respond(_call: int, items: list[dict[str, object]]) -> dict[str, str]:
            return {str(items[0]["id"]): "基本アクセス\u200b\u200bポート"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            output.write_text("ORIGINAL", encoding="utf-8")
            client = _ScriptedClient(respond)
            adapter = _RecordingAdapter(write_file=True)

            with self.assertRaisesRegex(
                TranslationError,
                "U\\+200B ZERO WIDTH SPACE",
            ) as caught:
                TranslationService(client).translate(
                    _project(root, ("Basic Access Port",)),
                    adapter,
                    output,
                    "key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

            message = str(caught.exception)
            self.assertIn("1回目の理由", message)
            self.assertIn("再試行後の理由", message)
            self.assertIn("翻訳ファイルへ書き込んでいません", message)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(output.read_text(encoding="utf-8"), "ORIGINAL")

    def test_unsafe_existing_translation_is_retranslated(self) -> None:
        def respond(_call: int, items: list[dict[str, object]]) -> dict[str, str]:
            return {str(items[0]["id"]): "基本アクセスポート"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = _ScriptedClient(respond)
            adapter = _RecordingAdapter()
            outcome = TranslationService(client).translate(
                _project(
                    root,
                    ("Basic Access Port",),
                    existing={"unit-1": "基本アクセス\u200b\u200bポート"},
                ),
                adapter,
                root / "ja_jp.snbt",
                "key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(preserve_existing=True),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual((outcome.reused, outcome.translated), (0, 1))
        self.assertEqual(adapter.calls[0]["unit-1"], "基本アクセスポート")

    def test_official_term_unicode_is_restored_from_immutable_token(self) -> None:
        official = "Հայերեն"
        glossary = GlossaryCatalog(
            entries={
                "Magic Item": GlossaryEntry(
                    source="Magic Item",
                    target=official,
                    key="item.example.magic",
                    mod_id="example",
                    translated=True,
                    provenance="example.jar!/assets/example/lang/ja_jp.json",
                )
            }
        )

        def respond(_call: int, items: list[dict[str, object]]) -> dict[str, str]:
            token = _TOKEN.search(str(items[0]["text"]))
            assert token is not None
            return {str(items[0]["id"]): token.group(0) + "を使う"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = _ScriptedClient(respond)
            adapter = _RecordingAdapter()
            TranslationService(client).translate(
                _project(root, ("Use Magic Item",)),
                adapter,
                root / "ja_jp.snbt",
                "key",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.calls[0]["unit-1"], official + "を使う")

    def test_non_japanese_target_can_use_new_script_and_zwj(self) -> None:
        translated = "वैज्ञानिक 👩\u200d🔬"

        def respond(_call: int, items: list[dict[str, object]]) -> dict[str, str]:
            return {str(items[0]["id"]): translated}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = _ScriptedClient(respond)
            adapter = _RecordingAdapter()
            TranslationService(client).translate(
                _project(root, ("Scientist",), target_locale="hi_in"),
                adapter,
                root / "hi_in.snbt",
                "key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.calls[0]["unit-1"], translated)

    def test_minecraft_formatting_and_cjk_extension_remain_valid(self) -> None:
        def respond(_call: int, items: list[dict[str, object]]) -> dict[str, str]:
            self.assertEqual(items[0]["text"], "Open")
            return {str(items[0]["id"]): "𠀀を開く"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = _ScriptedClient(respond)
            adapter = _RecordingAdapter()
            TranslationService(client).translate(
                _project(root, ("§aOpen",)),
                adapter,
                root / "ja_jp.snbt",
                "key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.calls[0]["unit-1"], "§a𠀀を開く")


if __name__ == "__main__":
    unittest.main()
