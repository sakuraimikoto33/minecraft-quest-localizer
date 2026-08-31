from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer import io_utils  # noqa: E402
from mq_localizer.adapters import create_default_registry  # noqa: E402
import mq_localizer.adapters.ftb_split_json5 as split_json5_module  # noqa: E402
import mq_localizer.adapters.ftb_split_snbt as split_snbt_module  # noqa: E402
from mq_localizer.domain import AdapterError  # noqa: E402
from mq_localizer.snbt import dump_lang_snbt, parse_lang_snbt  # noqa: E402


FIXTURES = Path(__file__).with_name("fixtures")
CASES = (
    {
        "fixture": "split_snbt",
        "suffix": ".snbt",
        "adapter_id": "ftb_split_snbt",
        "version": "1.21.1",
        "known_key": "chapter.00000000000000C1.title",
        "data_name": "data.snbt",
        "module": split_snbt_module,
    },
    {
        "fixture": "split_json5",
        "suffix": ".json5",
        "adapter_id": "ftb_split_json5",
        "version": "26.1.2",
        "known_key": "chapter.00000000000000F1.title",
        "data_name": "data.json5",
        "module": split_json5_module,
    },
)


def _snapshot(path: Path) -> dict[str, tuple[str, str]]:
    if not path.exists():
        return {}
    snapshot: dict[str, tuple[str, str]] = {}
    for candidate in sorted(path.rglob("*")):
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_dir():
            snapshot[relative] = ("directory", "")
        elif candidate.is_file():
            snapshot[relative] = (
                "file",
                hashlib.sha256(candidate.read_bytes()).hexdigest(),
            )
    return snapshot


def _quest_paths(instance: Path) -> tuple[Path, Path, Path]:
    quest_root = instance / "config" / "ftbquests" / "quests"
    lang_root = quest_root / "lang"
    return quest_root, lang_root / "en_us", lang_root / "ja_jp"


def _parse_language(path: Path, suffix: str) -> dict[str, str | list[str]]:
    text = path.read_text(encoding="utf-8")
    if suffix == ".snbt":
        return dict(parse_lang_snbt(text))
    return json.loads(text)


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                return
        raise unittest.SkipTest(
            f"directory symlink/junctionを作成できません: {symlink_error}"
        )


def _remove_directory_link(link: Path) -> None:
    try:
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            link.rmdir()
    except OSError:
        pass


class SplitOutputHardeningTests(unittest.TestCase):
    def copy_fixture(self, name: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name) / name
        shutil.copytree(FIXTURES / name, destination)
        return destination

    def test_moved_known_keys_are_canonicalized_without_touching_unknown_only_files(self) -> None:
        for case in CASES:
            with self.subTest(adapter=case["adapter_id"]):
                instance = self.copy_fixture(str(case["fixture"]))
                _quest_root, source_dir, target_dir = _quest_paths(instance)
                suffix = str(case["suffix"])
                known_key = str(case["known_key"])
                empty_key = "custom.empty_source_list"
                canonical_source = source_dir / f"chapter{suffix}"
                canonical_target = target_dir / f"chapter{suffix}"

                if suffix == ".snbt":
                    canonical_source.write_text(
                        "{\n"
                        f'  {known_key}: "&6Main&r"\n'
                        f"  {empty_key}: []\n"
                        "}\n",
                        encoding="utf-8",
                    )
                    canonical_target.write_text("{}\n", encoding="utf-8")
                    moved_text = (
                        "{\n"
                        f'  {known_key}: "&6移動済み訳&r"\n'
                        '  custom.keep: "既存の未知キー"\n'
                        "  custom.empty: []\n"
                        "}\n"
                    )
                    untouched_bytes = (
                        '{\r\n  custom.only: "byte exact"\r\n  custom.empty_only: []\r\n}\r\n'
                    ).encode("utf-8")
                else:
                    canonical_source.write_text(
                        json.dumps(
                            {known_key: "&6Main Chapter&r", empty_key: []},
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    canonical_target.write_text("{}\n", encoding="utf-8")
                    moved_text = json.dumps(
                        {
                            known_key: "&6移動済み訳&r",
                            "custom.keep": "既存の未知キー",
                            "custom.empty": [],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ) + "\n"
                    untouched_bytes = (
                        "{\r\n"
                        "  // This unknown-only file must not be rendered again.\r\n"
                        "  'custom.only': 'byte exact',\r\n"
                        "  'custom.empty_only': [],\r\n"
                        "}\r\n"
                    ).encode("utf-8")

                moved_relative = Path("legacy") / f"moved{suffix}"
                moved_path = target_dir / moved_relative
                moved_path.parent.mkdir(parents=True)
                moved_path.write_text(moved_text, encoding="utf-8")
                untouched_path = target_dir / "custom" / f"unknown_only{suffix}"
                untouched_path.parent.mkdir(parents=True)
                untouched_path.write_bytes(untouched_bytes)

                adapter = create_default_registry().detect(instance, "en_us")
                self.assertEqual(adapter.id, case["adapter_id"])
                project = adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version=str(case["version"]),
                )
                title_unit = next(unit for unit in project.units if unit.key == known_key)
                self.assertEqual(project.existing[title_unit.id], "&6移動済み訳&r")

                safety_module = case["module"]
                with patch.object(
                    safety_module,
                    "validate_split_locale_targets",
                    wraps=safety_module.validate_split_locale_targets,
                ) as validate_targets:
                    adapter.validate_output(project, target_dir)
                validated_relatives = {
                    Path(relative) for relative in validate_targets.call_args_list[-1].args[3]
                }
                self.assertIn(moved_relative, validated_relatives)

                translations = {
                    unit.id: project.existing.get(unit.id, f"訳:{unit.source}")
                    for unit in project.units
                }
                # Passing an explicit selection exercises the partial-write
                # branch. Empty arrays have no selected translation unit, so
                # they must not leak source-only known keys into that catalog.
                selected = frozenset(unit.id for unit in project.units)
                adapter.write(
                    project,
                    translations,
                    target_dir,
                    selected_unit_ids=selected,
                )

                canonical = _parse_language(canonical_target, suffix)
                moved = _parse_language(moved_path, suffix)
                self.assertEqual(canonical[known_key], "&6移動済み訳&r")
                self.assertNotIn(empty_key, canonical)
                self.assertNotIn(known_key, moved)
                self.assertEqual(moved["custom.keep"], "既存の未知キー")
                self.assertEqual(moved["custom.empty"], [])
                self.assertEqual(untouched_path.read_bytes(), untouched_bytes)

                # A complete catalog write retains the source schema's empty
                # array because no category subset is being requested.
                adapter.write(project, translations, target_dir)
                complete = _parse_language(canonical_target, suffix)
                self.assertEqual(complete[empty_key], [])
                self.assertEqual(untouched_path.read_bytes(), untouched_bytes)

    def test_validate_output_rescans_and_rejects_target_duplicates(self) -> None:
        for case in CASES:
            with self.subTest(adapter=case["adapter_id"]):
                instance = self.copy_fixture(str(case["fixture"]))
                _quest_root, _source_dir, target_dir = _quest_paths(instance)
                suffix = str(case["suffix"])
                known_key = str(case["known_key"])
                adapter = create_default_registry().detect(instance, "en_us")
                project = adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version=str(case["version"]),
                )

                duplicate = target_dir / "late" / f"duplicate{suffix}"
                duplicate.parent.mkdir(parents=True)
                if suffix == ".snbt":
                    duplicate.write_text(
                        f'{{\n  {known_key}: "late duplicate"\n}}\n',
                        encoding="utf-8",
                    )
                else:
                    duplicate.write_text(
                        json.dumps({known_key: "late duplicate"}, indent=2) + "\n",
                        encoding="utf-8",
                    )
                before = _snapshot(target_dir)

                with self.assertRaisesRegex(AdapterError, known_key):
                    adapter.validate_output(project, target_dir)

                self.assertEqual(_snapshot(target_dir), before)

    def test_symlinked_instance_is_allowed_but_target_locale_redirect_is_rejected(self) -> None:
        for case in CASES:
            with self.subTest(adapter=case["adapter_id"]):
                instance = self.copy_fixture(str(case["fixture"]))
                instance_alias = instance.parent / f"{instance.name}-alias"
                _make_directory_link(instance_alias, instance)
                self.addCleanup(_remove_directory_link, instance_alias)

                adapter = create_default_registry().detect(instance_alias, "en_us")
                project = adapter.load(
                    instance_alias,
                    "en_us",
                    "ja_jp",
                    minecraft_version=str(case["version"]),
                )
                adapter.validate_output(project, project.default_output)

                target_dir = project.default_output
                redirected = instance.parent / f"{instance.name}-redirected-target"
                target_dir.rename(redirected)
                _make_directory_link(target_dir, redirected)
                self.addCleanup(_remove_directory_link, target_dir)

                with self.assertRaisesRegex(AdapterError, "quest外.*symlink|quest外.*junction"):
                    adapter.validate_output(project, target_dir)

    def test_nested_source_reparse_point_is_rejected_before_loading(self) -> None:
        for case in CASES:
            with self.subTest(adapter=case["adapter_id"]):
                instance = self.copy_fixture(str(case["fixture"]))
                _quest_root, source_dir, _target_dir = _quest_paths(instance)
                linked_source = source_dir / "chapters"
                external_source = instance.parent / f"{instance.name}-external-chapters"
                linked_source.rename(external_source)
                _make_directory_link(linked_source, external_source)
                self.addCleanup(_remove_directory_link, linked_source)

                adapter = create_default_registry().detect(
                    instance,
                    "en_us",
                    str(case["version"]),
                )
                with self.assertRaisesRegex(
                    AdapterError,
                    "原文.*symlink|原文.*junction",
                ):
                    adapter.load(
                        instance,
                        "en_us",
                        "ja_jp",
                        minecraft_version=str(case["version"]),
                    )

    def test_target_change_between_plan_and_multiwrite_is_preserved(self) -> None:
        for case in CASES:
            with self.subTest(adapter=case["adapter_id"]):
                instance = self.copy_fixture(str(case["fixture"]))
                _quest_root, _source_dir, target_dir = _quest_paths(instance)
                suffix = str(case["suffix"])
                target = target_dir / f"chapter{suffix}"
                before_values = _parse_language(target, suffix)
                before_values["custom.concurrent"] = "before"
                if suffix == ".snbt":
                    target.write_text(
                        dump_lang_snbt(before_values),
                        encoding="utf-8",
                    )
                else:
                    target.write_text(
                        json.dumps(before_values, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )

                adapter = create_default_registry().detect(
                    instance,
                    "en_us",
                    str(case["version"]),
                )
                project = adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version=str(case["version"]),
                )
                translations = {unit.id: f"更新:{unit.source}" for unit in project.units}
                safety_module = case["module"]
                real_atomic_many = io_utils.atomic_write_many_text

                def edit_target_then_commit(*args: object, **kwargs: object) -> None:
                    current = _parse_language(target, suffix)
                    current["custom.concurrent"] = "external edit"
                    if suffix == ".snbt":
                        target.write_text(dump_lang_snbt(current), encoding="utf-8")
                    else:
                        target.write_text(
                            json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    real_atomic_many(*args, **kwargs)

                with patch.object(
                    safety_module,
                    "atomic_write_many_text",
                    side_effect=edit_target_then_commit,
                ):
                    with self.assertRaisesRegex(AdapterError, "確認後に変更"):
                        adapter.write(project, translations, target_dir)

                self.assertEqual(
                    _parse_language(target, suffix)["custom.concurrent"],
                    "external edit",
                )

    def test_multi_file_write_rolls_back_existing_and_new_outputs(self) -> None:
        for case in CASES:
            with self.subTest(adapter=case["adapter_id"]):
                instance = self.copy_fixture(str(case["fixture"]))
                adapter = create_default_registry().detect(instance, "en_us")
                project = adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version=str(case["version"]),
                )
                translations = {unit.id: f"更新:{unit.source}" for unit in project.units}
                real_atomic_write = io_utils.atomic_write_text

                for output, initially_exists in (
                    (project.default_output, True),
                    (instance / "brand-new-output", False),
                ):
                    with self.subTest(
                        adapter=case["adapter_id"], initially_exists=initially_exists
                    ):
                        before = _snapshot(output)
                        calls = 0

                        def fail_second_write(
                            path: Path,
                            text: str,
                            encoding: str = "utf-8",
                            newline: str | None = None,
                        ) -> None:
                            nonlocal calls
                            calls += 1
                            if calls == 2:
                                raise OSError("injected second-file failure")
                            return real_atomic_write(
                                path,
                                text,
                                encoding=encoding,
                                newline=newline,
                            )

                        with patch.object(
                            io_utils,
                            "atomic_write_text",
                            side_effect=fail_second_write,
                        ):
                            with self.assertRaisesRegex(OSError, "injected"):
                                adapter.write(project, translations, output)

                        self.assertEqual(calls, 2)
                        self.assertEqual(_snapshot(output), before)
                        self.assertEqual(output.exists(), initially_exists)

    def test_lang_directory_and_fallback_file_selection_use_configured_locale(self) -> None:
        for case in CASES:
            with self.subTest(adapter=case["adapter_id"]):
                instance = self.copy_fixture(str(case["fixture"]))
                quest_root, source_dir, _target_dir = _quest_paths(instance)
                lang_root = source_dir.parent
                fallback_dir = lang_root / "de_de"
                source_dir.rename(fallback_dir)
                (quest_root / str(case["data_name"])).write_text(
                    '{fallback_locale: "de_de"}\n',
                    encoding="utf-8",
                )

                adapter = create_default_registry().detect(lang_root, "en_us")
                self.assertEqual(adapter.id, case["adapter_id"])
                project = adapter.load(
                    lang_root,
                    "en_us",
                    "ja_jp",
                    minecraft_version=str(case["version"]),
                )
                self.assertEqual(project.source_locale, "de_de")
                self.assertEqual(project.source_path, fallback_dir)
                self.assertTrue(any("fallback_locale" in item for item in project.warnings))

                selected_file = fallback_dir / f"chapter{case['suffix']}"
                file_adapter = create_default_registry().detect(selected_file, "en_us")
                self.assertEqual(file_adapter.id, case["adapter_id"])
                file_project = file_adapter.load(
                    selected_file,
                    "en_us",
                    "ja_jp",
                    minecraft_version=str(case["version"]),
                )
                self.assertEqual(file_project.source_locale, "de_de")
                self.assertEqual(file_project.source_path, fallback_dir)


if __name__ == "__main__":
    unittest.main()
