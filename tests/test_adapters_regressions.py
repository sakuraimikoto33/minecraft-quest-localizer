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
from mq_localizer.adapters.ftb_legacy_raw import _escape_percent, _pack_format  # noqa: E402
from mq_localizer.domain import AdapterError  # noqa: E402
from mq_localizer.snbt import SnbtCompound, SnbtList, SnbtString, parse_snbt  # noqa: E402


FIXTURES = Path(__file__).with_name("fixtures")


def _snapshot(path: Path) -> dict[str, tuple[str, str]]:
    snapshot: dict[str, tuple[str, str]] = {}
    for candidate in sorted(path.rglob("*")):
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_dir():
            snapshot[relative] = ("directory", "")
        elif candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            snapshot[relative] = ("file", digest)
    return snapshot


class AdapterRegressionTests(unittest.TestCase):
    def copy_fixture(self, name: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name) / name
        shutil.copytree(FIXTURES / name, destination)
        return destination

    def test_split_json5_accepts_block_comments(self) -> None:
        instance = self.copy_fixture("split_json5")
        source = (
            instance
            / "config"
            / "ftbquests"
            / "quests"
            / "lang"
            / "en_us"
            / "chapter.json5"
        )
        source.write_text(
            """{
  /* A standard JSON5 block comment.
     It may span lines and contain // without starting another comment. */
  "chapter.00000000000000F1.title": '&6Main Chapter&r',
}
""",
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_split_json5")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="26.1.2")
        sources = {unit.key: unit.source for unit in project.units}
        self.assertEqual(sources["chapter.00000000000000F1.title"], "&6Main Chapter&r")

        output = instance / "block-comment-output"
        adapter.write(project, {unit.id: unit.source for unit in project.units}, output)
        rendered = json.loads((output / "chapter.json5").read_text(encoding="utf-8"))
        self.assertEqual(rendered["chapter.00000000000000F1.title"], "&6Main Chapter&r")

    def test_split_json5_rejects_duplicate_keys_across_documents(self) -> None:
        instance = self.copy_fixture("split_json5")
        source_dir = instance / "config" / "ftbquests" / "quests" / "lang" / "en_us"
        duplicate = source_dir / "chapters" / "duplicate.json5"
        duplicate.write_text(
            """{
  "chapter.00000000000000F1.title": 'Duplicate title',
}
""",
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(instance, "en_us")
        with self.assertRaises(AdapterError) as raised:
            adapter.load(instance, "en_us", "ja_jp", minecraft_version="26.1.2")

        message = str(raised.exception)
        self.assertIn("chapter.00000000000000F1.title", message)
        self.assertIn("chapter.json5", message)
        self.assertIn("duplicate.json5", message)

    def test_legacy_json_rejects_duplicate_keys(self) -> None:
        instance = self.copy_fixture("legacy_json")
        source = instance / "kubejs" / "assets" / "kubejs" / "lang" / "en_us.json"
        source.write_text(
            '{"duplicate.key":"first","duplicate.key":"second"}\n',
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(source, "en_us")
        self.assertEqual(adapter.id, "ftb_legacy_json")
        with self.assertRaisesRegex(AdapterError, "duplicate.key"):
            adapter.load(source, "en_us", "ja_jp", minecraft_version="1.20.1")

    def test_legacy_raw_transforms_hover_event_value_text_leaf(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        chapter_path = instance / "config" / "ftbquests" / "quests" / "chapters" / "legacy_hover.snbt"
        component = {
            "text": "Root label",
            "hoverEvent": {
                "action": "show_text",
                "value": {"text": "Legacy tooltip", "color": "aqua"},
            },
        }
        encoded_component = json.dumps(
            json.dumps(component, ensure_ascii=False, separators=(",", ":")),
            ensure_ascii=False,
        )
        chapter_path.write_text(
            """{
  id: "4444444444444444"
  quests: [{
    id: "5555555555555555"
    description: [
      %s
    ]
  }]
}
"""
            % encoded_component,
            encoding="utf-8",
        )
        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_legacy_raw")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        output = project.default_output
        self.assertIn("Legacy tooltip", {unit.source for unit in project.units})
        translations = {
            unit.id: "旧式ツールチップ" if unit.source == "Legacy tooltip" else unit.source
            for unit in project.units
        }
        adapter.write(project, translations, output)

        rendered_root = parse_snbt(
            (instance / "config" / "ftbquests" / "quests" / "chapters" / "legacy_hover.snbt").read_text(
                encoding="utf-8"
            )
        )
        self.assertIsInstance(rendered_root, SnbtCompound)
        assert isinstance(rendered_root, SnbtCompound)
        quests = rendered_root["quests"]
        self.assertIsInstance(quests, SnbtList)
        assert isinstance(quests, SnbtList)
        quest = quests[0]
        self.assertIsInstance(quest, SnbtCompound)
        assert isinstance(quest, SnbtCompound)
        description = quest["description"]
        self.assertIsInstance(description, SnbtList)
        assert isinstance(description, SnbtList)
        raw_component = description[0]
        self.assertIsInstance(raw_component, SnbtString)
        assert isinstance(raw_component, SnbtString)
        transformed = json.loads(raw_component.value)

        hover = transformed["hoverEvent"]
        self.assertEqual(hover["action"], "show_text")
        self.assertEqual(hover["value"]["color"], "aqua")
        self.assertNotIn("text", hover["value"])
        hover_key = hover["value"]["translate"]

        lang_dir = output / "assets" / "minecraft" / "lang"
        source_catalog = json.loads((lang_dir / "en_us.json").read_text(encoding="utf-8"))
        target_catalog = json.loads((lang_dir / "ja_jp.json").read_text(encoding="utf-8"))
        self.assertEqual(source_catalog[hover_key], "Legacy tooltip")
        self.assertEqual(target_catalog[hover_key], "旧式ツールチップ")

    def test_legacy_raw_records_component_display_stream_terminology_groups(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        chapter_path = (
            instance
            / "config"
            / "ftbquests"
            / "quests"
            / "chapters"
            / "terminology_groups.snbt"
        )
        # Deliberately put extra before text in JSON object order. Minecraft
        # still renders text before extra, so terminology grouping must use
        # component semantics rather than serialized field order.
        component = {
            "extra": [{"text": "Industrialization"}],
            "with": [{"text": "First argument"}, {"text": "Second argument"}],
            "hoverEvent": {
                "action": "show_text",
                "contents": {"text": "Tooltip"},
            },
            "text": "Modern ",
        }
        encoded_component = json.dumps(
            json.dumps(component, ensure_ascii=False, separators=(",", ":")),
            ensure_ascii=False,
        )
        chapter_path.write_text(
            """{
  id: "8888888888888888"
  quests: [{
    id: "9999999999999999"
    description: [%s]
  }]
}
"""
            % encoded_component,
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_legacy_raw")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        units = {unit.id: unit.source for unit in project.units}
        grouped_sources = [
            [units[unit_id] for unit_id in group]
            for group in project.metadata["terminology_groups"]
        ]

        self.assertIn(["Modern ", "Industrialization"], grouped_sources)
        self.assertIn(["First argument"], grouped_sources)
        self.assertIn(["Second argument"], grouped_sources)
        self.assertIn(["Tooltip"], grouped_sources)
        self.assertNotIn(
            ["Modern ", "Industrialization", "Tooltip"],
            grouped_sources,
        )

    def test_legacy_raw_reference_terms_only_record_single_primary_title_stream(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        chapter_path = (
            instance
            / "config"
            / "ftbquests"
            / "quests"
            / "chapters"
            / "reference_term_groups.snbt"
        )
        chapter_title = {
            # Serialized field order must not affect the rendered main stream.
            "extra": [{"text": "Horizons"}],
            "with": [{"text": "Title argument"}],
            "hoverEvent": {
                "action": "show_text",
                "contents": {"text": "Title tooltip"},
            },
            "text": "Arcane ",
        }
        dynamic_quest_title = [
            {"text": "Dynamic before"},
            {"translate": "example.dynamic"},
            {"text": "Dynamic after"},
        ]
        one_sided_dynamic_task_title = {
            "translate": "example.dynamic.task",
            "extra": [{"text": "Literal tail"}],
        }
        description = {"text": "Description prose"}
        encoded = [
            json.dumps(
                json.dumps(component, ensure_ascii=False, separators=(",", ":")),
                ensure_ascii=False,
            )
            for component in (
                chapter_title,
                dynamic_quest_title,
                one_sided_dynamic_task_title,
                description,
            )
        ]
        chapter_path.write_text(
            """{
  id: "C888888888888888"
  title: %s
  quests: [{
    id: "C999999999999999"
    title: %s
    tasks: [{
      id: "C777777777777777"
      title: %s
    }]
    description: [%s]
  }]
}
"""
            % tuple(encoded),
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(instance, "en_us")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        units = {unit.id: unit.source for unit in project.units}
        grouped_sources = [
            [units[unit_id] for unit_id in group]
            for group in project.metadata["reference_term_groups"]
        ]

        self.assertEqual(grouped_sources, [["Arcane ", "Horizons"]])
        flattened = {source for group in grouped_sources for source in group}
        self.assertNotIn("Title argument", flattened)
        self.assertNotIn("Title tooltip", flattened)
        self.assertNotIn("Dynamic before", flattened)
        self.assertNotIn("Dynamic after", flattened)
        self.assertNotIn("Literal tail", flattened)
        self.assertNotIn("Description prose", flattened)

    def test_legacy_raw_dynamic_components_split_but_empty_style_wrappers_join_streams(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        chapter_path = (
            instance
            / "config"
            / "ftbquests"
            / "quests"
            / "chapters"
            / "terminology_barriers.snbt"
        )
        components = (
            [
                {"text": "Modern "},
                {"translate": "example.middle"},
                {"text": "Industrialization"},
            ],
            [
                {"text": "Modern "},
                {"bold": True},
                {"text": "", "italic": True},
                {"text": "Industrialization"},
            ],
            [
                {"text": "Modern "},
                7,
                {"text": "Industrialization"},
            ],
            {
                "text": "Visible base",
                "hoverEvent": {
                    "action": "show_text",
                    "contents": [
                        {"text": "Modern "},
                        {"translate": "example.hover.middle"},
                        {"text": "Industrialization"},
                    ],
                },
            },
        )
        encoded_components = ",\n      ".join(
            json.dumps(
                json.dumps(component, ensure_ascii=False, separators=(",", ":")),
                ensure_ascii=False,
            )
            for component in components
        )
        chapter_path.write_text(
            """{
  id: "A888888888888888"
  quests: [{
    id: "A999999999999999"
    description: [
      %s
    ]
  }]
}
"""
            % encoded_components,
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(instance, "en_us")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        units = {unit.id: unit.source for unit in project.units}
        grouped_sources = [
            [units[unit_id] for unit_id in group]
            for group in project.metadata["terminology_groups"]
        ]

        self.assertIn(["Modern "], grouped_sources)
        self.assertIn(["Industrialization"], grouped_sources)
        self.assertEqual(
            grouped_sources.count(["Modern ", "Industrialization"]),
            1,
        )

    def test_legacy_raw_translate_key_collision_is_preserved_without_orphan_units(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        chapter_path = (
            instance
            / "config"
            / "ftbquests"
            / "quests"
            / "chapters"
            / "translate_collision.snbt"
        )
        components = (
            {
                "text": "Collision text first",
                "translate": {"malformed": True},
                "extra": [{"text": "Tail after"}],
            },
            {
                "translate": {"malformed": True},
                "text": "Collision translate first",
                "extra": [{"text": "Tail before"}],
            },
            {
                "selector": "@a",
                "separator": {"text": "Selector separator"},
            },
        )
        encoded_components = ",\n      ".join(
            json.dumps(
                json.dumps(component, ensure_ascii=False, separators=(",", ":")),
                ensure_ascii=False,
            )
            for component in components
        )
        chapter_path.write_text(
            """{
  id: "B888888888888888"
  quests: [{
    id: "B999999999999999"
    description: [
      %s
    ]
  }]
}
"""
            % encoded_components,
            encoding="utf-8",
        )
        adapter = create_default_registry().detect(instance, "en_us")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        output = project.default_output
        source_to_key = {unit.source: unit.key for unit in project.units}
        self.assertNotIn("Collision text first", source_to_key)
        self.assertNotIn("Collision translate first", source_to_key)
        self.assertIn("Tail after", source_to_key)
        self.assertIn("Tail before", source_to_key)
        self.assertIn("Selector separator", source_to_key)

        adapter.write(
            project,
            {unit.id: unit.source for unit in project.units},
            output,
        )
        rendered_root = parse_snbt(
            (
                instance
                / "config"
                / "ftbquests"
                / "quests"
                / "chapters"
                / "translate_collision.snbt"
            ).read_text(encoding="utf-8")
        )
        self.assertIsInstance(rendered_root, SnbtCompound)
        assert isinstance(rendered_root, SnbtCompound)
        quests = rendered_root["quests"]
        self.assertIsInstance(quests, SnbtList)
        assert isinstance(quests, SnbtList)
        quest = quests[0]
        self.assertIsInstance(quest, SnbtCompound)
        assert isinstance(quest, SnbtCompound)
        description = quest["description"]
        self.assertIsInstance(description, SnbtList)
        assert isinstance(description, SnbtList)
        transformed = []
        for value in description:
            self.assertIsInstance(value, SnbtString)
            assert isinstance(value, SnbtString)
            transformed.append(json.loads(value.value))

        self.assertEqual(transformed[0]["text"], "Collision text first")
        self.assertEqual(transformed[0]["translate"], {"malformed": True})
        self.assertEqual(
            transformed[0]["extra"][0]["translate"],
            source_to_key["Tail after"],
        )
        self.assertEqual(transformed[1]["text"], "Collision translate first")
        self.assertEqual(transformed[1]["translate"], {"malformed": True})
        self.assertEqual(
            transformed[1]["extra"][0]["translate"],
            source_to_key["Tail before"],
        )
        self.assertEqual(
            transformed[2]["separator"]["translate"],
            source_to_key["Selector separator"],
        )

    def test_legacy_raw_transforms_array_components_without_misreading_bracketed_prose(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        chapter_path = instance / "config" / "ftbquests" / "quests" / "chapters" / "array_text.snbt"
        component = [
            {"text": "Array root", "color": "gold"},
            [{"text": "Nested text", "italic": True}],
            " tail",
        ]
        encoded_component = json.dumps(
            json.dumps(component, ensure_ascii=False, separators=(",", ":")),
            ensure_ascii=False,
        )
        chapter_path.write_text(
            """{
  id: "6666666666666666"
  quests: [{
    id: "7777777777777777"
    description: [
      %s
      "[Optional] objective"
    ]
  }]
}
"""
            % encoded_component,
            encoding="utf-8",
        )
        adapter = create_default_registry().detect(instance, "en_us")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        output = project.default_output
        sources = {unit.source for unit in project.units}
        self.assertTrue({"Array root", "Nested text", " tail", "[Optional] objective"} <= sources)
        adapter.write(project, {unit.id: unit.source for unit in project.units}, output)

        rendered_root = parse_snbt(
            (instance / "config" / "ftbquests" / "quests" / "chapters" / "array_text.snbt").read_text(
                encoding="utf-8"
            )
        )
        self.assertIsInstance(rendered_root, SnbtCompound)
        assert isinstance(rendered_root, SnbtCompound)
        quests = rendered_root["quests"]
        self.assertIsInstance(quests, SnbtList)
        assert isinstance(quests, SnbtList)
        quest = quests[0]
        self.assertIsInstance(quest, SnbtCompound)
        assert isinstance(quest, SnbtCompound)
        description = quest["description"]
        self.assertIsInstance(description, SnbtList)
        assert isinstance(description, SnbtList)
        raw_node, optional_node = description
        self.assertIsInstance(raw_node, SnbtString)
        self.assertIsInstance(optional_node, SnbtString)
        assert isinstance(raw_node, SnbtString)
        assert isinstance(optional_node, SnbtString)
        transformed = json.loads(raw_node.value)
        self.assertEqual(transformed[0]["color"], "gold")
        self.assertIn("translate", transformed[0])
        self.assertEqual(transformed[1][0]["italic"], True)
        self.assertIn("translate", transformed[1][0])
        self.assertIn("translate", transformed[2])
        self.assertRegex(optional_node.value, r"^\{mq_localizer\..+\}$")

    def test_legacy_raw_percent_escaping_and_historical_pack_formats(self) -> None:
        self.assertEqual(
            _escape_percent("Progress 50% / already %% / placeholder %s"),
            "Progress 50%% / already %%%% / placeholder %%s",
        )
        cases = {
            "1.8.9": 1,
            "1.9.4": 2,
            "1.12.2": 3,
            "1.13.2": 4,
            "1.14.4": 4,
            "1.15.2": 5,
            "1.16.1": 5,
            "1.16.2": 6,
            "1.16.5": 6,
            "1.17.1": 7,
            "1.20.1": 15,
            "1.20.2": 18,
            "1.20.3": 22,
            "1.20.4": 22,
            "1.20.5": 32,
            "1.20.6": 32,
        }
        for version, expected in cases.items():
            with self.subTest(version=version):
                self.assertEqual(_pack_format(version), expected)

    def test_legacy_raw_incomplete_mapping_leaves_new_and_existing_outputs_untouched(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        adapter = create_default_registry().detect(instance, "en_us")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        self.assertGreater(len(project.units), 1)

        new_output = instance / "must-not-be-created"
        siblings_before = {candidate.name for candidate in instance.iterdir()}
        with self.assertRaises(AdapterError):
            adapter.write(project, {}, new_output)
        self.assertFalse(new_output.exists())
        self.assertEqual({candidate.name for candidate in instance.iterdir()}, siblings_before)

        existing_output = instance / "must-stay-unchanged"
        nested = existing_output / "custom" / "nested"
        nested.mkdir(parents=True)
        (existing_output / "sentinel.txt").write_text("keep me exactly\n", encoding="utf-8")
        (nested / "payload.bin").write_bytes(b"\x00\x01do-not-touch\xff")
        before = _snapshot(existing_output)

        incomplete = {project.units[0].id: "only one translation"}
        with self.assertRaises(AdapterError):
            adapter.write(project, incomplete, existing_output)

        self.assertEqual(_snapshot(existing_output), before)

    def test_split_writers_reject_quest_data_directories_without_mutation(self) -> None:
        cases = [
            ("split_snbt", "ftb_split_snbt", "1.21.1"),
            ("split_json5", "ftb_split_json5", "26.1.2"),
        ]
        for fixture, expected_adapter, minecraft_version in cases:
            with self.subTest(adapter=expected_adapter):
                instance = self.copy_fixture(fixture)
                quest_root = instance / "config" / "ftbquests" / "quests"

                # These sentinels model real quest chapter data at paths that
                # an unsafe split writer could overwrite with locale files.
                chapter_dir = quest_root / "chapters"
                chapter_dir.mkdir(parents=True, exist_ok=True)
                (chapter_dir / "welcome.snbt").write_text(
                    '{id: "ORIGINAL-QUEST-CHAPTER"}\n', encoding="utf-8"
                )
                (chapter_dir / "chapter.snbt").write_text(
                    '{id: "ANOTHER-ORIGINAL-CHAPTER"}\n', encoding="utf-8"
                )

                adapter = create_default_registry().detect(instance, "en_us")
                self.assertEqual(adapter.id, expected_adapter)
                project = adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version=minecraft_version,
                )
                translations = {unit.id: unit.source for unit in project.units}
                quest_before = _snapshot(quest_root)
                lang_before = _snapshot(quest_root / "lang")

                for unsafe_output in (quest_root, quest_root / "chapters"):
                    with self.subTest(adapter=expected_adapter, output=unsafe_output.name):
                        with self.assertRaises(AdapterError):
                            adapter.write(project, translations, unsafe_output)
                        self.assertEqual(_snapshot(quest_root), quest_before)
                        self.assertEqual(_snapshot(quest_root / "lang"), lang_before)

    def test_native_snbt_rejects_data_file_without_mutation(self) -> None:
        instance = self.copy_fixture("native_snbt")
        quest_root = instance / "config" / "ftbquests" / "quests"
        data_file = quest_root / "data.snbt"
        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_modern_snbt")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.21.1")
        translations = {unit.id: unit.source for unit in project.units}
        quest_before = _snapshot(quest_root)
        lang_before = _snapshot(quest_root / "lang")

        with self.assertRaises(AdapterError):
            adapter.write(project, translations, data_file)

        self.assertEqual(_snapshot(quest_root), quest_before)
        self.assertEqual(_snapshot(quest_root / "lang"), lang_before)

    def test_native_snbt_autodetects_configured_fallback_locale(self) -> None:
        instance = self.copy_fixture("native_snbt")
        quest_root = instance / "config" / "ftbquests" / "quests"
        source = quest_root / "lang" / "en_us.snbt"
        fallback = source.with_name("de_de.snbt")
        source.rename(fallback)
        (quest_root / "data.snbt").write_text(
            '{fallback_locale: "de_de"}\n',
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_modern_snbt")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.21.1")

        self.assertEqual(project.source_locale, "de_de")
        self.assertEqual(project.source_path, fallback)
        self.assertTrue(any("fallback_locale" in warning for warning in project.warnings))

    def test_legacy_raw_rejects_instance_root_without_mutation(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        adapter = create_default_registry().detect(instance, "en_us")
        self.assertEqual(adapter.id, "ftb_legacy_raw")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        translations = {unit.id: unit.source for unit in project.units}
        instance_before = _snapshot(instance)

        with self.assertRaises(AdapterError):
            adapter.write(project, translations, instance)

        self.assertEqual(_snapshot(instance), instance_before)


if __name__ == "__main__":
    unittest.main()
