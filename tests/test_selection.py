from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.adapters import create_default_registry  # noqa: E402
from mq_localizer.adapters.base import array_translation_is_selected  # noqa: E402
from mq_localizer.domain import AdapterError  # noqa: E402
from mq_localizer.glossary import GlossaryCatalog  # noqa: E402
from mq_localizer.snbt import parse_lang_snbt  # noqa: E402
from mq_localizer.translator import TranslationOptions, TranslationService  # noqa: E402


FIXTURES = Path(__file__).with_name("fixtures")


class TranslationSelectionAdapterTests(unittest.TestCase):
    def copy_fixture(self, name: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name) / name
        shutil.copytree(FIXTURES / name, destination)
        return destination

    @staticmethod
    def selected_mapping(project: object, category: str) -> tuple[dict[str, str], frozenset[str]]:
        units = [unit for unit in project.units if unit.category == category]
        selected = frozenset(unit.id for unit in units)
        translations = {
            unit.id: ("訳:" + unit.source if unit.source else unit.source)
            for unit in units
        }
        return translations, selected

    def test_every_adapter_assigns_expected_categories(self) -> None:
        cases = {
            "native_snbt": {"quest_title", "quest_description", "task_title"},
            "split_snbt": {"chapter_title", "quest_title", "quest_description", "task_title"},
            "split_json5": {"chapter_title", "quest_title", "quest_description", "task_title"},
            "legacy_json": {"quest_title", "quest_description", "other"},
            "legacy_raw": {
                "chapter_group_title",
                "chapter_title",
                "chapter_subtitle",
                "image_hover",
                "quest_title",
                "quest_subtitle",
                "quest_description",
                "task_title",
            },
        }
        for fixture, expected in cases.items():
            with self.subTest(fixture=fixture):
                instance = self.copy_fixture(fixture)
                adapter = create_default_registry().detect(instance, "en_us")
                minecraft_version = "1.20.1" if fixture == "legacy_raw" else "1.21.1"
                project = adapter.load(instance, "en_us", "ja_jp", minecraft_version)
                self.assertEqual({unit.category for unit in project.units}, expected)

    def test_legacy_raw_data_title_has_its_own_quest_book_category(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        data_file = instance / "config" / "ftbquests" / "quests" / "data.snbt"
        data_file.write_text(
            data_file.read_text(encoding="utf-8").replace(
                "{\n",
                '{\n  title: "Impostor Syndrome"\n',
                1,
            ),
            encoding="utf-8",
        )
        adapter = create_default_registry().detect(instance, "en_us")
        project = adapter.load(instance, "en_us", "ja_jp", "1.20.1")

        book_title = next(unit for unit in project.units if unit.source == "Impostor Syndrome")
        self.assertEqual(book_title.category, "quest_book_title")
        self.assertRegex(book_title.key, r"^mq_localizer\.file\.[0-9a-f]+\.title$")
        self.assertNotIn(
            book_title,
            [
                unit
                for unit in project.units
                if unit.category in {"quest_title", "other"}
            ],
        )

    def test_raw_json_book_title_stays_book_title_after_bundle_reload(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        data_file = instance / "config" / "ftbquests" / "quests" / "data.snbt"
        component = json.dumps(
            {"text": "Impostor ", "extra": [{"text": "Syndrome"}]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        data_file.write_text(
            data_file.read_text(encoding="utf-8").replace(
                "{\n",
                "{\n  title: " + json.dumps(component, ensure_ascii=False) + "\n",
                1,
            ),
            encoding="utf-8",
        )
        registry = create_default_registry()
        raw_adapter = registry.detect(instance, "en_us", "1.20.1")
        raw_project = raw_adapter.load(instance, "en_us", "ja_jp", "1.20.1")
        title_units = [
            unit
            for unit in raw_project.units
            if unit.source in {"Impostor ", "Syndrome"}
        ]
        self.assertEqual(len(title_units), 2)
        self.assertEqual(
            {unit.category for unit in title_units},
            {"quest_book_title"},
        )
        raw_adapter.write(
            raw_project,
            {unit.id: unit.source for unit in raw_project.units},
            raw_project.default_output,
        )

        json_adapter = registry.detect(raw_project.default_output, "en_us", "1.20.1")
        self.assertEqual(json_adapter.id, "ftb_legacy_json")
        reloaded = json_adapter.load(
            raw_project.default_output,
            "en_us",
            "ja_jp",
            "1.20.1",
        )
        reloaded_titles = [
            unit
            for unit in reloaded.units
            if unit.source in {"Impostor ", "Syndrome"}
        ]
        self.assertEqual(len(reloaded_titles), 2)
        self.assertEqual(
            {unit.category for unit in reloaded_titles},
            {"quest_book_title"},
        )

    def test_legacy_raw_deselection_removes_stale_existing_book_title(self) -> None:
        class PrefixClient:
            def __init__(self) -> None:
                self.calls: list[list[dict[str, str]]] = []

            def translate_batch(
                self,
                api_key: str,
                model: str,
                items: list[dict[str, str]],
                source_locale: str,
                target_locale: str,
                cancel: object = None,
            ) -> dict[str, str]:
                del api_key, model, source_locale, target_locale, cancel
                self.calls.append(items)
                return {item["id"]: "訳:" + item["text"] for item in items}

        instance = self.copy_fixture("legacy_raw")
        data_file = instance / "config" / "ftbquests" / "quests" / "data.snbt"
        data_file.write_text(
            data_file.read_text(encoding="utf-8").replace(
                "{\n",
                '{\n  title: "Impostor Syndrome"\n',
                1,
            ),
            encoding="utf-8",
        )
        adapter = create_default_registry().detect(instance, "en_us")
        initial = adapter.load(instance, "en_us", "ja_jp", "1.20.1")
        book_title = next(
            unit for unit in initial.units if unit.category == "quest_book_title"
        )
        stale = {
            unit.id: (
                "Impostor症候群" if unit.id == book_title.id else unit.source
            )
            for unit in initial.units
        }
        adapter.write(initial, stale, initial.default_output)

        project = adapter.load(instance, "en_us", "ja_jp", "1.20.1")
        self.assertEqual(project.existing[book_title.id], "Impostor症候群")
        client = PrefixClient()
        outcome = TranslationService(client).translate(
            project,
            adapter,
            project.default_output,
            "sk-test",
            "gpt-test",
            GlossaryCatalog(),
            TranslationOptions(
                selected_categories=frozenset({"task_title"}),
            ),
        )

        lang_file = (
            project.default_output
            / "assets"
            / "minecraft"
            / "lang"
            / "ja_jp.json"
        )
        target_catalog = json.loads(lang_file.read_text(encoding="utf-8"))
        self.assertNotIn(book_title.key, target_catalog)
        self.assertEqual(
            set(target_catalog),
            {unit.key for unit in project.units if unit.category == "task_title"},
        )
        self.assertTrue(client.calls)
        self.assertNotIn(
            book_title.id,
            {item["id"] for call in client.calls for item in call},
        )
        self.assertEqual(outcome.preserved_unselected, 0)

    def test_native_partial_write_contains_only_selected_field(self) -> None:
        instance = self.copy_fixture("native_snbt")
        adapter = create_default_registry().detect(instance, "en_us")
        project = adapter.load(instance, "en_us", "ja_jp", "1.21.1")
        translations, selected = self.selected_mapping(project, "quest_description")

        adapter.write(
            project,
            translations,
            project.default_output,
            selected_unit_ids=selected,
        )

        rendered = parse_lang_snbt(project.default_output.read_text(encoding="utf-8"))
        self.assertEqual(list(rendered), ["quest.00000000000000A1.quest_desc"])
        self.assertEqual(rendered["quest.00000000000000A1.quest_desc"][1], "")

    def test_partial_array_selection_is_rejected_without_writing_source_items(self) -> None:
        cases = (
            ("native_snbt", "1.21.1"),
            ("split_snbt", "1.21.1"),
            ("split_json5", "26.1.2"),
        )
        for fixture, minecraft_version in cases:
            with self.subTest(fixture=fixture):
                instance = self.copy_fixture(fixture)
                adapter = create_default_registry().detect(
                    instance,
                    "en_us",
                    minecraft_version,
                )
                project = adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version,
                )
                description_units = [
                    unit
                    for unit in project.units
                    if unit.category == "quest_description"
                ]
                self.assertGreater(len(description_units), 1)
                selected = description_units[0]
                output = project.default_output
                before = {
                    path.relative_to(output).as_posix(): path.read_bytes()
                    for path in output.rglob("*")
                    if path.is_file()
                } if output.is_dir() else {".": output.read_bytes()}

                with self.assertRaisesRegex(AdapterError, "配列全体"):
                    adapter.write(
                        project,
                        {selected.id: "選択した1行だけ"},
                        output,
                        selected_unit_ids=frozenset({selected.id}),
                    )

                after = {
                    path.relative_to(output).as_posix(): path.read_bytes()
                    for path in output.rglob("*")
                    if path.is_file()
                } if output.is_dir() else {".": output.read_bytes()}
                self.assertEqual(after, before)

    def test_empty_array_is_emitted_only_for_a_complete_catalog(self) -> None:
        self.assertTrue(
            array_translation_is_selected(
                "quest.empty",
                (),
                frozenset(),
                complete_catalog=True,
            )
        )
        self.assertFalse(
            array_translation_is_selected(
                "quest.empty",
                (),
                frozenset(),
                complete_catalog=False,
            )
        )

    def test_split_partial_writes_remove_unselected_keys_in_every_document(self) -> None:
        for fixture, suffix in (("split_snbt", ".snbt"), ("split_json5", ".json5")):
            with self.subTest(fixture=fixture):
                instance = self.copy_fixture(fixture)
                adapter = create_default_registry().detect(instance, "en_us")
                project = adapter.load(instance, "en_us", "ja_jp", "1.21.1")
                translations, selected = self.selected_mapping(project, "quest_description")
                adapter.write(
                    project,
                    translations,
                    project.default_output,
                    selected_unit_ids=selected,
                )

                chapter_path = project.default_output / f"chapter{suffix}"
                welcome_path = project.default_output / "chapters" / f"welcome{suffix}"
                if suffix == ".snbt":
                    chapter = parse_lang_snbt(chapter_path.read_text(encoding="utf-8"))
                    welcome = parse_lang_snbt(welcome_path.read_text(encoding="utf-8"))
                else:
                    chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
                    welcome = json.loads(welcome_path.read_text(encoding="utf-8"))
                self.assertEqual(chapter, {})
                self.assertEqual(list(welcome), ["quest.00000000000000F2.quest_desc"] if suffix == ".json5" else ["quest.00000000000000D2.quest_desc"])

    def test_legacy_outputs_keep_only_selected_target_translations(self) -> None:
        instance = self.copy_fixture("legacy_json")
        adapter = create_default_registry().detect(instance, "en_us")
        project = adapter.load(instance, "en_us", "ja_jp", "1.20.1")
        translations, selected = self.selected_mapping(project, "quest_description")
        adapter.write(project, translations, project.default_output, selected_unit_ids=selected)
        self.assertEqual(
            list(json.loads(project.default_output.read_text(encoding="utf-8"))),
            ["atm9.quest.welcome.description"],
        )

        raw_instance = self.copy_fixture("legacy_raw")
        raw_adapter = create_default_registry().detect(raw_instance, "en_us")
        raw_project = raw_adapter.load(raw_instance, "en_us", "ja_jp", "1.20.1")
        raw_translations, raw_selected = self.selected_mapping(raw_project, "quest_description")
        raw_adapter.write(
            raw_project,
            raw_translations,
            raw_project.default_output,
            selected_unit_ids=raw_selected,
        )
        lang_dir = (
            raw_project.default_output
            / "assets"
            / "minecraft"
            / "lang"
        )
        source_catalog = json.loads((lang_dir / "en_us.json").read_text(encoding="utf-8"))
        target_catalog = json.loads((lang_dir / "ja_jp.json").read_text(encoding="utf-8"))
        self.assertGreater(len(source_catalog), len(target_catalog))
        self.assertEqual(
            set(target_catalog),
            {unit.key for unit in raw_project.units if unit.category == "quest_description"},
        )


if __name__ == "__main__":
    unittest.main()
