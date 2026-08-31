from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mq_localizer.adapters.ftb_legacy_json as legacy_json_module  # noqa: E402
from mq_localizer.adapters.ftb_legacy_json import FtbLegacyJsonAdapter  # noqa: E402
from mq_localizer.domain import TranslationUnit  # noqa: E402


class LegacyJsonResourceContextTests(unittest.TestCase):
    def _load(
        self,
        catalog: dict[str, str],
        chapter_snbt: str,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        instance = Path(temporary.name) / "instance"
        chapter = instance / "config" / "ftbquests" / "quests" / "chapters" / "context.snbt"
        chapter.parent.mkdir(parents=True)
        chapter.write_text(chapter_snbt, encoding="utf-8")
        source = (
            instance
            / "resourcepacks"
            / "mq_localizer_ja_jp"
            / "assets"
            / "minecraft"
            / "lang"
            / "en_us.json"
        )
        source.parent.mkdir(parents=True)
        source.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return FtbLegacyJsonAdapter().load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )

    @staticmethod
    def _resources_by_key(project) -> dict[str, tuple[str, ...]]:
        return {unit.key: unit.resource_ids for unit in project.units}

    def test_translation_unit_resource_ids_default_is_backward_compatible(self) -> None:
        unit = TranslationUnit("id", "key", "source")

        self.assertEqual(unit.resource_ids, ())

    def test_only_task_and_reward_titles_receive_resources_from_same_compound(self) -> None:
        catalog = {
            "mq_localizer.chapter.context.title": "Extreme Reactors",
            "mq_localizer.quest.context.title": "Build a Reactor",
            "mq_localizer.task.controller.title": "Reinforced Reactor Controller",
            "mq_localizer.task.widget.title": "Widget",
            "mq_localizer.task.entity.title": "Zombie",
            "mq_localizer.reward.fluid.title": "Water",
            "mq_localizer.task.icon_only.title": "Icon only",
        }
        project = self._load(
            catalog,
            r'''
            {
                icon: "wrongmod:chapter_icon"
                title: "{mq_localizer.chapter.context.title}"
                quests: [{
                    icon: "wrongmod:quest_icon"
                    title: "{mq_localizer.quest.context.title}"
                    tasks: [
                        {
                            id: "TASK1"
                            icon: "wrongmod:task_icon"
                            item: "bigreactors:reinforced_reactorcontroller"
                            title: "{mq_localizer.task.controller.title}"
                            type: "item"
                        }
                        {
                            id: "TASK2"
                            item: {Count: 1b, id: "example:widget"}
                            title: '{"translate":"mq_localizer.task.widget.title"}'
                            type: "item"
                        }
                        {
                            entity: "minecraft:zombie"
                            title: "{mq_localizer.task.entity.title}"
                            type: "entity"
                        }
                        {
                            icon: "wrongmod:not_a_resource_hint"
                            title: "{mq_localizer.task.icon_only.title}"
                            type: "checkmark"
                        }
                    ]
                    rewards: [{
                        fluid: {id: "minecraft:water"}
                        title: "{mq_localizer.reward.fluid.title}"
                        type: "fluid"
                    }]
                }]
            }
            ''',
        )

        resources = self._resources_by_key(project)
        self.assertEqual(
            resources["mq_localizer.task.controller.title"],
            ("bigreactors:reinforced_reactorcontroller",),
        )
        self.assertEqual(
            resources["mq_localizer.task.widget.title"],
            ("example:widget",),
        )
        self.assertEqual(
            resources["mq_localizer.task.entity.title"],
            ("minecraft:zombie",),
        )
        self.assertEqual(
            resources["mq_localizer.reward.fluid.title"],
            ("minecraft:water",),
        )
        self.assertEqual(resources["mq_localizer.chapter.context.title"], ())
        self.assertEqual(resources["mq_localizer.quest.context.title"], ())
        self.assertEqual(resources["mq_localizer.task.icon_only.title"], ())

    def test_ambiguous_reuse_and_invalid_or_nested_ids_fail_closed(self) -> None:
        catalog = {
            "mq_localizer.task.ambiguous.title": "Ambiguous",
            "mq_localizer.task.same.title": "Same",
            "mq_localizer.task.without_resource.title": "Mixed",
            "mq_localizer.task.invalid.title": "Invalid",
            "mq_localizer.task.nested.title": "Nested",
            "mq_localizer.task.selector.title": "Selector",
            "mq_localizer.task.valid_path.title": "Valid path",
        }
        project = self._load(
            catalog,
            r'''
            {
                quests: [{
                    tasks: [
                        {item: "first:widget", title: "{mq_localizer.task.ambiguous.title}"}
                        {item: "second:widget", title: "{mq_localizer.task.ambiguous.title}"}
                        {item: "same:widget", title: "{mq_localizer.task.same.title}"}
                        {item: "same:widget", title: "{mq_localizer.task.same.title}"}
                        {item: "mixed:widget", title: "{mq_localizer.task.without_resource.title}"}
                        {title: "{mq_localizer.task.without_resource.title}"}
                        {item: "Invalid:Uppercase", title: "{mq_localizer.task.invalid.title}"}
                        {
                            item: {filter: {id: "nested:must_not_be_used"}}
                            title: "{mq_localizer.task.nested.title}"
                        }
                        {
                            item: {
                                Count: 1
                                id: "itemfilters:tag"
                                tag: {value: "forge:ingots/steel"}
                            }
                            title: "{mq_localizer.task.selector.title}"
                        }
                        {
                            item: "example:path/to.widget"
                            title: "{mq_localizer.task.valid_path.title}"
                        }
                    ]
                }]
            }
            ''',
        )

        resources = self._resources_by_key(project)
        self.assertEqual(resources["mq_localizer.task.ambiguous.title"], ())
        self.assertEqual(resources["mq_localizer.task.same.title"], ("same:widget",))
        self.assertEqual(resources["mq_localizer.task.without_resource.title"], ())
        self.assertEqual(resources["mq_localizer.task.invalid.title"], ())
        self.assertEqual(resources["mq_localizer.task.nested.title"], ())
        self.assertEqual(resources["mq_localizer.task.selector.title"], ())
        self.assertEqual(
            resources["mq_localizer.task.valid_path.title"],
            ("example:path/to.widget",),
        )
        self.assertTrue(any("関連付けを無効" in warning for warning in project.warnings))
        self.assertTrue(any("構造が不正" in warning for warning in project.warnings))

    def test_malformed_quest_snbt_does_not_block_language_catalog_loading(self) -> None:
        project = self._load(
            {
                "mq_localizer.task.broken.title": "Broken",
                "mq_localizer.task.second.title": "Second",
            },
            '''
            {
                quests: [{tasks: [{
                    item: "example:widget"
                    title: "{mq_localizer.task.broken.title}"
            ''',
        )

        self.assertEqual(
            self._resources_by_key(project)["mq_localizer.task.broken.title"],
            (),
        )
        self.assertTrue(any("quest SNBTを解析できない" in warning for warning in project.warnings))

    def test_resource_context_scan_reuses_the_existing_file_size_limit(self) -> None:
        catalog = {
            "mq_localizer.task.large.title": "Large",
            "mq_localizer.task.second.title": "Second",
        }
        with mock.patch.object(
            legacy_json_module,
            "_MAX_QUEST_REFERENCE_FILE_BYTES",
            64,
        ):
            project = self._load(
                catalog,
                '{tasks:[{item:"example:widget",title:"{mq_localizer.task.large.title}"}]}'
                + " " * 128,
            )

        self.assertEqual(
            self._resources_by_key(project)["mq_localizer.task.large.title"],
            (),
        )
        self.assertTrue(any("ファイルサイズが安全上限" in warning for warning in project.warnings))


if __name__ == "__main__":
    unittest.main()
