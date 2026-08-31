from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mq_localizer.glossary as glossary_module  # noqa: E402
from mq_localizer.glossary import (  # noqa: E402
    GlossaryCatalog,
    GlossaryEntry,
    GlossaryScanProgress,
    ModLanguageScanner,
)


def _write_jar(path: Path, files: dict[str, str | bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in files.items():
            data = contents.encode("utf-8") if isinstance(contents, str) else contents
            archive.writestr(name, data)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _complete_mod_files(mod_id: str, display_name: str) -> dict[str, str]:
    return {
        "fabric.mod.json": _json(
            {"schemaVersion": 1, "id": mod_id, "name": display_name}
        ),
        f"assets/{mod_id}/lang/en_us.json": _json(
            {f"item.{mod_id}.wrench": f"{display_name} Wrench"}
        ),
        f"assets/{mod_id}/lang/ja_jp.json": _json(
            {f"item.{mod_id}.wrench": f"{display_name}レンチ"}
        ),
    }


class GlossaryCoverageTests(unittest.TestCase):
    def test_no_archives_is_distinct_from_failed_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = ModLanguageScanner().scan(Path(directory), "en_us", "ja_jp")

        coverage = catalog.coverage
        self.assertEqual(coverage.scan_state, "no_archives")
        self.assertEqual(coverage.discovered_archives, 0)
        self.assertEqual(coverage.scanned_archives, 0)
        self.assertEqual(coverage.failed_archives, 0)
        self.assertFalse(coverage.has_protection)

    def test_all_discovered_archives_can_fail_without_looking_undiscovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "broken.jar"
            bad.write_bytes(b"not a zip archive")
            catalog = ModLanguageScanner().scan(Path(directory), "en_us", "ja_jp")

        coverage = catalog.coverage
        self.assertEqual(coverage.scan_state, "all_failed")
        self.assertEqual(coverage.discovered_archives, 1)
        self.assertEqual(coverage.scanned_archives, 0)
        self.assertEqual(coverage.failed_archives, 1)
        self.assertEqual(coverage.skipped_archives, 0)
        self.assertFalse(coverage.has_protection)

    def test_partial_archive_failure_preserves_successful_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jar(root / "a-good.jar", _complete_mod_files("example", "Example Mod"))
            (root / "b-broken.jar").write_bytes(b"not a zip archive")
            catalog = ModLanguageScanner().scan(root, "en_us", "ja_jp")

        coverage = catalog.coverage
        self.assertEqual(coverage.scan_state, "partial_failure")
        self.assertEqual(coverage.discovered_archives, 2)
        self.assertEqual(coverage.scanned_archives, 1)
        self.assertEqual(coverage.failed_archives, 1)
        self.assertEqual(coverage.term_state, "mod_names_and_official_terms")
        self.assertTrue(coverage.has_protection)
        self.assertIn("Example Mod", catalog.entries)

    def test_scan_budget_distinguishes_failed_and_unattempted_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            language = _json({"item.example.tool": "Budget Tool"})
            for index, name in enumerate(("a", "b", "c"), start=1):
                _write_jar(
                    root / f"{name}.jar",
                    {
                        "fabric.mod.json": _json(
                            {
                                "schemaVersion": 1,
                                "id": name,
                                "name": f"Budget Mod {index}",
                            }
                        ),
                        f"assets/{name}/lang/en_us.json": language,
                    },
                )
            with mock.patch.object(
                glossary_module,
                "_MAX_SCAN_LANGUAGE_BYTES",
                len(language.encode("utf-8")),
            ):
                catalog = ModLanguageScanner().scan(root, "en_us", "ja_jp")

        coverage = catalog.coverage
        self.assertEqual(coverage.scan_state, "partial_failure")
        self.assertEqual(coverage.discovered_archives, 3)
        self.assertEqual(coverage.scanned_archives, 1)
        self.assertEqual(coverage.failed_archives, 1)
        self.assertEqual(coverage.skipped_archives, 1)

    def test_mod_display_names_only_are_real_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "display-only.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "display", "name": "Display Mod"}
                    )
                },
            )
            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        coverage = catalog.coverage
        self.assertEqual(coverage.scan_state, "complete")
        self.assertEqual(coverage.term_state, "mod_names_only")
        self.assertEqual(coverage.mod_display_names, 1)
        self.assertEqual(coverage.official_terms, 0)
        self.assertTrue(coverage.has_protection)
        self.assertFalse(coverage.has_partial_warnings)

    def test_official_terms_only_are_partial_coverage_not_no_protection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "language-only.jar"
            _write_jar(
                jar,
                {
                    "assets/example/lang/en_us.json": _json(
                        {"item.example.wrench": "Official Wrench"}
                    ),
                    "assets/example/lang/ja_jp.json": _json(
                        {"item.example.wrench": "公式レンチ"}
                    ),
                },
            )
            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        coverage = catalog.coverage
        self.assertEqual(coverage.scan_state, "complete")
        self.assertEqual(coverage.term_state, "official_terms_only")
        self.assertEqual(coverage.mod_display_names, 0)
        self.assertEqual(coverage.official_terms, 1)
        self.assertTrue(coverage.has_protection)
        self.assertTrue(coverage.has_partial_warnings)
        self.assertEqual(coverage.archives_with_warnings, 1)
        self.assertEqual(coverage.partial_warning_count, 1)
        self.assertIn("1件走査成功", coverage.summary)
        self.assertIn("公式用語: 1件", coverage.summary)

    def test_complete_scan_reports_both_protection_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "complete.jar"
            _write_jar(jar, _complete_mod_files("complete", "Complete Mod"))
            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        coverage = catalog.coverage
        self.assertEqual(coverage.scan_state, "complete")
        self.assertEqual(coverage.term_state, "mod_names_and_official_terms")
        self.assertEqual(coverage.discovered_archives, 1)
        self.assertEqual(coverage.scanned_archives, 1)
        self.assertEqual(coverage.failed_archives, 0)
        self.assertEqual(coverage.partial_warning_count, 0)
        self.assertTrue(coverage.has_protection)
        self.assertEqual(catalog.coverage_summary(), coverage.summary)

    def test_minecraft_asset_warning_is_reported_as_partial_coverage(self) -> None:
        catalog = GlossaryCatalog(minecraft_asset_warning_count=2)

        self.assertTrue(catalog.coverage.has_partial_warnings)
        self.assertEqual(catalog.coverage.minecraft_asset_warning_count, 2)
        self.assertIn("Minecraft公式言語資産の読取警告 2件", catalog.coverage.summary)

    def test_progress_reports_each_attempt_before_and_after(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jar(root / "a-good.jar", _complete_mod_files("good", "Good Mod"))
            (root / "b-broken.jar").write_bytes(b"not a zip archive")
            observed: list[GlossaryScanProgress] = []

            catalog = ModLanguageScanner().scan(
                root,
                "en_us",
                "ja_jp",
                progress=observed.append,
            )

        self.assertEqual(catalog.coverage.scan_state, "partial_failure")
        self.assertEqual(
            [
                (event.current, event.total, event.archive_name, event.phase)
                for event in observed
            ],
            [
                (1, 2, "a-good.jar", "before"),
                (1, 2, "a-good.jar", "after"),
                (2, 2, "b-broken.jar", "before"),
                (2, 2, "b-broken.jar", "after"),
            ],
        )

    def test_progress_callback_exceptions_abort_and_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "callback.jar"
            _write_jar(jar, _complete_mod_files("callback", "Callback Mod"))

            def fail(_progress: GlossaryScanProgress) -> None:
                raise RuntimeError("progress callback failed")

            with self.assertRaisesRegex(RuntimeError, "progress callback failed"):
                ModLanguageScanner().scan(
                    jar,
                    "en_us",
                    "ja_jp",
                    progress=fail,
                )

    def test_progress_and_summary_distinguish_external_language_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(mods / "mod.jar", _complete_mod_files("mod", "Progress Mod"))
            language = game_root / "kubejs" / "assets" / "kubejs" / "lang"
            language.mkdir(parents=True)
            (language / "en_us.json").write_text(
                _json({"item.kubejs.tool": "Progress Kube Tool"}),
                encoding="utf-8",
            )
            observed: list[GlossaryScanProgress] = []

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
                progress=observed.append,
            )

        self.assertEqual(
            [
                (event.current, event.total, event.source_kind, event.phase)
                for event in observed
            ],
            [
                (1, 2, "mod_archive", "before"),
                (1, 2, "mod_archive", "after"),
                (2, 2, "kubejs", "before"),
                (2, 2, "kubejs", "after"),
            ],
        )
        coverage = catalog.coverage
        self.assertEqual(coverage.discovered_archives, 1)
        self.assertEqual(coverage.external_sources_discovered, 1)
        self.assertEqual(coverage.external_sources_scanned, 1)
        self.assertEqual(coverage.kubejs_sources_scanned, 1)
        self.assertEqual(coverage.resourcepack_sources_scanned, 0)
        self.assertTrue(coverage.resourcepacks_enabled)
        self.assertIn("Mod JAR: 1件検出", coverage.summary)
        self.assertIn("追加言語資産: 1件検出", coverage.summary)
        self.assertIn("resource pack 0件・走査有効", coverage.summary)

    def test_catalog_projections_preserve_external_coverage(self) -> None:
        catalog = GlossaryCatalog(
            external_sources_discovered=3,
            external_sources_scanned=2,
            external_sources_failed=1,
            external_sources_with_warnings=1,
            external_asset_warning_count=2,
            kubejs_sources_scanned=1,
            resourcepack_sources_scanned=1,
            resourcepacks_enabled=True,
        )

        projected = catalog.with_source_preserved_terms(["Unchecked Quest"])

        self.assertEqual(projected.coverage, catalog.coverage)
        self.assertEqual(projected.coverage.official_terms, 0)
        self.assertTrue(projected.coverage.has_partial_warnings)
        self.assertIn("2件走査成功", projected.coverage.summary)

    def test_resource_scoped_alias_preserves_coverage_and_source_tier(self) -> None:
        entry = GlossaryEntry(
            source="Tool Name",
            target="道具名",
            key="item.example.tool",
            mod_id="example",
            translated=True,
            provenance="KubeJS test",
            target_state="translated",
            source_tier="kubejs",
        )
        catalog = GlossaryCatalog(
            entries={entry.source: entry},
            evidence={entry.source: (entry,)},
            external_sources_discovered=2,
            external_sources_scanned=1,
            external_sources_failed=1,
            external_sources_with_warnings=1,
            external_asset_warning_count=1,
            kubejs_sources_scanned=1,
            resourcepacks_enabled=True,
            _contextual_filtering=False,
        )

        projected = catalog.scoped_for_resources(
            "Name Tool",
            ("example:tool",),
        )

        self.assertIsNot(projected, catalog)
        self.assertEqual(projected.entries["Name Tool"].target, "道具名")
        self.assertEqual(projected.entries["Name Tool"].source_tier, "kubejs")
        self.assertEqual(projected.coverage, catalog.coverage)
        self.assertEqual(projected.coverage.official_terms, 1)
        self.assertFalse(projected._contextual_filtering)


if __name__ == "__main__":
    unittest.main()
