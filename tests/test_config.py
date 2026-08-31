from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.config import AppSettings, SettingsStore  # noqa: E402
from mq_localizer.categories import DEFAULT_TRANSLATION_CATEGORY_IDS  # noqa: E402
from mq_localizer.openai_client import (  # noqa: E402
    DEFAULT_TRANSLATION_PROMPT,
    MAX_TRANSLATION_PROMPT_LENGTH,
)
from mq_localizer.scan_limits import GlossaryScanLimits, MIB_BYTES  # noqa: E402


class SettingsStoreTests(unittest.TestCase):
    def make_store(self) -> tuple[tempfile.TemporaryDirectory[str], SettingsStore]:
        temporary = tempfile.TemporaryDirectory()
        store = SettingsStore(Path(temporary.name) / "settings.json")
        return temporary, store

    def test_non_object_and_invalid_utf8_fall_back_to_defaults(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)

        store.path.write_text("[]", encoding="utf-8")
        self.assertEqual(store.load(), AppSettings())
        store.path.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(store.load(), AppSettings())

    def test_default_prompt_is_available_on_first_launch(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)

        settings = store.load()

        self.assertEqual(settings.translation_prompt, DEFAULT_TRANSLATION_PROMPT)
        self.assertIn("{source_locale}", settings.translation_prompt)
        self.assertIn("{target_locale}", settings.translation_prompt)

    def test_fields_are_validated_independently(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        store.path.write_text(
            json.dumps(
                {
                    "model": "gpt-test",
                    "cached_models": [" gpt-new ", "gpt-new", "o3"],
                    "fast_mode": True,
                    "debug_logging": True,
                    "translation_prompt": "   ",
                    "source_locale": ["not", "a", "string"],
                    "target_locale": "fr_fr",
                    "minecraft_version": "not-a-version",
                    "batch_size": "24",
                    "batch_char_limit": 12000,
                    "request_timeout": -1,
                    "max_retries": True,
                    "preserve_existing": False,
                    "skip_glossary_confirmation": True,
                    "scan_resourcepacks": True,
                    "save_api_key": False,
                    "api_key_ciphertext": "must-be-ignored",
                    "unknown": "ignored",
                }
            ),
            encoding="utf-8",
        )

        loaded = store.load()
        self.assertEqual(loaded.model, "gpt-test")
        self.assertEqual(loaded.cached_models, ["gpt-new", "o3"])
        self.assertTrue(loaded.fast_mode)
        self.assertTrue(loaded.debug_logging)
        self.assertEqual(loaded.translation_prompt, DEFAULT_TRANSLATION_PROMPT)
        self.assertEqual(loaded.source_locale, "en_us")
        self.assertEqual(loaded.target_locale, "fr_fr")
        self.assertEqual(loaded.minecraft_version, "")
        self.assertEqual(loaded.batch_size, 24)
        self.assertEqual(loaded.batch_char_limit, 12000)
        self.assertEqual(loaded.request_timeout, 120)
        self.assertEqual(loaded.max_retries, 3)
        self.assertFalse(loaded.preserve_existing)
        self.assertTrue(loaded.skip_glossary_confirmation)
        self.assertTrue(loaded.scan_resourcepacks)
        self.assertEqual(loaded.api_key_ciphertext, "")

    def test_save_round_trip_is_valid_json(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        settings = AppSettings(
            model="gpt-test",
            cached_models=["gpt-new", "gpt-test"],
            fast_mode=True,
            debug_logging=True,
            skip_glossary_confirmation=True,
            scan_resourcepacks=True,
            glossary_scan_limits_enabled=False,
            target_locale="de_de",
            minecraft_version="1.21.1",
            batch_size=12,
            max_retries=10,
            glossary_max_source_members=250_000,
            glossary_max_language_file_mib=32,
            glossary_max_source_language_mib=128,
            glossary_max_total_language_mib=1024,
        )

        store.save(settings)

        self.assertEqual(store.load(), settings)
        parsed = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", parsed)
        self.assertIs(parsed["debug_logging"], True)
        self.assertIs(parsed["skip_glossary_confirmation"], True)
        self.assertIs(parsed["scan_resourcepacks"], True)
        self.assertIs(parsed["glossary_scan_limits_enabled"], False)

    def test_glossary_scan_limit_defaults_and_byte_budgets(self) -> None:
        settings = AppSettings()

        self.assertTrue(settings.glossary_scan_limits_enabled)
        self.assertEqual(
            settings.glossary_scan_limits,
            GlossaryScanLimits(
                max_source_members=100_000,
                max_language_file_mib=16,
                max_source_language_mib=64,
                max_total_language_mib=512,
            ),
        )
        self.assertEqual(
            settings.glossary_scan_limits.max_language_file_bytes,
            16 * MIB_BYTES,
        )
        self.assertEqual(
            settings.glossary_scan_limits.max_source_language_bytes,
            64 * MIB_BYTES,
        )
        self.assertEqual(
            settings.glossary_scan_limits.max_total_language_bytes,
            512 * MIB_BYTES,
        )
        self.assertEqual(
            settings.glossary_scan_limits.effective_max_source_members,
            100_000,
        )
        self.assertEqual(
            settings.glossary_scan_limits.effective_max_language_file_bytes,
            16 * MIB_BYTES,
        )
        self.assertEqual(
            settings.glossary_scan_limits.effective_max_source_language_bytes,
            64 * MIB_BYTES,
        )
        self.assertEqual(
            settings.glossary_scan_limits.effective_max_total_language_bytes,
            512 * MIB_BYTES,
        )

    def test_disabled_glossary_scan_limits_preserve_values_but_have_no_effective_caps(
        self,
    ) -> None:
        limits = GlossaryScanLimits(
            enabled=False,
            max_source_members=250_000,
            max_language_file_mib=32,
            max_source_language_mib=128,
            max_total_language_mib=1024,
        )

        self.assertEqual(limits.max_source_members, 250_000)
        self.assertEqual(limits.max_language_file_mib, 32)
        self.assertEqual(limits.max_source_language_mib, 128)
        self.assertEqual(limits.max_total_language_mib, 1024)
        self.assertIsNone(limits.effective_max_source_members)
        self.assertIsNone(limits.effective_max_language_file_bytes)
        self.assertIsNone(limits.effective_max_source_language_bytes)
        self.assertIsNone(limits.effective_max_total_language_bytes)

    def test_glossary_scan_limits_load_only_as_a_valid_group(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        valid = {
            "glossary_scan_limits_enabled": False,
            "glossary_max_source_members": 250_000,
            "glossary_max_language_file_mib": 32,
            "glossary_max_source_language_mib": 128,
            "glossary_max_total_language_mib": 1024,
        }
        store.path.write_text(json.dumps(valid), encoding="utf-8")

        loaded = store.load()

        for name, value in valid.items():
            self.assertEqual(getattr(loaded, name), value)

        invalid_groups = (
            {**valid, "glossary_scan_limits_enabled": 1},
            {**valid, "glossary_max_source_members": True},
            {
                **valid,
                "glossary_max_language_file_mib": 129,
                "glossary_max_source_language_mib": 128,
            },
            {
                **valid,
                "glossary_max_source_language_mib": 1024,
                "glossary_max_total_language_mib": 512,
            },
        )
        defaults = AppSettings()
        for invalid in invalid_groups:
            with self.subTest(invalid=invalid):
                store.path.write_text(json.dumps(invalid), encoding="utf-8")
                loaded = store.load()
                for name in valid:
                    self.assertEqual(getattr(loaded, name), getattr(defaults, name))

    def test_glossary_scan_limit_constructor_rejects_bool_and_bad_relations(self) -> None:
        self.assertEqual(
            GlossaryScanLimits(
                max_source_members=1,
                max_language_file_mib=1,
                max_source_language_mib=1,
                max_total_language_mib=1,
            ).max_source_members,
            1,
        )
        self.assertEqual(
            GlossaryScanLimits(
                max_source_members=1_000_000,
                max_language_file_mib=256,
                max_source_language_mib=1024,
                max_total_language_mib=4096,
            ).max_total_language_mib,
            4096,
        )
        with self.assertRaises(TypeError):
            GlossaryScanLimits(max_source_members=True)
        with self.assertRaises(TypeError):
            GlossaryScanLimits(enabled=1)
        invalid_ranges = (
            {"max_source_members": 0},
            {"max_source_members": 1_000_001},
            {"max_language_file_mib": 0},
            {"max_language_file_mib": 257},
            {"max_source_language_mib": 0},
            {"max_source_language_mib": 1025},
            {"max_total_language_mib": 0},
            {"max_total_language_mib": 4097},
        )
        for invalid in invalid_ranges:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                GlossaryScanLimits(**invalid)
        with self.assertRaises(ValueError):
            GlossaryScanLimits(max_language_file_mib=65, max_source_language_mib=64)
        with self.assertRaises(ValueError):
            GlossaryScanLimits(
                max_source_language_mib=513,
                max_total_language_mib=512,
            )

    def test_removed_fields_are_ignored_and_not_written_back(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        store.path.write_text(
            json.dumps(
                {
                    "model": "gpt-saved",
                    "last_source_path": "C:/instance",
                    "last_mods_path": "C:/instance/mods",
                    "last_output_path": "C:/old-output",
                }
            ),
            encoding="utf-8",
        )

        loaded = store.load()

        self.assertEqual(loaded.model, "gpt-saved")
        self.assertEqual(loaded.last_source_path, "C:/instance")
        self.assertFalse(hasattr(loaded, "last_mods_path"))
        self.assertFalse(hasattr(loaded, "last_output_path"))

        store.save(loaded)
        rewritten = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertNotIn("last_mods_path", rewritten)
        self.assertNotIn("last_output_path", rewritten)

    def test_valid_unknown_locales_are_preserved_and_invalid_pairs_fall_back(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)

        store.save(
            AppSettings(
                source_locale="ZH_HANS_CN",
                target_locale="custom_locale_variant",
            )
        )
        migrated = store.load()
        self.assertEqual(migrated.source_locale, "zh_hans_cn")
        self.assertEqual(migrated.target_locale, "custom_locale_variant")

        store.path.write_text(
            json.dumps(
                {
                    "source_locale": "invalid-locale",
                    "target_locale": "also invalid",
                }
            ),
            encoding="utf-8",
        )
        invalid = store.load()
        self.assertEqual(
            (invalid.source_locale, invalid.target_locale),
            ("en_us", "ja_jp"),
        )

        store.path.write_text(
            json.dumps({"source_locale": "de_de", "target_locale": "de_de"}),
            encoding="utf-8",
        )
        identical = store.load()
        self.assertEqual(
            (identical.source_locale, identical.target_locale),
            ("en_us", "ja_jp"),
        )

    def test_invalid_skip_glossary_confirmation_values_fall_back_to_false(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)

        self.assertFalse(AppSettings().skip_glossary_confirmation)
        for invalid in (1, "true", None, []):
            with self.subTest(invalid=invalid):
                store.path.write_text(
                    json.dumps(
                        {
                            "model": "gpt-saved",
                            "skip_glossary_confirmation": invalid,
                        }
                    ),
                    encoding="utf-8",
                )

                loaded = store.load()

                self.assertEqual(loaded.model, "gpt-saved")
                self.assertFalse(loaded.skip_glossary_confirmation)

    def test_resourcepack_scan_migrates_off_and_rejects_non_boolean_values(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        self.assertFalse(AppSettings().scan_resourcepacks)

        store.path.write_text(
            json.dumps({"model": "gpt-legacy"}),
            encoding="utf-8",
        )
        migrated = store.load()
        self.assertEqual(migrated.model, "gpt-legacy")
        self.assertFalse(migrated.scan_resourcepacks)

        for invalid in (1, "true", None, [], {}):
            with self.subTest(invalid=invalid):
                store.path.write_text(
                    json.dumps(
                        {
                            "model": "gpt-saved",
                            "scan_resourcepacks": invalid,
                        }
                    ),
                    encoding="utf-8",
                )

                loaded = store.load()

                self.assertEqual(loaded.model, "gpt-saved")
                self.assertFalse(loaded.scan_resourcepacks)

    def test_invalid_cached_model_list_and_fast_mode_fall_back_independently(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        store.path.write_text(
            json.dumps(
                {
                    "model": "gpt-saved",
                    "cached_models": ["gpt-valid", ""],
                    "fast_mode": "yes",
                }
            ),
            encoding="utf-8",
        )

        loaded = store.load()

        self.assertEqual(loaded.model, "gpt-saved")
        self.assertEqual(loaded.cached_models, [])
        self.assertFalse(loaded.fast_mode)

    def test_debug_logging_defaults_off_and_rejects_non_boolean_values(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)

        self.assertFalse(AppSettings().debug_logging)
        store.path.write_text(
            json.dumps({"model": "gpt-saved", "debug_logging": True}),
            encoding="utf-8",
        )
        self.assertTrue(store.load().debug_logging)

        for invalid in (1, "true", None, [], {}):
            with self.subTest(invalid=invalid):
                store.path.write_text(
                    json.dumps(
                        {
                            "model": "gpt-saved",
                            "debug_logging": invalid,
                        }
                    ),
                    encoding="utf-8",
                )

                loaded = store.load()

                self.assertEqual(loaded.model, "gpt-saved")
                self.assertFalse(loaded.debug_logging)

    def test_selected_model_is_trimmed_when_loading_legacy_or_edited_settings(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        store.path.write_text(
            json.dumps({"model": "  gpt-saved  "}),
            encoding="utf-8",
        )

        self.assertEqual(store.load().model, "gpt-saved")

    def test_custom_prompt_round_trip_and_invalid_length_falls_back(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        custom = "Translate {source_locale} into {target_locale}.\nKeep the lore concise."
        settings = AppSettings(translation_prompt=custom)

        store.save(settings)

        self.assertEqual(store.load().translation_prompt, custom)
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["translation_prompt"] = "x" * (MAX_TRANSLATION_PROMPT_LENGTH + 1)
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(store.load().translation_prompt, DEFAULT_TRANSLATION_PROMPT)

    def test_exact_legacy_default_prompt_migrates_to_fragment_protocol(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        legacy_default = (
            "You are a professional Minecraft modpack localizer. Translate every item from "
            "{source_locale} to {target_locale}. Preserve meaning and natural in-game tone. "
            "Tokens shaped like __MQP_0000__ are immutable: copy each token exactly once, "
            "without changing its characters. Return one translation for every input id. "
            "Do not add explanations or notes."
        )
        store.path.write_text(
            json.dumps({"translation_prompt": f"  {legacy_default}  "}),
            encoding="utf-8",
        )

        loaded = store.load()

        self.assertEqual(loaded.translation_prompt, DEFAULT_TRANSLATION_PROMPT)
        self.assertIn("source_fragments", loaded.translation_prompt)
        self.assertNotIn("copy each token exactly once", loaded.translation_prompt)

    def test_user_customization_of_legacy_prompt_is_not_migrated(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        custom = (
            "Tokens shaped like __MQP_0000__ are immutable: copy each token exactly once. "
            "Use concise Japanese terminology selected by the user."
        )
        store.path.write_text(
            json.dumps({"translation_prompt": custom}),
            encoding="utf-8",
        )

        self.assertEqual(store.load().translation_prompt, custom)

    def test_translation_category_subset_round_trip_and_invalid_value_falls_back(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        settings = AppSettings(translation_categories=["quest_title", "quest_description"])

        store.save(settings)
        self.assertEqual(
            store.load().translation_categories,
            ["quest_title", "quest_description"],
        )

        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["translation_categories"] = ["quest_title", "unknown_category"]
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(
            set(store.load().translation_categories),
            DEFAULT_TRANSLATION_CATEGORY_IDS,
        )

        raw["translation_categories"] = []
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(store.load().translation_categories, [])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
    def test_dpapi_round_trip_and_no_plaintext_on_disk(self) -> None:
        temporary, store = self.make_store()
        self.addCleanup(temporary.cleanup)
        settings = AppSettings()
        key = "sk-test-value-that-must-not-be-plaintext"

        store.set_api_key(settings, key, True)
        store.save(settings)

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(store.read_api_key(store.load()), key)
        self.assertNotIn(key, store.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
