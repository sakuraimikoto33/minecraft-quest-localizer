from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import CancelledError, TranslationError  # noqa: E402
from mq_localizer.glossary import ModLanguageScanner  # noqa: E402
from mq_localizer.glossary_snapshot import (  # noqa: E402
    GlossaryFileState,
    GlossaryInputRecorder,
    GlossaryInputSnapshot,
    GlossaryPathWatch,
    assert_glossary_inputs_unchanged,
)
import mq_localizer.glossary_snapshot as glossary_snapshot_module  # noqa: E402
import mq_localizer.minecraft_assets as minecraft_assets_module  # noqa: E402
from mq_localizer.scan_limits import GlossaryScanLimits  # noqa: E402


def _write_zip(path: Path, files: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value.encode("utf-8"))


def _language_jar(path: Path, source: str = "Test Tool") -> None:
    _write_zip(
        path,
        {
            "fabric.mod.json": json.dumps(
                {"schemaVersion": 1, "id": "test", "name": "Test Mod"}
            ),
            "assets/test/lang/en_us.json": json.dumps(
                {"item.test.tool": source}
            ),
            "assets/test/lang/ja_jp.json": json.dumps(
                {"item.test.tool": "試験道具"}
            ),
        },
    )


def _write_minecraft_assets(root: Path, version: str) -> dict[str, Path]:
    client = (
        root
        / "libraries"
        / "com"
        / "mojang"
        / "minecraft"
        / version
        / f"minecraft-{version}-client.jar"
    )
    _write_zip(
        client,
        {
            "assets/minecraft/lang/en_us.json": json.dumps(
                {"item.minecraft.iron_ingot": "Iron Ingot"}
            )
        },
    )
    target_data = json.dumps(
        {"item.minecraft.iron_ingot": "鉄インゴット"},
        ensure_ascii=False,
    ).encode("utf-8")
    target_hash = hashlib.sha1(target_data).hexdigest()
    target = root / "assets" / "objects" / target_hash[:2] / target_hash
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(target_data)

    index_data = json.dumps(
        {
            "objects": {
                "minecraft/lang/ja_jp.json": {
                    "hash": target_hash,
                    "size": len(target_data),
                }
            }
        }
    ).encode("utf-8")
    index = root / "assets" / "indexes" / "snapshot-test.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_bytes(index_data)
    metadata = root / "meta" / "net.minecraft" / f"{version}.json"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(
        json.dumps(
            {
                "assetIndex": {
                    "id": "snapshot-test",
                    "sha1": hashlib.sha1(index_data).hexdigest(),
                    "size": len(index_data),
                }
            }
        ),
        encoding="utf-8",
    )
    return {
        "metadata": metadata,
        "client": client,
        "index": index,
        "target": target,
    }


class GlossaryInputSnapshotTests(unittest.TestCase):
    def test_configured_per_source_limit_is_saved_and_reused_for_revalidation(self) -> None:
        limits = GlossaryScanLimits(
            max_source_members=10,
            max_language_file_mib=1,
            max_source_language_mib=1,
            max_total_language_mib=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            assets = game_root / "kubejs" / "assets"
            assets.mkdir(parents=True)
            recorder = GlossaryInputRecorder(scan_limits=limits)
            recorder.capture_external_inputs(game_root, include_resourcepacks=False)
            snapshot = recorder.freeze()

            self.assertEqual(snapshot.scan_limits, limits)
            for index in range(12):
                (assets / f"added-{index}.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(TranslationError, "10件"):
                assert_glossary_inputs_unchanged(snapshot)

    def test_unchanged_inputs_are_verified_without_reopening_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _language_jar(mods / "test.jar")
            kube_lang = game_root / "kubejs" / "assets" / "kube" / "lang"
            kube_lang.mkdir(parents=True)
            (kube_lang / "en_us.json").write_text(
                json.dumps({"item.kube.tool": "Kube Tool"}),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
            )
            self.assertIsNotNone(catalog.input_snapshot)

            with mock.patch.object(
                zipfile,
                "ZipFile",
                side_effect=AssertionError("archive contents were reopened"),
            ):
                assert_glossary_inputs_unchanged(catalog.input_snapshot)

    def test_mod_archive_add_delete_and_change_are_detected(self) -> None:
        mutations = ("add", "delete", "change")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                mods = Path(directory) / "mods"
                archive = mods / "test.jar"
                _language_jar(archive)
                catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

                if mutation == "add":
                    _language_jar(mods / "added.zip", "Added Tool")
                elif mutation == "delete":
                    archive.unlink()
                else:
                    _language_jar(archive, "Changed Test Tool With Different Size")

                with self.assertRaisesRegex(TranslationError, "再解析"):
                    assert_glossary_inputs_unchanged(catalog.input_snapshot)

    def test_mod_archive_symlink_target_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mods = root / "mods"
            mods.mkdir()
            target = root / "archive-target.jar"
            _language_jar(target)
            archive = mods / "test.jar"
            try:
                archive.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"file symlinkを作成できない環境です: {exc}")

            catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")
            _language_jar(target, "Changed target with a different size")

            with self.assertRaisesRegex(TranslationError, "再解析"):
                assert_glossary_inputs_unchanged(catalog.input_snapshot)

    def test_reparse_state_tracks_followed_target_metadata(self) -> None:
        path = Path("virtual-mod.jar")
        link_stat = SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o777,
            st_size=14,
            st_mtime=1.0,
            st_mtime_ns=1,
            st_ctime=1.0,
            st_ctime_ns=1,
            st_dev=1,
            st_ino=2,
            st_file_attributes=0,
        )

        def target_state(size: int, modified: int) -> SimpleNamespace:
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644,
                st_size=size,
                st_mtime=float(modified),
                st_mtime_ns=modified,
                st_ctime=float(modified),
                st_ctime_ns=modified,
                st_dev=3,
                st_ino=4,
                st_file_attributes=0,
            )

        with (
            mock.patch.object(Path, "stat", return_value=target_state(100, 10)),
            mock.patch.object(
                glossary_snapshot_module,
                "_resolved_path",
                return_value="resolved-mod.jar",
            ),
        ):
            original = glossary_snapshot_module._state_from_stat(
                path,
                link_stat,
                follow_reparse_target=True,
            )
        with (
            mock.patch.object(Path, "stat", return_value=target_state(200, 20)),
            mock.patch.object(
                glossary_snapshot_module,
                "_resolved_path",
                return_value="resolved-mod.jar",
            ),
        ):
            changed = glossary_snapshot_module._state_from_stat(
                path,
                link_stat,
                follow_reparse_target=True,
            )

        snapshot = GlossaryInputSnapshot(
            paths=(
                GlossaryPathWatch(
                    "Mod archive",
                    str(path),
                    original,
                    follow_reparse_target=True,
                ),
            )
        )
        with mock.patch.object(
            glossary_snapshot_module,
            "_path_state",
            return_value=changed,
        ):
            with self.assertRaisesRegex(TranslationError, "再解析"):
                assert_glossary_inputs_unchanged(snapshot)

    def test_rejected_external_reparse_does_not_stat_its_target(self) -> None:
        reparse_stat = SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o777,
            st_size=14,
            st_mtime=1.0,
            st_mtime_ns=1,
            st_ctime=1.0,
            st_ctime_ns=1,
            st_dev=1,
            st_ino=2,
            st_file_attributes=0,
        )
        with (
            mock.patch.object(
                glossary_snapshot_module,
                "_followed_state",
                side_effect=AssertionError("rejected external link was followed"),
            ),
            mock.patch.object(
                glossary_snapshot_module,
                "_resolved_path",
                side_effect=AssertionError("rejected external link was resolved"),
            ),
            mock.patch.object(
                glossary_snapshot_module,
                "_unfollowed_reparse_identity",
                return_value="unfollowed-assets-link",
            ),
        ):
            for label in ("KubeJS assets", "resource pack"):
                with self.subTest(label=label):
                    state = glossary_snapshot_module._state_from_stat(
                        Path(label),
                        reparse_stat,
                        follow_reparse_target=False,
                    )
                    self.assertTrue(state.reparse)
                    self.assertIsNone(state.followed)

    def test_unrelated_mod_and_resourcepack_files_do_not_invalidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _language_jar(mods / "test.jar")
            pack = game_root / "resourcepacks" / "pack"
            pack_lang = pack / "assets" / "test" / "lang"
            pack_lang.mkdir(parents=True)
            (pack_lang / "en_us.json").write_text(
                json.dumps({"item.test.pack_tool": "Pack Tool"}),
                encoding="utf-8",
            )
            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

            (mods / "readme.txt").write_text("not an archive", encoding="utf-8")
            (game_root / "resourcepacks" / "notes.txt").write_text(
                "not a pack",
                encoding="utf-8",
            )
            (game_root / "resourcepacks" / "empty-folder").mkdir()
            (pack / "pack.mcmeta").write_text("{}", encoding="utf-8")
            assert_glossary_inputs_unchanged(catalog.input_snapshot)

    def test_kubejs_missing_creation_and_existing_file_change_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
            )
            (game_root / "kubejs").mkdir()
            # Merely creating an empty conventional folder does not change
            # which language assets the scanner would see.
            assert_glossary_inputs_unchanged(catalog.input_snapshot)
            lang = game_root / "kubejs" / "assets" / "kube" / "lang" / "en_us.json"
            lang.parent.mkdir(parents=True)
            lang.write_text(json.dumps({"item.kube.tool": "Kube Tool"}), encoding="utf-8")
            with self.assertRaisesRegex(TranslationError, "KubeJS"):
                assert_glossary_inputs_unchanged(catalog.input_snapshot)

            fresh = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
            )
            lang.write_text(
                json.dumps({"item.kube.tool": "Changed Kube Tool"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TranslationError, "KubeJS assets"):
                assert_glossary_inputs_unchanged(fresh.input_snapshot)

    def test_resourcepack_changes_are_ignored_when_off_and_detected_when_on(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            disabled = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=False,
            )
            pack_lang = (
                game_root
                / "resourcepacks"
                / "test-pack"
                / "assets"
                / "test"
                / "lang"
                / "en_us.json"
            )
            pack_lang.parent.mkdir(parents=True)
            pack_lang.write_text(
                json.dumps({"item.test.pack_tool": "Pack Tool"}),
                encoding="utf-8",
            )
            assert_glossary_inputs_unchanged(disabled.input_snapshot)
            assert disabled.input_snapshot is not None
            self.assertFalse(disabled.input_snapshot.resourcepacks_included)

            enabled = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )
            pack_lang.write_text(
                json.dumps({"item.test.pack_tool": "Changed Pack Tool"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TranslationError, "resourcepacks"):
                assert_glossary_inputs_unchanged(enabled.input_snapshot)

    def test_resourcepack_candidate_creation_is_detected_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            (game_root / "resourcepacks").mkdir()
            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )
            _write_zip(
                game_root / "resourcepacks" / "added.zip",
                {
                    "assets/test/lang/en_us.json": json.dumps(
                        {"item.test.tool": "Pack Tool"}
                    )
                },
            )
            with self.assertRaisesRegex(TranslationError, "resourcepacks"):
                assert_glossary_inputs_unchanged(catalog.input_snapshot)

    def test_minecraft_observed_metadata_client_index_and_object_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            instance = launcher / "instances" / "Pack"
            paths = _write_minecraft_assets(launcher, "1.20.1")
            with mock.patch.object(
                minecraft_assets_module,
                "_launcher_roots",
                return_value=(launcher,),
            ):
                catalog = ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                    instance_root=instance,
                )

            assert catalog.input_snapshot is not None
            watched = {Path(watch.path) for watch in catalog.input_snapshot.paths}
            self.assertTrue(set(paths.values()).issubset(watched))
            # The higher-priority alternative candidates are retained even
            # when missing at analysis time.
            self.assertIn(
                launcher / "versions" / "1.20.1" / "1.20.1.json",
                watched,
            )
            self.assertIn(
                launcher / "versions" / "1.20.1" / "1.20.1.jar",
                watched,
            )

            paths["target"].write_bytes(paths["target"].read_bytes() + b" ")
            with self.assertRaisesRegex(TranslationError, "Minecraft language object"):
                assert_glossary_inputs_unchanged(catalog.input_snapshot)

    def test_new_minecraft_candidate_invalidates_the_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            instance = launcher / "instances" / "Pack"
            paths = _write_minecraft_assets(launcher, "1.20.1")
            lower_priority = launcher / "versions" / "1.20.1" / "1.20.1.json"
            lower_priority.parent.mkdir(parents=True)
            paths["metadata"].replace(lower_priority)
            with mock.patch.object(
                minecraft_assets_module,
                "_launcher_roots",
                return_value=(launcher,),
            ):
                catalog = ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                    instance_root=instance,
                )
            # The preferred meta/ candidate was missing during analysis.  Its
            # later creation would change which version descriptor is loaded.
            alternative = paths["metadata"]
            alternative.parent.mkdir(parents=True, exist_ok=True)
            alternative.write_bytes(lower_priority.read_bytes())
            with self.assertRaisesRegex(TranslationError, "metadata candidate"):
                assert_glossary_inputs_unchanged(catalog.input_snapshot)

    def test_cancel_and_catalog_clone_preserve_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            _language_jar(mods / "test.jar")
            catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")
            projected = catalog.with_source_preserved_terms(["Quest Book Name"])
            self.assertIs(projected.input_snapshot, catalog.input_snapshot)

            cancel = Event()
            cancel.set()
            with self.assertRaises(CancelledError):
                assert_glossary_inputs_unchanged(catalog.input_snapshot, cancel)

    def test_snapshot_inventory_stops_at_the_safety_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            assets = game_root / "kubejs" / "assets"
            assets.mkdir(parents=True)
            for index in range(3):
                (assets / f"ignored-{index}.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(TranslationError, "1資産内.*2件"):
                recorder = GlossaryInputRecorder(
                    scan_limits=GlossaryScanLimits(max_source_members=2)
                )
                recorder.capture_external_inputs(
                    game_root,
                    include_resourcepacks=False,
                )

    def test_disabled_limits_allow_large_inventories_and_still_detect_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            mods.mkdir()
            archives = tuple(mods / f"mod-{index}.jar" for index in range(3))
            for archive in archives:
                archive.write_bytes(b"inventory only")

            kubejs_assets = game_root / "kubejs" / "assets"
            kubejs_assets.mkdir(parents=True)
            kubejs_files = tuple(
                kubejs_assets / f"entry-{index}.txt" for index in range(3)
            )
            for entry in kubejs_files:
                entry.write_text("x", encoding="utf-8")

            resourcepacks = game_root / "resourcepacks"
            resourcepacks.mkdir()
            for index in range(3):
                (resourcepacks / f"pack-{index}.zip").write_bytes(b"inventory only")
            folder_assets = resourcepacks / "folder-pack" / "assets"
            folder_assets.mkdir(parents=True)
            for index in range(3):
                (folder_assets / f"entry-{index}.txt").write_text(
                    "x",
                    encoding="utf-8",
                )

            limits = GlossaryScanLimits(enabled=False, max_source_members=2)
            recorder = GlossaryInputRecorder(scan_limits=limits)
            recorder.capture_mod_inputs(mods, archives)
            recorder.capture_external_inputs(
                game_root,
                include_resourcepacks=True,
            )
            snapshot = recorder.freeze()

            self.assertIsNone(limits.effective_max_source_members)
            watches = {watch.label: watch for watch in snapshot.inventories}
            self.assertGreater(len(watches["Mod archives"].state.entries), 2)
            self.assertGreater(watches["KubeJS assets"].state.entry_count, 2)
            self.assertGreater(watches["resourcepacks"].state.entry_count, 2)
            assert_glossary_inputs_unchanged(snapshot)

            kubejs_files[0].write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(TranslationError, "KubeJS assets"):
                assert_glossary_inputs_unchanged(snapshot)

    def test_snapshot_member_limit_is_independent_across_inventories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            mods.mkdir()
            archive = mods / "one.jar"
            archive.write_bytes(b"not parsed by the recorder")
            assets = game_root / "kubejs" / "assets"
            assets.mkdir(parents=True)
            (assets / "one.txt").write_text("x", encoding="utf-8")
            resourcepacks = game_root / "resourcepacks"
            resourcepacks.mkdir()
            (resourcepacks / "one.zip").write_bytes(b"x")

            limits = GlossaryScanLimits(max_source_members=1)
            recorder = GlossaryInputRecorder(scan_limits=limits)
            recorder.capture_mod_inputs(mods, (archive,))
            recorder.capture_external_inputs(
                game_root,
                include_resourcepacks=True,
            )
            snapshot = recorder.freeze()

            (resourcepacks / "two.zip").write_bytes(b"x")
            with self.assertRaisesRegex(TranslationError, "1資産内.*1件"):
                assert_glossary_inputs_unchanged(snapshot)

            changed_recorder = GlossaryInputRecorder(scan_limits=limits)
            changed_recorder.capture_mod_inputs(mods, (archive,))
            with self.assertRaisesRegex(TranslationError, "1資産内.*1件"):
                changed_recorder.capture_external_inputs(
                    game_root,
                    include_resourcepacks=True,
                )

    def test_resourcepack_folders_use_bounded_streaming_digest_per_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            resourcepacks = game_root / "resourcepacks"
            for pack_name in ("pack-a", "pack-b"):
                language = resourcepacks / pack_name / "assets" / "test" / "lang"
                language.mkdir(parents=True)
                (language / "en_us.json").write_text("{}", encoding="utf-8")

            limits = GlossaryScanLimits(max_source_members=3)
            recorder = GlossaryInputRecorder(scan_limits=limits)
            recorder.capture_external_inputs(game_root, include_resourcepacks=True)
            snapshot = recorder.freeze()
            pack_watch = next(
                watch for watch in snapshot.inventories if watch.label == "resourcepacks"
            )

            self.assertEqual(pack_watch.state.entries, ())
            self.assertTrue(pack_watch.state.digest)
            self.assertGreater(pack_watch.state.entry_count, limits.max_source_members)
            assert_glossary_inputs_unchanged(snapshot)

    def test_one_resourcepack_folder_cannot_exceed_per_source_snapshot_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            language = (
                game_root
                / "resourcepacks"
                / "limited-pack"
                / "assets"
                / "test"
                / "lang"
            )
            language.mkdir(parents=True)
            (language / "en_us.json").write_text("{}", encoding="utf-8")

            recorder = GlossaryInputRecorder(
                scan_limits=GlossaryScanLimits(max_source_members=2)
            )
            with self.assertRaisesRegex(
                TranslationError,
                "(?s)1資産内.*2件.*limited-pack",
            ):
                recorder.capture_external_inputs(
                    game_root,
                    include_resourcepacks=True,
                )

    def test_kubejs_symlink_ancestor_is_recorded_without_assets_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory) / "game"
            game_root.mkdir()
            kubejs = game_root / "kubejs"
            original_path_state = glossary_snapshot_module._path_state

            def guarded_path_state(
                path: Path,
                *,
                follow_reparse_target: bool = False,
            ) -> GlossaryFileState:
                if Path(path) == kubejs:
                    self.assertFalse(follow_reparse_target)
                    return GlossaryFileState(
                        status="present",
                        kind="directory",
                        reparse=True,
                        resolved_path="outside-kubejs",
                    )
                return original_path_state(
                    path,
                    follow_reparse_target=follow_reparse_target,
                )

            recorder = GlossaryInputRecorder()
            with (
                mock.patch.object(
                    glossary_snapshot_module,
                    "_path_state",
                    side_effect=guarded_path_state,
                ),
                mock.patch.object(
                    glossary_snapshot_module,
                    "_capture_directory_inventory",
                    side_effect=AssertionError("unsafe assets tree was traversed"),
                ),
            ):
                recorder.capture_external_inputs(game_root, include_resourcepacks=False)
                snapshot = recorder.freeze()
                assert_glossary_inputs_unchanged(snapshot)
            kube_watch = next(
                watch for watch in snapshot.inventories if watch.label == "KubeJS assets"
            )
            self.assertEqual(kube_watch.state.entries, ())
            self.assertEqual(
                kube_watch.state.error_identity,
                "",
            )
            self.assertEqual(
                kube_watch.state.root_state.error_identity,
                "guarded-ancestor-is-not-a-safe-directory",
            )


if __name__ == "__main__":
    unittest.main()
