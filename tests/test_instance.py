from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.instance import (  # noqa: E402
    InstanceInspectionError,
    inspect_instance_root,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        raise unittest.SkipTest(f"directory symlink/junctionを作成できません: {symlink_error}")


def _remove_directory_link(link: Path) -> None:
    try:
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            link.rmdir()
    except OSError:
        pass


class InstanceInspectionTests(unittest.TestCase):
    def make_root(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "instance"
        root.mkdir()
        return root

    def test_prism_mmc_pack_has_highest_priority_and_normalizes_game_child(self) -> None:
        root = self.make_root()
        game = root / ".minecraft"
        (game / "mods").mkdir(parents=True)
        _write_json(
            root / "mmc-pack.json",
            {
                "version": "26.1.2",
                "components": [
                    {"uid": "org.prismlauncher", "version": "9.0"},
                    {"uid": "net.minecraft", "version": "1.21.1"},
                ],
            },
        )
        (root / "instance.cfg").write_text("IntendedVersion=1.20.1\n", encoding="utf-8")
        _write_json(
            root / "manifest.json",
            {"version": "99.4.2", "minecraft": {"version": "1.19.2"}},
        )

        result = inspect_instance_root(root)

        self.assertEqual(result.instance_root, root)
        self.assertEqual(result.game_root, game)
        self.assertEqual(result.mods_path, game / "mods")
        self.assertEqual(result.minecraft_version, "1.21.1")
        self.assertIsNotNone(result.detected_by)
        self.assertEqual(result.detected_by.detector, "Prism/MultiMC mmc-pack.json")
        self.assertTrue(any("競合" in warning for warning in result.warnings))
        self.assertNotIn("26.1.2", {item.value for item in result.evidence})

        selected_game = inspect_instance_root(game)
        self.assertEqual(selected_game.instance_root, root)
        self.assertEqual(selected_game.selected_root, game)
        self.assertEqual(selected_game.minecraft_version, "1.21.1")

    def test_instance_cfg_is_used_when_mmc_pack_has_no_minecraft_component(self) -> None:
        root = self.make_root()
        (root / "mods").mkdir()
        _write_json(
            root / "mmc-pack.json",
            {"components": [{"uid": "net.fabricmc.fabric-loader", "version": "0.16.0"}]},
        )
        (root / "instance.cfg").write_text(
            "name=Example Pack\nIntendedVersion=1.20.1\n",
            encoding="utf-8",
        )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "1.20.1")
        self.assertEqual(result.detected_by.detector, "Prism/MultiMC instance.cfg")

    def test_calendar_style_minecraft_version_is_accepted_only_in_explicit_field(self) -> None:
        root = self.make_root()
        (root / "mods").mkdir()
        _write_json(
            root / "mmc-pack.json",
            {
                "version": "99.4.2",
                "components": [
                    {"uid": "net.minecraft", "version": "26.1.2"},
                ],
            },
        )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "26.1.2")
        self.assertEqual(result.detected_by.field, "components[0].version")
        self.assertNotIn("99.4.2", {item.value for item in result.evidence})

    def test_curseforge_sources_ignore_pack_release_version(self) -> None:
        root = self.make_root()
        game = root / "minecraft"
        (game / "mods").mkdir(parents=True)
        _write_json(
            root / "minecraftinstance.json",
            {
                "version": "42.7.0",
                "baseModLoader": {
                    "name": "neoforge-21.1.100",
                    "minecraftVersion": "1.21.1",
                },
            },
        )
        _write_json(
            root / "manifest.json",
            {
                "manifestType": "minecraftModpack",
                "version": "26.1.2",
                "minecraft": {"version": "1.20.1"},
            },
        )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "1.21.1")
        self.assertEqual(result.detected_by.detector, "CurseForge minecraftinstance.json")
        self.assertNotIn("26.1.2", {item.value for item in result.evidence})

    def test_generic_profiles_require_minecraft_specific_fields(self) -> None:
        root = self.make_root()
        (root / "mods").mkdir()
        _write_json(root / "profile.json", {"version": "1.20.1", "name": "Pack release"})
        _write_json(
            root / "instance.json",
            {
                "version": "26.1.2",
                "minecraftVersion": "1.19.4",
            },
        )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "1.19.4")
        self.assertEqual(result.detected_by.field, "minecraftVersion")
        self.assertNotIn("26.1.2", {item.value for item in result.evidence})

        (root / "instance.json").unlink()
        result_without_explicit_field = inspect_instance_root(root)
        self.assertEqual(result_without_explicit_field.minecraft_version, "")
        self.assertIsNone(result_without_explicit_field.detected_by)
        self.assertTrue(any("自動検出できません" in item for item in result_without_explicit_field.warnings))

    def test_equal_priority_conflict_is_not_resolved_arbitrarily(self) -> None:
        root = self.make_root()
        (root / "mods").mkdir()
        _write_json(
            root / "mmc-pack.json",
            {
                "components": [
                    {"uid": "net.minecraft", "version": "1.20.1"},
                    {"uid": "net.minecraft", "version": "1.21.1"},
                ]
            },
        )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "")
        self.assertTrue(any("同順位" in item for item in result.warnings))

    def test_ftb_quests_filename_is_a_last_resort_and_only_decodes_known_scheme(self) -> None:
        root = self.make_root()
        mods = root / "mods"
        mods.mkdir()
        with zipfile.ZipFile(mods / "ftb-quests-forge-2001.4.9.jar", "w") as archive:
            archive.writestr(
                "fabric.mod.json",
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "id": "ftbquests",
                        "depends": {"minecraft": ">=1.20.1"},
                    }
                ),
            )
        (mods / "ftb-quests-neoforge-26.1.2.jar").write_bytes(b"not a zip")

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "1.20.1")
        self.assertEqual(result.detected_by.detector, "FTB Quests JAR filename fallback")
        self.assertNotIn("26.1.2", {item.value for item in result.evidence})
        self.assertTrue(any("metadataを解析できません" in item for item in result.warnings))

    def test_ftb_jar_exact_metadata_beats_filename_code(self) -> None:
        root = self.make_root()
        mods = root / "mods"
        mods.mkdir()
        jar = mods / "ftb-quests-fabric-2101.1.0.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(
                "fabric.mod.json",
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "id": "ftbquests",
                        "depends": {"minecraft": "1.20.1"},
                    }
                ),
            )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "1.20.1")
        self.assertEqual(result.detected_by.detector, "FTB Quests JAR metadata")
        self.assertIn("1.21.1", {item.value for item in result.evidence})
        self.assertTrue(any("競合" in item for item in result.warnings))

    def test_current_ftb_filename_fallback_decodes_calendar_minecraft_version(self) -> None:
        root = self.make_root()
        mods = root / "mods"
        mods.mkdir()
        jar = mods / "ftb-quests-neoforge-26.1.2.1.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(
                "META-INF/neoforge.mods.toml",
                """
                modLoader = "javafml"
                loaderVersion = "[1,)"
                license = "All Rights Reserved"

                [[mods]]
                modId = "ftbquests"
                version = "26.1.2.1"

                [[dependencies.ftbquests]]
                modId = "minecraft"
                mandatory = true
                versionRange = "[26.1.2,26.1.3)"
                """,
            )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "26.1.2")
        self.assertEqual(result.detected_by.detector, "FTB Quests JAR filename fallback")

    def test_calendar_filename_fallback_rejects_pre_calendar_and_extra_version_parts(self) -> None:
        for filename in (
            "ftb-quests-neoforge-25.1.2.1.jar",
            "ftb-quests-neoforge-26.1.2.1.9.jar",
        ):
            with self.subTest(filename=filename):
                root = self.make_root()
                mods = root / "mods"
                mods.mkdir()
                with zipfile.ZipFile(mods / filename, "w") as archive:
                    archive.writestr(
                        "fabric.mod.json",
                        json.dumps({"schemaVersion": 1, "id": "ftbquests"}),
                    )

                result = inspect_instance_root(root)

                self.assertEqual(result.minecraft_version, "")

    def test_legacy_forge_mcmod_info_array_uses_only_ftb_quests_entry(self) -> None:
        root = self.make_root()
        mods = root / "mods"
        mods.mkdir()
        jar = mods / "FTBQuests-legacy.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(
                "mcmod.info",
                json.dumps(
                    [
                        {
                            "modid": "bundled_helper",
                            "name": "FTB Quests Helper",
                            "mcversion": "1.7.10",
                        },
                        {
                            "modid": "ftbquests",
                            "name": "FTB Quests",
                            "mcversion": "1.12.2",
                        },
                    ]
                ),
            )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "1.12.2")
        self.assertEqual(result.detected_by.detector, "FTB Quests JAR metadata")
        self.assertEqual(result.detected_by.field, "mcmod.info:[1].mcversion")
        self.assertNotIn("1.7.10", {item.value for item in result.evidence})

    def test_legacy_forge_mcmod_info_supports_direct_and_mod_list_objects(self) -> None:
        cases = (
            (
                "META-INF/mcmod.info",
                {"modid": "ftbquests", "mcversion": "1.12.2"},
                "meta-inf/mcmod.info:mcversion",
            ),
            (
                "mcmod.info",
                {
                    "modListVersion": 2,
                    "modList": [
                        {"modid": "ftbquests", "mcversion": "1.10.2"},
                    ],
                },
                "mcmod.info:modList[0].mcversion",
            ),
        )
        for member_name, metadata, expected_field in cases:
            with self.subTest(member_name=member_name):
                root = self.make_root()
                mods = root / "mods"
                mods.mkdir()
                with zipfile.ZipFile(mods / "FTBQuests-legacy.jar", "w") as archive:
                    archive.writestr(member_name, json.dumps(metadata))

                result = inspect_instance_root(root)

                self.assertEqual(
                    result.minecraft_version,
                    metadata.get("mcversion", "1.10.2"),
                )
                self.assertEqual(result.detected_by.field, expected_field)

    def test_legacy_forge_mcmod_info_requires_exact_identity_and_version(self) -> None:
        cases = (
            (
                "similar mod id",
                [{"modid": "ftbquests_addon", "name": "FTB Quests", "mcversion": "1.12.2"}],
            ),
            (
                "camel-case field",
                [{"modId": "ftbquests", "mcversion": "1.12.2"}],
            ),
            (
                "non-canonical mod id",
                [{"modid": "FTBQUESTS", "mcversion": "1.12.2"}],
            ),
            (
                "version range",
                [{"modid": "ftbquests", "mcversion": "[1.12.2]"}],
            ),
            (
                "placeholder version",
                [{"modid": "ftbquests", "mcversion": "${mcversion}"}],
            ),
        )
        for label, metadata in cases:
            with self.subTest(label=label):
                root = self.make_root()
                mods = root / "mods"
                mods.mkdir()
                with zipfile.ZipFile(mods / "FTBQuests-legacy.jar", "w") as archive:
                    archive.writestr("mcmod.info", json.dumps(metadata))

                result = inspect_instance_root(root)

                self.assertEqual(result.minecraft_version, "")
                self.assertEqual(result.evidence, ())
                if metadata[0].get("modid") != "ftbquests":
                    self.assertTrue(
                        any("filename fallbackを使用しません" in item for item in result.warnings)
                    )

    def test_legacy_forge_conflicting_ftb_entries_are_ambiguous(self) -> None:
        root = self.make_root()
        mods = root / "mods"
        mods.mkdir()
        with zipfile.ZipFile(mods / "FTBQuests-1202.9.0.15.jar", "w") as archive:
            archive.writestr(
                "mcmod.info",
                json.dumps(
                    [
                        {"modid": "ftbquests", "mcversion": "1.12.2"},
                        {"modid": "ftbquests", "mcversion": "1.11.2"},
                    ]
                ),
            )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "")
        self.assertEqual(
            {item.value for item in result.evidence if item.priority == 80},
            {"1.11.2", "1.12.2"},
        )
        self.assertTrue(any("同順位" in item for item in result.warnings))

    def test_legacy_forge_malformed_or_duplicate_metadata_fails_closed(self) -> None:
        cases = (
            ("bad modList", '{"modList":{"modid":"ftbquests","mcversion":"1.12.2"}}'),
            (
                "duplicate JSON key",
                '[{"modid":"ftbquests","mcversion":"1.12.2","mcversion":"1.11.2"}]',
            ),
            ("non-object entry", '[{"modid":"ftbquests","mcversion":"1.12.2"},null]'),
        )
        for label, metadata in cases:
            with self.subTest(label=label):
                root = self.make_root()
                mods = root / "mods"
                mods.mkdir()
                with zipfile.ZipFile(mods / "FTBQuests-legacy.jar", "w") as archive:
                    archive.writestr("mcmod.info", metadata)

                result = inspect_instance_root(root)

                self.assertEqual(result.minecraft_version, "")
                self.assertEqual(result.evidence, ())
                self.assertTrue(any("metadataを解析できません" in item for item in result.warnings))
                self.assertTrue(
                    any("filename fallbackを使用しません" in item for item in result.warnings)
                )

    def test_legacy_forge_metadata_keeps_zip_member_safety_limits(self) -> None:
        cases = (
            (
                "case-insensitive duplicate",
                (
                    ("mcmod.info", '[{"modid":"ftbquests","mcversion":"1.12.2"}]'),
                    ("MCMOD.INFO", '[{"modid":"ftbquests","mcversion":"1.12.2"}]'),
                ),
                "重複archive member",
            ),
            (
                "oversized metadata",
                (("mcmod.info", b" " * (1024 * 1024 + 1)),),
                "metadataが大きすぎます",
            ),
        )
        for label, members, expected_warning in cases:
            with self.subTest(label=label):
                root = self.make_root()
                mods = root / "mods"
                mods.mkdir()
                with zipfile.ZipFile(
                    mods / "FTBQuests-legacy.jar",
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                ) as archive:
                    for member_name, data in members:
                        archive.writestr(member_name, data)

                result = inspect_instance_root(root)

                self.assertEqual(result.minecraft_version, "")
                self.assertEqual(result.evidence, ())
                self.assertTrue(any(expected_warning in item for item in result.warnings))

    def test_legacy_forge_jar_reparse_point_is_not_followed(self) -> None:
        root = self.make_root()
        mods = root / "mods"
        mods.mkdir()
        jar = mods / "FTBQuests-linked.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(
                "mcmod.info",
                '[{"modid":"ftbquests","mcversion":"1.12.2"}]',
            )

        with patch(
            "mq_localizer.instance._is_reparse_point",
            side_effect=lambda path, _value: path == jar,
        ):
            result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "")
        self.assertEqual(result.evidence, ())
        self.assertTrue(any("linkを追跡せずスキップ" in item for item in result.warnings))

    def test_ftb_filename_fallback_requires_matching_mod_metadata(self) -> None:
        root = self.make_root()
        mods = root / "mods"
        mods.mkdir()
        jar = mods / "ftb-quests-fabric-2101.1.0.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(
                "fabric.mod.json",
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "id": "renamed_unrelated_mod",
                        "depends": {"minecraft": "1.21.1"},
                    }
                ),
            )

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "")
        self.assertEqual(result.evidence, ())
        self.assertTrue(any("filename fallbackを使用しません" in item for item in result.warnings))

    def test_malformed_and_duplicate_metadata_warns_then_uses_safe_fallback(self) -> None:
        root = self.make_root()
        (root / "mods").mkdir()
        (root / "mmc-pack.json").write_text(
            '{"components": [], "components": []}\n',
            encoding="utf-8",
        )
        (root / "instance.cfg").write_text("MinecraftVersion=1.18.2\n", encoding="utf-8")

        result = inspect_instance_root(root)

        self.assertEqual(result.minecraft_version, "1.18.2")
        self.assertTrue(any("重複JSONキー" in item for item in result.warnings))

    def test_game_root_and_mods_reparse_points_are_rejected(self) -> None:
        root = self.make_root()
        external = root.parent / "external-game"
        (external / "mods").mkdir(parents=True)
        linked_game = root / ".minecraft"
        _make_directory_link(linked_game, external)
        self.addCleanup(_remove_directory_link, linked_game)

        with self.assertRaisesRegex(InstanceInspectionError, "symlink|junction"):
            inspect_instance_root(root)

        _remove_directory_link(linked_game)
        game = root / ".minecraft"
        game.mkdir()
        external_mods = root.parent / "external-mods"
        external_mods.mkdir()
        linked_mods = game / "mods"
        _make_directory_link(linked_mods, external_mods)
        self.addCleanup(_remove_directory_link, linked_mods)

        with self.assertRaisesRegex(InstanceInspectionError, "mods.*symlink|mods.*junction"):
            inspect_instance_root(root)

    def test_selected_root_is_not_followed_and_metadata_links_are_skipped(self) -> None:
        root = self.make_root()
        external_root = root.parent / "external-root"
        (external_root / "mods").mkdir(parents=True)
        alias = root.parent / "instance-alias"
        _make_directory_link(alias, external_root)
        self.addCleanup(_remove_directory_link, alias)

        with self.assertRaisesRegex(InstanceInspectionError, "symlink|junction"):
            inspect_instance_root(alias)

        (root / "mods").mkdir()
        external_metadata = root.parent / "external-metadata"
        external_metadata.mkdir()
        _write_json(
            external_metadata / "payload.json",
            {"minecraftVersion": "1.21.1"},
        )
        linked_metadata = root / "profile.json"
        _make_directory_link(linked_metadata, external_metadata)
        self.addCleanup(_remove_directory_link, linked_metadata)

        result = inspect_instance_root(root)
        self.assertEqual(result.minecraft_version, "")
        self.assertTrue(any("追跡せず" in item for item in result.warnings))

    def test_ambiguous_or_invalid_roots_are_rejected_and_missing_mods_is_reported(self) -> None:
        root = self.make_root()
        (root / "minecraft").mkdir()
        (root / ".minecraft").mkdir()
        with self.assertRaisesRegex(InstanceInspectionError, "両方"):
            inspect_instance_root(root)

        selected_mods = root.parent / "mods"
        selected_mods.mkdir()
        with self.assertRaisesRegex(InstanceInspectionError, "親"):
            inspect_instance_root(selected_mods)

        missing = root.parent / "missing"
        with self.assertRaisesRegex(InstanceInspectionError, "存在しません"):
            inspect_instance_root(missing)

        plain = root.parent / "plain-instance"
        plain.mkdir()
        result = inspect_instance_root(plain)
        self.assertEqual(result.mods_path, plain / "mods")
        self.assertTrue(any("modsフォルダー" in item for item in result.warnings))


if __name__ == "__main__":
    unittest.main()
