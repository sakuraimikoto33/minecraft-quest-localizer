from __future__ import annotations

import io
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import CancelledError  # noqa: E402
import mq_localizer.glossary as glossary_module  # noqa: E402
import mq_localizer.minecraft_assets as minecraft_assets_module  # noqa: E402
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry, ModLanguageScanner  # noqa: E402
from mq_localizer.protection import TokenProtector, special_tokens  # noqa: E402
from mq_localizer.scan_limits import GlossaryScanLimits  # noqa: E402


def _write_jar(path: Path, files: dict[str, str | bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents.encode("utf-8") if isinstance(contents, str) else contents)


def _json(values: dict[str, object]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _synthetic_prefix(index: int) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    # Every synthetic term deliberately has the same first character.  The
    # previous one-character index therefore selected all 50,000 entries,
    # while the fixed-length prefix index selects only one small bucket.
    modulus = len(alphabet) ** 2
    value = index % modulus
    return (
        "A"
        + alphabet[(value // len(alphabet)) % len(alphabet)]
        + alphabet[value % len(alphabet)]
    )


def _write_minecraft_assets(
    root: Path,
    version: str,
    source_values: dict[str, str],
    target_values: dict[str, str],
) -> Path:
    client = (
        root
        / "libraries"
        / "com"
        / "mojang"
        / "minecraft"
        / version
        / f"minecraft-{version}-client.jar"
    )
    _write_jar(
        client,
        {"assets/minecraft/lang/en_us.json": _json(source_values)},
    )
    target_bytes = _json(target_values).encode("utf-8")
    digest = hashlib.sha1(target_bytes).hexdigest()
    target_object = root / "assets" / "objects" / digest[:2] / digest
    target_object.parent.mkdir(parents=True, exist_ok=True)
    target_object.write_bytes(target_bytes)
    index_id = "test-assets"
    index = root / "assets" / "indexes" / f"{index_id}.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index_bytes = _json(
        {
            "objects": {
                "minecraft/lang/ja_jp.json": {
                    "hash": digest,
                    "size": len(target_bytes),
                }
            }
        }
    ).encode("utf-8")
    index.write_bytes(index_bytes)
    index_digest = hashlib.sha1(index_bytes).hexdigest()
    metadata = root / "meta" / "net.minecraft" / f"{version}.json"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(
        _json(
            {
                "assetIndex": {
                    "id": index_id,
                    "sha1": index_digest,
                    "size": len(index_bytes),
                }
            }
        ),
        encoding="utf-8",
    )
    return target_object


class ModLanguageScannerTests(unittest.TestCase):
    def test_uppercase_archive_suffix_matches_snapshot_discovery_on_all_platforms(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "mods" / "UPPER.JAR"
            _write_jar(
                archive,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "upper", "name": "Upper Mod"}
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(root, "en_us", "ja_jp")

        self.assertEqual(catalog.discovered_archives, 1)
        self.assertEqual(catalog.scanned_archives, 1)
        self.assertIn("Upper Mod", catalog.entries)

    def test_configured_limits_are_not_forwarded_to_minecraft_loader(self) -> None:
        limits = GlossaryScanLimits(
            max_source_members=2,
            max_language_file_mib=1,
            max_source_language_mib=2,
            max_total_language_mib=3,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "limited.jar"
            _write_jar(
                archive,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "limited", "name": "Limited Mod"}
                    ),
                    "one.txt": "1",
                    "two.txt": "2",
                },
            )
            with mock.patch.object(
                glossary_module,
                "load_minecraft_language_bundle",
                return_value=None,
            ) as load_minecraft:
                catalog = ModLanguageScanner().scan(
                    archive,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                    instance_root=root,
                    limits=limits,
                )

        self.assertEqual(catalog.scanned_archives, 0)
        self.assertTrue(any("3 > 2" in warning for warning in catalog.warnings))
        self.assertNotIn("max_language_bytes", load_minecraft.call_args.kwargs)

    def test_configured_language_file_limit_does_not_reject_minecraft_asset_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory)
            _write_minecraft_assets(
                launcher,
                "1.20.1",
                {
                    "item.minecraft.limit_relic": "Minecraft Limit Relic",
                    "_padding_source": "B" * (1100 * 1024),
                },
                {
                    "item.minecraft.limit_relic": "マインクラフトの遺物",
                    "_padding_target": "A" * (1100 * 1024),
                },
            )
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
                    instance_root=launcher,
                    limits=GlossaryScanLimits(
                        max_language_file_mib=1,
                        max_source_language_mib=2,
                        max_total_language_mib=3,
                    ),
                )

        self.assertIn("Minecraft Limit Relic", catalog.entries)
        self.assertEqual(
            catalog.entries["Minecraft Limit Relic"].target,
            "マインクラフトの遺物",
        )
        self.assertTrue(catalog.entries["Minecraft Limit Relic"].translated)
        self.assertFalse(
            any("翻訳先公式言語 ja_jp" in warning for warning in catalog.warnings)
        )

    def test_minecraft_language_file_keeps_an_independent_fixed_safety_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory)
            _write_minecraft_assets(
                launcher,
                "1.20.1",
                {"item.minecraft.limit_relic": "Minecraft Limit Relic"},
                {
                    "item.minecraft.limit_relic": "マインクラフトの遺物",
                    "_padding": "A" * (1100 * 1024),
                },
            )
            with (
                mock.patch.object(
                    minecraft_assets_module,
                    "_launcher_roots",
                    return_value=(launcher,),
                ),
                mock.patch.object(
                    minecraft_assets_module,
                    "_MAX_MINECRAFT_LANGUAGE_BYTES",
                    1024 * 1024,
                ),
            ):
                catalog = ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                    instance_root=launcher,
                    limits=GlossaryScanLimits(
                        max_language_file_mib=2,
                        max_source_language_mib=2,
                        max_total_language_mib=3,
                    ),
                )

        self.assertEqual(
            catalog.entries["Minecraft Limit Relic"].target,
            "Minecraft Limit Relic",
        )
        self.assertFalse(catalog.entries["Minecraft Limit Relic"].translated)
        self.assertTrue(
            any(
                "翻訳先公式言語 ja_jp" in warning and "sizeが不正" in warning
                for warning in catalog.warnings
            )
        )

    def test_minecraft_client_locale_keeps_an_independent_fixed_safety_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory)
            _write_minecraft_assets(
                launcher,
                "1.20.1",
                {
                    "item.minecraft.limit_relic": "Minecraft Limit Relic",
                    "_padding_source": "B" * (1100 * 1024),
                },
                {"item.minecraft.limit_relic": "マインクラフトの遺物"},
            )
            with (
                mock.patch.object(
                    minecraft_assets_module,
                    "_launcher_roots",
                    return_value=(launcher,),
                ),
                mock.patch.object(
                    minecraft_assets_module,
                    "_MAX_MINECRAFT_LANGUAGE_BYTES",
                    1024 * 1024,
                ),
            ):
                catalog = ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                    instance_root=launcher,
                    limits=GlossaryScanLimits(
                        max_language_file_mib=2,
                        max_source_language_mib=2,
                        max_total_language_mib=3,
                    ),
                )

        self.assertNotIn("Minecraft Limit Relic", catalog.entries)
        self.assertTrue(
            any(
                "公式言語資産を読めませんでした" in warning
                and "サイズ上限を超えています" in warning
                for warning in catalog.warnings
            )
        )

    def test_configured_language_file_source_and_total_limits_are_independent(self) -> None:
        large_value = "A" * (600 * 1024)
        large_payload = _json({"item.test.large": large_value})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_limited = root / "file-limited.jar"
            _write_jar(
                file_limited,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "file", "name": "File Limited"}
                    ),
                    "assets/file/lang/en_us.json": _json(
                        {"item.file.large": "A" * (1024 * 1024)}
                    ),
                },
            )
            file_catalog = ModLanguageScanner().scan(
                file_limited,
                "en_us",
                "ja_jp",
                limits=GlossaryScanLimits(
                    max_language_file_mib=1,
                    max_source_language_mib=2,
                    max_total_language_mib=2,
                ),
            )

            source_limited = root / "source-limited.jar"
            _write_jar(
                source_limited,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "source", "name": "Source Limited"}
                    ),
                    "assets/one/lang/en_us.json": large_payload,
                    "assets/two/lang/en_us.json": large_payload,
                },
            )
            source_catalog = ModLanguageScanner().scan(
                source_limited,
                "en_us",
                "ja_jp",
                limits=GlossaryScanLimits(
                    max_language_file_mib=1,
                    max_source_language_mib=1,
                    max_total_language_mib=2,
                ),
            )

            mods = root / "total-mods"
            for name in ("a", "b"):
                _write_jar(
                    mods / f"{name}.jar",
                    {
                        "fabric.mod.json": _json(
                            {"schemaVersion": 1, "id": name, "name": f"{name} Mod"}
                        ),
                        f"assets/{name}/lang/en_us.json": large_payload,
                    },
                )
            total_catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                limits=GlossaryScanLimits(
                    max_language_file_mib=1,
                    max_source_language_mib=1,
                    max_total_language_mib=1,
                ),
            )

        self.assertTrue(any("JAR内の言語ファイル" in item for item in file_catalog.warnings))
        self.assertTrue(any("JAR単位の上限" in item for item in source_catalog.warnings))
        self.assertEqual(total_catalog.scanned_archives, 1)
        self.assertEqual(total_catalog.failed_archives, 1)
        self.assertTrue(any("全走査上限 1048576 bytes" in item for item in total_catalog.warnings))

    def test_disabling_configured_limits_removes_all_mod_scan_budgets(self) -> None:
        padding = "A" * (1100 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            for name in ("first", "second"):
                _write_jar(
                    mods / f"{name}.jar",
                    {
                        "fabric.mod.json": _json(
                            {
                                "schemaVersion": 1,
                                "id": name,
                                "name": f"{name.title()} Unlimited Mod",
                            }
                        ),
                        f"assets/{name}/lang/en_us.json": _json(
                            {
                                f"item.{name}.tool": f"{name.title()} Unlimited Tool",
                                "_padding": padding,
                            }
                        ),
                    },
                )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                limits=GlossaryScanLimits(
                    max_source_members=1,
                    max_language_file_mib=1,
                    max_source_language_mib=1,
                    max_total_language_mib=1,
                    enabled=False,
                ),
            )

        self.assertEqual(catalog.discovered_archives, 2)
        self.assertEqual(catalog.scanned_archives, 2)
        self.assertEqual(catalog.failed_archives, 0)
        self.assertIn("First Unlimited Tool", catalog.entries)
        self.assertIn("Second Unlimited Tool", catalog.entries)
        self.assertFalse(any("上限" in warning for warning in catalog.warnings))

    def test_disabling_configured_limits_removes_external_inventory_budgets(self) -> None:
        padding = "A" * (1100 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            kubejs_lang = game_root / "kubejs" / "assets" / "kubejs" / "lang"
            kubejs_lang.mkdir(parents=True)
            (kubejs_lang / "en_us.json").write_text(
                _json(
                    {
                        "item.kubejs.tool": "Unlimited Kube Tool",
                        "_padding": padding,
                    }
                ),
                encoding="utf-8",
            )
            (kubejs_lang / "ja_jp.json").write_text(
                _json({"item.kubejs.tool": "Kube訳"}),
                encoding="utf-8",
            )

            resourcepacks = game_root / "resourcepacks"
            folder_lang = resourcepacks / "folder" / "assets" / "folder" / "lang"
            folder_lang.mkdir(parents=True)
            (folder_lang / "en_us.json").write_text(
                _json(
                    {
                        "item.folder.tool": "Unlimited Folder Tool",
                        "_padding": padding,
                    }
                ),
                encoding="utf-8",
            )
            (folder_lang / "ja_jp.json").write_text(
                _json({"item.folder.tool": "Folder訳"}),
                encoding="utf-8",
            )
            _write_jar(
                resourcepacks / "zip.zip",
                {
                    "assets/zip/lang/en_us.json": _json(
                        {
                            "item.zip.tool": "Unlimited ZIP Tool",
                            "_padding": padding,
                        }
                    ),
                    "assets/zip/lang/ja_jp.json": _json(
                        {"item.zip.tool": "ZIP訳"}
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
                limits=GlossaryScanLimits(
                    max_source_members=1,
                    max_language_file_mib=1,
                    max_source_language_mib=1,
                    max_total_language_mib=1,
                    enabled=False,
                ),
            )

        self.assertEqual(catalog.external_sources_discovered, 3)
        self.assertEqual(catalog.external_sources_scanned, 3)
        self.assertEqual(catalog.external_sources_failed, 0)
        self.assertEqual(catalog.entries["Unlimited Kube Tool"].target, "Kube訳")
        self.assertEqual(catalog.entries["Unlimited Folder Tool"].target, "Folder訳")
        self.assertEqual(catalog.entries["Unlimited ZIP Tool"].target, "ZIP訳")

    def test_limited_archive_member_read_uses_limit_plus_one(self) -> None:
        class TrackingMember(io.BytesIO):
            requested = 0

            def read(self, size: int = -1) -> bytes:
                self.requested = size
                return super().read(size)

        class FakeInfo:
            file_size = 2

        class FakeArchive:
            def __init__(self) -> None:
                self.member = TrackingMember(b"four")

            def getinfo(self, name: str) -> FakeInfo:
                return FakeInfo()

            def open(self, info: FakeInfo, mode: str) -> TrackingMember:
                return self.member

        archive = FakeArchive()

        with self.assertRaisesRegex(ValueError, "実読込サイズ"):
            glossary_module._read_limited_archive_member(archive, "lang.json", 2, "test")

        self.assertEqual(archive.member.requested, 3)

    def test_scanner_enforces_zip_and_language_resource_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            too_many = root / "too-many.jar"
            _write_jar(
                too_many,
                {
                    "fabric.mod.json": _json({"schemaVersion": 1, "id": "many", "name": "Many Mod"}),
                    "one.txt": "1",
                    "two.txt": "2",
                },
            )
            with mock.patch.object(glossary_module, "_MAX_ARCHIVE_MEMBERS", 2):
                catalog = ModLanguageScanner().scan(too_many, "en_us", "ja_jp")
            self.assertEqual(catalog.scanned_archives, 0)
            self.assertTrue(
                any(
                    "JAR内のファイル・ディレクトリ項目数" in warning
                    for warning in catalog.warnings
                )
            )

            member_payload = _json({"item.member.tool": "Oversized Tool"})
            member_limited = root / "member-limited.jar"
            _write_jar(
                member_limited,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "member", "name": "Member Mod"}
                    ),
                    "assets/member/lang/en_us.json": member_payload,
                },
            )
            with mock.patch.object(
                glossary_module,
                "_MAX_LANGUAGE_MEMBER_BYTES",
                len(member_payload.encode("utf-8")) - 1,
            ):
                catalog = ModLanguageScanner().scan(member_limited, "en_us", "ja_jp")
            self.assertEqual(catalog.scanned_archives, 1)
            self.assertIn("Member Mod", catalog.entries)
            self.assertNotIn("Oversized Tool", catalog.entries)
            self.assertTrue(
                any("JAR内の言語ファイル" in warning for warning in catalog.warnings)
            )

            entry_limited = root / "entry-limited.jar"
            _write_jar(
                entry_limited,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "entries", "name": "Entries Mod"}
                    ),
                    "assets/entries/lang/en_us.json": _json(
                        {
                            "item.entries.one": "Entry One",
                            "item.entries.two": "Entry Two",
                            "item.entries.three": "Entry Three",
                        }
                    ),
                },
            )
            with mock.patch.object(glossary_module, "_MAX_LANGUAGE_ENTRIES_PER_MEMBER", 2):
                catalog = ModLanguageScanner().scan(
                    entry_limited,
                    "en_us",
                    "ja_jp",
                    limits=GlossaryScanLimits(enabled=False),
                )
            self.assertIn("Entries Mod", catalog.entries)
            self.assertNotIn("Entry One", catalog.entries)
            self.assertTrue(any("entry数" in warning for warning in catalog.warnings))

            first_payload = _json({"item.a.tool": "Archive Tool A"})
            second_payload = _json({"item.b.tool": "Archive Tool B"})
            archive_limited = root / "archive-limited.jar"
            _write_jar(
                archive_limited,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "container", "name": "Container Mod"}
                    ),
                    "assets/a/lang/en_us.json": first_payload,
                    "assets/b/lang/en_us.json": second_payload,
                },
            )
            declared_total = len(first_payload.encode("utf-8")) + len(second_payload.encode("utf-8"))
            with mock.patch.object(
                glossary_module,
                "_MAX_ARCHIVE_LANGUAGE_BYTES",
                declared_total - 1,
            ):
                catalog = ModLanguageScanner().scan(archive_limited, "en_us", "ja_jp")
            self.assertIn("Container Mod", catalog.entries)
            self.assertNotIn("Archive Tool A", catalog.entries)
            self.assertTrue(any("JAR単位の上限" in warning for warning in catalog.warnings))

            scan_root = root / "scan-budget"
            scan_payload = _json({"item.scan.tool": "Scan Tool"})
            for name in ("a", "b"):
                _write_jar(
                    scan_root / f"{name}.jar",
                    {
                        "fabric.mod.json": _json(
                            {"schemaVersion": 1, "id": name, "name": f"{name.upper()} Mod"}
                        ),
                        f"assets/{name}/lang/en_us.json": scan_payload,
                    },
                )
            with mock.patch.object(
                glossary_module,
                "_MAX_SCAN_LANGUAGE_BYTES",
                len(scan_payload.encode("utf-8")),
            ):
                catalog = ModLanguageScanner().scan(scan_root, "en_us", "ja_jp")
            self.assertEqual(catalog.scanned_archives, 1)
            self.assertIn("A Mod", catalog.entries)
            self.assertNotIn("B Mod", catalog.entries)
            self.assertTrue(any("全走査上限" in warning for warning in catalog.warnings))

    def test_duplicate_json_and_lang_keys_warn_and_use_safe_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "duplicate-lang.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "duplicates", "name": "Duplicates Mod"}
                    ),
                    "assets/jsondup/lang/en_us.json": (
                        '{"_comment":"first","item.jsondup.tool":"First Tool",'
                        '"_comment":"second","item.jsondup.tool":"Second Tool",'
                        '"block.jsondup.safe":"Safe JSON Block"}'
                    ),
                    "assets/jsondup/lang/ja_jp.json": _json(
                        {"block.jsondup.safe": "安全なJSONブロック"}
                    ),
                    "assets/langdup/lang/en_us.lang": (
                        "item.langdup.tool=Lang Tool\n"
                        "block.langdup.safe=Safe Lang Block\n"
                    ),
                    "assets/langdup/lang/ja_jp.lang": (
                        "item.langdup.tool=最初の道具\n"
                        "item.langdup.tool=二番目の道具\n"
                        "block.langdup.safe=安全なLangブロック\n"
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertIn("Duplicates Mod", catalog.entries)
        self.assertNotIn("First Tool", catalog.entries)
        self.assertNotIn("Second Tool", catalog.entries)
        self.assertEqual(catalog.entries["Safe JSON Block"].target, "安全なJSONブロック")
        self.assertEqual(catalog.entries["Lang Tool"].target, "Lang Tool")
        self.assertFalse(catalog.entries["Lang Tool"].translated)
        self.assertEqual(catalog.entries["Safe Lang Block"].target, "安全なLangブロック")
        self.assertEqual(len(catalog.warnings), 2)
        self.assertTrue(all("重複" in warning for warning in catalog.warnings))
        self.assertTrue(all("だけを除外" in warning for warning in catalog.warnings))
        self.assertTrue(all("他の用語は保護に利用" in warning for warning in catalog.warnings))
        self.assertFalse(any("_comment" in warning for warning in catalog.warnings))
        self.assertFalse(any("item.jsondup.tool" in warning for warning in catalog.warnings))
        self.assertFalse(any("item.langdup.tool" in warning for warning in catalog.warnings))
        details = "\n".join(catalog.debug_messages)
        self.assertIn('"_comment"', details)
        self.assertIn('"item.jsondup.tool"', details)
        self.assertIn('"item.langdup.tool"', details)

    def test_duplicate_key_summary_has_only_count_and_debug_has_every_key(self) -> None:
        keys = ("line\nbreak", "x" * 10_000)
        warning = glossary_module._duplicate_language_key_warning(
            "example.jar",
            "assets/example/lang/en_us.json",
            keys,
        )
        detail = glossary_module._duplicate_language_key_debug_message(
            "example.jar",
            "assets/example/lang/en_us.json",
            keys,
        )

        self.assertNotIn("\n", warning)
        self.assertIn("重複した2件", warning)
        self.assertNotIn(r"line\nbreak", warning)
        self.assertNotIn("x" * 100, warning)
        self.assertLess(len(warning), 500)
        self.assertIn(r"line\nbreak", detail)
        self.assertIn("x" * 10_000, detail)

    def test_json_duplicate_detection_uses_decoded_keys_and_discards_all_occurrences(self) -> None:
        parsed = glossary_module._read_lang_bytes(
            (
                '{"_comment":"same","_\\u0063omment":"same",'
                '"item.example.safe":"Safe Tool"}'
            ).encode("utf-8"),
            ".json",
        )

        self.assertEqual(parsed.duplicate_keys, ("_comment",))
        self.assertNotIn("_comment", parsed.values)
        self.assertEqual(parsed.values, {"item.example.safe": "Safe Tool"})

    def test_malformed_or_non_object_json_is_still_rejected(self) -> None:
        for payload in (
            b'{"item.example.tool":',
            b'["not", "an", "object"]',
            b'{"item.example.nan":NaN}',
            b'{"item.example.infinity":Infinity}',
            b'{"item.example.negative_infinity":-Infinity}',
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                glossary_module._read_lang_bytes(payload, ".json")

    def test_json_recovers_one_missing_comma_between_root_string_entries(self) -> None:
        payload = (
            '{\n'
            '  "item.example.first": "First { [ \\"quoted\\""\n'
            '  "item.example.second": "Second"\n'
            '}\n'
        ).encode("utf-8")

        parsed = glossary_module._read_lang_bytes(payload, ".json")

        self.assertEqual(
            parsed.values,
            {
                "item.example.first": 'First { [ "quoted"',
                "item.example.second": "Second",
            },
        )
        self.assertEqual(parsed.duplicate_keys, ())

    def test_json_comma_recovery_preserves_duplicate_key_detection(self) -> None:
        payload = (
            '{\n'
            '  "_comment": "First"\n'
            '  "_\\u0063omment": "Second",\n'
            '  "item.example.safe": "Safe"\n'
            '}\n'
        ).encode("utf-8")

        parsed = glossary_module._read_lang_bytes(payload, ".json")

        self.assertEqual(parsed.duplicate_keys, ("_comment",))
        self.assertEqual(parsed.values, {"item.example.safe": "Safe"})

    def test_json_comma_recovery_rejects_other_malformed_shapes(self) -> None:
        payloads = (
            b'{"outer":{"first":"First" "second":"Second"}}',
            b'{"outer":["First" "Second"]}',
            b'{"first":1 "second":"Second"}',
            b'{"first":true "second":"Second"}',
            b'{"first":null "second":"Second"}',
            b'{"first":{} "second":"Second"}',
            b'{"first":[] "second":"Second"}',
            b'{"first":"First" "second":2}',
            b'{"first":"First" "second"}',
            b'{"first":"First" "second":"Second" "third":"Third"}',
            b'{"first":"First" "second":"Second","invalid":NaN}',
            b'{"first":"First"',
            b'["First" "Second"]',
        )

        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                ValueError,
                r"言語JSONの構文エラー",
            ):
                glossary_module._read_lang_bytes(payload, ".json")

    def test_json_comma_recovery_keeps_the_jar_unchanged_and_uses_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "recoverable-language.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "recovery", "name": "Recovery Mod"}
                    ),
                    "assets/recovery/lang/en_us.json": _json(
                        {
                            "block.recovery.pink_force_field": "Magenta Force Field",
                            "item.recovery.safe_tool": "Safe Tool",
                        }
                    ),
                    "assets/recovery/lang/ja_jp.json": (
                        '{\n'
                        '  "block.recovery.pink_force_field": "赤紫のフォースフィールド"\n'
                        '  "item.recovery.safe_tool": "安全な道具"\n'
                        '}\n'
                    ),
                },
            )
            before = jar.read_bytes()

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

            self.assertEqual(jar.read_bytes(), before)

        self.assertEqual(
            catalog.entries["Magenta Force Field"].target,
            "赤紫のフォースフィールド",
        )
        self.assertEqual(catalog.entries["Safe Tool"].target, "安全な道具")
        self.assertEqual(catalog.warnings, [])

    def test_registry_child_and_description_keys_are_not_terminology(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "book-keys.jar"
            source_values = {
                "item.alexscaves.cave_book": "Cave Compendium",
                "item.alexscaves.cave_book.desc": "By Dr. Prof. Alexander Caverns, PhD.",
                "item.alexscaves.cave_book.general": "General",
                "item.alexscaves.cave_book.resources": "Resources",
                "item.alexscaves.cave_book.mobs": "Inhabitants",
                "item.alexscaves.cave_book.utilities": "Utilities",
                "item.alexscaves.cave_book.secrets": "Secrets",
                "item.alexscaves.orphan.desc": "A short orphan description.",
                "item.alexscaves.orphan_tooltip_2": "Short orphan tooltip",
                "item.alexscaves.natures_compass_warning": (
                    "Try using a Cave Biome Map to find this biome..."
                ),
                "item.alexscaves.general": "General Purpose Module",
                "item.alexscaves.field_manual": "Field Manual",
                "item.alexscaves.field_manual.desc": "Dotted Description Artifact",
                "item.alexscaves.tool": "Tool",
                "item.alexscaves.tool.hammer": "Forge Hammer",
                "item.alexscaves.tool.effect": "Tool applies a powerful effect nearby.",
                "item.alexscaves.long_resource_name": (
                    "The Very Long Yet Official Resource Backed Artifact Name Here"
                ),
                "block.alexscaves.abyssal_altar": "Abyssal Altar",
                "block.packagedauto.encoder": "Recipe Encoder",
                "block.packagedauto.encoder.recipe_type.prev": "Previous Recipe Type",
                "block.packagedauto.encoder.recipe_type.next": "Next Recipe Type",
                "enchantment.alexscaves.explosive_flavor": "Explosive Flavor",
                "effect.simplyswords.fatal_flicker": "Fatal Flicker, dashing forward.",
                "effect.alexscaves.multiline": "First line\nSecond line",
                "fluid.tconstruct.blazing_blood": "Blazing Blood",
                "fluid.tconstruct.blazing_blood.fluid_effect": (
                    "Blazing Blood sets nearby entities on fire."
                ),
                "entity.ad_astra.lunarian": "Lunarian",
                "entity.ad_astra.lunarian.armorer": "Lunarian Armorer",
                "entity.alexsmobs.terrapin": "Terrapin",
                "entity.alexsmobs.terrapin.variant_koopa": "Koopa",
                "item.elementalcraft.receptacle": "Source Receptacle",
                "item.elementalcraft.receptacle.fire": "Fire Source Receptacle",
                "item.mekanism.shield": "Shield",
                "item.mekanism.shield.red": "Red Shield",
                "block.minecraft.banner.runelic.runelic_period.light_blue": "Light Blue Rune .",
            }
            target_values = {
                key: f"訳: {value}" for key, value in source_values.items()
            }
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "alexscaves", "name": "Alex's Caves"}
                    ),
                    "assets/alexscaves/lang/en_us.json": _json(source_values),
                    "assets/alexscaves/lang/ja_jp.json": _json(target_values),
                    "assets/alexscaves/models/item/field_manual.desc.json": "{}",
                    "assets/alexscaves/models/item/tool/hammer.json": "{}",
                    "assets/alexscaves/models/item/long_resource_name.json": "{}",
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertIn("Cave Compendium", catalog.entries)
        self.assertIn("Field Manual", catalog.entries)
        self.assertIn("General Purpose Module", catalog.entries)
        self.assertIn("Explosive Flavor", catalog.entries)
        self.assertIn("Blazing Blood", catalog.entries)
        self.assertIn("Light Blue Rune .", catalog.entries)
        self.assertIn(
            "The Very Long Yet Official Resource Backed Artifact Name Here",
            catalog.entries,
        )
        self.assertIn("Abyssal Altar", catalog.entries)
        for derived_term in (
            "Dotted Description Artifact",
            "Forge Hammer",
            "Lunarian Armorer",
            "Koopa",
            "Fire Source Receptacle",
            "Red Shield",
        ):
            with self.subTest(derived_term=derived_term):
                self.assertIn(derived_term, catalog.entries)
        for non_term in (
            "By Dr. Prof. Alexander Caverns, PhD.",
            "General",
            "Resources",
            "Inhabitants",
            "Utilities",
            "Secrets",
            "A short orphan description.",
            "Short orphan tooltip",
            "Try using a Cave Biome Map to find this biome...",
            "Tool applies a powerful effect nearby.",
            "Previous Recipe Type",
            "Next Recipe Type",
            "Blazing Blood sets nearby entities on fire.",
            "Fatal Flicker, dashing forward.",
            "First line\nSecond line",
        ):
            with self.subTest(non_term=non_term):
                self.assertNotIn(non_term, catalog.entries)

    def test_cancel_is_checked_for_early_returns_and_after_the_last_archive(self) -> None:
        cancel = Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.mkdir()
            for location in (None, empty):
                with self.subTest(location=location), self.assertRaises(CancelledError):
                    ModLanguageScanner().scan(location, "en_us", "ja_jp", cancel)

            archive = Path(directory) / "last.jar"
            archive.write_bytes(b"read is overridden")
            cancel.clear()

            class CancellingScanner(ModLanguageScanner):
                def __init__(self) -> None:
                    self.calls = 0

                def _read_archive(
                    self,
                    jar_path: Path,
                    source_locale: str,
                    target_locale: str,
                    cancel_event: Event | None = None,
                    scan_language_budget: int | None = None,
                ) -> tuple[list[GlossaryEntry], list[str], int]:
                    self.calls += 1
                    cancel.set()
                    return [], [], 0

            scanner = CancellingScanner()
            with self.assertRaises(CancelledError):
                scanner.scan(archive, "en_us", "ja_jp", cancel)
            self.assertEqual(scanner.calls, 1)

    def test_longest_official_name_wins_over_contained_mod_name(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "Create": GlossaryEntry(
                    "Create", "Create", "mod.display_name.create", "create", False, "create.jar"
                ),
                "Create Wrench": GlossaryEntry(
                    "Create Wrench", "クリエイトレンチ", "item.create.wrench", "create", True, "create.jar"
                ),
                "CREATE": GlossaryEntry(
                    "CREATE", "クリエイト", "item.example.create", "example", True, "example.jar"
                ),
            }
        )
        source = "Use Create Wrench and CREATE."

        replacements = catalog.replacements_for(source)
        protected = TokenProtector().protect(source, replacements)

        self.assertEqual(
            replacements,
            {"Create Wrench": "クリエイトレンチ", "CREATE": "クリエイト"},
        )
        self.assertNotIn("Create Wrench", protected.protected)
        self.assertNotIn("CREATE", protected.protected)
        self.assertEqual(
            protected.restore(protected.protected),
            "Use クリエイトレンチ and クリエイト.",
        )

    def test_weak_terms_inside_unrelated_compound_names_are_not_protected(self) -> None:
        def entry(
            source: str,
            key: str,
            mod_id: str,
            target: str | None = None,
        ) -> GlossaryEntry:
            translated = target is not None and target != source
            return GlossaryEntry(
                source,
                target if translated else source,
                key,
                mod_id,
                translated,
                f"{mod_id}.jar!/assets/{mod_id}/lang/en_us.json",
            )

        terms = [
            entry("Reinforced", "effect.eidolon.reinforced", "eidolon"),
            entry("Controller", "block.xnet.controller", "xnet"),
            entry("Fluid Port", "item.xycraft.port_fluid", "xycraft"),
            entry("Basic Turbine", "block.generators.turbine_tier1", "generators"),
            entry("Reactor Glass", "block.mekanism.reactor_glass", "mekanism"),
            entry("Planets", "painting.darkpaintings.planets.title", "darkpaintings"),
            entry("Impostor", "entity.amongusdweller.among_us", "amongusdweller"),
            entry(
                "Crystallization",
                "enchantment.alexscaves.crystallization",
                "alexscaves",
            ),
            entry("Black Hole", "block.forbidden.black_hole", "forbidden"),
            entry("Keys", "tag.item.supplementaries.keys", "supplementaries"),
            entry("Vanilla", "item.croptopia.vanilla", "croptopia"),
            entry("Collector", "block.xycraft.collector", "xycraft"),
            GlossaryEntry(
                "Blueprint",
                "Blueprint",
                "mod.display_name.blueprint",
                "blueprint",
                False,
                "blueprint.jar!/META-INF/mods.toml",
            ),
            GlossaryEntry(
                "Citadel",
                "Citadel",
                "mod.display_name.citadel",
                "citadel",
                False,
                "citadel.jar!/META-INF/mods.toml",
            ),
            GlossaryEntry(
                "spark",
                "spark",
                "mod.display_name.spark",
                "spark",
                False,
                "spark.jar!/META-INF/mods.toml",
            ),
            entry(
                "Engineer's Blueprint",
                "item.immersiveengineering.blueprint",
                "immersiveengineering",
                "エンジニアの設計図",
            ),
            # Independent terminology supplies corpus evidence without a
            # hand-maintained English-word blacklist.
            entry("Reinforced Frame", "block.other.frame", "other1"),
            entry("Machine Controller", "block.other.controller", "other2"),
            entry("Fluid Tank", "block.other.tank", "other3"),
            entry("Item Port", "block.other.port", "other4"),
            entry("Basic Machine", "block.other.machine", "other5"),
            entry("Steam Turbine", "block.other.turbine", "other6"),
            entry("Reactor Casing", "block.other.casing", "other7"),
            entry("Glass Pane", "block.other.pane", "other8"),
            entry("Planet Drill", "block.other.drill", "other9"),
            entry("Black Quartz", "item.other.quartz", "other10"),
            entry("Worm Hole", "block.other.hole", "other11"),
            entry("Vanilla Extract", "item.other.extract", "other12"),
            entry("Heat Collector", "block.other.collector", "other13"),
        ]
        crystallization_conflict = entry(
            "Crystallization",
            "effect.aquamirae.crystallization",
            "aquamirae",
        )
        catalog = GlossaryCatalog(
            entries={term.source: term for term in terms},
            conflicts={
                "Crystallization": [
                    next(term for term in terms if term.source == "Crystallization"),
                    crystallization_conflict,
                ]
            },
        )

        for source in (
            "&#505050Reinforced Reactor Controller&r",
            "&#505050Reinforced Reactor Glass&r",
            "&#505050Basic Turbine Controller&r",
            "Reinforced Fluid Port",
            "Planets \\& Dimensions",
            "Impostor Syndrome",
            "Ruined Citadel",
            "Botania Spark Augments",
            "Crystallization Ritual",
            "Crystallization recipe data belongs to another mod.",
            "Common Black Hole Storage",
            "The Keys to the End",
            "Vanilla+",
        ):
            with self.subTest(source=source):
                self.assertEqual(catalog.replacements_for(source), {})

        self.assertEqual(
            catalog.replacements_for("Craft any Engineer's Blueprint"),
            {"Engineer's Blueprint": "エンジニアの設計図"},
        )
        self.assertEqual(
            catalog.replacements_for("Any Engineer's Blueprint"),
            {"Engineer's Blueprint": "エンジニアの設計図"},
        )

    def test_a_whole_title_style_does_not_scope_each_edge_word(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "Reinforced": GlossaryEntry(
                    "Reinforced", "Reinforced", "effect.a.reinforced", "a", False, "a.jar"
                ),
                "Reinforced Frame": GlossaryEntry(
                    "Reinforced Frame",
                    "Reinforced Frame",
                    "block.b.frame",
                    "b",
                    False,
                    "b.jar",
                ),
                "Pressure": GlossaryEntry(
                    "Pressure", "Pressure", "item.c.pressure", "c", False, "c.jar"
                ),
                "Pressure Valve": GlossaryEntry(
                    "Pressure Valve",
                    "Pressure Valve",
                    "block.d.valve",
                    "d",
                    False,
                    "d.jar",
                ),
            }
        )

        self.assertEqual(
            catalog.replacements_for("&#505050Reinforced Reactor&r"),
            {},
        )
        self.assertEqual(
            catalog.replacements_for("Use &ePressure&r carefully."),
            {"Pressure": "Pressure"},
        )

    def test_identical_labels_from_multiple_mods_are_common_evidence(self) -> None:
        first = GlossaryEntry(
            "Impostor",
            "Impostor",
            "entity.first.impostor",
            "first",
            False,
            "first.jar",
        )
        second = GlossaryEntry(
            "Impostor",
            "Impostor",
            "entity.second.impostor",
            "second",
            False,
            "second.jar",
        )
        catalog = GlossaryCatalog(
            entries={"Impostor": first},
            evidence={"Impostor": (first, second)},
        )

        self.assertEqual(catalog.replacements_for("An Impostor appeared."), {})
        self.assertEqual(
            catalog.replacements_for("Impostor"),
            {"Impostor": "Impostor"},
        )

    def test_lists_suffix_numbers_and_of_phrases_keep_complete_official_terms(self) -> None:
        def term(source: str, target: str, mod_id: str) -> GlossaryEntry:
            return GlossaryEntry(
                source,
                target,
                f"item.{mod_id}.{source.casefold().replace(' ', '_')}",
                mod_id,
                target != source,
                f"{mod_id}.jar",
            )

        entries = [
            term("Pink Slime", "ピンクスライム", "industrialforegoing"),
            term("Liquid Meat", "液体肉", "industrialforegoing"),
            term("Wooden Altar", "木の祭壇", "eidolon"),
            term("Straw Effigy", "藁の人形", "eidolon"),
            term("Guardian of Gaia", "ガイアの守護者", "botania"),
            term("Golden Fields", "黄金の野原", "paintings"),
            term("Pink Dye", "桃色の染料", "minecraft"),
            term("Blue Slime", "青いスライム", "tconstruct"),
            term("Energizer", "Energizer", "ambiguous"),
            term("Enervator", "アイテム放電機", "actuallyadditions"),
            term("Machine Energizer", "機械充電器", "other"),
            term("Advanced Enervator", "高度な放電機", "other2"),
        ]
        catalog = GlossaryCatalog(entries={entry.source: entry for entry in entries})

        self.assertEqual(
            catalog.replacements_for("Pink Slime \\& Liquid Meat"),
            {"Pink Slime": "ピンクスライム", "Liquid Meat": "液体肉"},
        )
        self.assertEqual(
            catalog.replacements_for("Wooden Altar and Straw Effigy"),
            {"Wooden Altar": "木の祭壇", "Straw Effigy": "藁の人形"},
        )
        self.assertEqual(
            catalog.replacements_for("&5Guardian of Gaia 2.0&r"),
            {"Guardian of Gaia": "ガイアの守護者"},
        )
        self.assertEqual(
            catalog.replacements_for("Golden Fields of Alfheim"),
            {"Golden Fields": "黄金の野原"},
        )
        self.assertEqual(
            catalog.replacements_for("Energizer \\& Enervator"),
            {"Energizer": "Energizer", "Enervator": "アイテム放電機"},
        )

    def test_resource_scope_aliases_reordered_official_name_for_only_that_unit(self) -> None:
        official = GlossaryEntry(
            "Reactor Controller (Reinforced)",
            "原子炉制御装置 (強化)",
            "block.bigreactors.reinforced_reactorcontroller",
            "bigreactors",
            True,
            "bigreactors.jar!/assets/bigreactors/lang/en_us.json",
            "translated",
        )
        unrelated_reinforced = GlossaryEntry(
            "Reinforced",
            "Reinforced",
            "effect.eidolon.reinforced",
            "eidolon",
            False,
            "eidolon.jar",
        )
        unrelated_controller = GlossaryEntry(
            "Controller",
            "Controller",
            "block.xnet.controller",
            "xnet",
            False,
            "xnet.jar",
        )
        catalog = GlossaryCatalog(
            entries={
                entry.source: entry
                for entry in (
                    official,
                    unrelated_reinforced,
                    unrelated_controller,
                )
            },
            evidence={official.source: (official,)},
        )

        scoped = catalog.scoped_for_resources(
            "&#505050Reinforced Reactor Controller&r",
            ("bigreactors:reinforced_reactorcontroller",),
        )
        replacements = scoped.replacements_for(
            "&#505050Reinforced Reactor Controller&r"
        )
        protected = TokenProtector().protect(
            "&#505050Reinforced Reactor Controller&r",
            replacements,
        )

        self.assertEqual(
            replacements,
            {"Reinforced Reactor Controller": "原子炉制御装置 (強化)"},
        )
        self.assertEqual(
            protected.restore(protected.protected),
            "&#505050原子炉制御装置 (強化)&r",
        )
        self.assertNotIn("Reinforced Reactor Controller", catalog.entries)

    def test_resource_scope_keeps_custom_word_order_when_official_target_is_missing(self) -> None:
        official = GlossaryEntry(
            "Reactor Controller (Reinforced)",
            "Reactor Controller (Reinforced)",
            "block.bigreactors.reinforced_reactorcontroller",
            "bigreactors",
            False,
            "bigreactors.jar",
            "missing",
        )
        catalog = GlossaryCatalog(
            entries={official.source: official},
            evidence={official.source: (official,)},
        )

        scoped = catalog.scoped_for_resources(
            "Reinforced Reactor Controller",
            ("bigreactors:reinforced_reactorcontroller",),
        )

        self.assertEqual(
            scoped.replacements_for("Reinforced Reactor Controller"),
            {"Reinforced Reactor Controller": "Reinforced Reactor Controller"},
        )

    def test_resource_scope_does_not_erase_meaningful_tier_or_suffix_content(self) -> None:
        tank = GlossaryEntry(
            "Tank",
            "タンク",
            "block.example.tank",
            "example",
            True,
            "example.jar",
            "translated",
        )
        storage_part = GlossaryEntry(
            "64k Storage Part",
            "64kストレージパーツ",
            "item.example.part_64k",
            "example",
            True,
            "example.jar",
            "translated",
        )
        catalog = GlossaryCatalog(
            entries={tank.source: tank, storage_part.source: storage_part},
            evidence={tank.source: (tank,), storage_part.source: (storage_part,)},
        )

        cases = (
            ("Tank 2.0", "example:tank"),
            ("Tank 改", "example:tank"),
            ("Tank +", "example:tank"),
            ("Tank (", "example:tank"),
            ("Tank )", "example:tank"),
            ("16k Storage Part", "example:part_64k"),
        )
        for visible, resource_id in cases:
            with self.subTest(visible=visible):
                scoped = catalog.scoped_for_resources(visible, (resource_id,))
                self.assertNotIn(visible, scoped.entries)
                self.assertNotIn(visible, scoped.replacements_for(visible))

    def test_unsafe_alias_shape_still_suppresses_an_unrelated_whole_title(self) -> None:
        direct_tank = GlossaryEntry(
            "Tank",
            "タンク",
            "block.example.tank",
            "example",
            True,
            "example.jar",
            "translated",
        )
        unrelated_tier = GlossaryEntry(
            "Tank 2.0",
            "別Modタンク",
            "block.other.tank_2",
            "other",
            True,
            "other.jar",
            "translated",
        )
        catalog = GlossaryCatalog(
            entries={
                direct_tank.source: direct_tank,
                unrelated_tier.source: unrelated_tier,
            },
            evidence={
                direct_tank.source: (direct_tank,),
                unrelated_tier.source: (unrelated_tier,),
            },
        )

        scoped = catalog.scoped_for_resources(
            "Tank 2.0",
            ("example:tank",),
        )

        self.assertNotIn("Tank 2.0", scoped.entries)
        self.assertNotIn("Tank 2.0", scoped.replacements_for("Tank 2.0"))

    def test_resource_scope_suppresses_casefolded_unrelated_mod_display(self) -> None:
        direct_tank = GlossaryEntry(
            "Tank",
            "タンク",
            "block.example.tank",
            "example",
            True,
            "example.jar",
            "translated",
        )

        def display(mod_id: str) -> GlossaryEntry:
            return GlossaryEntry(
                "Tank 2.0",
                "Tank 2.0",
                f"mod.display_name.{mod_id}",
                mod_id,
                False,
                f"{mod_id}.jar!/META-INF/mods.toml",
                "missing",
            )

        unrelated = display("other")
        unrelated_catalog = GlossaryCatalog(
            entries={direct_tank.source: direct_tank, unrelated.source: unrelated},
            evidence={direct_tank.source: (direct_tank,), unrelated.source: (unrelated,)},
        )
        scoped = unrelated_catalog.scoped_for_resources(
            "tank 2.0",
            ("example:tank",),
        )
        self.assertNotIn("Tank 2.0", scoped.entries)
        self.assertEqual(scoped.replacements_for("tank 2.0"), {})

        own_display = display("example")
        own_catalog = GlossaryCatalog(
            entries={direct_tank.source: direct_tank, own_display.source: own_display},
            evidence={direct_tank.source: (direct_tank,), own_display.source: (own_display,)},
        )
        own_scoped = own_catalog.scoped_for_resources(
            "tank 2.0",
            ("example:tank",),
        )
        self.assertEqual(
            own_scoped.replacements_for("tank 2.0"),
            {"tank 2.0": "tank 2.0"},
        )

    def test_resource_scope_suppresses_whole_title_from_an_unrelated_mod(self) -> None:
        collector = GlossaryEntry(
            "Collector",
            "Collector",
            "block.xycraft_machines.collector",
            "xycraft_machines",
            False,
            "xycraft.jar",
        )
        reprocessor = GlossaryEntry(
            "Reprocessor Collector",
            "再処理装置回収器",
            "block.bigreactors.reprocessorcollector",
            "bigreactors",
            True,
            "bigreactors.jar",
            "translated",
        )
        catalog = GlossaryCatalog(
            entries={collector.source: collector, reprocessor.source: reprocessor},
            evidence={
                collector.source: (collector,),
                reprocessor.source: (reprocessor,),
            },
        )

        self.assertEqual(
            catalog.replacements_for("&#435548Collector&r"),
            {"Collector": "Collector"},
        )
        scoped = catalog.scoped_for_resources(
            "&#435548Collector&r",
            ("bigreactors:reprocessorcollector",),
        )
        self.assertEqual(scoped.replacements_for("&#435548Collector&r"), {})
        self.assertIs(
            catalog.scoped_for_resources(
                "&#435548Collector&r",
                ("not a resource id",),
            ),
            catalog,
        )

    def test_resource_scope_suppresses_same_mod_term_from_a_different_resource_key(self) -> None:
        other_collector = GlossaryEntry(
            "Collector",
            "Collector",
            "block.bigreactors.other_collector",
            "bigreactors",
            False,
            "bigreactors.jar",
            "missing",
        )
        reprocessor = GlossaryEntry(
            "Reprocessor Collector",
            "再処理装置回収器",
            "block.bigreactors.reprocessorcollector",
            "bigreactors",
            True,
            "bigreactors.jar",
            "translated",
        )
        catalog = GlossaryCatalog(
            entries={
                other_collector.source: other_collector,
                reprocessor.source: reprocessor,
            },
            evidence={
                other_collector.source: (other_collector,),
                reprocessor.source: (reprocessor,),
            },
        )

        scoped = catalog.scoped_for_resources(
            "Collector",
            ("bigreactors:reprocessorcollector",),
        )

        self.assertEqual(scoped.replacements_for("Collector"), {})

    def test_resource_scope_suppresses_unrelated_term_when_direct_evidence_conflicts(self) -> None:
        global_collector = GlossaryEntry(
            "Collector",
            "Collector",
            "block.xycraft_machines.collector",
            "xycraft_machines",
            False,
            "xycraft.jar",
            "missing",
        )

        def direct(target: str, state: str = "translated") -> GlossaryEntry:
            return GlossaryEntry(
                "Reprocessor Collector",
                target,
                "block.bigreactors.reprocessorcollector",
                "bigreactors",
                state == "translated",
                "bigreactors.jar!/assets/bigreactors/lang/ja_jp.json",
                state,
            )

        evidence_cases = (
            (direct("再処理装置回収器"), direct("再処理コレクター")),
            (direct("再処理装置回収器"), direct("Reprocessor Collector", "explicit_source")),
        )
        for direct_evidence in evidence_cases:
            with self.subTest(states=[entry.target_state for entry in direct_evidence]):
                catalog = GlossaryCatalog(
                    entries={global_collector.source: global_collector},
                    evidence={
                        global_collector.source: (global_collector,),
                        "Reprocessor Collector": direct_evidence,
                    },
                )
                scoped = catalog.scoped_for_resources(
                    "Collector",
                    ("bigreactors:reprocessorcollector",),
                )

                self.assertEqual(scoped.replacements_for("Collector"), {})
                self.assertNotIn("Collector", scoped.entries)

    def test_structural_filter_resource_does_not_suppress_unrelated_title_name(self) -> None:
        pedestal = GlossaryEntry(
            "Sanguinary Pedestal",
            "Sanguinary Pedestal",
            "block.bloodmagic.sanguinary_pedestal",
            "bloodmagic",
            False,
            "bloodmagic.jar",
        )
        filter_item = GlossaryEntry(
            "OR",
            "OR",
            "item.itemfilters.or",
            "itemfilters",
            False,
            "itemfilters.jar",
        )
        catalog = GlossaryCatalog(
            entries={pedestal.source: pedestal, filter_item.source: filter_item},
            evidence={
                pedestal.source: (pedestal,),
                filter_item.source: (filter_item,),
            },
        )

        scoped = catalog.scoped_for_resources(
            "Sanguinary Pedestal",
            ("itemfilters:or",),
        )

        self.assertEqual(
            scoped.replacements_for("Sanguinary Pedestal"),
            {"Sanguinary Pedestal": "Sanguinary Pedestal"},
        )

    def test_sentence_initial_create_verb_is_not_mistaken_for_create_mod(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "Create": GlossaryEntry(
                    "Create",
                    "Create",
                    "mod.display_name.create",
                    "create",
                    False,
                    "create.jar!/META-INF/mods.toml",
                ),
                "Forgotten Minion": GlossaryEntry(
                    "Forgotten Minion",
                    "忘れ去られたミニオン",
                    "entity.undergarden.minion",
                    "undergarden",
                    True,
                    "undergarden.jar!/assets/undergarden/lang/en_us.json",
                ),
                "Apple": GlossaryEntry(
                    "Apple",
                    "Apple",
                    "mod.display_name.apple",
                    "apple",
                    False,
                    "apple.jar!/META-INF/mods.toml",
                ),
                "Machines create items": GlossaryEntry(
                    "Machines create items",
                    "Machines create items",
                    "item.example.create_usage",
                    "example",
                    False,
                    "example.jar!/assets/example/lang/en_us.json",
                ),
            }
        )

        self.assertEqual(
            catalog.replacements_for("&aCreate a Forgotten Minion"),
            {"Forgotten Minion": "忘れ去られたミニオン"},
        )
        self.assertEqual(
            catalog.replacements_for('"Create a Forgotten Minion"'),
            {"Forgotten Minion": "忘れ去られたミニオン"},
        )
        self.assertEqual(catalog.replacements_for("Create %s"), {})
        self.assertEqual(catalog.replacements_for("Create a %s"), {})
        for source in (
            "Gather materials. Create a Forgotten Minion",
            "Gather materials! \"Create a Forgotten Minion\"",
            "Gather materials\nCreate a Forgotten Minion",
            "- Create a Forgotten Minion",
            "* Create a Forgotten Minion",
            "1. Create a Forgotten Minion",
        ):
            with self.subTest(source=source):
                self.assertNotIn("Create", catalog.replacements_for(source))
        self.assertEqual(
            catalog.replacements_for("Build a Create machine"),
            {"Create": "Create"},
        )
        self.assertEqual(
            catalog.replacements_for("Use Create a lot"),
            {"Create": "Create"},
        )
        self.assertEqual(
            catalog.replacements_for("Apple a Day"),
            {"Apple": "Apple"},
        )
        for source in (
            "That stone does not create, it simply converts.",
            "I can create and rule over my own dimensions.",
            "This table will create what I need.",
        ):
            with self.subTest(source=source):
                self.assertNotIn("create", catalog.replacements_for(source))

    def test_ambiguous_one_word_labels_do_not_claim_unrelated_english_prose(self) -> None:
        def entry(
            source: str,
            target: str,
            key: str,
            mod_id: str,
            translated: bool = False,
        ) -> GlossaryEntry:
            return GlossaryEntry(
                source,
                target,
                key,
                mod_id,
                translated,
                f"{mod_id}.jar!/assets/{mod_id}/lang/en_us.json",
            )

        catalog = GlossaryCatalog(
            entries={
                "Good": entry("Good", "Good", "item.enderio.modifier_good", "enderio"),
                "Good Luck Charm": entry(
                    "Good Luck Charm",
                    "幸運のお守り",
                    "item.charms.good_luck",
                    "charms",
                    True,
                ),
                "Finding": entry(
                    "Finding",
                    "Finding",
                    "painting.mcwpaintings.finding.title",
                    "mcwpaintings",
                ),
                "Pressure": entry(
                    "Pressure",
                    "Pressure",
                    "item.ars_technica.thread_pressure",
                    "ars_technica",
                ),
                "Pressure Valve": entry(
                    "Pressure Valve",
                    "圧力弁",
                    "item.generators.pressure_valve",
                    "generators",
                    True,
                ),
                "Storage": entry(
                    "Storage",
                    "容量",
                    "key.categories.storage",
                    "functionalstorage",
                    True,
                ),
                "NBT Storage": entry(
                    "NBT Storage",
                    "NBTストレージ",
                    "block.peripherals.nbt_storage",
                    "peripherals",
                    True,
                ),
                "Functional Storage": GlossaryEntry(
                    "Functional Storage",
                    "Functional Storage",
                    "mod.display_name.functionalstorage",
                    "functionalstorage",
                    False,
                    "functionalstorage.jar!/META-INF/mods.toml",
                ),
                "Ratlantis": entry(
                    "Ratlantis",
                    "Ratlantis",
                    "biome.rats.ratlantis",
                    "rats",
                ),
                "Spiritfire": entry(
                    "Spiritfire",
                    "Spiritfire",
                    "block.occultism.spirit_fire",
                    "occultism",
                ),
                "Forgotten": entry(
                    "Forgotten",
                    "Forgotten",
                    "entity.quark.forgotten",
                    "quark",
                ),
                "Forgotten Minion": entry(
                    "Forgotten Minion",
                    "忘れ去られたミニオン",
                    "entity.undergarden.minion",
                    "undergarden",
                    True,
                ),
                "Carved Gloomgourd": entry(
                    "Carved Gloomgourd",
                    "くり抜かれたグルームゴード",
                    "block.undergarden.carved_gloomgourd",
                    "undergarden",
                    True,
                ),
            }
        )

        for source in (
            "Good luck!",
            "Finding the lost dimension of Ratlantis",
            "Air Pressure can be dangerous though",
            "AE2 & Refined Storage",
        ):
            with self.subTest(source=source):
                replacements = catalog.replacements_for(source)
                self.assertNotIn("Good", replacements)
                self.assertNotIn("Finding", replacements)
                self.assertNotIn("Pressure", replacements)
                self.assertNotIn("Storage", replacements)

        self.assertEqual(
            catalog.replacements_for("Finding the lost dimension of Ratlantis"),
            {"Ratlantis": "Ratlantis"},
        )
        self.assertEqual(
            catalog.replacements_for("Visit Ratlantis through Spiritfire."),
            {"Ratlantis": "Ratlantis", "Spiritfire": "Spiritfire"},
        )
        self.assertEqual(catalog.replacements_for("Pressure"), {"Pressure": "Pressure"})
        self.assertEqual(
            catalog.replacements_for("Use &ePressure&r carefully."),
            {"Pressure": "Pressure"},
        )
        self.assertEqual(
            catalog.replacements_for("Functional Storage uses Storage."),
            {"Functional Storage": "Functional Storage", "Storage": "容量"},
        )
        self.assertEqual(
            catalog.replacements_for(
                "Create a Forgotten Minion using a Forgotten Block and a "
                "Carved Gloomgourd."
            ),
            {
                "Forgotten Minion": "忘れ去られたミニオン",
                "Carved Gloomgourd": "くり抜かれたグルームゴード",
            },
        )

    def test_invalid_empty_official_target_falls_back_to_the_source(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "Widget": GlossaryEntry(
                    "Widget",
                    "",
                    "item.example.widget",
                    "example",
                    True,
                    "future-provider",
                )
            }
        )

        spans = catalog.replacement_spans_for_parts(["Use Widget"])[0]
        self.assertEqual([(item.start, item.end, item.replacement) for item in spans], [(4, 10, "Widget")])
        source_spans, candidate_spans = catalog.layout_replacement_spans_for_pair(
            "Use Widget",
            "Widgetを使う",
        )
        self.assertEqual(len(source_spans), 1)
        self.assertEqual(len(candidate_spans), 1)
        self.assertTrue(
            catalog.candidate_preserves_term_layout(
                ["Use Widget"],
                ["Widgetを使う"],
            )
        )

    def test_ascii_mod_case_matching_keeps_original_indices_after_unicode(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "Create": GlossaryEntry(
                    "Create", "Create", "mod.display_name.create", "create", False, "create.jar"
                )
            }
        )
        source = "İcReAtEを使う"

        replacements = catalog.replacements_for(source)
        protected = TokenProtector().protect(source, replacements)

        self.assertEqual(replacements, {"cReAtE": "cReAtE"})
        self.assertNotIn("cReAtE", protected.protected)
        self.assertEqual(protected.restore(protected.protected), source)

    def test_mod_name_inside_protected_syntax_is_not_returned_as_a_term_span(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "ElementalCraft": GlossaryEntry(
                    "ElementalCraft",
                    "ElementalCraft",
                    "mod.display_name.elementalcraft",
                    "elementalcraft",
                    False,
                    "elementalcraft.jar",
                )
            }
        )
        source = (
            "ElementalCraft #elementalcraft:gems/fine_water "
            "https://example.invalid/elementalcraft "
            "{item:elementalcraft:gem}"
        )

        spans = catalog.replacement_spans_for_parts([source])[0]

        self.assertEqual(
            [(span.start, span.end, span.replacement) for span in spans],
            [(0, len("ElementalCraft"), "ElementalCraft")],
        )
        protected = TokenProtector().protect(source, term_spans=spans)
        self.assertEqual(protected.restore(protected.protected), source)

        styled = "Elemental&bCraft"
        styled_spans = catalog.replacement_spans_for_parts([styled])[0]
        self.assertEqual(len(styled_spans), 2)
        self.assertIsNotNone(styled_spans[0].group_id)
        self.assertEqual(styled_spans[0].group_id, styled_spans[1].group_id)
        styled_protected = TokenProtector().protect(styled, term_spans=styled_spans)
        self.assertEqual(styled_protected.restore(styled_protected.protected), styled)

    def test_translated_official_term_inside_resource_id_is_not_expected_in_output(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "gems": GlossaryEntry(
                    "gems",
                    "宝石",
                    "item.example.gems",
                    "example",
                    True,
                    "example.jar!/assets/example/lang/en_us.json",
                )
            }
        )
        source = "Use example:gems/fine_water"

        self.assertEqual(catalog.replacement_spans_for_parts([source]), [[]])
        self.assertEqual(catalog.replacements_for(source), {})
        self.assertTrue(catalog.candidate_preserves_terms([source], [source]))
        self.assertTrue(catalog.candidate_preserves_term_layout([source], [source]))

    def test_large_glossary_prefix_index_limits_per_text_candidates(self) -> None:
        chosen_index = 12_345
        entries: dict[str, GlossaryEntry] = {}
        chosen_source = ""
        for index in range(50_000):
            source = f"{_synthetic_prefix(index)} Synthetic Term {index:05d}"
            entries[source] = GlossaryEntry(
                source,
                "合成訳語",
                "item.synthetic.term",
                "synthetic",
                True,
                "synthetic.jar",
            )
            if index == chosen_index:
                chosen_source = source
        entries["Create"] = GlossaryEntry(
            "Create", "Create", "mod.display_name.create", "create", False, "create.jar"
        )
        catalog = GlossaryCatalog(entries=entries)
        text = f"Use {chosen_source} with cReAtE."

        candidates = catalog._candidate_sources(text)
        replacements = catalog.replacements_for(text)

        self.assertIn(chosen_source, candidates)
        self.assertIn("Create", candidates)
        self.assertLess(len(candidates), 100)
        self.assertEqual(replacements, {chosen_source: "合成訳語", "cReAtE": "cReAtE"})

    def test_formatting_in_metadata_name_is_removed_only_for_visible_matching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "styled-name.jar"
            _write_jar(
                jar,
                {"fabric.mod.json": _json({"schemaVersion": 1, "id": "create", "name": "&bCreate"})},
            )
            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertIn("Create", catalog.entries)
        self.assertNotIn("&bCreate", catalog.entries)
        source = "Use &bCREATE&r."
        protected = TokenProtector().protect(source, catalog.replacements_for(source))
        self.assertNotIn("CREATE", protected.protected)
        self.assertEqual(protected.restore(protected.protected), source)

    def test_common_loader_display_names_are_always_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            _write_jar(
                mods / "a-localization.jar",
                {
                    "assets/localization/lang/en_us.json": _json({"itemGroup.localization": "Create"}),
                    "assets/localization/lang/ja_jp.json": _json({"itemGroup.localization": "クリエイト"}),
                },
            )
            _write_jar(
                mods / "forge.jar",
                {
                    "META-INF/mods.toml": (
                        'modLoader="javafml"\n'
                        'loaderVersion="[47,)"\n'
                        '[[mods]]\n'
                        'modId="create"\n'
                        'displayName="Create"\n'
                    ),
                    "assets/create/lang/en_us.json": _json({"itemGroup.create": "Create"}),
                    "assets/create/lang/ja_jp.json": _json({"itemGroup.create": "クリエイト"}),
                },
            )
            _write_jar(
                mods / "neoforge.jar",
                {
                    "META-INF/neoforge.mods.toml": (
                        'modLoader="javafml"\n'
                        'loaderVersion="[1,)"\n'
                        '[[mods]]\n'
                        'modId="modern_industrialization"\n'
                        'displayName="Modern Industrialization"\n'
                    )
                },
            )
            _write_jar(
                mods / "fabric.jar",
                {"fabric.mod.json": _json({"schemaVersion": 1, "id": "sodium", "name": "Sodium"})},
            )
            _write_jar(
                mods / "quilt.jar",
                {
                    "quilt.mod.json": _json(
                        {
                            "schema_version": 1,
                            "quilt_loader": {
                                "id": "quilted_fabric_api",
                                "metadata": {"name": "Quilted Fabric API"},
                            },
                        }
                    )
                },
            )
            _write_jar(
                mods / "legacy.jar",
                {"mcmod.info": _json([{"modid": "thaumcraft", "name": "Thaumcraft"}])},
            )

            catalog = ModLanguageScanner().scan(Path(directory), "en_us", "ja_jp")

        self.assertEqual(catalog.scanned_archives, 6)
        self.assertEqual(len(catalog.warnings), 1)
        self.assertIn("a-localization.jar", catalog.warnings[0])
        self.assertIn("表示名", catalog.warnings[0])
        for source in (
            "Create",
            "Modern Industrialization",
            "Sodium",
            "Quilted Fabric API",
            "Thaumcraft",
        ):
            with self.subTest(source=source):
                entry = catalog.entries[source]
                self.assertFalse(entry.translated)
                self.assertEqual(entry.target, source)
                self.assertTrue(entry.key.startswith("mod.display_name."))

        # A translated item-group label must not override the product display name.
        self.assertEqual(catalog.entries["Create"].target, "Create")
        self.assertNotIn("Create", catalog.conflicts)
        self.assertEqual(
            catalog.replacements_for("Build a Create machine with Sodium installed."),
            {"Create": "Create", "Sodium": "Sodium"},
        )
        protected = TokenProtector().protect(
            "Build a Create machine.",
            catalog.replacements_for("Build a Create machine."),
        )
        self.assertNotIn("Create", protected.protected)
        placeholder = next(iter(protected.replacements))
        self.assertEqual(protected.restore(f"{placeholder}の機械を作る。"), "Createの機械を作る。")

        styled = "Modern &bIndustrialization"
        styled_protected = TokenProtector().protect(styled, catalog.replacements_for(styled))
        self.assertNotIn("Modern", styled_protected.protected)
        self.assertNotIn("Industrialization", styled_protected.protected)
        self.assertEqual(styled_protected.restore(styled_protected.protected), styled)

        split_replacements = catalog.replacements_for_parts(["Modern ", "Industrialization"])
        self.assertEqual(split_replacements[0], {"Modern ": "Modern "})
        self.assertEqual(split_replacements[1], {"Industrialization": "Industrialization"})

    def test_concrete_neoforge_metadata_ignores_inactive_forge_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "enhancedbossbars.jar"
            _write_jar(
                jar,
                {
                    "META-INF/neoforge.mods.toml": (
                        'modLoader="javafml"\n'
                        'loaderVersion="[1,)"\n'
                        '[[mods]]\n'
                        'modId="enhancedbossbars"\n'
                        'displayName="Enhanced Boss Bars"\n'
                    ),
                    "META-INF/mods.toml": (
                        'modLoader="javafml"\n'
                        'loaderVersion="[1,)"\n'
                        '[[mods]]\n'
                        'modId="${mod_id}"\n'
                        'displayName="${mod_name}"\n'
                        '[[dependencies.${mod_id}]]\n'
                    ),
                    "assets/enhancedbossbars/lang/en_us.json": _json(
                        {"item.enhancedbossbars.example": "Example Boss Bar"}
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertEqual(catalog.warnings, [])
        self.assertIn("Enhanced Boss Bars", catalog.entries)
        self.assertIn("Example Boss Bar", catalog.entries)

    def test_neoforge_metadata_does_not_hide_other_forge_metadata_errors(self) -> None:
        broken_forge_metadata = (
            "# ${mod_id} appears only in a comment\n"
            "[[mods]\n"
            'modId="broken"\n'
        )
        mixed_forge_metadata = (
            '[[mods]]\nmodId="forge_addon"\n'
            'displayName="Forge Addon"\n'
            '[[dependencies.${mod_id}]]\n'
        )
        for name, forge_metadata in (
            ("unrelated-broken", broken_forge_metadata),
            ("mixed-template", mixed_forge_metadata),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                jar = Path(directory) / f"{name}.jar"
                _write_jar(
                    jar,
                    {
                        "META-INF/neoforge.mods.toml": (
                            '[[mods]]\nmodId="neo_main"\n'
                            'displayName="Neo Main"\n'
                        ),
                        "META-INF/mods.toml": forge_metadata,
                    },
                )

                catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

                self.assertIn("Neo Main", catalog.entries)
                self.assertTrue(
                    any("META-INF/mods.toml" in warning for warning in catalog.warnings)
                )

    def test_valid_neoforge_and_forge_metadata_are_both_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "dual-loader.jar"
            _write_jar(
                jar,
                {
                    "META-INF/neoforge.mods.toml": (
                        '[[mods]]\nmodId="neo_main"\n'
                        'displayName="Neo Main"\n'
                    ),
                    "META-INF/mods.toml": (
                        '[[mods]]\nmodId="forge_addon"\n'
                        'displayName="Forge Addon"\n'
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertEqual(catalog.warnings, [])
        self.assertIn("Neo Main", catalog.entries)
        self.assertIn("Forge Addon", catalog.entries)

    def test_cjk_mod_name_is_protected_before_a_japanese_particle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "cjk.jar"
            _write_jar(
                jar,
                {"fabric.mod.json": _json({"schemaVersion": 1, "id": "industry", "name": "工業"})},
            )
            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        text = "工業を使う"
        protected = TokenProtector().protect(text, catalog.replacements_for(text))
        self.assertNotIn("工業", protected.protected)
        self.assertEqual(protected.restore(protected.protected), text)

    def test_manifest_title_is_used_only_as_metadata_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "manifest-only.jar"
            _write_jar(
                jar,
                {
                    "META-INF/MANIFEST.MF": (
                        "Manifest-Version: 1.0\r\n"
                        "Implementation-Title: Farmer's De\r\n"
                        " light\r\n"
                        "Automatic-Module-Name: farmersdelight\r\n\r\n"
                        "Name: META-INF/embedded.jar\r\n"
                        "Implementation-Title: Embedded Library\r\n\r\n"
                    )
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        entry = catalog.entries["Farmer's Delight"]
        self.assertEqual(entry.target, "Farmer's Delight")
        self.assertEqual(entry.mod_id, "farmersdelight")
        self.assertIn("MANIFEST.MF", entry.provenance)
        self.assertNotIn("Embedded Library", catalog.entries)

    def test_loader_library_manifest_overrides_primary_mod_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for fml_mod_type in ("LIBRARY", "GAMELIBRARY", "LANGPROVIDER"):
                with self.subTest(fml_mod_type=fml_mod_type):
                    jar = Path(directory) / f"{fml_mod_type.casefold()}.jar"
                    members = {
                        "META-INF/MANIFEST.MF": (
                            "Manifest-Version: 1.0\r\n"
                            "Implementation-Title: Loader Support Library\r\n"
                            "Automatic-Module-Name: example.loader.library\r\n"
                            f"FMLModType: {fml_mod_type}\r\n\r\n"
                        ),
                        "META-INF/mods.toml": (
                            'modLoader="javafml"\n'
                            'loaderVersion="[1,)"\n'
                            '[[mods]]\n'
                            'modId="example_loader_library"\n'
                            'displayName="Must Not Be A Mod Name"\n'
                        ),
                    }
                    if fml_mod_type == "LIBRARY":
                        members.update(
                            {
                                "assets/example/lang/en_us.json": _json(
                                    {"item.example.library_tool": "Library Tool"}
                                ),
                                "assets/example/lang/ja_jp.json": _json(
                                    {"item.example.library_tool": "ライブラリーツール"}
                                ),
                            }
                        )
                    _write_jar(jar, members)

                    catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

                    self.assertEqual(catalog.scanned_archives, 1)
                    self.assertEqual(catalog.warnings, [])
                    self.assertNotIn("Loader Support Library", catalog.entries)
                    self.assertNotIn("Must Not Be A Mod Name", catalog.entries)
                    if fml_mod_type == "LIBRARY":
                        self.assertEqual(
                            catalog.entries["Library Tool"].target,
                            "ライブラリーツール",
                        )
                    else:
                        self.assertEqual(catalog.entries, {})

    def test_named_manifest_section_cannot_mark_the_jar_as_a_library(self) -> None:
        manifest = (
            "Manifest-Version: 1.0\r\n"
            "Implementation-Title: Real Mod\r\n\r\n"
            "Name: META-INF/embedded.jar\r\n"
            "FMLModType: LIBRARY\r\n\r\n"
        ).encode("utf-8")

        self.assertFalse(glossary_module._manifest_declares_library(manifest))
        self.assertEqual(
            glossary_module._read_manifest_metadata(manifest),
            [("manifest", "Real Mod")],
        )
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "named-section.jar"
            _write_jar(
                jar,
                {
                    "META-INF/MANIFEST.MF": manifest,
                    "fabric.mod.json": _json(
                        {
                            "schemaVersion": 1,
                            "id": "real_mod",
                            "name": "Primary Real Mod",
                        }
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertIn("Primary Real Mod", catalog.entries)
        self.assertEqual(catalog.warnings, [])

    def test_missing_loader_display_name_falls_back_to_mod_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            _write_jar(
                mods / "forge.jar",
                {
                    "META-INF/mods.toml": (
                        'modLoader="javafml"\nloaderVersion="[47,)"\n'
                        '[[mods]]\nmodId="forge_example"\n'
                    )
                },
            )
            _write_jar(
                mods / "fabric.jar",
                {"fabric.mod.json": _json({"schemaVersion": 1, "id": "fabric_example"})},
            )
            _write_jar(
                mods / "localized.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "localized", "name": {"en-US": "Localized Name"}}
                    )
                },
            )

            catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

        for name in ("forge_example", "fabric_example", "Localized Name"):
            self.assertIn(name, catalog.entries)
            self.assertFalse(catalog.entries[name].translated)

    def test_bad_mod_metadata_warns_but_keeps_language_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "partly-broken.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": "{ definitely not json",
                    "assets/working/lang/en_us.json": _json({"item.working.tool": "Working Tool"}),
                    "assets/working/lang/ja_jp.json": _json({"item.working.tool": "作業道具"}),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertEqual(catalog.scanned_archives, 1)
        self.assertEqual(catalog.entries["Working Tool"].target, "作業道具")
        self.assertTrue(any("fabric.mod.json" in warning for warning in catalog.warnings))
        self.assertTrue(any("Modメタデータ" in warning for warning in catalog.warnings))

    def test_bad_target_language_warns_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "bad-target.jar"
            _write_jar(
                jar,
                {
                    "assets/example/lang/en_us.json": _json({"item.example.tool": "Safe Tool"}),
                    "assets/example/lang/ja_jp.json": b"\xff\xfe",
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertEqual(catalog.scanned_archives, 1)
        self.assertEqual(catalog.entries["Safe Tool"].target, "Safe Tool")
        self.assertFalse(catalog.entries["Safe Tool"].translated)
        self.assertTrue(any("原文を保持" in warning for warning in catalog.warnings))

    def test_translated_missing_and_identical_target_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pack = Path(directory)
            _write_jar(
                pack / "mods" / "example.jar",
                {
                    "assets/example/lang/en_us.json": _json(
                        {
                            "item.example.widget": "Copper Widget",
                            "block.example.machine": "Untranslated Machine",
                            "entity.example.guide": "Identical Guide",
                            "itemGroup.example": "Example Technology",
                            "gui.example.button": "Ignored GUI Label",
                            "item.example.short": "Ax",
                            "item.example.format": "Count %s",
                        }
                    ),
                    "assets/example/lang/ja_jp.json": _json(
                        {
                            "item.example.widget": "銅の装置",
                            "entity.example.guide": "Identical Guide",
                            "itemGroup.example": "Exampleテクノロジー",
                        }
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(pack, "en_us", "ja_jp")

        self.assertEqual(catalog.scanned_archives, 1)
        self.assertEqual(len(catalog.warnings), 1)
        self.assertTrue(any("使用可能な表示名" in warning for warning in catalog.warnings))
        self.assertFalse(any("item.example.format" in warning for warning in catalog.warnings))
        self.assertEqual(
            set(catalog.entries),
            {"Copper Widget", "Untranslated Machine", "Identical Guide", "Example Technology"},
        )
        translated = catalog.entries["Copper Widget"]
        self.assertTrue(translated.translated)
        self.assertEqual(translated.target_state, "translated")
        self.assertEqual(translated.target, "銅の装置")
        self.assertEqual(translated.key, "item.example.widget")
        self.assertEqual(translated.mod_id, "example")
        self.assertIn("example.jar!/assets/example/lang/en_us.json", translated.provenance)
        self.assertEqual(catalog.entries["Example Technology"].target, "Exampleテクノロジー")

        for source in ("Untranslated Machine", "Identical Guide"):
            self.assertFalse(catalog.entries[source].translated)
            self.assertEqual(catalog.entries[source].target, source)
        self.assertEqual(
            catalog.entries["Untranslated Machine"].target_state,
            "missing",
        )
        self.assertEqual(
            catalog.entries["Identical Guide"].target_state,
            "explicit_source",
        )
        self.assertEqual(
            catalog.replacements_for("Use Copper Widget with Untranslated Machine."),
            {"Copper Widget": "銅の装置", "Untranslated Machine": "Untranslated Machine"},
        )

    def test_two_character_uppercase_technical_term_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "energy.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "energy", "name": "Energy Mod"}
                    ),
                    "assets/energy/lang/en_us.json": _json(
                        {
                            "item.energy.xp": "XP",
                            "item.energy.mixed": "Ax",
                        }
                    ),
                },
            )
            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertIn("XP", catalog.entries)
        self.assertNotIn("Ax", catalog.entries)
        self.assertEqual(catalog.replacements_for("Use XP here."), {"XP": "XP"})

    def test_official_target_uses_visible_text_and_rejects_incompatible_special_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "styled-official.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "example", "name": "Example Mod"}
                    ),
                    "assets/example/lang/en_us.json": _json(
                        {
                            "item.example.sword": "Rainbow Sword",
                            "item.example.hammer": "§6Styled Hammer§r",
                            "item.example.manual": "Safe Manual",
                            "item.example.link": "Link Manual",
                            "item.example.lines": "Line Manual",
                            "item.example.reserved": "Reserved Manual",
                            "item.example.source_token": "Magic {0} Manual",
                        }
                    ),
                    "assets/example/lang/ja_jp.json": _json(
                        {
                            "item.example.sword": "§b虹の剣§r",
                            "item.example.hammer": "&c装飾ハンマー&r",
                            "item.example.manual": "安全な説明書 {0}",
                            "item.example.link": "リンク説明書 https://example.invalid/guide",
                            "item.example.lines": "行の\n説明書",
                            "item.example.reserved": "予約説明書 __MQP_0000__",
                            "item.example.source_token": "魔法の{0}説明書",
                        }
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertEqual(catalog.entries["Rainbow Sword"].target, "虹の剣")
        self.assertEqual(catalog.entries["Styled Hammer"].target, "装飾ハンマー")
        self.assertTrue(catalog.entries["Rainbow Sword"].translated)
        self.assertTrue(catalog.entries["Styled Hammer"].translated)
        for source in ("Safe Manual", "Link Manual", "Line Manual", "Reserved Manual"):
            with self.subTest(source=source):
                self.assertEqual(catalog.entries[source].target, source)
                self.assertFalse(catalog.entries[source].translated)
        self.assertNotIn("Magic {0} Manual", catalog.entries)
        self.assertEqual(len(catalog.warnings), 4)
        self.assertFalse(any("装飾以外の保護token" in warning for warning in catalog.warnings))
        self.assertTrue(all("原語" in warning for warning in catalog.warnings))

        text = "Use §aRainbow Sword§r and Styled Hammer."
        protected = TokenProtector().protect(text, catalog.replacements_for(text))
        restored = protected.restore(protected.protected)
        self.assertEqual(restored, "Use §a虹の剣§r and 装飾ハンマー.")
        self.assertEqual(special_tokens(restored), special_tokens(text))

        styled_inside = "Rainbow §aSword§r"
        styled_protected = TokenProtector().protect(
            styled_inside,
            catalog.replacements_for(styled_inside),
        )
        self.assertEqual(styled_protected.restore(styled_protected.protected), styled_inside)
        split_replacements = catalog.replacements_for_parts(["Rainbow ", "Sword"])
        self.assertEqual(split_replacements, [{"Rainbow ": "Rainbow "}, {"Sword": "Sword"}])

        incompatible = "Read Safe Manual and Link Manual."
        incompatible_protected = TokenProtector().protect(
            incompatible,
            catalog.replacements_for(incompatible),
        )
        self.assertEqual(incompatible_protected.restore(incompatible_protected.protected), incompatible)

    def test_printf_display_arguments_derive_only_key_proven_fixed_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "printf-labels.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "example", "name": "Example Mod"}
                    ),
                    "assets/example/lang/en_us.json": _json(
                        {
                            "block.example.netherite_chest": "%s%sNetherite Chest",
                            "block.example.limited_iron_barrel_1": (
                                "Limited %s%sIron Barrel I"
                            ),
                            "item.example.composite_lens": "Composite Lens: %s %s",
                            "item.example.spawner": "%sSpawner",
                            "item.example.wandering": "%s is wandering",
                            "item.example.charm": "Charm of %s",
                            "item.example.item_import_limit": "Item Import Limit: %s/s",
                            "item.example.repair_efficiency": "Repair Efficiency: %d%%",
                            "item.example.variable_name": "%s",
                            "item.example.percent_widget": "Percent %% Widget",
                            "item.example.newline_widget": "Newline %n Widget",
                            "item.example.stack_downgrade_tier_1": "Stack Downgrade Tier 1",
                            "item.example.laser_lens": "Laser Lens",
                        }
                    ),
                    "assets/example/lang/ja_jp.json": _json(
                        {
                            "block.example.netherite_chest": "%sネザライトのチェスト",
                            "block.example.limited_iron_barrel_1": "%s鉄の限定樽 I",
                            "item.example.composite_lens": "複合レンズ: %s %s",
                            "item.example.spawner": "%sのスポナー",
                            "item.example.wandering": "%sが歩き回っています",
                            "item.example.charm": "%sのお守り",
                            "item.example.item_import_limit": "アイテム搬入上限: %s/秒",
                            "item.example.repair_efficiency": "修理効率: %d%%",
                            "item.example.variable_name": "%s",
                            "item.example.percent_widget": "割合ウィジェット",
                            "item.example.newline_widget": "改行ウィジェット",
                            "item.example.stack_downgrade_tier_1": "スタック数減少/8",
                            "item.example.laser_lens": "%sのレーザーレンズ",
                            "tooltip.example.lens": "レーザーレンズ",
                        }
                    ),
                    "assets/example/models/item/charm.json": "{}",
                    "assets/example/models/item/item_import_limit.json": "{}",
                    "assets/example/models/item/repair_efficiency.json": "{}",
                    "assets/example/models/item/variable_name.json": "{}",
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertEqual(catalog.entries["Netherite Chest"].target, "ネザライトのチェスト")
        self.assertEqual(catalog.entries["Limited Iron Barrel I"].target, "鉄の限定樽 I")
        self.assertEqual(catalog.entries["Composite Lens"].target, "複合レンズ")
        self.assertEqual(
            catalog.entries["Stack Downgrade Tier 1"].target,
            "スタック数減少/8",
        )
        self.assertEqual(catalog.entries["Spawner"].target, "スポナー")
        self.assertEqual(catalog.entries["Laser Lens"].target, "レーザーレンズ")
        self.assertTrue(catalog.entries["Spawner"].translated)
        self.assertTrue(catalog.entries["Laser Lens"].translated)
        self.assertNotIn("is wandering", catalog.entries)
        self.assertNotIn("Charm of", catalog.entries)
        self.assertNotIn("Item Import Limit:  /s", catalog.entries)
        self.assertNotIn("Repair Efficiency", catalog.entries)
        self.assertNotIn("Percent Widget", catalog.entries)
        self.assertNotIn("Newline Widget", catalog.entries)
        for dynamic_key in (
            "item.example.wandering",
            "item.example.charm",
            "item.example.item_import_limit",
            "item.example.repair_efficiency",
            "item.example.variable_name",
        ):
            with self.subTest(dynamic_key=dynamic_key):
                self.assertFalse(
                    any(dynamic_key in warning for warning in catalog.warnings)
                )
        self.assertFalse(any("netherite_chest" in warning for warning in catalog.warnings))
        self.assertFalse(any("composite_lens" in warning for warning in catalog.warnings))
        self.assertFalse(
            any("stack_downgrade_tier_1" in warning for warning in catalog.warnings)
        )
        self.assertFalse(any("item.example.spawner" in warning for warning in catalog.warnings))
        self.assertFalse(any("item.example.laser_lens" in warning for warning in catalog.warnings))
        self.assertEqual(catalog.warnings, [])

    def test_japanese_possessive_target_derivation_is_fail_closed(self) -> None:
        self.assertEqual(
            glossary_module._independent_fixed_target_labels(
                (
                    "レーザーレンズ",
                    "§bレーザーレンズ§r",
                    "%sのレーザーレンズ",
                    "名前を表示",
                    "動作中",
                    "説明文です。",
                )
            ),
            frozenset({"レーザーレンズ"}),
        )
        verified_names = (
            "スポナー",
            "レーザーレンズ",
            "オーグメント",
            "設計図",
            "断片",
            "混合宝石",
            "混合生地",
            "金属シート",
            "型板",
            "道標",
        )
        for fixed_name in verified_names:
            raw_target = f"%sの{fixed_name}"
            with self.subTest(raw_target=raw_target):
                kwargs = (
                    {"independent_fixed_targets": frozenset({fixed_name})}
                    if fixed_name == "レーザーレンズ"
                    else {"source_had_printf": True}
                )
                accepted, derived = glossary_module._fixed_target_terminology_label(
                    raw_target,
                    "ja_jp",
                    **kwargs,
                )
                self.assertEqual(accepted, (fixed_name, True, 1))
                self.assertTrue(derived)

        for raw_target in ("%1$sの設計図", "%1$-10.5sの設計図"):
            with self.subTest(raw_target=raw_target):
                accepted, derived = glossary_module._fixed_target_terminology_label(
                    raw_target,
                    "ja_jp",
                    source_had_printf=True,
                )
                self.assertEqual(accepted, ("設計図", True, 1))
                self.assertTrue(derived)

        for raw_target, fixed_name in (
            ("%sの炎の剣", "炎の剣"),
            ("%sのつるはし", "つるはし"),
        ):
            with self.subTest(raw_target=raw_target):
                accepted, derived = glossary_module._fixed_target_terminology_label(
                    raw_target,
                    "ja_jp",
                    source_had_printf=True,
                )
                self.assertEqual(accepted, (fixed_name, True, 1))
                self.assertTrue(derived)

        for raw_target, locale in (
            ("%sのBlueprint", "en_us"),
            ("%s%sの設計図", "ja_jp"),
            ("%dの設計図", "ja_jp"),
            ("%1$.2fの設計図", "ja_jp"),
            ("前%sの設計図", "ja_jp"),
            ("%sが設計図", "ja_jp"),
            ("%sの", "ja_jp"),
            ("%sの設計図{0}", "ja_jp"),
            ("%sの設定を更新しました。", "ja_jp"),
            ("%sの設定を更新しました", "ja_jp"),
            ("%sの残量が少ない", "ja_jp"),
            ("%sの利用が必要", "ja_jp"),
            ("%sの名前を表示", "ja_jp"),
            ("%sの動作中", "ja_jp"),
            ("%sの説明です", "ja_jp"),
            ("%sの動作しています", "ja_jp"),
            ("%sの充電完了", "ja_jp"),
            ("%sの処理失敗", "ja_jp"),
            ("%sの機能有効", "ja_jp"),
            ("%sの機能無効", "ja_jp"),
            ("%sの充電必要", "ja_jp"),
            ("%sの材料不足", "ja_jp"),
        ):
            with self.subTest(raw_target=raw_target, locale=locale):
                _label, was_derived = glossary_module._fixed_target_terminology_label(
                    raw_target,
                    locale,
                    source_had_printf=True,
                )
                self.assertFalse(was_derived)

        _label, was_derived = glossary_module._fixed_target_terminology_label(
            "%sのレーザーレンズ",
            "ja_jp",
        )
        self.assertFalse(was_derived)

    def test_printf_target_without_a_complete_fixed_name_still_warns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "incomplete-target.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "rats", "name": "Rats"}
                    ),
                    "assets/rats/lang/en_us.json": _json(
                        {"item.rats.rat_nugget_ore": 'Rat "Nugget"'}
                    ),
                    "assets/rats/lang/ja_jp.json": _json(
                        {"item.rats.rat_nugget_ore": '%s§o"Nugget"'}
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        entry = catalog.entries['Rat "Nugget"']
        self.assertEqual(entry.target, 'Rat "Nugget"')
        self.assertFalse(entry.translated)
        self.assertEqual(len(catalog.warnings), 1)
        self.assertIn("原文にないprintf引数", catalog.warnings[0])

    def test_metadata_display_suppresses_redundant_resource_shaped_language_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "branded.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {
                            "schemaVersion": 1,
                            "id": "branded",
                            "name": "Eidolon:Repraised",
                        }
                    ),
                    "assets/branded/lang/en_us.json": _json(
                        {"key.categories.branded": "Eidolon:Repraised"}
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertIn("Eidolon:Repraised", catalog.entries)
        self.assertFalse(any("key.categories.branded" in warning for warning in catalog.warnings))

    def test_eidolon_family_names_are_preserved_without_cross_mod_borrowing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory)
            jar = mods / "eidolon-repraised.jar"
            _write_jar(
                jar,
                {
                    "META-INF/neoforge.mods.toml": (
                        '[[mods]]\nmodId="eidolon_repraised"\n'
                        'displayName="Eidolon : Repraised"\n'
                    ),
                    "assets/eidolon_repraised/lang/en_us.json": _json(
                        {
                            "item.eidolon_repraised.arcane_gold_ingot": (
                                "Arcane Gold Ingot"
                            ),
                            "item.eidolon_repraised.arcane_gold_nugget": (
                                "Arcane Gold Nugget"
                            ),
                            "block.eidolon_repraised.arcane_gold_block": (
                                "Arcane Gold Block"
                            ),
                            "item.eidolon_repraised.lesser_soul_gem": (
                                "Lesser Soul Gem"
                            ),
                            "item.eidolon_repraised.shadow_gem": "Shadow Gem",
                            # This is a real Codex label, not a registry term.
                            # The material family must provide the private alias.
                            "eidolon_repraised.codex.chapter.arcane_gold": (
                                "Arcane Gold"
                            ),
                            # This short Codex family name is independently
                            # corroborated by the registered Lesser Soul Gem.
                            "eidolon_repraised.codex.chapter.soul_gems": (
                                "Soul Gems"
                            ),
                        }
                    ),
                    "assets/eidolon_repraised/models/item/lesser_soul_gem.json": "{}",
                },
            )
            _write_jar(
                mods / "occultism.jar",
                {
                    "fabric.mod.json": _json(
                        {
                            "schemaVersion": 1,
                            "id": "occultism",
                            "name": "Occultism",
                        }
                    ),
                    "assets/occultism/lang/en_us.json": _json(
                        {"item.occultism.soul_gem": "Soul Gem"}
                    ),
                    "assets/occultism/lang/ja_jp.json": _json(
                        {"item.occultism.soul_gem": "魂の宝石"}
                    ),
                    "assets/occultism/models/item/soul_gem.json": "{}",
                },
            )

            catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

        self.assertNotIn("Arcane Gold", catalog.entries)
        self.assertEqual(catalog.entries["Soul Gem"].target, "魂の宝石")
        self.assertEqual(catalog.entries["Soul Gems"].target, "Soul Gems")
        self.assertEqual(
            catalog.entries["Soul Gems"].key,
            "eidolon_repraised.codex.chapter.soul_gems",
        )
        self.assertEqual(
            catalog.replacements_for("Arcane Gold is used in rituals."),
            {"Arcane Gold": "Arcane Gold"},
        )
        self.assertEqual(
            catalog.replacements_for("Use an Arcane Gold Ingot."),
            {"Arcane Gold Ingot": "Arcane Gold Ingot"},
        )
        self.assertEqual(
            catalog.replacements_for(
                "Arcane Gold, Soul Gems, and Shadow Gems are your alchemy staples."
            ),
            {
                "Arcane Gold": "Arcane Gold",
                "Soul Gems": "Soul Gems",
                "Shadow Gems": "Shadow Gems",
            },
        )
        self.assertFalse(
            catalog.candidate_preserves_terms(
                ["Arcane Gold, Soul Gems, and Shadow Gems are your alchemy staples."],
                ["Arcane Gold、魂の宝石、Shadow Gemsは錬金術の必需品です。"],
            )
        )
        # The derived base is deliberately weak: it must not claim part of a
        # different proper name or generate another guessed plural.
        self.assertEqual(catalog.replacements_for("Arcane Gold Dust"), {})
        self.assertEqual(catalog.replacements_for("Mystic Arcane Gold"), {})
        self.assertEqual(catalog.replacements_for("Arcane Golds"), {})

    def test_codex_family_name_requires_same_namespace_registry_corroboration(self) -> None:
        source_labels = {
            "item.example.lesser_soul_gem": "Lesser Soul Gem",
            "example.codex.chapter.soul_gems": "Soul Gems",
            "other.codex.chapter.soul_gems": "Soul Gems",
            "example.codex.chapter.soul_stones": "Soul Stones",
            "example.codex.chapter.gems": "Gems",
            "example.codex.page.soul_gems": "Soul Gems",
            "item.example.lesser_mana_gem": "Lesser Mana Gem",
            "example.codex.chapter.mana_gems": "Mana Gems",
        }

        proven = glossary_module._registry_backed_codex_chapter_keys(
            source_labels,
            frozenset({"item.example.lesser_soul_gem"}),
        )

        self.assertEqual(
            proven,
            frozenset({"example.codex.chapter.soul_gems"}),
        )
        self.assertEqual(
            glossary_module._registry_backed_codex_chapter_keys(
                source_labels,
                frozenset(),
            ),
            frozenset(),
        )

    def test_material_base_requires_one_complete_key_aligned_family(self) -> None:
        def source_only(
            source: str,
            key: str,
            mod_id: str = "example",
            source_tier: glossary_module._SourceTier = "mod",
        ) -> GlossaryEntry:
            return GlossaryEntry(
                source=source,
                target=source,
                key=key,
                mod_id=mod_id,
                translated=False,
                provenance=key,
                target_state="missing",
                source_tier=source_tier,
            )

        incomplete = GlossaryCatalog(
            entries={
                "Arcane Gold Ingot": source_only(
                    "Arcane Gold Ingot", "item.example.arcane_gold_ingot"
                ),
                "Arcane Gold Nugget": source_only(
                    "Arcane Gold Nugget", "item.example.arcane_gold_nugget"
                ),
            }
        )
        mismatched_key = GlossaryCatalog(
            entries={
                "Arcane Gold Ingot": source_only(
                    "Arcane Gold Ingot", "item.example.arcane_gold_ingot"
                ),
                "Arcane Gold Nugget": source_only(
                    "Arcane Gold Nugget", "item.example.mana_gold_nugget"
                ),
                "Arcane Gold Block": source_only(
                    "Arcane Gold Block", "block.example.arcane_gold_block"
                ),
            }
        )
        split_providers = GlossaryCatalog(
            entries={
                "Arcane Gold Ingot": source_only(
                    "Arcane Gold Ingot",
                    "item.alpha.arcane_gold_ingot",
                    "alpha",
                ),
                "Arcane Gold Nugget": source_only(
                    "Arcane Gold Nugget",
                    "item.alpha.arcane_gold_nugget",
                    "alpha",
                ),
                "Arcane Gold Block": source_only(
                    "Arcane Gold Block",
                    "block.beta.arcane_gold_block",
                    "beta",
                ),
            }
        )
        one_word_base = GlossaryCatalog(
            entries={
                "Iron Ingot": source_only(
                    "Iron Ingot", "item.minecraft.iron_ingot", "minecraft"
                ),
                "Iron Nugget": source_only(
                    "Iron Nugget", "item.minecraft.iron_nugget", "minecraft"
                ),
                "Iron Block": source_only(
                    "Iron Block", "block.minecraft.iron_block", "minecraft"
                ),
            }
        )
        mixed_fallback_tiers = GlossaryCatalog(
            entries={
                "Arcane Gold Ingot": source_only(
                    "Arcane Gold Ingot",
                    "item.example.arcane_gold_ingot",
                    source_tier="mod",
                ),
                "Arcane Gold Nugget": source_only(
                    "Arcane Gold Nugget",
                    "item.example.arcane_gold_nugget",
                    source_tier="kubejs",
                ),
                "Arcane Gold Block": source_only(
                    "Arcane Gold Block",
                    "block.example.arcane_gold_block",
                    source_tier="resourcepack",
                ),
            }
        )

        for name, catalog in (
            ("incomplete", incomplete),
            ("mismatched key", mismatched_key),
            ("split providers", split_providers),
            ("one-word base", one_word_base),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    catalog.replacements_for("Use Arcane Gold for this recipe."),
                    {},
                )
        self.assertNotIn(
            "Iron",
            one_word_base.replacements_for("Iron out the details."),
        )
        self.assertEqual(
            mixed_fallback_tiers.replacements_for("Use Arcane Gold."),
            {"Arcane Gold": "Arcane Gold"},
        )

    def test_material_base_never_guesses_a_translation_and_exact_entry_wins(self) -> None:
        def translated(source: str, target: str, key: str) -> GlossaryEntry:
            return GlossaryEntry(
                source=source,
                target=target,
                key=key,
                mod_id="example",
                translated=True,
                provenance=key,
                target_state="translated",
            )

        family = {
            "Arcane Gold Ingot": translated(
                "Arcane Gold Ingot",
                "秘儀の金インゴット",
                "item.example.arcane_gold_ingot",
            ),
            "Arcane Gold Nugget": translated(
                "Arcane Gold Nugget", "秘儀の金塊", "item.example.arcane_gold_nugget"
            ),
            "Arcane Gold Block": translated(
                "Arcane Gold Block",
                "秘儀の金ブロック",
                "block.example.arcane_gold_block",
            ),
        }
        derived = GlossaryCatalog(entries=family)
        exact = GlossaryCatalog(
            entries={
                **family,
                "Arcane Gold": translated(
                    "Arcane Gold", "秘儀の金", "item.example.arcane_gold"
                ),
            }
        )
        plural_collision = GlossaryCatalog(
            entries={
                "Arcane Shard": translated(
                    "Arcane Shard", "秘片", "item.example.arcane_shard"
                ),
                "Arcane Shards Ingot": translated(
                    "Arcane Shards Ingot",
                    "秘片インゴット",
                    "item.example.arcane_shards_ingot",
                ),
                "Arcane Shards Nugget": translated(
                    "Arcane Shards Nugget",
                    "秘片ナゲット",
                    "item.example.arcane_shards_nugget",
                ),
                "Arcane Shards Block": translated(
                    "Arcane Shards Block",
                    "秘片ブロック",
                    "block.example.arcane_shards_block",
                ),
            }
        )

        self.assertEqual(
            derived.replacements_for("Use Arcane Gold and an Arcane Gold Ingot."),
            {
                "Arcane Gold": "Arcane Gold",
                "Arcane Gold Ingot": "秘儀の金インゴット",
            },
        )
        self.assertEqual(
            exact.replacements_for("Use Arcane Gold."),
            {"Arcane Gold": "秘儀の金"},
        )
        self.assertEqual(
            plural_collision.replacements_for("Use Arcane Shards."),
            {"Arcane Shards": "Arcane Shards"},
        )

    def test_minecraft_assets_supply_plural_alias_over_untranslated_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            instance = launcher / "instances" / "Pack"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            _write_minecraft_assets(
                launcher,
                "1.20.1",
                {"item.minecraft.iron_ingot": "Iron Ingot"},
                {"item.minecraft.iron_ingot": "鉄インゴット"},
            )
            _write_jar(
                mods / "tags.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "tags", "name": "Tags"}
                    ),
                    "assets/tags/lang/en_us.json": _json(
                        {"tag.item.tags.iron_ingots": "Iron Ingots"}
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                minecraft_version="1.20.1",
                instance_root=instance,
            )

        self.assertEqual(catalog.entries["Iron Ingot"].target, "鉄インゴット")
        self.assertEqual(catalog.entries["Iron Ingots"].target_state, "missing")
        self.assertEqual(
            catalog.replacements_for("Put Iron Ingots into the furnace."),
            {"Iron Ingots": "鉄インゴット"},
        )
        protected = TokenProtector().protect(
            "Put Iron Ingots into the furnace.",
            catalog.replacements_for("Put Iron Ingots into the furnace."),
        )
        self.assertEqual(
            protected.restore(protected.protected),
            "Put 鉄インゴット into the furnace.",
        )

    def test_minecraft_asset_terms_share_fail_closed_printf_detection(self) -> None:
        untrusted_key = "item.minecraft.line\nforged-warning." + "x" * 10_000
        bundle = minecraft_assets_module.MinecraftLanguageBundle(
            source_values={
                "block.minecraft.limited_iron_barrel_1": (
                    "Limited %s%sIron Barrel I"
                ),
                "item.minecraft.spawner": "%sSpawner",
                "item.minecraft.laser_lens": "Laser Lens",
                "item.minecraft.wandering": "%s is wandering",
                "item.minecraft.repair_efficiency": "Repair Efficiency: %d%%",
                untrusted_key: "Unsafe Minecraft Tool",
            },
            target_values={
                "block.minecraft.limited_iron_barrel_1": "%s鉄の限定樽 I",
                "item.minecraft.spawner": "%sのスポナー",
                "item.minecraft.laser_lens": "%sのレーザーレンズ",
                "tooltip.minecraft.lens": "レーザーレンズ",
                "item.minecraft.wandering": "%sが歩き回っています",
                "item.minecraft.repair_efficiency": "修理効率: %d%%",
                untrusted_key: "\u200b",
            },
            source_provenance="minecraft-client.jar!/assets/minecraft/lang/en_us.json",
            target_provenance="assets/objects/ja_jp",
        )
        warnings: list[str] = []

        entries = glossary_module._minecraft_language_entries(
            bundle,
            None,
            warnings,
            "ja_jp",
        )
        by_source = {entry.source: entry for entry in entries}

        self.assertEqual(by_source["Limited Iron Barrel I"].target, "鉄の限定樽 I")
        self.assertEqual(by_source["Spawner"].target, "スポナー")
        self.assertEqual(by_source["Laser Lens"].target, "レーザーレンズ")
        self.assertIs(
            by_source["Spawner"].provenance,
            by_source["Laser Lens"].provenance,
        )
        self.assertNotIn("is wandering", by_source)
        self.assertNotIn("Repair Efficiency", by_source)
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("\n", warnings[0])
        self.assertIn(r"\nforged-warning", warnings[0])
        self.assertLess(len(warnings[0]), 500)

    def test_minecraft_duplicate_language_keys_use_count_only_warning_and_debug_details(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            instance = launcher / "instances" / "Pack"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            _write_minecraft_assets(
                launcher,
                "1.20.1",
                {"item.minecraft.iron_ingot": "Iron Ingot"},
                {"item.minecraft.iron_ingot": "鉄インゴット"},
            )
            client = (
                launcher
                / "libraries"
                / "com"
                / "mojang"
                / "minecraft"
                / "1.20.1"
                / "minecraft-1.20.1-client.jar"
            )
            _write_jar(
                client,
                {
                    "assets/minecraft/lang/en_us.json": (
                        '{"item.minecraft.iron_ingot":"Iron Ingot",'
                        '"item.minecraft.first_duplicate":"First",'
                        '"item.minecraft.first_duplicate":"Second",'
                        '"block.minecraft.second_duplicate":"First",'
                        '"block.minecraft.second_duplicate":"Second"}'
                    )
                },
            )
            _write_jar(
                mods / "empty.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "empty", "name": "Empty"}
                    )
                },
            )

            with mock.patch.object(
                minecraft_assets_module,
                "_launcher_roots",
                return_value=(launcher,),
            ):
                catalog = ModLanguageScanner().scan(
                    mods,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                    instance_root=instance,
                )

        warnings = "\n".join(catalog.warnings)
        debug_details = "\n".join(catalog.debug_messages)
        self.assertIn("JSON keyが2件重複しています", warnings)
        self.assertNotIn("item.minecraft.first_duplicate", warnings)
        self.assertNotIn("block.minecraft.second_duplicate", warnings)
        self.assertIn("重複JSONキー詳細", debug_details)
        self.assertIn("件数: 2", debug_details)
        self.assertIn('"item.minecraft.first_duplicate"', debug_details)
        self.assertIn('"block.minecraft.second_duplicate"', debug_details)
        self.assertEqual(catalog.coverage.minecraft_asset_warning_count, 1)

    def test_corrupt_minecraft_target_asset_warns_and_preserves_source_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            instance = launcher / "instances" / "Pack"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            target_object = _write_minecraft_assets(
                launcher,
                "1.20.1",
                {"item.minecraft.iron_ingot": "Iron Ingot"},
                {"item.minecraft.iron_ingot": "鉄インゴット"},
            )
            target_object.write_bytes(b"corrupt")
            _write_jar(
                mods / "empty.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "empty", "name": "Empty"}
                    )
                },
            )

            with mock.patch.object(
                minecraft_assets_module,
                "_launcher_roots",
                return_value=(launcher,),
            ):
                catalog = ModLanguageScanner().scan(
                    mods,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                    instance_root=instance,
                )

        self.assertIn("Iron Ingot", catalog.entries)
        self.assertEqual(catalog.entries["Iron Ingot"].target, "Iron Ingot")
        self.assertEqual(catalog.entries["Iron Ingot"].target_state, "missing")
        self.assertEqual(
            catalog.replacements_for("Store Iron Ingot."),
            {"Iron Ingot": "Iron Ingot"},
        )
        self.assertTrue(any("翻訳先公式言語" in warning for warning in catalog.warnings))
        self.assertTrue(any("size" in warning or "SHA-1" in warning for warning in catalog.warnings))
        self.assertEqual(catalog.coverage.minecraft_asset_warning_count, 1)
        self.assertTrue(catalog.coverage.has_partial_warnings)

    def test_corrupt_minecraft_asset_index_is_rejected_before_object_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            instance = launcher / "instances" / "Pack"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            _write_minecraft_assets(
                launcher,
                "1.20.1",
                {"item.minecraft.iron_ingot": "Iron Ingot"},
                {"item.minecraft.iron_ingot": "鉄インゴット"},
            )
            index_path = launcher / "assets" / "indexes" / "test-assets.json"
            corrupted = index_path.read_bytes().replace(b"ja_jp", b"ja_xp", 1)
            self.assertEqual(len(corrupted), index_path.stat().st_size)
            index_path.write_bytes(corrupted)

            with mock.patch.object(
                minecraft_assets_module,
                "_launcher_roots",
                return_value=(launcher,),
            ):
                catalog = ModLanguageScanner().scan(
                    mods,
                    "en_us",
                    "ja_jp",
                    minecraft_version="1.20.1",
                    instance_root=instance,
                )

        self.assertIn("Iron Ingot", catalog.entries)
        self.assertEqual(catalog.entries["Iron Ingot"].target, "Iron Ingot")
        self.assertEqual(catalog.entries["Iron Ingot"].target_state, "missing")
        self.assertEqual(catalog.coverage.minecraft_asset_warning_count, 1)
        self.assertTrue(any("asset index" in warning for warning in catalog.warnings))
        self.assertTrue(any("SHA-1" in warning for warning in catalog.warnings))

    def test_asset_index_id_only_metadata_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "launcher"
            instance = launcher / "instances" / "Pack"
            mods = instance / "mods"
            mods.mkdir(parents=True)
            _write_minecraft_assets(
                launcher,
                "1.20.1",
                {"item.minecraft.iron_ingot": "Iron Ingot"},
                {"item.minecraft.iron_ingot": "鉄インゴット"},
            )
            metadata_path = launcher / "meta" / "net.minecraft" / "1.20.1.json"
            metadata_path.write_text(
                _json({"assetIndex": {"id": "test-assets"}}),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                minecraft_version="1.20.1",
                instance_root=instance,
            )

        self.assertEqual(catalog.entries["Iron Ingot"].target, "鉄インゴット")
        self.assertEqual(catalog.coverage.minecraft_asset_warning_count, 0)

    def test_plural_alias_conflicts_and_project_titles_fail_closed(self) -> None:
        def translated(source: str, target: str, key: str) -> GlossaryEntry:
            return GlossaryEntry(
                source=source,
                target=target,
                key=key,
                mod_id=key.split(".")[1],
                translated=True,
                provenance=key,
                target_state="translated",
            )

        catalog = GlossaryCatalog(
            entries={
                "Iron Ingot": translated(
                    "Iron Ingot", "鉄インゴット", "item.minecraft.iron_ingot"
                ),
                "Flux Battery": translated(
                    "Flux Battery", "フラックス電池", "item.alpha.flux_battery"
                ),
                "Flux Batteries": translated(
                    "Flux Batteries", "蓄電器群", "item.beta.flux_batteries"
                ),
                "Red Paint": translated(
                    "Red Paint", "赤い塗料", "item.alpha.red_paint"
                ),
                "Paint": translated("Paint", "塗料", "item.alpha.paint"),
            }
        )

        self.assertEqual(
            catalog.replacements_for("Iron Ingots and Red Paints"),
            {"Iron Ingots": "鉄インゴット", "Red Paints": "赤い塗料"},
        )
        self.assertEqual(
            catalog.replacements_for("Use Flux Batteries."),
            {"Flux Batteries": "Flux Batteries"},
        )
        self.assertNotIn("Paints", catalog.replacements_for("Paints facades."))
        preserved = catalog.with_source_preserved_terms(["Iron Ingots"])
        self.assertEqual(
            preserved.replacements_for("Read the Iron Ingots quest."),
            {"Iron Ingots": "Iron Ingots"},
        )

    def test_plural_aliases_cover_safe_regular_endings_and_uppercase(self) -> None:
        def translated(source: str, target: str, key: str) -> GlossaryEntry:
            return GlossaryEntry(
                source=source,
                target=target,
                key=key,
                mod_id="example",
                translated=True,
                provenance=key,
                target_state="translated",
            )

        catalog = GlossaryCatalog(
            entries={
                "Paint Brush": translated(
                    "Paint Brush", "ペイントブラシ", "item.example.paint_brush"
                ),
                "Tool Box": translated(
                    "Tool Box", "ツールボックス", "item.example.tool_box"
                ),
                "Red Dish": translated(
                    "Red Dish", "赤い皿", "item.example.red_dish"
                ),
                "Signal Buzz": translated(
                    "Signal Buzz", "信号ブザー", "item.example.signal_buzz"
                ),
                "Power Battery": translated(
                    "Power Battery", "動力電池", "item.example.power_battery"
                ),
                "STEEL BLOCK": translated(
                    "STEEL BLOCK", "鋼鉄ブロック", "block.example.steel_block"
                ),
            }
        )

        text = (
            "Paint Brushes, Tool Boxes, Red Dishes, Signal Buzzes, "
            "Power Batteries, and STEEL BLOCKS"
        )
        self.assertEqual(
            catalog.replacements_for(text),
            {
                "Paint Brushes": "ペイントブラシ",
                "Tool Boxes": "ツールボックス",
                "Red Dishes": "赤い皿",
                "Signal Buzzes": "信号ブザー",
                "Power Batteries": "動力電池",
                "STEEL BLOCKS": "鋼鉄ブロック",
            },
        )

    def test_untranslated_registry_names_preserve_safe_regular_plurals(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "Power Battery": GlossaryEntry(
                    source="Power Battery",
                    target="Power Battery",
                    key="item.example.power_battery",
                    mod_id="example",
                    translated=False,
                    provenance="target locale missing",
                    target_state="missing",
                ),
                "Copper Widget": GlossaryEntry(
                    source="Copper Widget",
                    target="Copper Widget",
                    key="item.example.copper_widget",
                    mod_id="example",
                    translated=False,
                    provenance="target locale explicitly uses source",
                    target_state="explicit_source",
                ),
                "Iron Ingot": GlossaryEntry(
                    source="Iron Ingot",
                    target="鉄インゴット",
                    key="item.minecraft.iron_ingot",
                    mod_id="minecraft",
                    translated=True,
                    provenance="Minecraft en_us/ja_jp",
                    target_state="translated",
                ),
                "Signal Crystal": GlossaryEntry(
                    source="Signal Crystal",
                    target="Signal Crystal",
                    key="item.example.signal_crystal",
                    mod_id="example",
                    translated=False,
                    provenance="singular target locale missing",
                    target_state="missing",
                ),
                "Signal Crystals": GlossaryEntry(
                    source="Signal Crystals",
                    target="信号結晶群",
                    key="tag.item.example.signal_crystals",
                    mod_id="example",
                    translated=True,
                    provenance="explicit plural target",
                    target_state="translated",
                ),
            }
        )

        text = (
            "Power Battery and Power Batteries; Copper Widget and Copper Widgets; "
            "Iron Ingot and Iron Ingots; Signal Crystal and Signal Crystals"
        )
        self.assertEqual(
            catalog.replacements_for(text),
            {
                "Power Battery": "Power Battery",
                "Power Batteries": "Power Batteries",
                "Copper Widget": "Copper Widget",
                "Copper Widgets": "Copper Widgets",
                "Iron Ingot": "鉄インゴット",
                "Iron Ingots": "鉄インゴット",
                "Signal Crystal": "Signal Crystal",
                "Signal Crystals": "信号結晶群",
            },
        )

    def test_explicit_source_plural_blocks_a_derived_translation(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "Iron Ingot": GlossaryEntry(
                    source="Iron Ingot",
                    target="鉄インゴット",
                    key="item.minecraft.iron_ingot",
                    mod_id="minecraft",
                    translated=True,
                    provenance="Minecraft en_us/ja_jp",
                    target_state="translated",
                ),
                "Iron Ingots": GlossaryEntry(
                    source="Iron Ingots",
                    target="Iron Ingots",
                    key="tag.item.example.iron_ingots",
                    mod_id="example",
                    translated=False,
                    provenance="Mod en_us/ja_jp",
                    target_state="explicit_source",
                ),
            }
        )

        self.assertEqual(
            catalog.replacements_for("Store Iron Ingots."),
            {"Iron Ingots": "Iron Ingots"},
        )

    def test_control_or_invisible_official_target_warns_and_preserves_source(self) -> None:
        untrusted_key = "item.example.line\nforged-warning." + "x" * 10_000
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "invisible-target.jar"
            _write_jar(
                jar,
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "example", "name": "Example Mod"}
                    ),
                    "assets/example/lang/en_us.json": _json(
                        {
                            "item.example.alpha": "Alpha Tool",
                            "item.example.beta": "Beta Tool",
                            untrusted_key: "Gamma Tool",
                        }
                    ),
                    "assets/example/lang/ja_jp.json": _json(
                        {
                            "item.example.alpha": "\u200b",
                            "item.example.beta": "\x00",
                            untrusted_key: "\u200b",
                        }
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        for source in ("Alpha Tool", "Beta Tool", "Gamma Tool"):
            with self.subTest(source=source):
                self.assertEqual(catalog.entries[source].target, source)
                self.assertFalse(catalog.entries[source].translated)
        self.assertEqual(len(catalog.warnings), 3)
        self.assertTrue(
            all("制御文字" in warning and "不可視文字" in warning for warning in catalog.warnings)
        )
        untrusted_warning = next(
            warning for warning in catalog.warnings if r"\nforged-warning" in warning
        )
        self.assertNotIn("\n", untrusted_warning)
        self.assertLess(len(untrusted_warning), 500)
        self.assertIs(
            catalog.entries["Alpha Tool"].provenance,
            catalog.entries["Beta Tool"].provenance,
        )

    def test_missing_target_locale_keeps_source_and_lang_files_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "legacy.jar"
            _write_jar(
                jar,
                {
                    "assets/legacy/lang/en_us.lang": (
                        "# comment\n"
                        "item.legacy.hammer=Steam Hammer\n"
                        "block.legacy.boiler=Large Boiler\n"
                        "gui.legacy.ignore=Ignore Me\n"
                    )
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja_jp")

        self.assertEqual(catalog.scanned_archives, 1)
        self.assertEqual(set(catalog.entries), {"Steam Hammer", "Large Boiler"})
        self.assertTrue(all(not entry.translated for entry in catalog.entries.values()))
        self.assertEqual(catalog.replacements_for("Build a Large Boiler"), {"Large Boiler": "Large Boiler"})

    def test_language_file_locale_separators_and_case_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory) / "localized-paths.jar"
            _write_jar(
                jar,
                {
                    "assets/example/lang/EN-US.json": _json({"item.example.tool": "Locale Tool"}),
                    "assets/example/lang/JA-jp.json": _json({"item.example.tool": "ロケール道具"}),
                },
            )

            catalog = ModLanguageScanner().scan(jar, "en_us", "ja-JP")

        self.assertEqual(catalog.entries["Locale Tool"].target, "ロケール道具")
        self.assertTrue(catalog.entries["Locale Tool"].translated)

    def test_conflicting_mod_translations_fall_back_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            _write_jar(
                mods / "a.jar",
                {
                    "assets/a/lang/en_us.json": _json({"item.a.gear": "Mystic Gear"}),
                    "assets/a/lang/ja_jp.json": _json({"item.a.gear": "神秘の歯車"}),
                },
            )
            _write_jar(
                mods / "b.jar",
                {
                    "assets/b/lang/en_us.json": _json({"item.b.gear": "Mystic Gear"}),
                    "assets/b/lang/ja_jp.json": _json({"item.b.gear": "魔法のギア"}),
                },
            )

            catalog = ModLanguageScanner().scan(Path(directory), "en_us", "ja_jp")

        self.assertEqual(catalog.scanned_archives, 2)
        self.assertIn("Mystic Gear", catalog.conflicts)
        self.assertEqual(
            {entry.target for entry in catalog.conflicts["Mystic Gear"]},
            {"神秘の歯車", "魔法のギア"},
        )
        resolved = catalog.entries["Mystic Gear"]
        self.assertFalse(resolved.translated)
        self.assertEqual(resolved.target, "Mystic Gear")
        self.assertEqual(catalog.replacements_for("Find Mystic Gear"), {"Mystic Gear": "Mystic Gear"})

    def test_same_identity_missing_target_is_neutral_and_order_independent(self) -> None:
        for translated_first in (False, True):
            with self.subTest(translated_first=translated_first), tempfile.TemporaryDirectory() as directory:
                mods = Path(directory) / "mods"
                translated_files = {
                    "assets/shared/lang/en_us.json": _json(
                        {"item.shared.gear": "Shared Gear"}
                    ),
                    "assets/shared/lang/ja_jp.json": _json(
                        {"item.shared.gear": "共有の歯車"}
                    ),
                }
                missing_files = {
                    "assets/shared/lang/en_us.json": _json(
                        {"item.shared.gear": "Shared Gear"}
                    )
                }
                first, second = (
                    (translated_files, missing_files)
                    if translated_first
                    else (missing_files, translated_files)
                )
                _write_jar(mods / "a.jar", first)
                _write_jar(mods / "b.jar", second)

                catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

                resolved = catalog.entries["Shared Gear"]
                self.assertTrue(resolved.translated)
                self.assertEqual(resolved.target, "共有の歯車")
                self.assertEqual(resolved.target_state, "translated")
                self.assertNotIn("Shared Gear", catalog.conflicts)
                self.assertEqual(len(catalog.evidence["Shared Gear"]), 2)

    def test_explicit_source_or_another_identity_does_not_borrow_translation(self) -> None:
        scenarios = {
            "same_identity_explicit_source": (
                {
                    "assets/shared/lang/en_us.json": _json(
                        {"item.shared.gear": "Shared Gear"}
                    ),
                    "assets/shared/lang/ja_jp.json": _json(
                        {"item.shared.gear": "Shared Gear"}
                    ),
                },
                {
                    "assets/shared/lang/en_us.json": _json(
                        {"item.shared.gear": "Shared Gear"}
                    ),
                    "assets/shared/lang/ja_jp.json": _json(
                        {"item.shared.gear": "共有の歯車"}
                    ),
                },
            ),
            "different_identity_missing": (
                {
                    "assets/first/lang/en_us.json": _json(
                        {"item.first.gear": "Shared Gear"}
                    )
                },
                {
                    "assets/second/lang/en_us.json": _json(
                        {"item.second.gear": "Shared Gear"}
                    ),
                    "assets/second/lang/ja_jp.json": _json(
                        {"item.second.gear": "共有の歯車"}
                    ),
                },
            ),
        }
        for name, (first, second) in scenarios.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                mods = Path(directory) / "mods"
                _write_jar(mods / "a.jar", first)
                _write_jar(mods / "b.jar", second)

                catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

                resolved = catalog.entries["Shared Gear"]
                self.assertFalse(resolved.translated)
                self.assertEqual(resolved.target, "Shared Gear")
                self.assertIn("Shared Gear", catalog.conflicts)
                self.assertEqual(len(catalog.conflicts["Shared Gear"]), 2)

    def test_different_identities_with_the_same_official_target_are_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            for name in ("first", "second"):
                _write_jar(
                    mods / f"{name}.jar",
                    {
                        f"assets/{name}/lang/en_us.json": _json(
                            {f"item.{name}.gear": "Shared Gear"}
                        ),
                        f"assets/{name}/lang/ja_jp.json": _json(
                            {f"item.{name}.gear": "共有の歯車"}
                        ),
                    },
                )

            catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

        self.assertEqual(catalog.entries["Shared Gear"].target, "共有の歯車")
        self.assertNotIn("Shared Gear", catalog.conflicts)
        self.assertEqual(len(catalog.evidence["Shared Gear"]), 2)

    def test_bad_archive_is_warned_without_hiding_good_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            mods.mkdir(parents=True)
            (mods / "bad.jar").write_bytes(b"not a zip")
            _write_jar(
                mods / "good.jar",
                {
                    "assets/good/lang/en_us.json": _json({"item.good.tool": "Useful Tool"}),
                    "assets/good/lang/ja_jp.json": _json({"item.good.tool": "便利な道具"}),
                },
            )

            catalog = ModLanguageScanner().scan(Path(directory), "en_us", "ja_jp")

        self.assertEqual(catalog.scanned_archives, 1)
        self.assertEqual(catalog.entries["Useful Tool"].target, "便利な道具")
        self.assertEqual(len(catalog.warnings), 2)
        self.assertIn("bad.jar", catalog.warnings[0])
        self.assertIn("good.jar", catalog.warnings[1])
        self.assertIn("表示名", catalog.warnings[1])

    def test_project_reference_overrides_official_translation_without_changing_coverage(self) -> None:
        official = GlossaryEntry(
            source="Arcane Horizons",
            target="秘術の地平線",
            key="item.example.arcane_horizons",
            mod_id="example",
            translated=True,
            provenance="example.jar",
            target_state="translated",
        )
        catalog = GlossaryCatalog(
            entries={official.source: official},
            evidence={official.source: (official,)},
            scanned_archives=1,
            discovered_archives=1,
        )

        augmented = catalog.with_source_preserved_terms(["Arcane Horizons"])

        self.assertEqual(
            augmented.replacements_for("Visit Arcane Horizons."),
            {"Arcane Horizons": "Arcane Horizons"},
        )
        self.assertEqual(catalog.entries[official.source], official)
        self.assertEqual(augmented.coverage, catalog.coverage)

    def test_project_reference_keeps_existing_untranslated_mod_display_evidence(self) -> None:
        create = GlossaryEntry(
            source="Create",
            target="Create",
            key="mod.display_name.create",
            mod_id="create",
            translated=False,
            provenance="create.jar",
        )
        catalog = GlossaryCatalog(entries={create.source: create})

        augmented = catalog.with_source_preserved_terms(["Create"])

        self.assertEqual(augmented.mod_display_name_count, catalog.mod_display_name_count)
        self.assertEqual(
            augmented.replacements_for("Use Create."),
            {"Create": "Create"},
        )
        self.assertEqual(augmented.replacements_for("Create a machine."), {})

    def test_ambiguous_one_word_project_titles_do_not_hide_ordinary_prose(self) -> None:
        evidence: dict[str, tuple[GlossaryEntry, ...]] = {}
        entries: dict[str, GlossaryEntry] = {}
        for source in ("Blocks", "Plants"):
            candidates = tuple(
                GlossaryEntry(
                    source=source,
                    target=source,
                    key=f"item.{mod_id}.{source.lower()}",
                    mod_id=mod_id,
                    translated=False,
                    provenance=f"{mod_id}.jar",
                )
                for mod_id in ("first", "second")
            )
            entries[source] = candidates[0]
            evidence[source] = candidates
        catalog = GlossaryCatalog(entries=entries, evidence=evidence)
        augmented = catalog.with_source_preserved_terms(
            ["Blocks", "Plants", "Chants", "Ratlantis"]
        )

        self.assertEqual(augmented.replacements_for("Fluix Blocks can be used."), {})
        self.assertEqual(
            augmented.replacements_for("Plants and harvests a 9x9 field."),
            {},
        )
        self.assertEqual(augmented.replacements_for("Signs and Chants"), {})
        self.assertEqual(
            augmented.replacements_for("Visit Ratlantis."),
            {"Ratlantis": "Ratlantis"},
        )
        self.assertEqual(
            augmented.replacements_for("Complete the Blocks quest."),
            {"Blocks": "Blocks"},
        )

    def test_unsafe_project_reference_values_are_not_added(self) -> None:
        catalog = GlossaryCatalog()

        augmented = catalog.with_source_preserved_terms(
            ["", "   ", "Line\nBreak", "Count %s", "https://example.invalid"]
        )

        self.assertIs(augmented, catalog)

    def test_project_reference_can_span_an_escaped_ampersand_without_overlapping_syntax(self) -> None:
        catalog = GlossaryCatalog().with_source_preserved_terms(
            [r"Planets \& Dimensions"]
        )
        source = r"Read Planets \& Dimensions chapter."

        spans = catalog.replacement_spans_for_parts([source])[0]
        protected = TokenProtector().protect(source, term_spans=spans)

        self.assertEqual(
            [source[span.start : span.end] for span in spans],
            ["Planets ", " Dimensions"],
        )
        self.assertNotIn("Planets", protected.protected)
        self.assertNotIn("Dimensions", protected.protected)
        self.assertEqual(protected.restore(protected.protected), source)
        self.assertTrue(catalog.candidate_preserves_terms([source], [source]))
        self.assertFalse(
            catalog.candidate_preserves_terms(
                [source],
                [r"惑星 \& 次元のchapterを読む。"],
            )
        )

    def test_action_phrase_project_title_is_not_frozen_when_used_as_an_instruction(self) -> None:
        catalog = GlossaryCatalog().with_source_preserved_terms(
            ["Kill The Warden", "Visit the Sunken City"]
        )

        self.assertEqual(
            catalog.replacements_for("Kill The Warden to finish."),
            {},
        )
        self.assertEqual(
            catalog.replacements_for("Visit the Sunken City and return."),
            {},
        )
        self.assertEqual(catalog.replacements_for("Kill The Warden"), {})
        self.assertEqual(catalog.replacements_for("Kill The Warden."), {})
        self.assertEqual(catalog.replacements_for("Kill The Warden now."), {})
        self.assertEqual(
            catalog.replacements_for('Complete the "Kill The Warden" quest.'),
            {"Kill The Warden": "Kill The Warden"},
        )

    def test_quoted_title_list_with_a_following_label_is_reference_evidence(self) -> None:
        undergarden_evidence = tuple(
            GlossaryEntry(
                source="Undergarden",
                target="Undergarden",
                key=f"item.{mod_id}.undergarden",
                mod_id=mod_id,
                translated=False,
                provenance=f"{mod_id}.jar",
            )
            for mod_id in ("first", "second")
        )
        blocks_evidence = tuple(
            GlossaryEntry(
                source="Blocks",
                target="Blocks",
                key=f"item.{mod_id}.blocks",
                mod_id=mod_id,
                translated=False,
                provenance=f"{mod_id}.jar",
            )
            for mod_id in ("first", "second")
        )
        catalog = GlossaryCatalog(
            entries={
                "Undergarden": undergarden_evidence[0],
                "Blocks": blocks_evidence[0],
            },
            evidence={
                "Undergarden": undergarden_evidence,
                "Blocks": blocks_evidence,
            },
        ).with_source_preserved_terms(["Undergarden", "Blocks"])

        self.assertEqual(
            catalog.replacements_for(
                r'Read the "Extended Crafting \& Undergarden" chapters.'
            ),
            {"Undergarden": "Undergarden"},
        )
        self.assertEqual(
            catalog.replacements_for('Read the "Fluix Blocks" chapter.'),
            {},
        )

    def test_gerund_shaped_machine_name_is_not_treated_as_an_action_title(self) -> None:
        catalog = GlossaryCatalog().with_source_preserved_terms(["Crushing Wheels"])

        self.assertEqual(
            catalog.replacements_for("Welcome to Crushing Wheels"),
            {"Crushing Wheels": "Crushing Wheels"},
        )

    def test_game_root_scans_kubejs_and_resourcepacks_only_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "example.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "example", "name": "Example Mod"}
                    ),
                    "assets/example/lang/en_us.json": _json(
                        {"item.example.wrench": "Mod Wrench"}
                    ),
                    "assets/example/lang/ja_jp.json": _json(
                        {"item.example.wrench": "Modレンチ"}
                    ),
                },
            )
            kubejs = game_root / "kubejs"
            kubejs_lang = kubejs / "assets" / "kubejs" / "lang"
            kubejs_lang.mkdir(parents=True)
            (kubejs_lang / "en_us.json").write_text(
                _json({"item.kubejs.manual.desc": "Kube Guide"}),
                encoding="utf-8",
            )
            (kubejs_lang / "ja_jp.json").write_text(
                _json({"item.kubejs.manual.desc": "Kubeガイド"}),
                encoding="utf-8",
            )
            model = kubejs / "assets" / "kubejs" / "models" / "item"
            model.mkdir(parents=True)
            (model / "manual.desc.json").write_text("{}", encoding="utf-8")

            folder_pack = game_root / "resourcepacks" / "folder-pack"
            folder_lang = folder_pack / "assets" / "folder" / "lang"
            folder_lang.mkdir(parents=True)
            (folder_lang / "en_us.lang").write_text(
                "item.folder.relic=Folder Relic\n",
                encoding="utf-8",
            )
            (folder_lang / "ja_jp.lang").write_text(
                "item.folder.relic=フォルダーの遺物\n",
                encoding="utf-8",
            )
            _write_jar(
                game_root / "resourcepacks" / "zip-pack.zip",
                {
                    "assets/zipped/lang/en_us.json": _json(
                        {"item.zipped.relic": "ZIP Relic"}
                    ),
                    "assets/zipped/lang/ja_jp.json": _json(
                        {"item.zipped.relic": "ZIPの遺物"}
                    ),
                },
            )

            without_packs = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
            )
            with_packs = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(without_packs.entries["Mod Wrench"].target, "Modレンチ")
        self.assertEqual(without_packs.entries["Kube Guide"].target, "Kubeガイド")
        self.assertNotIn("Folder Relic", without_packs.entries)
        self.assertNotIn("ZIP Relic", without_packs.entries)
        self.assertEqual(without_packs.external_sources_discovered, 1)
        self.assertEqual(without_packs.kubejs_sources_scanned, 1)
        self.assertEqual(without_packs.resourcepack_sources_scanned, 0)
        self.assertFalse(without_packs.resourcepacks_enabled)

        self.assertEqual(with_packs.entries["Folder Relic"].target, "フォルダーの遺物")
        self.assertEqual(with_packs.entries["ZIP Relic"].target, "ZIPの遺物")
        self.assertEqual(with_packs.external_sources_discovered, 3)
        self.assertEqual(with_packs.external_sources_scanned, 3)
        self.assertEqual(with_packs.external_sources_failed, 0)
        self.assertEqual(with_packs.kubejs_sources_scanned, 1)
        self.assertEqual(with_packs.resourcepack_sources_scanned, 2)
        self.assertTrue(with_packs.resourcepacks_enabled)
        self.assertIn("追加言語資産: 3件検出", with_packs.coverage.summary)
        self.assertIn("resource pack 2件・走査有効", with_packs.coverage.summary)

    def test_game_root_inference_is_limited_to_an_explicit_mods_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            mods.mkdir()
            language = game_root / "kubejs" / "assets" / "kubejs" / "lang"
            language.mkdir(parents=True)
            (language / "en_us.json").write_text(
                _json({"item.kubejs.token": "Kube Token"}),
                encoding="utf-8",
            )

            inferred = ModLanguageScanner().scan(mods, "en_us", "ja_jp")
            ambiguous = ModLanguageScanner().scan(game_root, "en_us", "ja_jp")
            explicit_without_mods = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
            )

        self.assertIn("Kube Token", inferred.entries)
        self.assertNotIn("Kube Token", ambiguous.entries)
        self.assertIn("Kube Token", explicit_without_mods.entries)
        self.assertEqual(explicit_without_mods.discovered_archives, 0)
        self.assertEqual(explicit_without_mods.external_sources_scanned, 1)

    def test_external_zip_paths_fail_closed_without_hiding_safe_pack_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            resourcepacks = game_root / "resourcepacks"
            safe_lang = resourcepacks / "safe" / "assets" / "safe" / "lang"
            safe_lang.mkdir(parents=True)
            (safe_lang / "en_us.json").write_text(
                _json({"item.safe.relic": "Safe Pack Relic"}),
                encoding="utf-8",
            )
            _write_jar(
                resourcepacks / "unsafe.zip",
                {
                    "../outside.txt": "unsafe",
                    "assets/unsafe/lang/en_us.json": _json(
                        {"item.unsafe.relic": "Unsafe Relic"}
                    ),
                },
            )
            _write_jar(
                resourcepacks / "duplicate.zip",
                {
                    "assets/duplicate/lang/en_us.json": _json(
                        {"item.duplicate.first": "First Duplicate"}
                    ),
                    "ASSETS/DUPLICATE/LANG/EN_US.JSON": _json(
                        {"item.duplicate.second": "Second Duplicate"}
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
                limits=GlossaryScanLimits(enabled=False),
            )

        self.assertIn("Safe Pack Relic", catalog.entries)
        self.assertNotIn("Unsafe Relic", catalog.entries)
        self.assertNotIn("First Duplicate", catalog.entries)
        self.assertNotIn("Second Duplicate", catalog.entries)
        self.assertEqual(catalog.external_sources_discovered, 3)
        self.assertEqual(catalog.external_sources_scanned, 1)
        self.assertEqual(catalog.external_sources_failed, 2)
        self.assertEqual(catalog.external_asset_warning_count, 2)
        self.assertTrue(catalog.coverage.has_partial_warnings)
        self.assertTrue(any("安全でない言語資産内の項目パス" in item for item in catalog.warnings))
        self.assertTrue(any("ZIP内の項目パスが重複" in item for item in catalog.warnings))

    def test_external_duplicate_language_keys_exclude_only_ambiguous_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            language = game_root / "kubejs" / "assets" / "kubejs" / "lang"
            language.mkdir(parents=True)
            (language / "en_us.json").write_text(
                '{"item.kubejs.duplicate":"First",'
                '"item.kubejs.duplicate":"Second",'
                '"item.kubejs.safe":"Safe Kube Tool"}',
                encoding="utf-8",
            )
            (language / "ja_jp.json").write_text(
                _json(
                    {
                        "item.kubejs.duplicate": "重複",
                        "item.kubejs.safe": "安全なKube道具",
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
            )

        self.assertNotIn("First", catalog.entries)
        self.assertNotIn("Second", catalog.entries)
        self.assertEqual(catalog.entries["Safe Kube Tool"].target, "安全なKube道具")
        self.assertEqual(catalog.external_sources_scanned, 1)
        self.assertEqual(catalog.external_sources_with_warnings, 1)
        self.assertEqual(catalog.external_asset_warning_count, 1)
        self.assertTrue(any("重複した1件のkeyだけを除外" in item for item in catalog.warnings))
        self.assertFalse(
            any("item.kubejs.duplicate" in item for item in catalog.warnings)
        )
        self.assertTrue(
            any("item.kubejs.duplicate" in item for item in catalog.debug_messages)
        )

    def test_external_assets_use_the_same_deterministic_conflict_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            resourcepacks = game_root / "resourcepacks"
            for pack_name, target in (("a-pack", "最初の共有遺物"), ("b-pack", "別の共有遺物")):
                language = resourcepacks / pack_name / "assets" / "shared" / "lang"
                language.mkdir(parents=True)
                (language / "en_us.json").write_text(
                    _json({"item.shared.relic": "Shared Pack Relic"}),
                    encoding="utf-8",
                )
                (language / "ja_jp.json").write_text(
                    _json({"item.shared.relic": target}),
                    encoding="utf-8",
                )

            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(catalog.entries["Shared Pack Relic"].target, "Shared Pack Relic")
        self.assertFalse(catalog.entries["Shared Pack Relic"].translated)
        self.assertIn("Shared Pack Relic", catalog.conflicts)
        self.assertEqual(len(catalog.evidence["Shared Pack Relic"]), 2)

    def test_target_only_resourcepack_joins_validated_mod_source_by_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "source.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "joined", "name": "Joined Mod"}
                    ),
                    "assets/joined/lang/en_us.json": _json(
                        {
                            "item.joined.relic": "Joined Relic",
                            "item.joined.lesser_soul_gem": "Lesser Soul Gem",
                            "joined.codex.chapter.soul_gems": "Soul Gems",
                        }
                    ),
                    "assets/joined/models/item/lesser_soul_gem.json": "{}",
                },
            )
            target_lang = (
                game_root
                / "resourcepacks"
                / "ja-only"
                / "assets"
                / "joined"
                / "lang"
            )
            target_lang.mkdir(parents=True)
            (target_lang / "ja_jp.json").write_text(
                _json(
                    {
                        "item.joined.relic": "結合された遺物",
                        "joined.codex.chapter.soul_gems": "ソウルジェム",
                        "item.joined.unproven": "原文証拠のない値",
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(catalog.entries["Joined Relic"].target, "結合された遺物")
        self.assertTrue(catalog.entries["Joined Relic"].translated)
        self.assertIn("target-only", catalog.entries["Joined Relic"].provenance)
        self.assertEqual(catalog.entries["Soul Gems"].target, "ソウルジェム")
        self.assertEqual(catalog.entries["Soul Gems"].source_tier, "resourcepack")
        self.assertNotIn("原文証拠のない値", catalog.entries)

    def test_target_only_warning_escapes_and_bounds_untrusted_keys(self) -> None:
        untrusted_key = "item.joined.line\nforged-warning." + "x" * 10_000
        untrusted_namespace = "joined" + "x" * 10_000
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "source.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "joined", "name": "Joined Mod"}
                    ),
                    f"assets/{untrusted_namespace}/lang/en_us.json": _json(
                        {untrusted_key: "Unsafe Target-only Relic"}
                    ),
                },
            )
            _write_jar(
                game_root / "resourcepacks" / "ja-only.zip",
                {
                    f"assets/{untrusted_namespace}/lang/ja_jp.json": _json(
                        {untrusted_key: "\u200b"}
                    )
                },
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(
            catalog.entries["Unsafe Target-only Relic"].target,
            "Unsafe Target-only Relic",
        )
        warning = next(
            item for item in catalog.warnings if r"\nforged-warning" in item
        )
        self.assertNotIn("\n", warning)
        self.assertLess(len(warning), 500)

    def test_target_only_resourcepack_can_join_minecraft_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            version = "1.20.1"
            _write_minecraft_assets(
                game_root,
                version,
                {"item.minecraft.test_relic": "Minecraft Test Relic"},
                {},
            )
            target_lang = (
                game_root
                / "resourcepacks"
                / "minecraft-ja"
                / "assets"
                / "minecraft"
                / "lang"
            )
            target_lang.mkdir(parents=True)
            (target_lang / "ja_jp.json").write_text(
                _json({"item.minecraft.test_relic": "Minecraft試験遺物"}),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                minecraft_version=version,
                instance_root=game_root,
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(
            catalog.entries["Minecraft Test Relic"].target,
            "Minecraft試験遺物",
        )
        self.assertTrue(catalog.entries["Minecraft Test Relic"].translated)

    def test_partial_target_only_key_joins_even_when_pack_has_same_namespace_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "source.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "partial", "name": "Partial Mod"}
                    ),
                    "assets/partial/lang/en_us.json": _json(
                        {"item.partial.relic": "Partial Override Relic"}
                    ),
                },
            )
            pack_lang = (
                game_root
                / "resourcepacks"
                / "partial-pack"
                / "assets"
                / "partial"
                / "lang"
            )
            pack_lang.mkdir(parents=True)
            (pack_lang / "en_us.json").write_text(
                _json({"item.partial.own": "Pack Own Tool"}),
                encoding="utf-8",
            )
            (pack_lang / "ja_jp.json").write_text(
                _json(
                    {
                        "item.partial.own": "パック自身の道具",
                        "item.partial.relic": "部分上書きの遺物",
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(
            catalog.entries["Partial Override Relic"].target,
            "部分上書きの遺物",
        )
        self.assertEqual(catalog.entries["Pack Own Tool"].target, "パック自身の道具")

    def test_target_only_printf_translation_uses_validated_source_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "source.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "printf", "name": "Printf Mod"}
                    ),
                    "assets/printf/lang/en_us.json": _json(
                        {"item.printf.spawner": "%sSpawner"}
                    ),
                },
            )
            target_lang = (
                game_root
                / "resourcepacks"
                / "printf-ja"
                / "assets"
                / "printf"
                / "lang"
            )
            target_lang.mkdir(parents=True)
            (target_lang / "ja_jp.json").write_text(
                _json({"item.printf.spawner": "%sのスポナー"}),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(catalog.entries["Spawner"].target, "スポナー")
        self.assertTrue(catalog.entries["Spawner"].source_had_printf)

    def test_target_only_candidate_cannot_borrow_lower_tier_source_or_printf_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            kube_target = game_root / "kubejs" / "assets" / "shared" / "lang"
            kube_target.mkdir(parents=True)
            (kube_target / "ja_jp.json").write_text(
                _json(
                    {
                        "item.shared.relic": "Kubeの遺物",
                        "item.shared.spawner": "%sのスポナー",
                    }
                ),
                encoding="utf-8",
            )
            pack_source = (
                game_root
                / "resourcepacks"
                / "lower-source"
                / "assets"
                / "shared"
                / "lang"
            )
            pack_source.mkdir(parents=True)
            (pack_source / "en_us.json").write_text(
                _json(
                    {
                        "item.shared.relic": "Lower Pack Relic",
                        "item.shared.spawner": "%sSpawner",
                    }
                ),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        targets = {entry.target for entry in catalog.entries.values()}
        self.assertEqual(catalog.entries["Lower Pack Relic"].target, "Lower Pack Relic")
        self.assertEqual(catalog.entries["Spawner"].target, "Spawner")
        self.assertNotIn("Kubeの遺物", targets)
        self.assertNotIn("スポナー", targets)

    def test_target_only_kubejs_candidate_can_use_higher_mod_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "source.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "shared", "name": "Shared Mod"}
                    ),
                    "assets/shared/lang/en_us.json": _json(
                        {"item.shared.relic": "Higher Mod Relic"}
                    ),
                },
            )
            target = game_root / "kubejs" / "assets" / "shared" / "lang"
            target.mkdir(parents=True)
            (target / "ja_jp.json").write_text(
                _json({"item.shared.relic": "Kubeフォールバック遺物"}),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
            )

        self.assertEqual(catalog.entries["Higher Mod Relic"].target, "Kubeフォールバック遺物")
        self.assertEqual(catalog.entries["Higher Mod Relic"].source_tier, "kubejs")

    def test_target_only_is_not_joined_when_best_source_tier_disagrees_on_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            for jar_name, source in (
                ("a-old.jar", "Old Revision Relic"),
                ("b-new.jar", "New Revision Relic"),
            ):
                _write_jar(
                    mods / jar_name,
                    {
                        "fabric.mod.json": _json(
                            {"schemaVersion": 1, "id": jar_name, "name": jar_name}
                        ),
                        "assets/revision/lang/en_us.json": _json(
                            {"item.revision.relic": source}
                        ),
                    },
                )
            _write_jar(
                mods / "c-translation.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "translation", "name": "Translation"}
                    ),
                    "assets/revision/lang/ja_jp.json": _json(
                        {"item.revision.relic": "版の不明な遺物"}
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

        targets = {entry.target for entry in catalog.entries.values()}
        self.assertEqual(catalog.entries["Old Revision Relic"].target, "Old Revision Relic")
        self.assertEqual(catalog.entries["New Revision Relic"].target, "New Revision Relic")
        self.assertNotIn("版の不明な遺物", targets)

    def test_unreadable_present_source_file_does_not_create_target_only_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "source.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "source", "name": "Source Mod"}
                    ),
                    "assets/shared/lang/en_us.json": _json(
                        {"item.shared.relic": "Readable Mod Relic"}
                    ),
                },
            )
            pack = (
                game_root
                / "resourcepacks"
                / "broken-source"
                / "assets"
                / "shared"
                / "lang"
            )
            pack.mkdir(parents=True)
            (pack / "en_us.json").write_text("{not valid json", encoding="utf-8")
            (pack / "ja_jp.json").write_text(
                _json({"item.shared.relic": "証拠のない上書き"}),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        targets = {entry.target for entry in catalog.entries.values()}
        self.assertEqual(catalog.entries["Readable Mod Relic"].target, "Readable Mod Relic")
        self.assertNotIn("証拠のない上書き", targets)
        self.assertTrue(any("原文言語ファイルを読めませんでした" in warning for warning in catalog.warnings))

    def test_target_only_translation_mod_joins_source_mod_and_conflicts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            _write_jar(
                mods / "a-source.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "source", "name": "Source Mod"}
                    ),
                    "assets/sharedmod/lang/en_us.json": _json(
                        {"item.sharedmod.relic": "Translation Mod Relic"}
                    ),
                },
            )
            _write_jar(
                mods / "b-ja.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "ja", "name": "Japanese Mod"}
                    ),
                    "assets/sharedmod/lang/ja_jp.json": _json(
                        {"item.sharedmod.relic": "翻訳Modの遺物"}
                    ),
                },
            )

            translated = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

            _write_jar(
                mods / "c-ja-conflict.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "ja2", "name": "Other Japanese Mod"}
                    ),
                    "assets/sharedmod/lang/ja_jp.json": _json(
                        {"item.sharedmod.relic": "競合する別の遺物"}
                    ),
                },
            )
            conflicted = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

        self.assertEqual(translated.entries["Translation Mod Relic"].target, "翻訳Modの遺物")
        self.assertEqual(translated.entries["Translation Mod Relic"].source_tier, "mod")
        self.assertEqual(
            conflicted.entries["Translation Mod Relic"].target,
            "Translation Mod Relic",
        )
        self.assertIn("Translation Mod Relic", conflicted.conflicts)

    def test_target_only_translation_mod_uses_minecraft_missing_source_and_ignores_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = "1.20.1"
            key = "item.minecraft.mod_fallback"
            _write_minecraft_assets(
                root,
                version,
                {key: "Minecraft Mod Fallback"},
                {},
            )
            _write_jar(
                root / "mods" / "translation.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "translation", "name": "Translation Mod"}
                    ),
                    "assets/minecraft/lang/ja_jp.json": _json(
                        {
                            key: "Modフォールバック訳",
                            "item.minecraft.unknown_only": "原文のない未知訳",
                        }
                    ),
                },
            )

            catalog = ModLanguageScanner().scan(
                root / "mods",
                "en_us",
                "ja_jp",
                minecraft_version=version,
                instance_root=root,
            )

        self.assertEqual(
            catalog.entries["Minecraft Mod Fallback"].target,
            "Modフォールバック訳",
        )
        self.assertNotIn("原文のない未知訳", catalog.entries)

    def test_two_target_only_resourcepacks_with_different_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "source.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "joined", "name": "Joined Mod"}
                    ),
                    "assets/joined/lang/en_us.json": _json(
                        {"item.joined.relic": "Conflicted Joined Relic"}
                    ),
                },
            )
            for pack_name, target in (("a-ja", "最初の遺物"), ("b-ja", "二番目の遺物")):
                target_lang = (
                    game_root
                    / "resourcepacks"
                    / pack_name
                    / "assets"
                    / "joined"
                    / "lang"
                )
                target_lang.mkdir(parents=True)
                (target_lang / "ja_jp.json").write_text(
                    _json({"item.joined.relic": target}),
                    encoding="utf-8",
                )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(
            catalog.entries["Conflicted Joined Relic"].target,
            "Conflicted Joined Relic",
        )
        self.assertFalse(catalog.entries["Conflicted Joined Relic"].translated)
        self.assertIn("Conflicted Joined Relic", catalog.conflicts)

    def test_source_tier_priority_blocks_lower_targets_and_allows_missing_fallback(self) -> None:
        source = "Priority Relic"
        key = "item.priority.relic"

        def entry(
            tier: glossary_module._SourceTier,
            state: str,
            target: str,
        ) -> GlossaryEntry:
            return GlossaryEntry(
                source=source,
                target=target,
                key=key,
                mod_id="priority",
                translated=state == "translated" and target != source,
                provenance=f"{tier}-{state}-{target}",
                target_state=state,
                source_tier=tier,
            )

        lower = [
            entry("mod", "translated", "Mod訳"),
            entry("kubejs", "translated", "KubeJS訳"),
            entry("resourcepack", "translated", "Pack訳"),
        ]
        for minecraft_state, minecraft_target, expected in (
            ("translated", "Minecraft訳", "Minecraft訳"),
            ("explicit_source", source, source),
            ("rejected", source, source),
        ):
            with self.subTest(minecraft_state=minecraft_state):
                catalog = GlossaryCatalog()
                glossary_module._resolve_glossary_evidence(
                    catalog,
                    {source: [entry("minecraft", minecraft_state, minecraft_target), *lower]},
                )
                self.assertEqual(catalog.entries[source].target, expected)
                self.assertEqual(
                    {item.source_tier for item in catalog.evidence[source]},
                    {"minecraft"},
                )

        fallback_cases = (
            (
                [
                    entry("minecraft", "missing", source),
                    entry("mod", "translated", "Mod fallback"),
                    entry("kubejs", "translated", "Kube fallback"),
                ],
                "Mod fallback",
                {"minecraft", "mod"},
            ),
            (
                [
                    entry("minecraft", "missing", source),
                    entry("mod", "missing", source),
                    entry("kubejs", "translated", "Kube fallback"),
                    entry("resourcepack", "translated", "Pack fallback"),
                ],
                "Kube fallback",
                {"minecraft", "mod", "kubejs"},
            ),
            (
                [
                    entry("minecraft", "missing", source),
                    entry("mod", "missing", source),
                    entry("kubejs", "missing", source),
                    entry("resourcepack", "translated", "Pack fallback"),
                ],
                "Pack fallback",
                {"minecraft", "mod", "kubejs", "resourcepack"},
            ),
        )
        for evidence, expected, expected_tiers in fallback_cases:
            with self.subTest(expected=expected):
                catalog = GlossaryCatalog()
                glossary_module._resolve_glossary_evidence(catalog, {source: evidence})
                self.assertEqual(catalog.entries[source].target, expected)
                self.assertEqual(
                    {item.source_tier for item in catalog.evidence[source]},
                    expected_tiers,
                )

    def test_source_tier_priority_suppresses_lower_different_source_and_is_deterministic(self) -> None:
        key = "item.priority.relic"
        high = GlossaryEntry(
            "Authoritative Relic",
            "Authoritative Relic",
            key,
            "priority",
            False,
            "minecraft-source",
            target_state="missing",
            source_tier="minecraft",
        )
        lower = GlossaryEntry(
            "Renamed Lower Relic",
            "下位の訳",
            key,
            "priority",
            True,
            "mod-source",
            target_state="translated",
            source_tier="mod",
        )

        results: list[GlossaryCatalog] = []
        for evidence in (
            {
                high.source: [high],
                lower.source: [lower],
            },
            {
                lower.source: [lower],
                high.source: [high],
            },
        ):
            catalog = GlossaryCatalog()
            glossary_module._resolve_glossary_evidence(catalog, evidence)
            results.append(catalog)

        for catalog in results:
            self.assertIn(high.source, catalog.entries)
            self.assertNotIn(lower.source, catalog.entries)
            self.assertNotIn(lower.source, catalog.evidence)
        self.assertEqual(results[0].entries, results[1].entries)
        self.assertEqual(results[0].evidence, results[1].evidence)

    def test_lower_tier_different_identity_cannot_cancel_higher_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            _write_jar(
                game_root / "mods" / "higher.jar",
                {
                    "assets/higher/lang/en_us.json": _json(
                        {"item.higher.tool": "Cross Identity Tool"}
                    ),
                    "assets/higher/lang/ja_jp.json": _json(
                        {"item.higher.tool": "上位Mod訳"}
                    ),
                },
            )
            lower_lang = (
                game_root
                / "resourcepacks"
                / "lower"
                / "assets"
                / "lower"
                / "lang"
            )
            lower_lang.mkdir(parents=True)
            (lower_lang / "en_us.json").write_text(
                _json({"item.lower.tool": "Cross Identity Tool"}),
                encoding="utf-8",
            )
            (lower_lang / "ja_jp.json").write_text(
                _json({"item.lower.tool": "下位Pack訳"}),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                game_root / "mods",
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(catalog.entries["Cross Identity Tool"].target, "上位Mod訳")
        self.assertEqual(catalog.entries["Cross Identity Tool"].source_tier, "mod")
        self.assertNotIn("Cross Identity Tool", catalog.conflicts)
        self.assertEqual(
            {entry.source_tier for entry in catalog.evidence["Cross Identity Tool"]},
            {"mod", "resourcepack"},
        )
        scoped = catalog.scoped_for_resources(
            "Cross Identity Tool",
            ("lower:tool",),
        )
        self.assertEqual(scoped.entries["Cross Identity Tool"].target, "下位Pack訳")
        self.assertEqual(
            scoped.entries["Cross Identity Tool"].source_tier,
            "resourcepack",
        )

    def test_lower_different_identity_is_not_borrowed_when_higher_is_missing(self) -> None:
        source = "Unborrowed Authority Tool"
        higher_missing = GlossaryEntry(
            source,
            source,
            "item.higher.tool",
            "higher",
            False,
            "Mod source",
            target_state="missing",
            source_tier="mod",
        )
        lower_translation = GlossaryEntry(
            source,
            "別IDのPack訳",
            "item.lower.tool",
            "lower",
            True,
            "Pack translation",
            target_state="translated",
            source_tier="resourcepack",
        )
        catalog = GlossaryCatalog()

        glossary_module._resolve_glossary_evidence(
            catalog,
            {source: [lower_translation, higher_missing]},
        )

        self.assertEqual(catalog.entries[source].target, source)
        self.assertEqual(catalog.entries[source].target_state, "missing")
        self.assertEqual(catalog.entries[source].source_tier, "mod")
        self.assertNotIn(source, catalog.conflicts)
        self.assertEqual(
            {entry.source_tier for entry in catalog.evidence[source]},
            {"mod", "resourcepack"},
        )

    def test_same_identity_lower_fallback_keeps_higher_source_authority(self) -> None:
        source = "Fallback Authority Tool"
        higher_missing = GlossaryEntry(
            source,
            source,
            "item.shared.tool",
            "shared",
            False,
            "Mod source",
            target_state="missing",
            source_tier="mod",
        )
        same_identity_fallback = GlossaryEntry(
            source,
            "同一IDのPackフォールバック",
            "item.shared.tool",
            "shared",
            True,
            "Pack fallback",
            target_state="translated",
            source_tier="resourcepack",
        )
        lower_other_identity = GlossaryEntry(
            source,
            "別IDのPack訳",
            "item.other.tool",
            "other",
            True,
            "Other pack identity",
            target_state="translated",
            source_tier="resourcepack",
        )
        catalog = GlossaryCatalog()

        glossary_module._resolve_glossary_evidence(
            catalog,
            {
                source: [
                    lower_other_identity,
                    same_identity_fallback,
                    higher_missing,
                ]
            },
        )

        self.assertEqual(
            catalog.entries[source].target,
            "同一IDのPackフォールバック",
        )
        self.assertEqual(catalog.entries[source].source_tier, "resourcepack")
        self.assertNotIn(source, catalog.conflicts)

    def test_equal_authority_identities_with_different_fallbacks_conflict(self) -> None:
        source = "Equal Authority Tool"

        def entry(
            mod_id: str,
            key: str,
            tier: glossary_module._SourceTier,
            state: str,
            target: str,
        ) -> GlossaryEntry:
            return GlossaryEntry(
                source,
                target,
                key,
                mod_id,
                state == "translated",
                f"{mod_id}-{tier}-{state}",
                target_state=state,
                source_tier=tier,
            )

        evidence = [
            entry("first", "item.first.tool", "mod", "missing", source),
            entry("first", "item.first.tool", "resourcepack", "translated", "一番訳"),
            entry("second", "item.second.tool", "mod", "missing", source),
            entry("second", "item.second.tool", "kubejs", "translated", "二番訳"),
        ]
        catalog = GlossaryCatalog()

        glossary_module._resolve_glossary_evidence(catalog, {source: evidence})

        self.assertEqual(catalog.entries[source].target, source)
        self.assertEqual(catalog.entries[source].source_tier, "mod")
        self.assertIn(source, catalog.conflicts)
        self.assertEqual(len(catalog.conflicts[source]), 4)

    def test_shadowed_lower_unsafe_target_does_not_create_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            key = "item.shadowed.relic"
            _write_jar(
                mods / "authoritative.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "shadowed", "name": "Shadowed Mod"}
                    ),
                    "assets/shadowed/lang/en_us.json": _json({key: "Shadowed Relic"}),
                    "assets/shadowed/lang/ja_jp.json": _json({key: "上位の安全な遺物"}),
                },
            )
            kube = game_root / "kubejs" / "assets" / "shadowed" / "lang"
            kube.mkdir(parents=True)
            (kube / "en_us.json").write_text(
                _json({key: "Shadowed Relic"}),
                encoding="utf-8",
            )
            (kube / "ja_jp.json").write_text(
                _json({key: '%s§o"Nugget"'}),
                encoding="utf-8",
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
            )

        self.assertEqual(catalog.entries["Shadowed Relic"].target, "上位の安全な遺物")
        self.assertEqual(catalog.external_asset_warning_count, 0)
        self.assertEqual(catalog.external_sources_with_warnings, 0)
        self.assertFalse(catalog.coverage.has_partial_warnings)
        self.assertFalse(any("原文にないprintf引数" in warning for warning in catalog.warnings))

    def test_retained_invalid_target_only_mod_warning_is_counted_as_mod(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            key = "item.rats.rat_nugget_ore"
            _write_jar(
                mods / "a-source.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "rats", "name": "Rats"}
                    ),
                    "assets/rats/lang/en_us.json": _json({key: 'Rat "Nugget"'}),
                },
            )
            _write_jar(
                mods / "b-ja.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "rats_ja", "name": "Rats JA"}
                    ),
                    "assets/rats/lang/ja_jp.json": _json({key: '%s§o"Nugget"'}),
                },
            )

            catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

        self.assertEqual(catalog.entries['Rat "Nugget"'].target, 'Rat "Nugget"')
        self.assertEqual(catalog.partial_warning_count, 1)
        self.assertEqual(catalog.archives_with_warnings, 1)
        self.assertEqual(catalog.external_asset_warning_count, 0)
        self.assertEqual(catalog.external_sources_with_warnings, 0)
        self.assertTrue(any("原文にないprintf引数" in warning for warning in catalog.warnings))

    def test_same_tier_conflicts_and_missing_are_fail_closed_and_neutral(self) -> None:
        source = "Same Tier Relic"
        key = "item.same.relic"

        def entry(state: str, target: str, provenance: str) -> GlossaryEntry:
            return GlossaryEntry(
                source,
                target,
                key,
                "same",
                state == "translated" and target != source,
                provenance,
                target_state=state,
                source_tier="resourcepack",
            )

        cases = (
            (
                [entry("translated", "同じ訳", "a"), entry("translated", "同じ訳", "b")],
                "同じ訳",
                False,
            ),
            (
                [entry("missing", source, "a"), entry("translated", "安全な訳", "b")],
                "安全な訳",
                False,
            ),
            (
                [entry("translated", "一番", "a"), entry("translated", "二番", "b")],
                source,
                True,
            ),
            (
                [entry("explicit_source", source, "a"), entry("translated", "訳", "b")],
                source,
                True,
            ),
            (
                [entry("rejected", source, "a"), entry("translated", "訳", "b")],
                source,
                True,
            ),
        )
        for evidence, expected, conflicted in cases:
            with self.subTest(expected=expected, evidence=evidence):
                catalog = GlossaryCatalog()
                glossary_module._resolve_glossary_evidence(catalog, {source: evidence})
                self.assertEqual(catalog.entries[source].target, expected)
                self.assertEqual(source in catalog.conflicts, conflicted)

    def test_actual_four_tier_scan_prefers_minecraft_and_hides_lower_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            version = "1.20.1"
            key = "item.minecraft.priority_relic"
            _write_minecraft_assets(
                game_root,
                version,
                {key: "Four Tier Relic"},
                {key: "Minecraft最優先訳"},
            )
            _write_jar(
                game_root / "mods" / "tier.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "tier", "name": "Tier Mod"}
                    ),
                    "assets/minecraft/lang/en_us.json": _json({key: "Four Tier Relic"}),
                    "assets/minecraft/lang/ja_jp.json": _json({key: "Mod訳"}),
                },
            )
            kube_lang = game_root / "kubejs" / "assets" / "minecraft" / "lang"
            kube_lang.mkdir(parents=True)
            (kube_lang / "en_us.json").write_text(_json({key: "Four Tier Relic"}), encoding="utf-8")
            (kube_lang / "ja_jp.json").write_text(_json({key: "KubeJS訳"}), encoding="utf-8")
            pack_lang = (
                game_root
                / "resourcepacks"
                / "tier-pack"
                / "assets"
                / "minecraft"
                / "lang"
            )
            pack_lang.mkdir(parents=True)
            (pack_lang / "en_us.json").write_text(_json({key: "Four Tier Relic"}), encoding="utf-8")
            (pack_lang / "ja_jp.json").write_text(_json({key: "Pack訳"}), encoding="utf-8")

            catalog = ModLanguageScanner().scan(
                game_root / "mods",
                "en_us",
                "ja_jp",
                minecraft_version=version,
                instance_root=game_root,
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(catalog.entries["Four Tier Relic"].target, "Minecraft最優先訳")
        self.assertEqual(
            {item.source_tier for item in catalog.evidence["Four Tier Relic"]},
            {"minecraft"},
        )

    def test_external_zip_member_limit_fails_only_that_language_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            resourcepacks = game_root / "resourcepacks"
            _write_jar(
                resourcepacks / "limited.zip",
                {
                    "assets/limited/lang/en_us.json": _json(
                        {"item.limited.tool": "Limited Tool"}
                    ),
                    "pack.mcmeta": _json({"pack": {"pack_format": 15, "description": "test"}}),
                },
            )
            safe_language = resourcepacks / "safe" / "assets" / "safe" / "lang"
            safe_language.mkdir(parents=True)
            (safe_language / "en_us.json").write_text(
                _json({"item.safe.tool": "Safe Limit Tool"}),
                encoding="utf-8",
            )

            with mock.patch.object(glossary_module, "_MAX_ARCHIVE_MEMBERS", 3):
                catalog = ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    game_root=game_root,
                    include_resourcepacks=True,
                )

        # Discovery, the ZIP and the folder tree all remain at or below the
        # exact patched bound.
        self.assertIn("Limited Tool", catalog.entries)
        self.assertIn("Safe Limit Tool", catalog.entries)
        self.assertEqual(catalog.external_sources_failed, 0)

        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            _write_jar(
                game_root / "resourcepacks" / "too-many.zip",
                {
                    "assets/limited/lang/en_us.json": _json(
                        {"item.limited.tool": "Too Many Tool"}
                    ),
                    "pack.mcmeta": "{}",
                },
            )
            with mock.patch.object(glossary_module, "_MAX_ARCHIVE_MEMBERS", 1):
                limited_catalog = ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    game_root=game_root,
                    include_resourcepacks=True,
                )

        self.assertNotIn("Too Many Tool", limited_catalog.entries)
        self.assertEqual(limited_catalog.external_sources_failed, 1)
        self.assertTrue(any("ZIP内項目数" in item for item in limited_catalog.warnings))

    def test_cancellation_stops_before_external_asset_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            language = game_root / "kubejs" / "assets" / "kubejs" / "lang"
            language.mkdir(parents=True)
            (language / "en_us.json").write_text(
                _json({"item.kubejs.tool": "Cancelled Kube Tool"}),
                encoding="utf-8",
            )
            cancel = Event()

            def cancel_before_external(progress: glossary_module.GlossaryScanProgress) -> None:
                if progress.source_kind == "kubejs" and progress.phase == "before":
                    cancel.set()

            with self.assertRaises(CancelledError):
                ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    cancel,
                    cancel_before_external,
                    game_root=game_root,
                )

    def test_external_assets_share_the_global_language_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            mod_language = _json({"item.mod.tool": "Budget Mod Tool"})
            _write_jar(
                mods / "budget.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "mod", "name": "Budget Mod"}
                    ),
                    "assets/mod/lang/en_us.json": mod_language,
                },
            )
            kubejs_language = game_root / "kubejs" / "assets" / "kubejs" / "lang"
            kubejs_language.mkdir(parents=True)
            (kubejs_language / "en_us.json").write_text(
                _json({"item.kubejs.tool": "Budget Kube Tool"}),
                encoding="utf-8",
            )
            _write_jar(
                game_root / "resourcepacks" / "later.zip",
                {
                    "assets/later/lang/en_us.json": _json(
                        {"item.later.tool": "Later Pack Tool"}
                    )
                },
            )

            with mock.patch.object(
                glossary_module,
                "_MAX_SCAN_LANGUAGE_BYTES",
                len(mod_language.encode("utf-8")),
            ):
                catalog = ModLanguageScanner().scan(
                    mods,
                    "en_us",
                    "ja_jp",
                    game_root=game_root,
                    include_resourcepacks=True,
                )

        self.assertIn("Budget Mod Tool", catalog.entries)
        self.assertNotIn("Budget Kube Tool", catalog.entries)
        self.assertNotIn("Later Pack Tool", catalog.entries)
        self.assertEqual(catalog.external_sources_discovered, 2)
        self.assertEqual(catalog.external_sources_failed, 1)
        self.assertEqual(catalog.external_sources_skipped, 1)
        self.assertTrue(any("全走査上限" in item for item in catalog.warnings))

    def test_member_limit_is_independent_for_each_asset_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "first.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "first", "name": "First Mod"}
                    ),
                    "assets/first/lang/en_us.json": _json(
                        {"item.first.tool": "First Member Tool"}
                    ),
                },
            )
            kubejs_lang = game_root / "kubejs" / "assets" / "kubejs" / "lang"
            kubejs_lang.mkdir(parents=True)
            (kubejs_lang / "en_us.json").write_text(
                _json({"item.kubejs.tool": "Kube Member Tool"}),
                encoding="utf-8",
            )
            _write_jar(
                game_root / "resourcepacks" / "later.zip",
                {
                    "assets/later/lang/en_us.json": _json(
                        {"item.later.tool": "Later Member Tool"}
                    )
                },
            )

            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
                limits=GlossaryScanLimits(max_source_members=3),
            )

        self.assertIn("First Member Tool", catalog.entries)
        self.assertIn("Kube Member Tool", catalog.entries)
        self.assertIn("Later Member Tool", catalog.entries)
        self.assertEqual(catalog.scanned_archives, 1)
        self.assertEqual(catalog.external_sources_scanned, 2)
        self.assertEqual(catalog.external_sources_failed, 0)
        self.assertEqual(catalog.external_sources_skipped, 0)

    def test_member_limit_failure_does_not_skip_later_mod_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            _write_jar(
                mods / "a-too-many.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "bad", "name": "Bad Mod"}
                    ),
                    "one.txt": "1",
                    "two.txt": "2",
                },
            )
            _write_jar(
                mods / "b-safe.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "safe", "name": "Safe Mod"}
                    ),
                    "assets/safe/lang/en_us.json": _json(
                        {"item.safe.tool": "Safe Later Tool"}
                    ),
                },
            )
            catalog = ModLanguageScanner().scan(
                mods,
                "en_us",
                "ja_jp",
                limits=GlossaryScanLimits(max_source_members=2),
            )

        self.assertEqual(catalog.failed_archives, 1)
        self.assertEqual(catalog.scanned_archives, 1)
        self.assertIn("Safe Later Tool", catalog.entries)

    def test_mod_candidate_discovery_stops_at_per_source_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            mods.mkdir()
            for index in range(3):
                (mods / f"ignored-{index}.txt").write_text("x", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "mods直下.*3 > 2"):
                ModLanguageScanner().scan(
                    mods,
                    "en_us",
                    "ja_jp",
                    limits=GlossaryScanLimits(max_source_members=2),
                )

    def test_resourcepack_discovery_limit_cannot_starve_mod_member_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            mods = game_root / "mods"
            _write_jar(
                mods / "priority.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "priority", "name": "Priority Mod"}
                    ),
                    "assets/priority/lang/en_us.json": _json(
                        {"item.priority.tool": "Priority Budget Tool"}
                    ),
                },
            )
            resourcepacks = game_root / "resourcepacks"
            for index in range(3):
                (resourcepacks / f"pack-{index}").mkdir(parents=True)

            with mock.patch.object(glossary_module, "_MAX_ARCHIVE_MEMBERS", 2):
                catalog = ModLanguageScanner().scan(
                    mods,
                    "en_us",
                    "ja_jp",
                    game_root=game_root,
                    include_resourcepacks=True,
                )

        self.assertIn("Priority Budget Tool", catalog.entries)
        self.assertEqual(catalog.scanned_archives, 1)
        self.assertTrue(any("resourcepacksフォルダー全体" in warning for warning in catalog.warnings))

    def test_failed_language_read_does_not_refund_shared_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            resourcepacks = game_root / "resourcepacks"
            bad_payload = _json({"item.bad.tool": "Read Then Fail Tool"})
            _write_jar(
                resourcepacks / "a-bad.zip",
                {"assets/bad/lang/en_us.json": bad_payload},
            )
            _write_jar(
                resourcepacks / "b-good.zip",
                {
                    "assets/good/lang/en_us.json": _json(
                        {"item.good.tool": "Must Not Read Tool"}
                    )
                },
            )
            original_reader = glossary_module._read_limited_zip_info

            def read_then_fail(
                archive: zipfile.ZipFile,
                info: zipfile.ZipInfo,
                remaining: int,
            ) -> bytes:
                data = original_reader(archive, info, remaining)
                if "/bad/" in info.filename:
                    raise ValueError("simulated failure after bounded read")
                return data

            with (
                mock.patch.object(
                    glossary_module,
                    "_MAX_SCAN_LANGUAGE_BYTES",
                    len(bad_payload.encode("utf-8")),
                ),
                mock.patch.object(
                    glossary_module,
                    "_read_limited_zip_info",
                    side_effect=read_then_fail,
                ),
            ):
                catalog = ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    game_root=game_root,
                    include_resourcepacks=True,
                )

        self.assertNotIn("Read Then Fail Tool", catalog.entries)
        self.assertNotIn("Must Not Read Tool", catalog.entries)
        self.assertEqual(catalog.external_sources_scanned, 1)
        self.assertEqual(catalog.external_sources_failed, 1)
        self.assertTrue(any("simulated failure after bounded read" in item for item in catalog.warnings))
        self.assertTrue(any("全走査上限" in item for item in catalog.warnings))

    def test_unsafe_resourcepacks_root_is_counted_as_a_failed_language_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            (game_root / "resourcepacks").write_text("not a directory", encoding="utf-8")

            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertEqual(catalog.external_sources_discovered, 1)
        self.assertEqual(catalog.external_sources_scanned, 0)
        self.assertEqual(catalog.external_sources_failed, 1)
        self.assertEqual(catalog.external_asset_warning_count, 1)
        self.assertTrue(catalog.coverage.has_partial_warnings)
        self.assertTrue(any("resourcepacksフォルダー全体" in item for item in catalog.warnings))

    def test_resourcepack_zip_open_handle_identity_blocks_path_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            safe_zip = game_root / "resourcepacks" / "safe.zip"
            swapped_zip = game_root / "swapped.zip"
            _write_jar(
                safe_zip,
                {
                    "assets/safe/lang/en_us.json": _json(
                        {"item.safe.tool": "Original Safe Tool"}
                    )
                },
            )
            _write_jar(
                swapped_zip,
                {
                    "assets/swapped/lang/en_us.json": _json(
                        {"item.swapped.tool": "Swapped Outside Tool"}
                    )
                },
            )
            original_open = Path.open

            def swapped_open(path: Path, *args: object, **kwargs: object):
                if path == safe_zip and args and args[0] == "rb":
                    return original_open(swapped_zip, *args, **kwargs)
                return original_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=swapped_open):
                catalog = ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    game_root=game_root,
                    include_resourcepacks=True,
                )

        self.assertNotIn("Original Safe Tool", catalog.entries)
        self.assertNotIn("Swapped Outside Tool", catalog.entries)
        self.assertEqual(catalog.external_sources_failed, 1)
        self.assertTrue(any("差し替え" in item for item in catalog.warnings))

    def test_resourcepack_zip_preflight_bounds_central_directory_before_zipfile(self) -> None:
        central_size = glossary_module._MAX_EXTERNAL_ZIP_CENTRAL_DIRECTORY_BYTES + 1
        central = b"x" * central_size
        eocd = struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0,
            0,
            1,
            1,
            central_size,
            0,
            0,
        )
        payload = central + eocd

        class FakeStat:
            st_size = len(payload)

        with self.assertRaisesRegex(ValueError, "項目一覧サイズ"):
            glossary_module._preflight_resourcepack_zip(
                io.BytesIO(payload),
                FakeStat(),
                None,
            )

    def test_forged_eocd_count_is_rejected_independently_for_each_pack(self) -> None:
        def forge_entry_count(path: Path, claimed: int) -> None:
            payload = bytearray(path.read_bytes())
            index = payload.rfind(b"PK\x05\x06")
            self.assertGreaterEqual(index, 0)
            struct.pack_into("<H", payload, index + 8, claimed)
            struct.pack_into("<H", payload, index + 10, claimed)
            path.write_bytes(payload)

        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            resourcepacks = game_root / "resourcepacks"
            for name in ("a-forged.zip", "b-forged.zip", "c-unreached.zip"):
                path = resourcepacks / name
                _write_jar(
                    path,
                    {
                        f"assets/{name[0]}/lang/en_us.json": _json(
                            {f"item.{name[0]}.tool": f"{name} Tool"}
                        ),
                        "pack.mcmeta": _json(
                            {"pack": {"pack_format": 15, "description": "test"}}
                        ),
                    },
                )
                forge_entry_count(path, 1)

            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
                include_resourcepacks=True,
            )

        self.assertNotIn("a-forged.zip Tool", catalog.entries)
        self.assertNotIn("b-forged.zip Tool", catalog.entries)
        self.assertNotIn("c-unreached.zip Tool", catalog.entries)
        self.assertEqual(catalog.external_sources_failed, 3)
        self.assertEqual(catalog.external_sources_skipped, 0)
        self.assertTrue(any("項目数が一致しません" in warning for warning in catalog.warnings))

    def test_corrupt_central_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt-central.zip"
            _write_jar(
                path,
                {f"assets/test/file-{index}.txt": "x" for index in range(100)},
            )
            payload = bytearray(path.read_bytes())
            eocd_index = payload.rfind(b"PK\x05\x06")
            self.assertGreaterEqual(eocd_index, 0)
            struct.pack_into("<H", payload, eocd_index + 8, 1)
            struct.pack_into("<H", payload, eocd_index + 10, 1)
            last_header = payload.rfind(b"PK\x01\x02", 0, eocd_index)
            self.assertGreaterEqual(last_header, 0)
            payload[last_header : last_header + 4] = b"BROK"
            path.write_bytes(payload)

            budget = glossary_module._SharedScanBudget(
                remaining_language_bytes=1,
            )
            with path.open("rb") as handle, self.assertRaises(zipfile.BadZipFile):
                glossary_module._preflight_resourcepack_zip(
                    handle,
                    os.fstat(handle.fileno()),
                    budget,
                )

    def test_bounded_scandir_stops_at_limit_plus_one_before_sorting(self) -> None:
        consumed = 0

        class Child:
            def __init__(self, name: str) -> None:
                self.name = name

        class FakeScandir:
            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def __iter__(self):
                nonlocal consumed
                for index in range(10):
                    consumed += 1
                    if consumed > 3:
                        raise AssertionError("上限+1より先まで列挙しました")
                    yield Child(str(index))

        with (
            mock.patch.object(os, "scandir", return_value=FakeScandir()),
            self.assertRaisesRegex(ValueError, "上限"),
        ):
            glossary_module._bounded_sorted_scandir(
                Path("unused"),
                2,
                "test項目数",
                None,
            )

        self.assertEqual(consumed, 3)

    def test_resourcepack_zip_compressed_language_size_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            payload = _json({"item.large.tool": "Compressed Size Tool"})
            _write_jar(
                game_root / "resourcepacks" / "compressed.zip",
                {"assets/large/lang/en_us.json": payload},
            )
            with mock.patch.object(
                glossary_module,
                "_MAX_LANGUAGE_COMPRESSED_MEMBER_BYTES",
                len(payload.encode("utf-8")) - 1,
            ):
                catalog = ModLanguageScanner().scan(
                    None,
                    "en_us",
                    "ja_jp",
                    game_root=game_root,
                    include_resourcepacks=True,
                )

        self.assertNotIn("Compressed Size Tool", catalog.entries)
        self.assertEqual(catalog.external_sources_scanned, 1)
        self.assertTrue(any("圧縮サイズが上限" in item for item in catalog.warnings))

    def test_mod_language_compressed_member_size_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory) / "mods"
            payload = _json({"item.large.tool": "Compressed Mod Tool"})
            _write_jar(
                mods / "compressed.jar",
                {
                    "fabric.mod.json": _json(
                        {"schemaVersion": 1, "id": "large", "name": "Large Mod"}
                    ),
                    "assets/large/lang/en_us.json": payload,
                },
            )
            with mock.patch.object(
                glossary_module,
                "_MAX_LANGUAGE_COMPRESSED_MEMBER_BYTES",
                len(payload.encode("utf-8")) - 1,
            ):
                catalog = ModLanguageScanner().scan(mods, "en_us", "ja_jp")

        self.assertNotIn("Compressed Mod Tool", catalog.entries)
        self.assertEqual(catalog.scanned_archives, 1)
        self.assertEqual(catalog.partial_warning_count, 1)
        self.assertTrue(any("圧縮サイズが上限" in item for item in catalog.warnings))

    def test_external_directory_reparse_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game_root = Path(directory)
            assets = game_root / "kubejs" / "assets"
            assets.mkdir(parents=True)
            outside = game_root / "outside"
            outside.mkdir()
            (outside / "en_us.json").write_text(
                _json({"item.outside.tool": "Outside Tool"}),
                encoding="utf-8",
            )
            linked = assets / "linked"
            try:
                linked.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinkを作成できない環境です: {exc}")

            catalog = ModLanguageScanner().scan(
                None,
                "en_us",
                "ja_jp",
                game_root=game_root,
            )

        self.assertNotIn("Outside Tool", catalog.entries)
        self.assertEqual(catalog.external_sources_discovered, 1)
        self.assertEqual(catalog.external_sources_scanned, 0)
        self.assertEqual(catalog.external_sources_failed, 1)
        self.assertTrue(any("symlink" in item for item in catalog.warnings))


if __name__ == "__main__":
    unittest.main()
