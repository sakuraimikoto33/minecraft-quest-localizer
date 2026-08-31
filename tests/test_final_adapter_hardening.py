from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mq_localizer.adapters.ftb_legacy_raw as legacy_raw_module  # noqa: E402
import mq_localizer.adapters.ftb_legacy_json as legacy_json_module  # noqa: E402
import mq_localizer.adapters.ftb_modern as modern_module  # noqa: E402
import mq_localizer.adapters.ftb_split_json5 as split_json5_module  # noqa: E402
import mq_localizer.adapters.ftb_split_snbt as split_snbt_module  # noqa: E402
from mq_localizer.adapters import create_default_registry  # noqa: E402
from mq_localizer.application import LocalizerApplication  # noqa: E402
from mq_localizer.domain import AdapterError  # noqa: E402
from mq_localizer.glossary import GlossaryCatalog  # noqa: E402
from mq_localizer.snbt import parse_lang_snbt  # noqa: E402
from mq_localizer.translator import TranslationOptions, TranslationService  # noqa: E402


FIXTURES = Path(__file__).with_name("fixtures")

_PROTECTED_TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")


def _prefix_inside_protected_segment(text: str) -> str:
    """Simulate translation without moving prose across layout placeholders."""
    matches = list(_PROTECTED_TOKEN.finditer(text))
    if not matches:
        return "訳:" + text
    cursor = 0
    for match in (*matches, None):
        end = match.start() if match is not None else len(text)
        for index in range(cursor, end):
            if text[index].isalnum():
                return text[:index] + "訳:" + text[index:]
        if match is not None:
            cursor = match.end()
    return text


class _PrefixClient:
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
        self.calls.append(items)
        return {
            item["id"]: _prefix_inside_protected_segment(item["text"])
            for item in items
        }


def _snapshot(path: Path) -> dict[str, tuple[str, str]]:
    if not path.exists():
        return {}
    if path.is_file():
        return {".": ("file", hashlib.sha256(path.read_bytes()).hexdigest())}
    result: dict[str, tuple[str, str]] = {}
    for candidate in sorted(path.rglob("*")):
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_symlink():
            result[relative] = ("symlink", os.readlink(candidate))
        elif candidate.is_dir():
            result[relative] = ("directory", "")
        elif candidate.is_file():
            result[relative] = (
                "file",
                hashlib.sha256(candidate.read_bytes()).hexdigest(),
            )
    return result


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                return
        raise unittest.SkipTest(f"directory symlink/junctionを作成できません: {symlink_error}")


def _remove_directory_link(link: Path) -> None:
    try:
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            link.rmdir()
    except OSError:
        pass


class FinalAdapterHardeningTests(unittest.TestCase):
    def copy_fixture(self, name: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        destination = Path(temporary.name) / name
        shutil.copytree(FIXTURES / name, destination)
        return destination

    def test_legacy_raw_rejects_nested_output_reparse_point_before_api(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        quest_root = instance / "config" / "ftbquests" / "quests"
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        external = instance / "external-destination"
        external.mkdir()
        sentinel = external / "sentinel.bin"
        sentinel.write_bytes(b"must-not-change")
        link = output / "assets"
        link.parent.mkdir(parents=True)
        _make_directory_link(link, external)
        self.addCleanup(_remove_directory_link, link)

        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        source_before = _snapshot(quest_root)

        with self.assertRaisesRegex(AdapterError, "symlink|junction"):
            adapter.load(
                instance,
                "en_us",
                "ja_jp",
                minecraft_version="1.20.1",
            )

        self.assertEqual(_snapshot(quest_root), source_before)
        self.assertEqual(sentinel.read_bytes(), b"must-not-change")

    def test_legacy_raw_rejects_source_reparse_point_before_recursive_copy(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        quest_root = instance / "config" / "ftbquests" / "quests"
        external = instance / "external-source"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("outside\n", encoding="utf-8")
        link = quest_root / "chapters" / "linked"
        _make_directory_link(link, external)
        self.addCleanup(_remove_directory_link, link)

        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        with self.assertRaisesRegex(AdapterError, "原文 quest.*symlink|原文 quest.*junction"):
            adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")

    def test_raw_probe_backup_safety_error_does_not_hide_valid_split_locale(self) -> None:
        split_instance = self.copy_fixture("split_snbt")
        split_quests = split_instance / "config" / "ftbquests" / "quests"
        split_backup = split_quests.with_name("quests.bak")
        shutil.copytree(split_quests, split_backup)
        split_external = split_instance / "external-backup"
        split_external.mkdir()
        split_link = split_backup / "linked"
        _make_directory_link(split_link, split_external)
        self.addCleanup(_remove_directory_link, split_link)

        self.assertEqual(
            create_default_registry().detect(
                split_instance,
                "en_us",
                "1.21.1",
            ).id,
            "ftb_split_snbt",
        )

    def test_unrelated_kubejs_link_does_not_block_raw_output(self) -> None:
        raw_instance = self.copy_fixture("legacy_raw")
        raw_kubejs = raw_instance / "kubejs"
        raw_kubejs.mkdir()
        raw_external = raw_instance / "external-kubejs"
        raw_external.mkdir()
        sentinel = raw_external / "sentinel.txt"
        sentinel.write_text("outside\n", encoding="utf-8")
        raw_link = raw_kubejs / "linked"
        _make_directory_link(raw_link, raw_external)
        self.addCleanup(_remove_directory_link, raw_link)

        adapter = create_default_registry().detect(
            raw_instance,
            "en_us",
            "1.20.1",
        )
        self.assertEqual(adapter.id, "ftb_legacy_raw")
        project = adapter.load(
            raw_instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        adapter.write(
            project,
            {unit.id: "訳:" + unit.source for unit in project.units},
            project.default_output,
        )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")
        self.assertTrue(
            (raw_kubejs / "assets" / "mq_localizer" / "lang" / "ja_jp.json").is_file()
        )
        self.assertFalse((raw_instance / "resourcepacks").exists())

    def test_kubejs_output_path_reparse_is_rejected_before_catalog_read(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        kubejs = instance / "kubejs"
        kubejs.mkdir()
        external = instance / "external-assets"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("outside\n", encoding="utf-8")
        assets = kubejs / "assets"
        _make_directory_link(assets, external)
        self.addCleanup(_remove_directory_link, assets)

        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        with self.assertRaisesRegex(AdapterError, "symlink|junction"):
            adapter.load(
                instance,
                "en_us",
                "ja_jp",
                minecraft_version="1.20.1",
            )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")

    def test_legacy_raw_rejects_every_output_ancestor_of_source(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        quest_root = instance / "config" / "ftbquests" / "quests"
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        before = _snapshot(instance)

        for output in (instance, instance / "config", quest_root.parent):
            with self.subTest(output=output), self.assertRaisesRegex(AdapterError, "変更できません"):
                adapter.validate_output(project, output)

        self.assertEqual(_snapshot(instance), before)

    def test_explicit_legacy_raw_adapter_rejects_modern_minecraft_versions(self) -> None:
        for version in ("1.21.1", "26.1.2"):
            with self.subTest(version=version):
                instance = self.copy_fixture("legacy_raw")
                adapter = create_default_registry().get("ftb_legacy_raw")
                before = _snapshot(instance)

                with self.assertRaisesRegex(AdapterError, "1.21以降.*使用できません"):
                    adapter.load(
                        instance,
                        "en_us",
                        "ja_jp",
                        minecraft_version=version,
                    )

                self.assertEqual(_snapshot(instance), before)

    def test_legacy_raw_rejects_unknown_version_at_registry_and_adapter_boundaries(self) -> None:
        for adapter_id in ("auto", "ftb_legacy_raw"):
            for version in ("", "unknown", "0.0"):
                with self.subTest(adapter_id=adapter_id, version=version):
                    instance = self.copy_fixture("legacy_raw")
                    before = _snapshot(instance)

                    with self.assertRaisesRegex(
                        AdapterError,
                        "Minecraftバージョン.*旧版raw SNBT",
                    ):
                        LocalizerApplication().analyze(
                            source_path=instance,
                            adapter_id=adapter_id,
                            source_locale="en_us",
                            target_locale="ja_jp",
                            minecraft_version=version,
                        )

                    self.assertEqual(_snapshot(instance), before)

        with self.subTest(adapter_id="direct-default"):
            instance = self.copy_fixture("legacy_raw")
            before = _snapshot(instance)
            with self.assertRaisesRegex(
                AdapterError,
                "Minecraftバージョン.*旧版raw SNBT",
            ):
                create_default_registry().get("ftb_legacy_raw").load(
                    instance,
                    "en_us",
                    "ja_jp",
                )
            self.assertEqual(_snapshot(instance), before)

    def test_legacy_raw_stage_and_swap_failures_leave_existing_and_new_output_unchanged(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        quest_root = config_root / "quests"
        extra_source = quest_root / "custom" / "payload.bin"
        extra_source.parent.mkdir()
        extra_source.write_bytes(b"copy every source file")
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        retained = output / "custom-user-file.bin"
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_bytes(b"retain existing resourcepack content")
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        translations = {unit.id: "訳:" + unit.source for unit in project.units}
        self.assertEqual(project.default_output, output)
        config_before = _snapshot(config_root)
        output_before = _snapshot(output)

        real_atomic_write = legacy_raw_module.atomic_write_text
        calls = 0

        def fail_second_stage_write(
            path: Path,
            text: str,
            encoding: str = "utf-8",
            newline: str | None = None,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected stage write failure")
            real_atomic_write(path, text, encoding=encoding, newline=newline)

        with patch.object(
            legacy_raw_module,
            "atomic_write_text",
            side_effect=fail_second_stage_write,
        ):
            with self.assertRaisesRegex(OSError, "injected"):
                adapter.write(project, translations, output)

        self.assertEqual(_snapshot(config_root), config_before)
        self.assertEqual(_snapshot(output), output_before)
        self.assertFalse((config_root / "quests.bak").exists())

        real_replace = legacy_raw_module._replace_path
        failed = False

        def fail_quest_directory_install(source: Path, destination: Path) -> None:
            nonlocal failed
            if (
                not failed
                and destination == quest_root
                and source.name.startswith(".quests.stage-")
            ):
                failed = True
                raise OSError("injected directory swap failure")
            real_replace(source, destination)

        with patch.object(
            legacy_raw_module,
            "_replace_path",
            side_effect=fail_quest_directory_install,
        ):
            with self.assertRaisesRegex(AdapterError, "injected"):
                adapter.write(project, translations, output)

        self.assertTrue(failed)
        self.assertEqual(_snapshot(config_root), config_before)
        self.assertEqual(_snapshot(output), output_before)
        self.assertEqual(list(output.parent.glob(f".{output.name}.backup-*")), [])
        self.assertEqual(list(output.parent.glob(f".{output.name}.stage-*")), [])
        self.assertEqual(list(config_root.glob(".quests.*-*")), [])
        self.assertEqual(list(config_root.parent.glob(".quests.stage-*")), [])

    def test_legacy_raw_second_stage_creation_failure_removes_first_stage_and_empty_parents(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        translations = {unit.id: "訳:" + unit.source for unit in project.units}
        before = _snapshot(instance)
        real_mkdtemp = legacy_raw_module.tempfile.mkdtemp
        calls = 0

        def fail_second_stage(*args: object, **kwargs: object) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second stage creation failure")
            return real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(
            legacy_raw_module.tempfile,
            "mkdtemp",
            side_effect=fail_second_stage,
        ):
            with self.assertRaisesRegex(OSError, "second stage creation"):
                adapter.write(project, translations, project.default_output)

        self.assertEqual(_snapshot(instance), before)
        self.assertEqual(list(config_root.parent.glob(".quests.stage-*")), [])
        self.assertFalse((instance / "resourcepacks").exists())

    def test_legacy_raw_renames_only_live_quests_and_preserves_parent_identity(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        quest_root = config_root / "quests"
        backup_root = config_root / "quests.bak"
        parent_identity = config_root.stat().st_ino
        quest_identity = quest_root.stat().st_ino
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        real_replace = legacy_raw_module._replace_path
        moves: list[tuple[Path, Path]] = []

        def reject_parent_swap(source: Path, destination: Path) -> None:
            if source == config_root or destination == config_root:
                raise AssertionError("config/ftbquests must never be renamed")
            moves.append((source, destination))
            real_replace(source, destination)

        with patch.object(
            legacy_raw_module,
            "_replace_path",
            side_effect=reject_parent_swap,
        ):
            adapter.write(
                project,
                {unit.id: "訳:" + unit.source for unit in project.units},
                project.default_output,
            )

        self.assertIn((quest_root, backup_root), moves)
        if parent_identity:
            self.assertEqual(config_root.stat().st_ino, parent_identity)
        if quest_identity:
            self.assertEqual(backup_root.stat().st_ino, quest_identity)
        self.assertTrue(quest_root.is_dir())

    def test_legacy_raw_quest_lock_is_actionable_and_rolls_back_language_output(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        quest_root = config_root / "quests"
        backup_root = config_root / "quests.bak"
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        before = _snapshot(config_root)
        real_replace = legacy_raw_module._replace_path

        def deny_live_quest_rename(source: Path, destination: Path) -> None:
            if source == quest_root and destination == backup_root:
                raise PermissionError(13, "injected access denied", str(source))
            real_replace(source, destination)

        with patch.object(
            legacy_raw_module,
            "_replace_path",
            side_effect=deny_live_quest_rename,
        ):
            with self.assertRaisesRegex(
                AdapterError,
                "Minecraftを完全に終了.*再解析して再試行",
            ):
                adapter.write(
                    project,
                    {unit.id: "訳:" + unit.source for unit in project.units},
                    output,
                )

        self.assertEqual(_snapshot(config_root), before)
        self.assertFalse(backup_root.exists())
        self.assertFalse(output.exists())
        self.assertEqual(list(config_root.glob(".quests.*-*")), [])
        self.assertEqual(list(config_root.parent.glob(".quests.stage-*")), [])

    def test_legacy_raw_recovers_when_quest_rename_raises_after_moving(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        quest_root = config_root / "quests"
        backup_root = config_root / "quests.bak"
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        before = _snapshot(config_root)
        real_replace = legacy_raw_module._replace_path
        injected = False

        def raise_after_original_move(source: Path, destination: Path) -> None:
            nonlocal injected
            if not injected and source == quest_root and destination == backup_root:
                injected = True
                real_replace(source, destination)
                raise OSError("injected after quest rename")
            real_replace(source, destination)

        with patch.object(
            legacy_raw_module,
            "_replace_path",
            side_effect=raise_after_original_move,
        ):
            with self.assertRaisesRegex(AdapterError, "injected after quest rename"):
                adapter.write(
                    project,
                    {unit.id: "訳:" + unit.source for unit in project.units},
                    output,
                )

        self.assertTrue(injected)
        self.assertEqual(_snapshot(config_root), before)
        self.assertFalse(output.exists())
        self.assertEqual(list(config_root.glob(".quests.*-*")), [])

    def test_legacy_raw_recovers_when_asset_backup_rename_raises_after_moving(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        retained = output / "retained.txt"
        retained.parent.mkdir(parents=True)
        retained.write_text("existing\n", encoding="utf-8")
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        config_before = _snapshot(config_root)
        output_before = _snapshot(output)
        real_replace = legacy_raw_module._replace_path
        injected = False

        def raise_after_asset_backup_move(source: Path, destination: Path) -> None:
            nonlocal injected
            if (
                not injected
                and source == output
                and destination.name.startswith(f".{output.name}.backup-")
            ):
                injected = True
                real_replace(source, destination)
                raise OSError("injected after asset rename")
            real_replace(source, destination)

        with patch.object(
            legacy_raw_module,
            "_replace_path",
            side_effect=raise_after_asset_backup_move,
        ):
            with self.assertRaisesRegex(OSError, "injected after asset rename"):
                adapter.write(
                    project,
                    {unit.id: "訳:" + unit.source for unit in project.units},
                    output,
                )

        self.assertTrue(injected)
        self.assertEqual(_snapshot(config_root), config_before)
        self.assertEqual(_snapshot(output), output_before)
        self.assertEqual(list(output.parent.glob(f".{output.name}.*-*")), [])

    def test_legacy_raw_preserves_quest_edit_made_after_rollback_precheck(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        quest_root = config_root / "quests"
        backup_root = config_root / "quests.bak"
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        original = _snapshot(quest_root)
        real_replace = legacy_raw_module._replace_path
        real_matches = legacy_raw_module._relocated_snapshot_matches
        install_failed = False
        edit_injected = False

        def fail_after_staged_install(source: Path, destination: Path) -> None:
            nonlocal install_failed
            if (
                not install_failed
                and destination == quest_root
                and source.name.startswith(".quests.stage-")
            ):
                install_failed = True
                real_replace(source, destination)
                raise OSError("injected after staged quest install")
            real_replace(source, destination)

        def edit_after_rollback_precheck(expected: object, path: Path) -> bool:
            nonlocal edit_injected
            matched = real_matches(expected, path)  # type: ignore[arg-type]
            expected_path = getattr(expected, "path", Path())
            if (
                install_failed
                and not edit_injected
                and path == quest_root
                and expected_path.name.startswith(".quests.stage-")
            ):
                edit_injected = True
                (quest_root / "external-after-check.txt").write_text(
                    "preserve me\n",
                    encoding="utf-8",
                )
            return matched

        with (
            patch.object(
                legacy_raw_module,
                "_replace_path",
                side_effect=fail_after_staged_install,
            ),
            patch.object(
                legacy_raw_module,
                "_relocated_snapshot_matches",
                side_effect=edit_after_rollback_precheck,
            ),
        ):
            with self.assertRaisesRegex(AdapterError, "別名で保持|自動復元"):
                adapter.write(
                    project,
                    {unit.id: "訳:" + unit.source for unit in project.units},
                    output,
                )

        recoveries = list(config_root.glob(".quests.recovery-*"))
        self.assertTrue(install_failed)
        self.assertTrue(edit_injected)
        self.assertEqual(_snapshot(quest_root), original)
        self.assertFalse(backup_root.exists())
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(
            (recoveries[0] / "external-after-check.txt").read_text(encoding="utf-8"),
            "preserve me\n",
        )
        self.assertFalse(output.exists())

    def test_legacy_raw_preserves_asset_edit_made_while_quests_activate(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        quest_root = config_root / "quests"
        backup_root = config_root / "quests.bak"
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        quest_before = _snapshot(quest_root)
        real_replace = legacy_raw_module._replace_path
        edited = False

        def edit_asset_before_quest_move(source: Path, destination: Path) -> None:
            nonlocal edited
            if not edited and source == quest_root and destination == backup_root:
                edited = True
                (output / "external-after-asset-commit.txt").write_text(
                    "preserve me\n",
                    encoding="utf-8",
                )
            real_replace(source, destination)

        with patch.object(
            legacy_raw_module,
            "_replace_path",
            side_effect=edit_asset_before_quest_move,
        ):
            with self.assertRaisesRegex(AdapterError, "完全には復元"):
                adapter.write(
                    project,
                    {unit.id: "訳:" + unit.source for unit in project.units},
                    output,
                )

        recoveries = list(output.parent.glob(f".{output.name}.recovery-*"))
        self.assertTrue(edited)
        self.assertEqual(_snapshot(quest_root), quest_before)
        self.assertFalse(backup_root.exists())
        self.assertFalse(output.exists())
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(
            (recoveries[0] / "external-after-asset-commit.txt").read_text(
                encoding="utf-8"
            ),
            "preserve me\n",
        )

    def test_legacy_raw_failed_rollback_keeps_original_and_explains_lock(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        quest_root = config_root / "quests"
        backup_root = config_root / "quests.bak"
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        original = _snapshot(quest_root)
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        real_replace = legacy_raw_module._replace_path

        def lock_install_and_restore(source: Path, destination: Path) -> None:
            if destination == quest_root and (
                source.name.startswith(".quests.stage-") or source == backup_root
            ):
                raise PermissionError(13, "injected locked quest folder", str(source))
            real_replace(source, destination)

        with patch.object(
            legacy_raw_module,
            "_replace_path",
            side_effect=lock_install_and_restore,
        ):
            with self.assertRaisesRegex(
                AdapterError,
                "(?s)自動復元も完了できませんでした.*Minecraftを完全に終了",
            ):
                adapter.write(
                    project,
                    {unit.id: "訳:" + unit.source for unit in project.units},
                    output,
                )

        self.assertFalse(quest_root.exists())
        self.assertEqual(_snapshot(backup_root), original)
        self.assertFalse(output.exists())

    def test_legacy_raw_managed_rerun_does_not_rename_quest_directories(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        quest_root = config_root / "quests"
        backup_root = config_root / "quests.bak"
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        adapter.write(
            project,
            {unit.id: unit.source for unit in project.units},
            project.default_output,
        )
        quest_before = _snapshot(quest_root)
        backup_before = _snapshot(backup_root)
        rerun_adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        rerun = rerun_adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        real_replace = legacy_raw_module._replace_path

        def reject_quest_rename(source: Path, destination: Path) -> None:
            if source.parent == config_root or destination.parent == config_root:
                raise AssertionError("managed rerun must retain quests and quests.bak")
            real_replace(source, destination)

        with patch.object(
            legacy_raw_module,
            "_replace_path",
            side_effect=reject_quest_rename,
        ):
            rerun_adapter.write(
                rerun,
                {unit.id: "再:" + unit.source for unit in rerun.units},
                rerun.default_output,
            )

        self.assertEqual(_snapshot(quest_root), quest_before)
        self.assertEqual(_snapshot(backup_root), backup_before)

    def test_legacy_raw_recovers_missing_active_quests_from_permanent_backup(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        quest_root = config_root / "quests"
        backup_root = config_root / "quests.bak"
        quest_root.rename(backup_root)
        backup_before = _snapshot(backup_root)
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")

        self.assertEqual(project.metadata["legacy_source_kind"], "backup")
        adapter.write(
            project,
            {unit.id: "訳:" + unit.source for unit in project.units},
            project.default_output,
        )

        self.assertEqual(_snapshot(backup_root), backup_before)
        self.assertTrue(quest_root.is_dir())
        self.assertNotEqual(_snapshot(quest_root), backup_before)
        self.assertEqual(list(config_root.glob(".quests.*-*")), [])
        self.assertEqual(list(config_root.parent.glob(".quests.stage-*")), [])

    def test_legacy_raw_preserves_unrelated_config_edit_during_staging(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        config_root = instance / "config" / "ftbquests"
        unrelated = config_root / "ftbquests.snbt"
        unrelated.write_text("before\n", encoding="utf-8")
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        real_atomic_write = legacy_raw_module.atomic_write_text
        changed = False

        def edit_unrelated_config(
            path: Path,
            text: str,
            encoding: str = "utf-8",
            newline: str | None = None,
        ) -> None:
            nonlocal changed
            real_atomic_write(path, text, encoding=encoding, newline=newline)
            if not changed:
                changed = True
                unrelated.write_text("changed externally\n", encoding="utf-8")

        with patch.object(
            legacy_raw_module,
            "atomic_write_text",
            side_effect=edit_unrelated_config,
        ):
            adapter.write(
                project,
                {unit.id: "訳:" + unit.source for unit in project.units},
                project.default_output,
            )

        self.assertTrue(changed)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "changed externally\n")

    def test_legacy_raw_rejects_output_change_during_staging(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        sentinel = output / "user-note.txt"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("confirmed\n", encoding="utf-8")
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        translations = {unit.id: "訳:" + unit.source for unit in project.units}
        self.assertEqual(project.default_output, output)

        real_atomic_write = legacy_raw_module.atomic_write_text
        changed = False

        def change_output_during_stage(
            path: Path,
            text: str,
            encoding: str = "utf-8",
            newline: str | None = None,
        ) -> None:
            nonlocal changed
            real_atomic_write(path, text, encoding=encoding, newline=newline)
            if not changed:
                changed = True
                sentinel.write_text("changed externally\n", encoding="utf-8")

        with patch.object(
            legacy_raw_module,
            "atomic_write_text",
            side_effect=change_output_during_stage,
        ):
            with self.assertRaisesRegex(AdapterError, "確認後に変更"):
                adapter.write(project, translations, output)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "changed externally\n")
        self.assertEqual(list(output.parent.glob(f".{output.name}.stage-*")), [])
        self.assertEqual(list(output.parent.glob(f".{output.name}.backup-*")), [])

    def test_legacy_raw_preserves_edit_after_final_check_before_swap(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        sentinel = output / "user-note.txt"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("confirmed\n", encoding="utf-8")
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        translations = {unit.id: "訳:" + unit.source for unit in project.units}
        self.assertEqual(project.default_output, output)
        real_assert = legacy_raw_module.assert_path_unchanged
        changed = False

        def edit_immediately_after_final_check(snapshot: object) -> None:
            nonlocal changed
            real_assert(snapshot)
            if not changed:
                changed = True
                sentinel.write_text("external edit after check\n", encoding="utf-8")

        with patch.object(
            legacy_raw_module,
            "assert_path_unchanged",
            side_effect=edit_immediately_after_final_check,
        ):
            with self.assertRaisesRegex(AdapterError, "確認後に変更"):
                adapter.write(project, translations, output)

        self.assertEqual(
            sentinel.read_text(encoding="utf-8"),
            "external edit after check\n",
        )
        self.assertEqual(list(output.parent.glob(f".{output.name}.stage-*")), [])
        self.assertEqual(list(output.parent.glob(f".{output.name}.backup-*")), [])

    def test_legacy_raw_rejects_source_change_during_staging(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        translations = {unit.id: "訳:" + unit.source for unit in project.units}
        output = project.default_output
        output_before = _snapshot(output)
        source = (
            instance
            / "config"
            / "ftbquests"
            / "quests"
            / "chapters"
            / "welcome.snbt"
        )
        original = source.read_text(encoding="utf-8")

        real_atomic_write = legacy_raw_module.atomic_write_text
        changed = False

        def change_source_during_stage(
            path: Path,
            text: str,
            encoding: str = "utf-8",
            newline: str | None = None,
        ) -> None:
            nonlocal changed
            real_atomic_write(path, text, encoding=encoding, newline=newline)
            if not changed:
                changed = True
                source.write_text(original + "\n# changed externally\n", encoding="utf-8")

        with patch.object(
            legacy_raw_module,
            "atomic_write_text",
            side_effect=change_source_during_stage,
        ):
            with self.assertRaisesRegex(AdapterError, "翻訳元が解析後に変更"):
                adapter.write(project, translations, output)

        self.assertTrue(source.read_text(encoding="utf-8").endswith("# changed externally\n"))
        self.assertEqual(_snapshot(output), output_before)
        self.assertEqual(list(output.parent.glob(f".{output.name}.stage-*")), [])

    def test_legacy_raw_rejects_source_change_after_analysis(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.20.1")
        output_before = _snapshot(project.default_output)
        source = (
            instance
            / "config"
            / "ftbquests"
            / "quests"
            / "chapters"
            / "welcome.snbt"
        )
        source.write_text(
            source.read_text(encoding="utf-8") + "\n# changed after analysis\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(AdapterError, "翻訳元が解析後に変更"):
            adapter.write(
                project,
                {unit.id: "訳:" + unit.source for unit in project.units},
                project.default_output,
            )

        self.assertEqual(_snapshot(project.default_output), output_before)

    def test_unselected_existing_lists_are_removed_from_target_locale(self) -> None:
        cases = (
            ("native_snbt", "1.21.1", "quest.00000000000000A1.quest_desc"),
            ("split_snbt", "1.21.1", "quest.00000000000000D2.quest_desc"),
            ("split_json5", "26.1.2", "quest.00000000000000F2.quest_desc"),
        )
        for fixture, version, description_key in cases:
            with self.subTest(fixture=fixture):
                instance = self.copy_fixture(fixture)
                adapter = create_default_registry().detect(instance, "en_us", version)
                project = adapter.load(instance, "en_us", "ja_jp", minecraft_version=version)
                source_before = _snapshot(project.source_path)
                client = _PrefixClient()

                TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(
                        preserve_existing=False,
                        selected_categories=frozenset({"task_title"}),
                    ),
                )

                self.assertEqual(len(client.calls), 1)
                if fixture == "native_snbt":
                    rendered = parse_lang_snbt(
                        project.default_output.read_text(encoding="utf-8")
                    )
                else:
                    suffix = ".snbt" if fixture == "split_snbt" else ".json5"
                    target = project.default_output / "chapters" / f"welcome{suffix}"
                    rendered = (
                        parse_lang_snbt(target.read_text(encoding="utf-8"))
                        if suffix == ".snbt"
                        else json.loads(target.read_text(encoding="utf-8"))
                    )
                self.assertNotIn(description_key, rendered)
                self.assertTrue(any(key.startswith("task.") for key in rendered))
                self.assertEqual(_snapshot(project.source_path), source_before)

    def test_single_catalog_and_legacy_bundle_preserve_existing_unknown_keys(self) -> None:
        native = self.copy_fixture("native_snbt")
        native_adapter = create_default_registry().detect(native, "en_us", "1.21.1")
        native_project = native_adapter.load(
            native,
            "en_us",
            "ja_jp",
            minecraft_version="1.21.1",
        )
        native_project.default_output.write_text(
            '{\n  custom.manual: "手動訳"\n  custom.lines: ["一", "二"]\n}\n',
            encoding="utf-8",
        )
        native_adapter.write(
            native_project,
            {unit.id: "訳:" + unit.source for unit in native_project.units},
            native_project.default_output,
        )
        native_values = parse_lang_snbt(
            native_project.default_output.read_text(encoding="utf-8")
        )
        self.assertEqual(native_values["custom.manual"], "手動訳")
        self.assertEqual(native_values["custom.lines"], ["一", "二"])

        legacy_json = self.copy_fixture("legacy_json")
        json_adapter = create_default_registry().detect(legacy_json, "en_us", "1.20.1")
        json_project = json_adapter.load(
            legacy_json,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        json_project.default_output.write_text(
            json.dumps(
                {
                    "custom.target.only": "手動訳",
                    "mq_localizer.file.obsolete.title": "古い自動生成訳",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        json_adapter.write(
            json_project,
            {unit.id: "訳:" + unit.source for unit in json_project.units},
            json_project.default_output,
        )
        json_values = json.loads(json_project.default_output.read_text(encoding="utf-8"))
        self.assertEqual(json_values["custom.target.only"], "手動訳")
        self.assertNotIn("mq_localizer.file.obsolete.title", json_values)

        legacy_raw = self.copy_fixture("legacy_raw")
        raw_lang = (
            legacy_raw
            / "resourcepacks"
            / "mq_localizer_ja_jp"
            / "assets"
            / "minecraft"
            / "lang"
        )
        raw_lang.mkdir(parents=True, exist_ok=True)
        (raw_lang / "ja_jp.json").write_text(
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
        (raw_lang / "en_us.json").write_text(
            json.dumps(
                {
                    "custom.source.only": "Manual source",
                    "mq_localizer.quest.obsolete.title": "Obsolete generated source",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        raw_adapter = create_default_registry().detect(legacy_raw, "en_us", "1.20.1")
        raw_project = raw_adapter.load(
            legacy_raw,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        self.assertEqual(raw_project.default_output, raw_lang.parents[2])
        raw_adapter.write(
            raw_project,
            {unit.id: "訳:" + unit.source for unit in raw_project.units},
            raw_project.default_output,
        )
        raw_target_values = json.loads((raw_lang / "ja_jp.json").read_text(encoding="utf-8"))
        raw_source_values = json.loads((raw_lang / "en_us.json").read_text(encoding="utf-8"))
        self.assertEqual(raw_target_values["custom.target.only"], "手動訳")
        self.assertEqual(raw_source_values["custom.source.only"], "Manual source")
        self.assertNotIn("mq_localizer.quest.obsolete.title", raw_target_values)
        self.assertNotIn("mq_localizer.quest.obsolete.title", raw_source_values)

    def test_malformed_or_duplicate_existing_targets_fail_without_mutation(self) -> None:
        cases = (
            (
                "native_snbt",
                "1.21.1",
                lambda instance: instance
                / "config"
                / "ftbquests"
                / "quests"
                / "lang"
                / "ja_jp.snbt",
                b"{ broken",
            ),
            (
                "legacy_json",
                "1.20.1",
                lambda instance: instance
                / "kubejs"
                / "assets"
                / "kubejs"
                / "lang"
                / "ja_jp.json",
                b'{"duplicate.key":"one","duplicate.key":"two"}\n',
            ),
            (
                "legacy_raw",
                "1.20.1",
                lambda instance: instance
                / "resourcepacks"
                / "mq_localizer_ja_jp"
                / "assets"
                / "minecraft"
                / "lang"
                / "ja_jp.json",
                b'{"mq_localizer.quest.1111111111111111.title":"one",'
                b'"mq_localizer.quest.1111111111111111.title":"two"}\n',
            ),
        )
        for fixture, version, target_factory, invalid in cases:
            with self.subTest(fixture=fixture):
                instance = self.copy_fixture(fixture)
                target = target_factory(instance)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(invalid)
                before = _snapshot(instance)
                adapter = create_default_registry().detect(instance, "en_us", version)

                with self.assertRaises(AdapterError):
                    adapter.load(instance, "en_us", "ja_jp", minecraft_version=version)

                self.assertEqual(target.read_bytes(), invalid)
                self.assertEqual(_snapshot(instance), before)

    def test_late_existing_target_corruption_fails_preflight_before_api(self) -> None:
        cases = (
            (
                "native_snbt",
                "1.21.1",
                lambda project: project.default_output,
                b"{ broken",
            ),
            (
                "legacy_json",
                "1.20.1",
                lambda project: project.default_output,
                b'{"duplicate.key":"one","duplicate.key":"two"}\n',
            ),
            (
                "legacy_raw",
                "1.20.1",
                lambda project: project.default_output
                / "assets"
                / "minecraft"
                / "lang"
                / "ja_jp.json",
                b'{"duplicate.key":"one","duplicate.key":"two"}\n',
            ),
        )
        for fixture, version, target_factory, invalid in cases:
            with self.subTest(fixture=fixture):
                instance = self.copy_fixture(fixture)
                adapter = create_default_registry().detect(instance, "en_us", version)
                project = adapter.load(instance, "en_us", "ja_jp", minecraft_version=version)
                target = target_factory(project)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(invalid)
                before = _snapshot(instance)
                client = _PrefixClient()

                with self.assertRaises(AdapterError):
                    TranslationService(client).translate(
                        project,
                        adapter,
                        project.default_output,
                        "api-key",
                        "gpt-test",
                        GlossaryCatalog(),
                        TranslationOptions(preserve_existing=False),
                    )

                self.assertEqual(client.calls, [])
                self.assertEqual(target.read_bytes(), invalid)
                self.assertEqual(_snapshot(instance), before)

    def test_auto_detection_uses_version_and_ignores_unrelated_kubejs_catalog(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        before = _snapshot(instance)
        with self.assertRaisesRegex(AdapterError, "1.21.*locale"):
            LocalizerApplication().analyze(
                source_path=instance,
                adapter_id="auto",
                source_locale="en_us",
                target_locale="ja_jp",
                minecraft_version="1.21.1",
            )
        self.assertEqual(_snapshot(instance), before)

        unrelated = instance / "kubejs" / "assets" / "kubejs" / "lang" / "en_us.json"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text(
            json.dumps(
                {
                    "item.kubejs.copper_widget": "Copper Widget",
                    "block.kubejs.machine_frame": "Machine Frame",
                    "quest.foo.title": "Unrelated KubeJS screen",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        self.assertEqual(adapter.id, "ftb_legacy_raw")

    def test_legacy_json_prefers_unique_high_confidence_candidate(self) -> None:
        instance = self.copy_fixture("legacy_json")
        weak_candidate = (
            instance / "kubejs" / "assets" / "kubejs" / "lang" / "en_us.json"
        )
        strong_candidate = (
            instance
            / "resourcepacks"
            / "quest-locale"
            / "assets"
            / "ftbquests"
            / "lang"
            / "en_us.json"
        )
        strong_candidate.parent.mkdir(parents=True)
        strong_candidate.write_text(
            json.dumps(
                {
                    "pack.quest.start.title": "Strong quest title",
                    "pack.quest.start.description": "Strong quest description",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")
        self.assertEqual(adapter.id, "ftb_legacy_json")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )

        self.assertEqual(project.source_path, strong_candidate)
        self.assertEqual(project.default_output, strong_candidate.with_name("ja_jp.json"))
        self.assertNotEqual(project.source_path, weak_candidate)
        self.assertFalse(project.default_output.exists())

    def test_legacy_json_equal_strong_candidates_stop_without_raw_fallback_or_write(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        candidates = (
            instance / "kubejs" / "assets" / "ftbquests" / "lang" / "en_us.json",
            instance
            / "resourcepacks"
            / "quest-locale"
            / "assets"
            / "ftblang"
            / "lang"
            / "en_us.json",
        )
        for index, candidate in enumerate(candidates, start=1):
            candidate.parent.mkdir(parents=True)
            candidate.write_text(
                json.dumps({f"pack.quest.{index}.title": f"Quest {index}"}) + "\n",
                encoding="utf-8",
            )

        registry = create_default_registry()
        legacy_json = registry.get("ftb_legacy_json")
        self.assertEqual(legacy_json.probe(instance, "en_us"), 65)
        self.assertEqual(
            registry.detect(instance, "en_us", "1.20.1").id,
            "ftb_legacy_json",
        )
        before = _snapshot(instance)

        with self.assertRaises(AdapterError) as raised:
            LocalizerApplication(registry).analyze(
                source_path=instance,
                adapter_id="auto",
                source_locale="en_us",
                target_locale="ja_jp",
                minecraft_version="1.20.1",
            )

        message = str(raised.exception)
        self.assertIn("原文言語JSONを自動で1つに特定できません", message)
        self.assertIn("同じ優先度になった候補: 2件", message)
        self.assertIn("kubejs/assets/ftbquests/lang/en_us.json", message)
        self.assertIn(
            "resourcepacks/quest-locale/assets/ftblang/lang/en_us.json",
            message,
        )
        self.assertIn("誤った翻訳先言語JSONへ書き込まない", message)
        self.assertNotIn("ファイルを直接選択", message)
        self.assertEqual(_snapshot(instance), before)
        self.assertFalse(any(candidate.with_name("ja_jp.json").exists() for candidate in candidates))

    def test_legacy_json_referenced_weak_catalog_wins_over_already_keyed_raw_quest(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        candidate = instance / "kubejs" / "assets" / "kubejs" / "lang" / "en_us.json"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(
            json.dumps(
                {
                    "existing.translation.key": "Referenced text",
                    "pack.quest.start.title": "Quest title",
                    "pack.quest.start.description": "Quest description",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        registry = create_default_registry()
        adapter = registry.detect(instance, "en_us", "1.20.1")
        project = adapter.load(instance, "en_us", "ja_jp", "1.20.1")

        self.assertEqual(adapter.id, "ftb_legacy_json")
        self.assertEqual(project.source_path, candidate)
        self.assertFalse(project.default_output.exists())

    def test_legacy_json_unreferenced_weak_catalog_does_not_hide_raw_quest(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        candidate = instance / "kubejs" / "assets" / "kubejs" / "lang" / "en_us.json"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(
            json.dumps(
                {
                    "unrelated.quest.title": "Unrelated title",
                    "unrelated.quest.description": "Unrelated description",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        adapter = create_default_registry().detect(instance, "en_us", "1.20.1")

        self.assertEqual(adapter.id, "ftb_legacy_raw")

    def test_modern_rejects_every_reparse_component_on_conventional_route(self) -> None:
        routes = (
            Path("config/ftbquests"),
            Path("config/ftbquests/quests/lang"),
        )
        for index, relative_link in enumerate(routes):
            with self.subTest(relative_link=relative_link):
                instance = self.copy_fixture("native_snbt")
                link = instance / relative_link
                external = instance.parent / f"external-modern-{index}"
                link.rename(external)
                _make_directory_link(link, external)
                self.addCleanup(_remove_directory_link, link)
                source = (
                    external / "quests" / "lang" / "en_us.snbt"
                    if relative_link.name.lower() == "ftbquests"
                    else external / "en_us.snbt"
                )
                source_before = source.read_bytes()
                adapter = create_default_registry().get("ftb_modern_snbt")

                with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                    adapter.probe(instance, "en_us")
                with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                    adapter.load(
                        instance,
                        "en_us",
                        "ja_jp",
                        minecraft_version="1.21.1",
                    )

                self.assertEqual(source.read_bytes(), source_before)

    def test_split_and_raw_adapters_reject_a_redirected_config_ancestor(self) -> None:
        cases = (
            ("split_snbt", "ftb_split_snbt", "1.21.1"),
            ("split_json5", "ftb_split_json5", "26.1.2"),
            ("legacy_raw", "ftb_legacy_raw", "1.20.1"),
        )
        for index, (fixture, adapter_id, version) in enumerate(cases):
            with self.subTest(adapter=adapter_id):
                instance = self.copy_fixture(fixture)
                config = instance / "config"
                external = instance.parent / f"external-config-{index}"
                config.rename(external)
                sentinel = external / "sentinel.txt"
                sentinel.write_text("outside\n", encoding="utf-8")
                _make_directory_link(config, external)
                self.addCleanup(_remove_directory_link, config)
                adapter = create_default_registry().get(adapter_id)

                with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                    adapter.probe(instance, "en_us")
                with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                    adapter.load(
                        instance,
                        "en_us",
                        "ja_jp",
                        minecraft_version=version,
                    )

                self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")

    def test_legacy_raw_rejects_redirected_default_output_ancestor(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        adapter = create_default_registry().get("ftb_legacy_raw")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.20.1",
        )
        external = instance.parent / "external-legacy-output"
        external.mkdir()
        link = instance / "resourcepacks"
        if link.exists():
            shutil.rmtree(link)
        _make_directory_link(link, external)
        self.addCleanup(_remove_directory_link, link)

        with self.assertRaisesRegex(AdapterError, "symlink|junction"):
            adapter.validate_output(project, project.default_output)

        self.assertEqual(list(external.iterdir()), [])

    def test_legacy_raw_rejects_preexisting_redirected_output_before_catalog_read(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        external = instance.parent / "external-preexisting-legacy-output"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("outside\n", encoding="utf-8")
        link = instance / "resourcepacks"
        if link.exists():
            shutil.rmtree(link)
        _make_directory_link(link, external)
        self.addCleanup(_remove_directory_link, link)
        adapter = create_default_registry().get("ftb_legacy_raw")

        with patch.object(legacy_raw_module, "_read_existing_json_catalog") as read_catalog:
            with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                )

        read_catalog.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")

    def test_legacy_raw_rejects_nested_output_redirect_before_catalog_read(self) -> None:
        instance = self.copy_fixture("legacy_raw")
        output = instance / "resourcepacks" / "mq_localizer_ja_jp"
        lang_dir = (
            output
            / "assets"
            / "minecraft"
            / "lang"
        )
        external = instance.parent / "external-nested-legacy-output"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("outside\n", encoding="utf-8")
        lang_dir.parent.mkdir(parents=True, exist_ok=True)
        _make_directory_link(lang_dir, external)
        self.addCleanup(_remove_directory_link, lang_dir)
        adapter = create_default_registry().get("ftb_legacy_raw")

        with patch.object(legacy_raw_module, "_read_existing_json_catalog") as read_catalog:
            with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                )

        read_catalog.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")

    def test_split_load_rejects_preexisting_output_redirects_before_catalog_read(self) -> None:
        cases = (
            ("split_snbt", "ftb_split_snbt", "1.21.1", split_snbt_module),
            ("split_json5", "ftb_split_json5", "26.1.2", split_json5_module),
        )
        for fixture, adapter_id, version, module in cases:
            for nested in (False, True):
                with self.subTest(adapter=adapter_id, nested=nested):
                    instance = self.copy_fixture(fixture)
                    target = (
                        instance
                        / "config"
                        / "ftbquests"
                        / "quests"
                        / "lang"
                        / "ja_jp"
                    )
                    external = instance.parent / f"external-{adapter_id}-{int(nested)}"
                    external.mkdir()
                    sentinel = external / "sentinel.txt"
                    sentinel.write_text("outside\n", encoding="utf-8")
                    if nested:
                        link = target / "linked"
                    else:
                        shutil.rmtree(target)
                        link = target
                    _make_directory_link(link, external)
                    self.addCleanup(_remove_directory_link, link)
                    adapter = create_default_registry().get(adapter_id)

                    with patch.object(module, "_read_target_documents") as read_catalog:
                        with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                            adapter.load(
                                instance,
                                "en_us",
                                "ja_jp",
                                minecraft_version=version,
                            )

                    read_catalog.assert_not_called()
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")

    def test_single_locale_adapters_allow_explicit_directory_root_alias(self) -> None:
        cases = (
            ("native_snbt", "ftb_modern_snbt", "1.21.1"),
            ("legacy_json", "ftb_legacy_json", "1.20.1"),
        )
        for index, (fixture, adapter_id, version) in enumerate(cases):
            with self.subTest(adapter=adapter_id):
                instance = self.copy_fixture(fixture)
                alias = instance.parent / f"instance-alias-{index}"
                _make_directory_link(alias, instance)
                self.addCleanup(_remove_directory_link, alias)

                adapter = create_default_registry().detect(alias, "en_us", version)
                self.assertEqual(adapter.id, adapter_id)
                project = adapter.load(
                    alias,
                    "en_us",
                    "ja_jp",
                    minecraft_version=version,
                )
                adapter.validate_output(project, project.default_output)

                self.assertTrue(project.source_path.is_relative_to(alias))

    def test_modern_rejects_reparse_fallback_metadata_before_reading(self) -> None:
        instance = self.copy_fixture("native_snbt")
        data_file = instance / "config" / "ftbquests" / "quests" / "data.snbt"
        data_file.unlink()
        external = instance.parent / "external-fallback-data"
        external.mkdir()
        (external / "secret.txt").write_text("do not read\n", encoding="utf-8")
        _make_directory_link(data_file, external)
        self.addCleanup(_remove_directory_link, data_file)
        adapter = create_default_registry().get("ftb_modern_snbt")

        with patch.object(modern_module, "parse_snbt") as parse_fallback:
            with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                adapter.probe(instance, "de_de")

        parse_fallback.assert_not_called()

    def test_modern_ignores_unused_fallback_when_split_locale_exists(self) -> None:
        cases = (
            ("split_snbt", "ftb_split_snbt", "1.21.1"),
            ("split_json5", "ftb_split_json5", "26.1.2"),
        )
        for index, (fixture, expected_adapter, version) in enumerate(cases):
            with self.subTest(adapter=expected_adapter):
                instance = self.copy_fixture(fixture)
                data_file = (
                    instance
                    / "config"
                    / "ftbquests"
                    / "quests"
                    / "data.snbt"
                )
                if data_file.exists():
                    data_file.unlink()
                external = instance.parent / f"unused-modern-fallback-{index}"
                external.mkdir()
                (external / "secret.txt").write_text(
                    "must not be inspected\n",
                    encoding="utf-8",
                )
                _make_directory_link(data_file, external)
                self.addCleanup(_remove_directory_link, data_file)
                modern = create_default_registry().get("ftb_modern_snbt")

                with patch.object(modern_module, "parse_snbt") as parse_fallback:
                    self.assertEqual(modern.probe(instance, "en_us"), 0)
                    detected = create_default_registry().detect(
                        instance,
                        "en_us",
                        version,
                    )

                parse_fallback.assert_not_called()
                self.assertEqual(detected.id, expected_adapter)

    def test_legacy_json_recursive_search_skips_reparse_subtree(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        instance = root / "instance"
        instance.mkdir()
        external = root / "external-catalog"
        external.mkdir()
        source = external / "en_us.json"
        source.write_text(
            json.dumps(
                {
                    "quest.external.title": "External title",
                    "quest.external.description": "External description",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        link = instance / "ftbquests"
        _make_directory_link(link, external)
        self.addCleanup(_remove_directory_link, link)
        adapter = create_default_registry().get("ftb_legacy_json")

        with patch.object(
            legacy_json_module,
            "read_text_detect",
            wraps=legacy_json_module.read_text_detect,
        ) as read_catalog:
            self.assertEqual(adapter.probe(instance, "en_us"), 0)
            with self.assertRaisesRegex(AdapterError, "見つかりません"):
                adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                )

        self.assertEqual(read_catalog.call_count, 0)
        self.assertFalse((external / "ja_jp.json").exists())

    def test_legacy_json_direct_file_rejects_reparse_ancestor(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        instance = root / "instance"
        instance.mkdir()
        external = root / "external-catalog"
        external.mkdir()
        source = external / "en_us.json"
        source.write_text(
            '{"quest.external.title":"External title"}\n',
            encoding="utf-8",
        )
        link = instance / "ftbquests"
        _make_directory_link(link, external)
        self.addCleanup(_remove_directory_link, link)
        selected = link / "en_us.json"
        adapter = create_default_registry().get("ftb_legacy_json")

        with self.assertRaisesRegex(AdapterError, "symlink|junction"):
            adapter.probe(selected, "en_us")
        with self.assertRaisesRegex(AdapterError, "symlink|junction"):
            adapter.load(
                selected,
                "en_us",
                "ja_jp",
                minecraft_version="1.20.1",
            )

    def test_modern_direct_file_rejects_reparse_ancestor(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        instance = root / "instance"
        instance.mkdir()
        external = root / "external-lang"
        external.mkdir()
        source = external / "en_us.snbt"
        source.write_text(
            '{quest.external.title: "External title"}\n',
            encoding="utf-8",
        )
        link = instance / "lang"
        _make_directory_link(link, external)
        self.addCleanup(_remove_directory_link, link)
        selected = link / "en_us.snbt"
        adapter = create_default_registry().get("ftb_modern_snbt")

        with self.assertRaisesRegex(AdapterError, "symlink|junction"):
            adapter.probe(selected, "en_us")
        with self.assertRaisesRegex(AdapterError, "symlink|junction"):
            adapter.load(
                selected,
                "en_us",
                "ja_jp",
                minecraft_version="1.21.1",
            )

    def test_single_locale_direct_source_rejects_parent_traversal_before_read(self) -> None:
        cases = (
            (
                "ftb_modern_snbt",
                "1.21.1",
                "en_us.snbt",
                '{quest.external.title: "External title"}\n',
                modern_module,
            ),
            (
                "ftb_legacy_json",
                "1.20.1",
                "en_us.json",
                '{"quest.external.title":"External title"}\n',
                legacy_json_module,
            ),
        )
        for index, (adapter_id, version, filename, content, module) in enumerate(cases):
            with self.subTest(adapter=adapter_id):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                instance = root / f"instance-{index}"
                instance.mkdir()
                external = root / f"external-{index}"
                pivot = external / "pivot"
                pivot.mkdir(parents=True)
                external_lang = external / "lang"
                external_lang.mkdir()
                source = external_lang / filename
                source.write_text(content, encoding="utf-8")
                link = instance / "link"
                _make_directory_link(link, pivot)
                self.addCleanup(_remove_directory_link, link)
                selected = link / ".." / "lang" / filename
                adapter = create_default_registry().get(adapter_id)

                with patch.object(module, "read_text_detect") as read_source:
                    with self.assertRaisesRegex(AdapterError, "親ディレクトリ参照"):
                        adapter.probe(selected, "en_us")
                    with self.assertRaisesRegex(AdapterError, "親ディレクトリ参照"):
                        adapter.load(
                            selected,
                            "en_us",
                            "ja_jp",
                            minecraft_version=version,
                        )

                read_source.assert_not_called()
                self.assertFalse((external_lang / filename.replace("en_us", "ja_jp")).exists())

    def test_single_locale_output_rejects_parent_traversal_before_partition(self) -> None:
        cases = (
            ("native_snbt", "ftb_modern_snbt", "1.21.1", "ja_jp.snbt", modern_module),
            ("legacy_json", "ftb_legacy_json", "1.20.1", "ja_jp.json", legacy_json_module),
        )
        for index, (fixture, adapter_id, version, filename, module) in enumerate(cases):
            with self.subTest(adapter=adapter_id):
                instance = self.copy_fixture(fixture)
                adapter = create_default_registry().get(adapter_id)
                project = adapter.load(
                    instance,
                    "en_us",
                    "ja_jp",
                    minecraft_version=version,
                )
                external = instance.parent / f"external-output-traversal-{index}"
                pivot = external / "pivot"
                pivot.mkdir(parents=True)
                external_lang = external / "lang"
                external_lang.mkdir()
                link = instance / f"output-link-{index}"
                _make_directory_link(link, pivot)
                self.addCleanup(_remove_directory_link, link)
                output = link / ".." / "lang" / filename

                with patch.object(module, "_partition_existing") as partition:
                    with self.assertRaisesRegex(AdapterError, "親ディレクトリ参照"):
                        adapter.validate_output(project, output)

                partition.assert_not_called()
                self.assertFalse((external_lang / filename).exists())

    def test_literal_tilde_source_and_output_validate_the_paths_callers_use(self) -> None:
        instance = self.copy_fixture("native_snbt")
        adapter = create_default_registry().get("ftb_modern_snbt")
        project = adapter.load(
            instance,
            "en_us",
            "ja_jp",
            minecraft_version="1.21.1",
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        working = root / "working"
        working.mkdir()
        safe_home_lang = root / "safe-home" / "lang"
        safe_home_lang.mkdir(parents=True)
        (safe_home_lang / "en_us.snbt").write_text(
            '{quest.safe.title: "Safe home title"}\n',
            encoding="utf-8",
        )
        external_lang = root / "external" / "lang"
        external_lang.mkdir(parents=True)
        (external_lang / "en_us.snbt").write_text(
            '{quest.external.title: "External title"}\n',
            encoding="utf-8",
        )
        external_target = external_lang / "ja_jp.snbt"
        external_target.write_text(
            '{quest.external.title: "外部訳"}\n',
            encoding="utf-8",
        )
        literal_tilde = working / "~"
        literal_tilde.mkdir()
        link = literal_tilde / "lang"
        _make_directory_link(link, external_lang)
        self.addCleanup(_remove_directory_link, link)
        selected = Path("~") / "lang" / "en_us.snbt"
        output = Path("~") / "lang" / "ja_jp.snbt"
        original_cwd = Path.cwd()
        try:
            os.chdir(working)
            with patch.dict(
                os.environ,
                {
                    "HOME": str(safe_home_lang.parent),
                    "USERPROFILE": str(safe_home_lang.parent),
                },
            ):
                with patch.object(modern_module, "read_text_detect") as read_source:
                    with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                        adapter.probe(selected, "en_us")
                    with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                        adapter.load(
                            selected,
                            "en_us",
                            "ja_jp",
                            minecraft_version="1.21.1",
                        )
                read_source.assert_not_called()

                with patch.object(modern_module, "_partition_existing") as partition:
                    with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                        adapter.validate_output(project, output)
                partition.assert_not_called()
        finally:
            os.chdir(original_cwd)

        self.assertEqual(
            external_target.read_text(encoding="utf-8"),
            '{quest.external.title: "外部訳"}\n',
        )

    def test_single_locale_load_rejects_reparse_target_before_partition(self) -> None:
        cases = (
            (
                "native_snbt",
                "ftb_modern_snbt",
                "1.21.1",
                lambda instance: instance
                / "config"
                / "ftbquests"
                / "quests"
                / "lang"
                / "ja_jp.snbt",
                modern_module,
            ),
            (
                "legacy_json",
                "ftb_legacy_json",
                "1.20.1",
                lambda instance: instance
                / "kubejs"
                / "assets"
                / "kubejs"
                / "lang"
                / "ja_jp.json",
                legacy_json_module,
            ),
        )
        for index, (fixture, adapter_id, version, target_factory, module) in enumerate(cases):
            with self.subTest(adapter=adapter_id):
                instance = self.copy_fixture(fixture)
                target = target_factory(instance)
                target.unlink()
                external = instance.parent / f"external-target-{index}"
                external.mkdir()
                (external / "secret.txt").write_text("do not read\n", encoding="utf-8")
                _make_directory_link(target, external)
                self.addCleanup(_remove_directory_link, target)
                adapter = create_default_registry().get(adapter_id)

                with patch.object(module, "_partition_existing") as partition:
                    with self.assertRaisesRegex(AdapterError, "symlink|junction"):
                        adapter.load(
                            instance,
                            "en_us",
                            "ja_jp",
                            minecraft_version=version,
                        )

                partition.assert_not_called()


if __name__ == "__main__":
    unittest.main()
