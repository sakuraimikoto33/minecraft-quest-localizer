from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.adapters import create_default_registry  # noqa: E402
from mq_localizer.domain import AdapterError, TranslationProject  # noqa: E402
from mq_localizer.snbt import SnbtCompound, SnbtList, SnbtString, parse_lang_snbt, parse_snbt  # noqa: E402


FIXTURES = Path(__file__).with_name("fixtures")


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(path: Path) -> dict[str, str]:
    return {
        candidate.relative_to(path).as_posix(): _file_hash(candidate)
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file()
    }


class AdapterTestCase(unittest.TestCase):
    def copy_fixture(self, name: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name) / name
        shutil.copytree(FIXTURES / name, destination)
        return destination

    def merge_existing(
        self,
        project: TranslationProject,
        new_values: dict[str, str],
    ) -> dict[str, str]:
        merged = dict(project.existing)
        for unit in project.units:
            if unit.id in merged:
                continue
            self.assertIn(unit.key, new_values, f"fixture has no translation for {unit.key}")
            merged[unit.id] = new_values[unit.key]
        return merged

    def existing_by_key(self, project: TranslationProject) -> dict[str, str]:
        return {
            unit.key: project.existing[unit.id]
            for unit in project.units
            if unit.id in project.existing
        }


class NativeSnbtAdapterTests(AdapterTestCase):
    def test_autodetect_load_merge_write_and_preserve_source(self) -> None:
        instance = self.copy_fixture("native_snbt")
        source = instance / "config" / "ftbquests" / "quests" / "lang" / "en_us.snbt"
        source_hash = _file_hash(source)

        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_modern_snbt")

        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.21.1")
        self.assertEqual(
            [unit.key for unit in project.units],
            [
                "quest.00000000000000A1.title",
                "quest.00000000000000A1.quest_desc[0]",
                "quest.00000000000000A1.quest_desc[1]",
                "quest.00000000000000A1.quest_desc[2]",
                "task.00000000000000B2.title",
            ],
        )
        self.assertEqual(
            self.existing_by_key(project),
            {
                "quest.00000000000000A1.title": "&6既存の題名&r",
                "quest.00000000000000A1.quest_desc[0]": "&a既存の1行目&r",
                "quest.00000000000000A1.quest_desc[1]": "",
            },
        )

        translations = self.merge_existing(
            project,
            {
                "quest.00000000000000A1.quest_desc[2]": "次の行\n続き",
                "task.00000000000000B2.title": "&b装置&rを作る",
            },
        )
        adapter.write(project, translations, project.default_output)

        self.assertEqual(_file_hash(source), source_hash)
        output_text = project.default_output.read_text(encoding="utf-8")
        self.assertIn('"次の行\\n続き"', output_text)
        self.assertEqual(
            parse_lang_snbt(output_text),
            {
                "quest.00000000000000A1.title": "&6既存の題名&r",
                "quest.00000000000000A1.quest_desc": [
                    "&a既存の1行目&r",
                    "",
                    "次の行\n続き",
                ],
                "task.00000000000000B2.title": "&b装置&rを作る",
            },
        )


class SplitSnbtAdapterTests(AdapterTestCase):
    def test_autodetect_load_merge_write_tree_and_preserve_sources(self) -> None:
        instance = self.copy_fixture("split_snbt")
        source_dir = instance / "config" / "ftbquests" / "quests" / "lang" / "en_us"
        source_hashes = _tree_hashes(source_dir)

        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_split_snbt")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.21.1")

        self.assertEqual(
            self.existing_by_key(project),
            {
                "chapter.00000000000000C1.title": "&6既存のメイン&r",
                "quest.00000000000000D2.title": "&e既存クエスト&r",
                "quest.00000000000000D2.quest_desc[0]": "&a既存の1行目&r",
                "quest.00000000000000D2.quest_desc[1]": "",
            },
        )
        translations = self.merge_existing(
            project,
            {
                "quest.00000000000000D2.quest_desc[2]": "続き\n次行",
                "task.00000000000000E3.title": "&b機械&rを作る",
            },
        )
        adapter.write(project, translations, project.default_output)

        self.assertEqual(_tree_hashes(source_dir), source_hashes)
        self.assertEqual(
            sorted(path.relative_to(project.default_output).as_posix() for path in project.default_output.rglob("*.snbt")),
            ["chapter.snbt", "chapters/welcome.snbt"],
        )
        chapter = parse_lang_snbt((project.default_output / "chapter.snbt").read_text(encoding="utf-8"))
        welcome_text = (project.default_output / "chapters" / "welcome.snbt").read_text(encoding="utf-8")
        welcome = parse_lang_snbt(welcome_text)
        self.assertEqual(chapter, {"chapter.00000000000000C1.title": "&6既存のメイン&r"})
        self.assertEqual(
            welcome["quest.00000000000000D2.quest_desc"],
            ["&a既存の1行目&r", "", "続き\n次行"],
        )
        self.assertEqual(welcome["task.00000000000000E3.title"], "&b機械&rを作る")
        self.assertIn('"続き\\n次行"', welcome_text)


class SplitJson5AdapterTests(AdapterTestCase):
    def test_autodetect_load_merge_write_tree_and_preserve_json5_sources(self) -> None:
        instance = self.copy_fixture("split_json5")
        source_dir = instance / "config" / "ftbquests" / "quests" / "lang" / "en_us"
        source_hashes = _tree_hashes(source_dir)

        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_split_json5")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="26.1.2")

        self.assertEqual(
            self.existing_by_key(project),
            {
                "chapter.00000000000000F1.title": "&6既存の章&r",
                "quest.00000000000000F2.title": "&e既存クエスト&r",
                "quest.00000000000000F2.quest_desc[0]": "&a既存の1行目&r",
                "quest.00000000000000F2.quest_desc[1]": "",
            },
        )
        translations = self.merge_existing(
            project,
            {
                "quest.00000000000000F2.quest_desc[2]": "JSON5の続き\n次行",
                "task.00000000000000F3.title": "&b部品&rを作る",
            },
        )
        adapter.write(project, translations, project.default_output)

        self.assertEqual(_tree_hashes(source_dir), source_hashes)
        self.assertEqual(
            sorted(path.relative_to(project.default_output).as_posix() for path in project.default_output.rglob("*.json5")),
            ["chapter.json5", "chapters/welcome.json5"],
        )
        chapter = json.loads((project.default_output / "chapter.json5").read_text(encoding="utf-8"))
        welcome_path = project.default_output / "chapters" / "welcome.json5"
        welcome = json.loads(welcome_path.read_text(encoding="utf-8"))
        self.assertEqual(chapter, {"chapter.00000000000000F1.title": "&6既存の章&r"})
        self.assertEqual(
            welcome["quest.00000000000000F2.quest_desc"],
            ["&a既存の1行目&r", "", "JSON5の続き\n次行"],
        )
        self.assertEqual(welcome["task.00000000000000F3.title"], "&b部品&rを作る")


class LegacyExportedJsonAdapterTests(AdapterTestCase):
    def test_autodetect_load_merge_write_and_preserve_source(self) -> None:
        instance = self.copy_fixture("legacy_json")
        source = instance / "kubejs" / "assets" / "kubejs" / "lang" / "en_us.json"
        source_hash = _file_hash(source)

        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_legacy_json")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")

        self.assertEqual(
            self.existing_by_key(project),
            {"atm9.quest.welcome.title": "&6既存のようこそ&r"},
        )
        translations = self.merge_existing(
            project,
            {
                "atm9.quest.welcome.description": "1行目\n\n2行目",
                "atm9.quest.machine": "&bMekanism&rの機械",
            },
        )
        adapter.write(project, translations, project.default_output)

        self.assertEqual(_file_hash(source), source_hash)
        self.assertEqual(
            json.loads(project.default_output.read_text(encoding="utf-8")),
            {
                "atm9.quest.welcome.title": "&6既存のようこそ&r",
                "atm9.quest.welcome.description": "1行目\n\n2行目",
                "atm9.quest.machine": "&bMekanism&rの機械",
            },
        )


class LegacyRawSnbtAdapterTests(AdapterTestCase):
    def test_autodetect_installs_keyized_quests_backup_and_resourcepack(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        quest_root = instance / "config" / "ftbquests" / "quests"
        source_hashes = _tree_hashes(quest_root)
        existing_lang = (
            instance
            / "resourcepacks"
            / "mq_localizer_ja_jp"
            / "assets"
            / "minecraft"
            / "lang"
        )
        existing_lang.mkdir(parents=True)
        shutil.copy2(
            instance
            / "mq-localizer-output"
            / "ftbquests-legacy-ja_jp"
            / "resourcepacks"
            / "mq_localizer_ja_jp"
            / "assets"
            / "minecraft"
            / "lang"
            / "ja_jp.json",
            existing_lang / "ja_jp.json",
        )

        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_legacy_raw")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")

        existing = self.existing_by_key(project)
        self.assertEqual(
            existing,
            {"mq_localizer.quest.1111111111111111.title": "既存のクエスト名"},
        )
        translations_by_source = {
            "Main Group": "メイングループ",
            "Welcome &6Home&r": "ようこそ &6ホーム&r",
            "First chapter": "最初のチャプター",
            "Hover text": "ホバーテキスト",
            "&eFirst steps&r": "&e最初の手順&r",
            "Use &aMekanism&r.": "&aMekanism&rを使います。",
            "Open docs": "ドキュメントを開く",
            "Tooltip": "ツールチップ",
            " now": " 今すぐ",
            "Craft &bWidget&r": "&bWidget&rを作る",
        }
        translations = dict(project.existing)
        for unit in project.units:
            if unit.id in translations:
                continue
            self.assertIn(unit.source, translations_by_source, f"unexpected extracted text: {unit.source!r}")
            translations[unit.id] = translations_by_source[unit.source]

        adapter.write(project, translations, project.default_output)

        backup_root = quest_root.with_name("quests.bak")
        self.assertEqual(_tree_hashes(backup_root), source_hashes)
        copied_root = quest_root
        self.assertNotEqual(_tree_hashes(copied_root), source_hashes)

        copied_chapter_path = copied_root / "chapters" / "welcome.snbt"
        copied_chapter = parse_snbt(copied_chapter_path.read_text(encoding="utf-8"))
        self.assertIsInstance(copied_chapter, SnbtCompound)
        assert isinstance(copied_chapter, SnbtCompound)
        quests = copied_chapter["quests"]
        self.assertIsInstance(quests, SnbtList)
        assert isinstance(quests, SnbtList)
        quest = quests[0]
        self.assertIsInstance(quest, SnbtCompound)
        assert isinstance(quest, SnbtCompound)
        self.assertEqual(quest["title"].value, "{mq_localizer.quest.1111111111111111.title}")

        description = quest["description"]
        self.assertIsInstance(description, SnbtList)
        assert isinstance(description, SnbtList)
        description_values = [item.value for item in description if isinstance(item, SnbtString)]
        self.assertEqual(description_values[1:4], ["", "{@pagebreak}", "{existing.translation.key}"])
        self.assertTrue(description_values[0].startswith("{mq_localizer.quest.1111111111111111.description.1"))

        component = json.loads(description_values[4])
        self.assertNotIn("text", component)
        self.assertTrue(component["translate"].startswith("mq_localizer.quest.1111111111111111.description.5.part."))
        self.assertEqual(component["color"], "gold")
        self.assertIs(component["bold"], True)
        self.assertEqual(
            component["clickEvent"],
            {"action": "open_url", "value": "https://example.invalid/docs"},
        )
        self.assertEqual(component["hoverEvent"]["action"], "show_text")
        self.assertIn("translate", component["hoverEvent"]["contents"])
        self.assertIs(component["extra"][0]["italic"], True)
        self.assertIn("translate", component["extra"][0])

        lang_dir = project.default_output / "assets" / "minecraft" / "lang"
        source_catalog = json.loads((lang_dir / "en_us.json").read_text(encoding="utf-8"))
        target_catalog = json.loads((lang_dir / "ja_jp.json").read_text(encoding="utf-8"))
        self.assertEqual(source_catalog.keys(), target_catalog.keys())
        self.assertEqual(
            target_catalog["mq_localizer.quest.1111111111111111.title"],
            "既存のクエスト名",
        )
        self.assertEqual(source_catalog[component["translate"]], "Open docs")
        self.assertEqual(target_catalog[component["translate"]], "ドキュメントを開く")
        hover_key = component["hoverEvent"]["contents"]["translate"]
        self.assertEqual(source_catalog[hover_key], "Tooltip")
        self.assertEqual(target_catalog[hover_key], "ツールチップ")
        extra_key = component["extra"][0]["translate"]
        self.assertEqual(source_catalog[extra_key], " now")
        self.assertEqual(target_catalog[extra_key], " 今すぐ")
        formatted_key = next(key for key, value in source_catalog.items() if value == "Use &aMekanism&r.")
        self.assertEqual(target_catalog[formatted_key], "&aMekanism&rを使います。")
        self.assertIn("", description_values)
        self.assertTrue((project.default_output / "pack.mcmeta").is_file())

    def test_kubejs_directory_routes_language_assets_without_resourcepack(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        (instance / "kubejs").mkdir()
        existing_lang = instance / "kubejs" / "assets" / "mq_localizer" / "lang"
        existing_lang.mkdir(parents=True)
        (existing_lang / "en_us.json").write_text(
            json.dumps(
                {
                    "custom.source.only": "Manual source",
                    "mq_localizer.quest.obsolete.title": "Obsolete source",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (existing_lang / "ja_jp.json").write_text(
            json.dumps(
                {
                    "custom.target.only": "手動訳",
                    "mq_localizer.quest.obsolete.title": "古い自動生成訳",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        quest_root = instance / "config" / "ftbquests" / "quests"
        source_hashes = _tree_hashes(quest_root)

        adapter = create_default_registry().detect(instance, "en_us")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        self.assertEqual(project.metadata["legacy_language_delivery"], "kubejs")
        self.assertFalse(project.metadata["resourcepack_activation_required"])
        self.assertEqual(
            project.default_output,
            instance / "kubejs" / "assets" / "mq_localizer",
        )

        translations = {unit.id: f"訳:{unit.source}" for unit in project.units}
        adapter.write(project, translations, project.default_output)

        self.assertEqual(_tree_hashes(quest_root.with_name("quests.bak")), source_hashes)
        self.assertTrue((project.default_output / "lang" / "en_us.json").is_file())
        self.assertTrue((project.default_output / "lang" / "ja_jp.json").is_file())
        source_catalog = json.loads(
            (project.default_output / "lang" / "en_us.json").read_text(encoding="utf-8")
        )
        target_catalog = json.loads(
            (project.default_output / "lang" / "ja_jp.json").read_text(encoding="utf-8")
        )
        self.assertEqual(source_catalog["custom.source.only"], "Manual source")
        self.assertEqual(target_catalog["custom.target.only"], "手動訳")
        self.assertNotIn("mq_localizer.quest.obsolete.title", source_catalog)
        self.assertNotIn("mq_localizer.quest.obsolete.title", target_catalog)
        self.assertFalse((project.default_output / "pack.mcmeta").exists())
        self.assertFalse((instance / "resourcepacks" / "mq_localizer_ja_jp").exists())

    def test_language_delivery_change_after_analysis_requires_reanalysis(self) -> None:
        without_kubejs = self.copy_fixture("legacy_raw")
        adapter = create_default_registry().detect(without_kubejs, "en_us")
        project = adapter.load(
            without_kubejs,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        (without_kubejs / "kubejs").mkdir()

        with self.assertRaisesRegex(AdapterError, "KubeJS.*再解析"):
            adapter.write(
                project,
                {unit.id: unit.source for unit in project.units},
                project.default_output,
            )
        self.assertFalse(project.default_output.exists())

        with_kubejs = self.copy_fixture("legacy_raw")
        (with_kubejs / "kubejs").mkdir()
        adapter = create_default_registry().detect(with_kubejs, "en_us")
        project = adapter.load(
            with_kubejs,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        (with_kubejs / "kubejs").rmdir()

        with self.assertRaisesRegex(AdapterError, "KubeJS.*再解析"):
            adapter.write(
                project,
                {unit.id: unit.source for unit in project.units},
                project.default_output,
            )
        self.assertFalse((with_kubejs / "kubejs").exists())
        self.assertFalse((with_kubejs / "resourcepacks").exists())

    def test_second_raw_run_reuses_quests_backup_without_overwriting_it(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        adapter = create_default_registry().detect(instance, "en_us")
        first = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        first_translations = {unit.id: unit.source for unit in first.units}
        adapter.write(first, first_translations, first.default_output)
        backup = instance / "config" / "ftbquests" / "quests.bak"
        backup_hashes = _tree_hashes(backup)

        rerun_adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(rerun_adapter.id, "ftb_legacy_raw")
        rerun = rerun_adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        self.assertEqual(rerun.source_path, backup)
        self.assertEqual(rerun.metadata["legacy_source_kind"], "backup")
        rerun_translations = {unit.id: f"再:{unit.source}" for unit in rerun.units}
        rerun_adapter.write(rerun, rerun_translations, rerun.default_output)

        self.assertEqual(_tree_hashes(backup), backup_hashes)
        target = json.loads(
            (
                rerun.default_output
                / "assets"
                / "minecraft"
                / "lang"
                / "ja_jp.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(target)
        self.assertTrue(all(value.startswith("再:") for value in target.values()))

    def test_managed_rerun_wins_only_on_legacy_minecraft_when_native_lang_appears(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        adapter.write(
            project,
            {unit.id: unit.source for unit in project.units},
            project.default_output,
        )
        native_lang = (
            instance
            / "config"
            / "ftbquests"
            / "quests"
            / "lang"
            / "en_us.snbt"
        )
        native_lang.parent.mkdir()
        native_lang.write_text('{test.key: "English"}\n', encoding="utf-8")
        native_lang_bytes = native_lang.read_bytes()

        self.assertEqual(
            create_default_registry().detect(instance, "en_us", "1.20.1").id,
            "ftb_legacy_raw",
        )
        self.assertEqual(
            create_default_registry().detect(instance, "en_us", "1.21.1").id,
            "ftb_modern_snbt",
        )
        rerun_adapter = create_default_registry().detect(
            instance,
            "en_us",
            "1.20.1",
        )
        rerun = rerun_adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        rerun_adapter.write(
            rerun,
            {unit.id: f"再:{unit.source}" for unit in rerun.units},
            rerun.default_output,
        )
        self.assertEqual(native_lang.read_bytes(), native_lang_bytes)

    def test_managed_rerun_accepts_writer_normalized_mixed_newlines(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        chapter = (
            instance
            / "config"
            / "ftbquests"
            / "quests"
            / "chapters"
            / "welcome.snbt"
        )
        original = chapter.read_bytes().decode("utf-8")
        mixed = original.replace("\n", "\r\n").replace("\r\n", "\n", 1)
        chapter.write_bytes(mixed.encode("utf-8"))

        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        adapter.write(
            project,
            {unit.id: unit.source for unit in project.units},
            project.default_output,
        )

        self.assertEqual(
            create_default_registry().detect(instance, "en_us", "1.20.1").id,
            "ftb_legacy_raw",
        )

    def test_existing_raw_quests_and_backup_are_rejected_as_ambiguous(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        quests = instance / "config" / "ftbquests" / "quests"
        shutil.copytree(quests, quests.with_name("quests.bak"))

        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_legacy_raw")
        with self.assertRaisesRegex(AdapterError, "両方が原本形式"):
            adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")

    def test_unrelated_mq_localizer_text_does_not_mark_active_as_generated(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        quests = instance / "config" / "ftbquests" / "quests"
        shutil.copytree(quests, quests.with_name("quests.bak"))
        data = quests / "data.snbt"
        data.write_text(
            data.read_text(encoding="utf-8") + "\n# mq_localizer.unrelated\n",
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(instance, "en_us")
        with self.assertRaisesRegex(AdapterError, "両方が原本形式"):
            adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")


if __name__ == "__main__":
    unittest.main()
