from __future__ import annotations

import os
import sys
import shutil
import tempfile
import tkinter as tk
import time
import unittest
from pathlib import Path
from queue import Queue
from threading import Event, Lock
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.categories import FTB_TRANSLATION_CATEGORIES  # noqa: E402
from mq_localizer.analysis_log import SessionAnalysisLog  # noqa: E402
from mq_localizer.config import AppSettings, SettingsStore  # noqa: E402
from mq_localizer.domain import (  # noqa: E402
    CancelledError,
    TranslationOutcome,
    TranslationProject,
)
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry  # noqa: E402
from mq_localizer.openai_client import (  # noqa: E402
    DEFAULT_TRANSLATION_PROMPT,
    ModelInfo,
    OpenAIRetryEvent,
)
from mq_localizer.scan_limits import GlossaryScanLimits  # noqa: E402
from mq_localizer.ui import (  # noqa: E402
    MainWindow,
    SettingsDialog,
    _DurableLogEvent,
    _WorkerTranslationEvent,
    _analysis_identity,
    _api_key_is_environment_value,
    _bounded_window_size,
    _copy_settings,
    _fitted_scrollable_window_size,
    _format_translation_completion,
    _format_full_warning_section,
    _format_openai_retry,
    _format_warning_block,
    _glossary_confirmation_reason,
    _glossary_identity,
    _glossary_scan_limits_summary,
    _glossary_status_text,
    _is_valid_locale,
    _locale_candidates,
    _minecraft_version_text,
    _model_candidates,
    _output_confirmation,
    _project_output_text,
    _redact_sensitive,
    _saved_instance_path,
    _save_api_key_initially_selected,
    _translation_completion_dialog,
    _validated_glossary_scan_limits,
    _window_geometry,
)


FIXTURES = Path(__file__).with_name("fixtures")


class _FakePromptText:
    def __init__(self, value: str) -> None:
        self.value = value
        self.focused = False

    def delete(self, _start: str, _end: str) -> None:
        self.value = ""

    def insert(self, _position: str, value: str) -> None:
        self.value = value

    def focus_set(self) -> None:
        self.focused = True

    def get(self, _start: str, _end: str) -> str:
        return self.value


class _FakeStringVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _FakeWindow:
    def __init__(self) -> None:
        self.destroyed = False

    def winfo_exists(self) -> bool:
        return not self.destroyed

    def destroy(self) -> None:
        self.destroyed = True


def _walk_widgets(widget: tk.Misc) -> list[tk.Misc]:
    result = [widget]
    for child in widget.winfo_children():
        result.extend(_walk_widgets(child))
    return result


class UiSecurityHelperTests(unittest.TestCase):
    @staticmethod
    def _legacy_raw_project(delivery: str) -> TranslationProject:
        instance = Path("instance")
        asset = (
            instance / "kubejs" / "assets" / "mq_localizer"
            if delivery == "kubejs"
            else instance / "resourcepacks" / "mq_localizer_ja_jp"
        )
        return TranslationProject(
            adapter_id="ftb_legacy_raw",
            adapter_label="FTB Quests 1.20.x以前（生のquest SNBT）",
            source_path=instance / "config" / "ftbquests" / "quests",
            default_output=asset,
            source_locale="en_us",
            target_locale="ja_jp",
            units=[],
            metadata={
                "active_quest_root": instance / "config" / "ftbquests" / "quests",
                "backup_quest_root": instance / "config" / "ftbquests" / "quests.bak",
                "asset_output_root": asset,
                "legacy_source_kind": "active",
                "legacy_language_delivery": delivery,
                "resourcepack_activation_required": delivery == "resourcepack",
            },
        )

    def test_legacy_raw_output_confirmation_lists_every_destination(self) -> None:
        for delivery, expected_label in (
            ("resourcepack", "リソースパック"),
            ("kubejs", "KubeJS言語資産"),
        ):
            with self.subTest(delivery=delivery):
                project = self._legacy_raw_project(delivery)
                output_text = _project_output_text(project)
                title, message, forced = _output_confirmation(project)

                self.assertIn("クエスト:", output_text)
                self.assertIn("quests.bak", output_text)
                self.assertIn(expected_label, output_text)
                self.assertTrue(forced)
                self.assertIn("旧版クエスト", title)
                self.assertIn("quests.bak", message)
                self.assertIn(str(project.default_output), message)
                self.assertIn("Minecraftを完全に終了", message)

    def test_resourcepack_activation_notice_is_raw_resourcepack_only(self) -> None:
        outcome = TranslationOutcome(
            output_path=Path("fallback-output"),
            total=1,
            translated=1,
            reused=0,
            copied_without_translation=0,
            glossary_terms=0,
        )
        resourcepack = self._legacy_raw_project("resourcepack")
        kubejs = self._legacy_raw_project("kubejs")
        generic = TranslationProject(
            adapter_id="ftb_modern_snbt",
            adapter_label="modern",
            source_path=Path("source.snbt"),
            default_output=Path("lang/ja_jp.snbt"),
            source_locale="en_us",
            target_locale="ja_jp",
            units=[],
            metadata={"resourcepack_activation_required": True},
        )

        self.assertIn(
            "リソースパック画面",
            _format_translation_completion(outcome, resourcepack),
        )
        self.assertNotIn(
            "リソースパック画面",
            _format_translation_completion(outcome, kubejs),
        )
        generic_completion = _format_translation_completion(outcome, generic)
        self.assertIn("出力先: fallback-output", generic_completion)
        self.assertNotIn("リソースパック画面", generic_completion)

        kind, title, message = _translation_completion_dialog(resourcepack, 2)
        self.assertEqual(kind, "warning")
        self.assertIn("有効化", title)
        self.assertIn("必ず有効化", message)
        self.assertIn("確認事項が 2 件", message)

        kind, title, message = _translation_completion_dialog(kubejs, 0)
        self.assertEqual((kind, title), ("info", "翻訳完了"))
        self.assertNotIn("必ず有効化", message)

    def test_translated_event_warns_for_resourcepack_but_not_kubejs(self) -> None:
        outcome = TranslationOutcome(
            output_path=Path("output"),
            total=1,
            translated=1,
            reused=0,
            copied_without_translation=0,
            glossary_terms=0,
        )
        for delivery, expected_warning in (("resourcepack", True), ("kubejs", False)):
            with self.subTest(delivery=delivery):
                project = self._legacy_raw_project(delivery)
                analysis = SimpleNamespace(
                    analyzed=SimpleNamespace(project=project),
                    instance=SimpleNamespace(warnings=()),
                    glossary=GlossaryCatalog(),
                    scan_resourcepacks=False,
                    log_path=None,
                    log_error="",
                )
                main = object.__new__(MainWindow)
                main.events = Queue()
                main.events.put(("translated", (outcome, analysis)))
                main._drain_after_id = None
                main.root = SimpleNamespace(  # type: ignore[assignment]
                    after=lambda _delay, _callback: "timer-id",
                    after_idle=lambda _callback: "idle-id",
                )
                main._set_detected_results = lambda _analysis: None  # type: ignore[method-assign]
                main.progress_var = _FakeStringVar()  # type: ignore[assignment]
                main.status_var = _FakeStringVar()  # type: ignore[assignment]
                main._render_durable_log_event = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
                main._append_log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
                main._report_session_log_error = lambda _error: None  # type: ignore[method-assign]
                main._set_busy = lambda _busy: None  # type: ignore[method-assign]
                main._save_settings = lambda: None  # type: ignore[method-assign]
                main.close_pending = False

                with (
                    patch("mq_localizer.ui.messagebox.showwarning") as showwarning,
                    patch("mq_localizer.ui.messagebox.showinfo") as showinfo,
                ):
                    main._drain_events()

                self.assertEqual(showwarning.call_count, int(expected_warning))
                self.assertEqual(showinfo.call_count, int(not expected_warning))
                shown = showwarning if expected_warning else showinfo
                self.assertIn(str(project.default_output), shown.call_args.args[1])
                if expected_warning:
                    self.assertIn("有効化", shown.call_args.args[0])
                    self.assertIn("必ず有効化", shown.call_args.args[1])

    def test_openai_retry_message_distinguishes_transport_reason_and_scope(self) -> None:
        message = _format_openai_retry(
            OpenAIRetryEvent(
                attempt=2,
                max_retries=5,
                delay=2.0,
                endpoint="/responses",
                status=None,
                request_id="req-safe-123",
                kind="timeout",
            )
        )

        self.assertIn("対象: 翻訳リクエスト", message)
        self.assertIn("追加試行: 2/5", message)
        self.assertIn("理由: 通信timeout", message)
        self.assertIn("完了済みの翻訳バッチは保持", message)
        self.assertIn("req-safe-123", message)

        unsafe_request_id = _format_openai_retry(
            OpenAIRetryEvent(
                attempt=1,
                max_retries=1,
                delay=1.0,
                endpoint="/models",
                status=503,
                request_id="bad\nrequest-id",
                kind="http",
            )
        )
        self.assertIn("対象: モデル一覧取得", unsafe_request_id)
        self.assertIn("理由: HTTP 503", unsafe_request_id)
        self.assertNotIn("bad", unsafe_request_id)
        self.assertNotIn("完了済みの翻訳バッチ", unsafe_request_id)

    def test_missing_mod_name_protection_requires_an_explicit_confirmation(self) -> None:
        self.assertIn("modsフォルダー", _glossary_confirmation_reason(GlossaryCatalog()))
        official_only = GlossaryCatalog(
            entries={
                "Wrench": GlossaryEntry(
                    "Wrench",
                    "レンチ",
                    "item.example.wrench",
                    "example",
                    True,
                    "example.jar!/assets/example/lang/en_us.json",
                )
            },
            scanned_archives=1,
            discovered_archives=1,
        )
        self.assertIn("Mod表示名", _glossary_confirmation_reason(official_only))
        official_only.entries["Example Mod"] = GlossaryEntry(
            "Example Mod",
            "Example Mod",
            "mod.display_name.example",
            "example",
            False,
            "example.jar!/META-INF/mods.toml",
        )
        self.assertEqual(_glossary_confirmation_reason(official_only), "")
        # A generic informational warning is not proof that protection is absent.
        official_only.warnings.append("broken.jar could not be read")
        self.assertEqual(_glossary_confirmation_reason(official_only), "")
        official_only.failed_archives = 1
        official_only.discovered_archives = 2
        self.assertIn("1件のJAR", _glossary_confirmation_reason(official_only))
        self.assertIn("Mod表示名 1件", _glossary_confirmation_reason(official_only))

    def test_minecraft_terms_remain_partial_protection_without_readable_mod_jars(self) -> None:
        minecraft_only = GlossaryCatalog(
            entries={
                "Iron Ingot": GlossaryEntry(
                    "Iron Ingot",
                    "鉄インゴット",
                    "item.minecraft.iron_ingot",
                    "minecraft",
                    True,
                    "Minecraft公式言語資産",
                )
            }
        )

        for discovered, failed in ((0, 0), (2, 2)):
            with self.subTest(discovered=discovered, failed=failed):
                minecraft_only.discovered_archives = discovered
                minecraft_only.failed_archives = failed
                reason = _glossary_confirmation_reason(minecraft_only)
                self.assertIn("公式用語 1件は保護", reason)
                self.assertIn("Mod表示名とMod公式用語", reason)
                self.assertTrue(
                    _glossary_status_text(minecraft_only).startswith("一部有効")
                )

    def test_minecraft_asset_read_warning_requires_partial_confirmation(self) -> None:
        catalog = GlossaryCatalog(
            entries={
                "Example Mod": GlossaryEntry(
                    "Example Mod",
                    "Example Mod",
                    "mod.display_name.example",
                    "example",
                    False,
                    "example.jar!/META-INF/mods.toml",
                )
            },
            scanned_archives=1,
            discovered_archives=1,
            minecraft_asset_warning_count=1,
        )

        self.assertIn("Minecraft本体の公式言語資産", _glossary_confirmation_reason(catalog))
        self.assertTrue(_glossary_status_text(catalog).startswith("一部有効"))

    def test_external_asset_failures_require_a_readable_partial_confirmation(self) -> None:
        protected = GlossaryCatalog(
            entries={
                "Example Mod": GlossaryEntry(
                    "Example Mod",
                    "Example Mod",
                    "mod.display_name.example",
                    "example",
                    False,
                    "example.jar!/META-INF/mods.toml",
                )
            },
            scanned_archives=1,
            discovered_archives=1,
            external_sources_discovered=2,
            external_sources_scanned=1,
            external_sources_failed=1,
            external_asset_warning_count=1,
            kubejs_sources_scanned=1,
            resourcepacks_enabled=True,
        )

        reason = _glossary_confirmation_reason(protected)

        self.assertIn("追加言語資産 1件を走査できず", reason)
        self.assertIn("確認事項が計1件", reason)
        self.assertTrue(_glossary_status_text(protected).startswith("一部有効"))

    def test_external_assets_are_described_when_no_protection_was_found(self) -> None:
        catalog = GlossaryCatalog(
            external_sources_discovered=1,
            external_sources_scanned=1,
            kubejs_sources_scanned=1,
        )

        reason = _glossary_confirmation_reason(catalog)

        self.assertIn("追加言語資産 1件中1件", reason)
        self.assertIn("保護用語を取得できませんでした", reason)

    def test_glossary_status_distinguishes_full_partial_and_unavailable(self) -> None:
        unavailable = GlossaryCatalog()
        self.assertTrue(_glossary_status_text(unavailable).startswith("利用不可"))

        protected = GlossaryCatalog(
            entries={
                "Example Mod": GlossaryEntry(
                    "Example Mod",
                    "Example Mod",
                    "mod.display_name.example",
                    "example",
                    False,
                    "example.jar!/META-INF/mods.toml",
                )
            },
            discovered_archives=1,
            scanned_archives=1,
        )
        self.assertTrue(_glossary_status_text(protected).startswith("有効"))
        protected.partial_warning_count = 1
        protected.archives_with_warnings = 1
        self.assertTrue(_glossary_status_text(protected).startswith("一部有効"))

    def test_glossary_confirmation_skip_emits_only_a_readable_log_event(self) -> None:
        main = object.__new__(MainWindow)
        main.events = Queue()
        main.cancel_event = Event()
        glossary = GlossaryCatalog()
        reason = _glossary_confirmation_reason(glossary)

        main._await_glossary_confirmation(glossary, skip=True)

        event, payload = main.events.get_nowait()
        self.assertEqual(event, "glossary_confirmation_skipped")
        self.assertIsInstance(payload, _DurableLogEvent)
        self.assertIn(reason, payload.message)
        self.assertFalse(payload.persisted)
        self.assertTrue(main.events.empty())

    def test_glossary_confirmation_is_unchanged_when_skip_is_disabled(self) -> None:
        main = object.__new__(MainWindow)
        main.events = Queue()
        main.cancel_event = Event()
        decision = SimpleNamespace(ready=Event(), approved=True)
        decision.ready.set()

        with patch("mq_localizer.ui._ApprovalDecision", return_value=decision):
            main._await_glossary_confirmation(GlossaryCatalog(), skip=False)

        event, payload = main.events.get_nowait()
        self.assertEqual(event, "confirm_without_glossary")
        self.assertIs(payload[0], decision)
        self.assertIn("modsフォルダー", payload[1])
        self.assertFalse(payload[2])

    def test_glossary_confirmation_denial_still_cancels_translation(self) -> None:
        main = object.__new__(MainWindow)
        main.events = Queue()
        main.cancel_event = Event()
        decision = SimpleNamespace(ready=Event(), approved=False)
        decision.ready.set()

        with (
            patch("mq_localizer.ui._ApprovalDecision", return_value=decision),
            self.assertRaisesRegex(CancelledError, "固有名詞保護"),
        ):
            main._await_glossary_confirmation(GlossaryCatalog(), skip=False)

    def test_complete_glossary_needs_no_confirmation_or_skip_notice(self) -> None:
        main = object.__new__(MainWindow)
        main.events = Queue()
        main.cancel_event = Event()
        glossary = GlossaryCatalog(
            entries={
                "Example Mod": GlossaryEntry(
                    "Example Mod",
                    "Example Mod",
                    "mod.display_name.example",
                    "example",
                    False,
                    "example.jar!/META-INF/mods.toml",
                )
            },
            discovered_archives=1,
            scanned_archives=1,
        )

        main._await_glossary_confirmation(glossary, skip=False)
        main._await_glossary_confirmation(glossary, skip=True)

        self.assertTrue(main.events.empty())

    def test_skipped_glossary_confirmation_reason_is_written_to_gui_log(self) -> None:
        main = object.__new__(MainWindow)
        main.events = Queue()
        main.events.put(
            (
                "glossary_confirmation_skipped",
                ("テスト用の確認理由", True),
            )
        )
        main._drain_after_id = "old"
        main.root = SimpleNamespace(after=lambda _delay, _callback: "timer-id")  # type: ignore[assignment]
        logged: list[tuple[str, str]] = []
        main._append_log = (  # type: ignore[method-assign]
            lambda text, tag=None, **_kwargs: logged.append((text, tag))
        )

        main._drain_events()

        self.assertEqual(len(logged), 1)
        self.assertIn("続行確認をスキップ", logged[0][0])
        self.assertIn("保護処理は無効になっていません", logged[0][0])
        self.assertIn("テスト用の確認理由", logged[0][0])
        self.assertEqual(logged[0][1], "warning")

    def test_locale_validation_matches_persisted_settings_format(self) -> None:
        for value in ("en_us", "ja_jp", "zh_hans_cn", "EN_US"):
            self.assertTrue(_is_valid_locale(value), value)
        for value in ("", "_en", "en_", "en__us", "en-us", "日本語"):
            self.assertFalse(_is_valid_locale(value), value)

    def test_locale_dropdown_candidates_cover_minecraft_and_preserve_valid_saved_values(self) -> None:
        choices = _locale_candidates(
            "ZH_HANS_CN",
            "custom_locale_variant",
            "invalid-locale",
            "ja_jp",
        )

        self.assertEqual(choices[:2], ("en_us", "ja_jp"))
        for official in ("de_de", "fr_fr", "ko_kr", "pt_br", "zh_cn", "zh_tw"):
            self.assertIn(official, choices)
        self.assertIn("zh_hans_cn", choices)
        self.assertIn("custom_locale_variant", choices)
        self.assertNotIn("invalid-locale", choices)
        self.assertEqual(len(choices), len(set(choices)))

    def test_json5_layout_infers_calendar_style_minecraft_generation(self) -> None:
        instance = SimpleNamespace(minecraft_version="", detected_by=None)
        analyzed = SimpleNamespace(adapter=SimpleNamespace(id="ftb_split_json5"))

        self.assertEqual(
            _minecraft_version_text(instance, analyzed),
            "26.1.2以降（FTB Questsの分割JSON5形式から推定）",
        )

    def test_api_keys_and_bearer_tokens_are_redacted(self) -> None:
        explicit = "custom-secret-value-123456"
        text = (
            "failed custom-secret-value-123456; "
            "Authorization: Bearer abc.def_123; "
            "fallback sk-proj-abcdefghijklmnopqrstuvwxyz"
        )

        redacted = _redact_sensitive(text, explicit)

        self.assertNotIn(explicit, redacted)
        self.assertNotIn("abc.def_123", redacted)
        self.assertNotIn("sk-proj-", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_saved_legacy_source_path_migrates_to_one_instance_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory) / "instance"
            source = instance / "config" / "ftbquests" / "quests" / "lang" / "en_us.snbt"
            source.parent.mkdir(parents=True)
            source.write_text("{}", encoding="utf-8")
            (instance / "mods").mkdir()
            settings = AppSettings(last_source_path=str(source))

            self.assertEqual(_saved_instance_path(settings), str(instance))

    def test_large_warning_block_is_bounded_and_reports_omission(self) -> None:
        log_path = Path("C:/logs/analysis.log")
        block = _format_warning_block(
            [f"warning {index}" for index in range(10_000)],
            full_log_path=log_path,
        )

        self.assertIn("確認事項（10000件）", block)
        self.assertIn("残り 9900件", block)
        self.assertIn(str(log_path), block)
        self.assertNotIn("warning 9999", block)

    def test_full_warning_section_keeps_every_warning_and_indents_continuations(self) -> None:
        warnings = [f"warning {index}" for index in range(150)]
        warnings.append("two lines\ncontinued")

        block = _format_full_warning_section(warnings, title="all warnings")

        self.assertIn("all warnings（151件）", block)
        self.assertIn("150. warning 149", block)
        self.assertIn("151. two lines\n   | continued", block)

    def test_analysis_identity_includes_source_snapshot_and_glossary_contents(self) -> None:
        instance = SimpleNamespace(
            selected_root=Path("instance"),
            instance_root=Path("instance"),
            game_root=Path("instance"),
            mods_path=Path("instance/mods"),
            minecraft_version="1.21.1",
            detected_by=("mmc-pack.json", "1.21.1"),
            evidence=(("mmc-pack.json", "1.21.1"),),
            warnings=(),
        )

        def analyzed(snapshot: str) -> SimpleNamespace:
            return SimpleNamespace(
                adapter=SimpleNamespace(id="ftb_modern_snbt"),
                project=SimpleNamespace(
                    source_path=Path("en_us.snbt"),
                    default_output=Path("ja_jp.snbt"),
                    source_locale="en_us",
                    target_locale="ja_jp",
                    metadata={"source_snapshot": snapshot},
                ),
            )

        self.assertNotEqual(
            _analysis_identity(analyzed("before"), instance),
            _analysis_identity(analyzed("after"), instance),
        )
        changed_detection = SimpleNamespace(
            selected_root=instance.selected_root,
            instance_root=instance.instance_root,
            game_root=instance.game_root,
            mods_path=instance.mods_path,
            minecraft_version=instance.minecraft_version,
            detected_by=("instance.cfg", "1.21.1"),
            evidence=(("instance.cfg", "1.21.1"),),
            warnings=("mmc-pack.jsonを読み取れませんでした",),
        )
        self.assertNotEqual(
            _analysis_identity(analyzed("before"), instance),
            _analysis_identity(analyzed("before"), changed_detection),
        )
        self.assertNotEqual(
            _analysis_identity(
                analyzed("before"),
                instance,
                scan_resourcepacks=False,
            ),
            _analysis_identity(
                analyzed("before"),
                instance,
                scan_resourcepacks=True,
            ),
        )
        first = GlossaryCatalog(scanned_archives=1, discovered_archives=1)
        second = GlossaryCatalog(scanned_archives=1, discovered_archives=1, warnings=["changed"])
        self.assertNotEqual(_glossary_identity(first), _glossary_identity(second))

    def test_settings_copy_does_not_share_category_list(self) -> None:
        original = AppSettings(
            cached_models=["gpt-test"],
            translation_categories=["quest_title"],
        )

        copied = _copy_settings(original)
        copied.cached_models.append("o3")
        copied.translation_categories.append("quest_description")

        self.assertEqual(original.cached_models, ["gpt-test"])
        self.assertEqual(original.translation_categories, ["quest_title"])
        self.assertIsNot(copied.cached_models, original.cached_models)
        self.assertIsNot(copied.translation_categories, original.translation_categories)

    def test_glossary_scan_limit_summary_uses_counts_and_mib(self) -> None:
        summary = _glossary_scan_limits_summary(
            GlossaryScanLimits(
                max_source_members=250_000,
                max_language_file_mib=32,
                max_source_language_mib=128,
                max_total_language_mib=1024,
            )
        )

        self.assertNotIn("走査上限", summary)
        self.assertIn("1資産の項目数 250,000件", summary)
        self.assertIn(
            "言語ファイル 1件 32 MiB・1資産合計 128 MiB・"
            "全資産合計 1,024 MiB",
            summary,
        )

    def test_glossary_scan_limit_summary_is_disabled_when_overall_toggle_is_off(
        self,
    ) -> None:
        self.assertEqual(
            _glossary_scan_limits_summary(GlossaryScanLimits(enabled=False)),
            "無効",
        )

    def test_glossary_scan_limit_ui_validation_is_fail_closed(self) -> None:
        limits = _validated_glossary_scan_limits(
            True,
            250_000,
            32,
            128,
            1024,
        )
        self.assertEqual(limits.max_source_members, 250_000)
        self.assertTrue(limits.enabled)

        invalid_values = (
            (1, 250_000, 32, 128, 1024, "有効・無効"),
            (True, True, 32, 128, 1024, "整数"),
            (True, 0, 32, 128, 1024, "1〜1,000,000"),
            (True, 250_000, 129, 128, 1024, "1資産の言語ファイル"),
            (True, 250_000, 32, 1024, 512, "全資産の言語ファイル"),
        )
        for *values, expected in invalid_values:
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, expected):
                    _validated_glossary_scan_limits(*values)

    def test_settings_dialog_snapshot_keeps_current_main_window_edits(self) -> None:
        main = object.__new__(MainWindow)
        main.settings = AppSettings(
            translation_categories=[category.id for category in FTB_TRANSLATION_CATEGORIES],
            last_source_path="old-instance",
        )
        selected_id = FTB_TRANSLATION_CATEGORIES[0].id
        main.category_vars = {
            category.id: _FakeStringVar(category.id == selected_id)
            for category in FTB_TRANSLATION_CATEGORIES
        }  # type: ignore[assignment]
        main.instance_var = _FakeStringVar("C:/current-instance")  # type: ignore[assignment]
        main._instance_info = SimpleNamespace(minecraft_version="1.21.1")

        snapshot = main._settings_snapshot_from_ui()

        self.assertEqual(snapshot.translation_categories, [selected_id])
        self.assertEqual(snapshot.last_source_path, "C:/current-instance")
        self.assertEqual(snapshot.minecraft_version, "1.21.1")
        self.assertEqual(snapshot.adapter_id, "auto")
        self.assertNotEqual(main.settings.translation_categories, [selected_id])
        self.assertEqual(main.settings.last_source_path, "old-instance")

    def test_main_autosave_does_not_overwrite_unified_dialog_settings(self) -> None:
        main = object.__new__(MainWindow)
        main.settings = AppSettings(
            source_locale="fr_fr",
            target_locale="de_de",
            preserve_existing=False,
            skip_glossary_confirmation=True,
            scan_resourcepacks=True,
        )
        main._instance_info = None
        main.category_vars = {
            category.id: _FakeStringVar(False)
            for category in FTB_TRANSLATION_CATEGORIES
        }  # type: ignore[assignment]
        main.instance_var = _FakeStringVar("C:/instance")  # type: ignore[assignment]
        saved: list[AppSettings] = []
        main.store = SimpleNamespace(  # type: ignore[assignment]
            save=lambda settings: saved.append(_copy_settings(settings))
        )
        main._append_log = lambda _message: None  # type: ignore[method-assign]

        main._save_settings()

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].source_locale, "fr_fr")
        self.assertEqual(saved[0].target_locale, "de_de")
        self.assertFalse(saved[0].preserve_existing)
        self.assertTrue(saved[0].skip_glossary_confirmation)
        self.assertTrue(saved[0].scan_resourcepacks)

    def test_model_candidates_migrate_selected_model_and_deduplicate_cache(self) -> None:
        self.assertEqual(
            _model_candidates(AppSettings(model="gpt-persisted")),
            ["gpt-persisted"],
        )
        self.assertEqual(
            _model_candidates(
                AppSettings(
                    model="gpt-selected",
                    cached_models=["o3", "gpt-selected", "o3"],
                )
            ),
            ["o3", "gpt-selected"],
        )
        self.assertEqual(
            _model_candidates(
                AppSettings(model="gpt-selected", cached_models=["o3"])
            ),
            ["gpt-selected", "o3"],
        )

    def test_window_size_is_bounded_by_screen_margin(self) -> None:
        self.assertEqual(
            _bounded_window_size(
                800,
                600,
                1020,
                900,
                horizontal_margin=80,
                vertical_margin=120,
            ),
            (720, 480),
        )
        self.assertEqual(
            _bounded_window_size(
                1920,
                1080,
                1020,
                900,
                horizontal_margin=80,
                vertical_margin=120,
            ),
            (1020, 900),
        )

    def test_scrollable_window_fit_includes_both_scrollbars(self) -> None:
        self.assertEqual(
            _fitted_scrollable_window_size(
                911,
                900,
                17,
                17,
                1020,
                900,
            ),
            (1020, 917),
        )
        self.assertEqual(
            _fitted_scrollable_window_size(
                1100,
                1000,
                17,
                18,
                1020,
                900,
            ),
            (1117, 1018),
        )

    def test_window_geometry_preserves_absolute_negative_monitor_coordinates(self) -> None:
        self.assertEqual(_window_geometry(800, 720), "800x720")
        self.assertEqual(
            _window_geometry(800, 720, (123, 145)),
            "800x720+123+145",
        )
        self.assertEqual(
            _window_geometry(800, 720, (-1920, -100)),
            "800x720+-1920+-100",
        )


class SettingsPromptTests(unittest.TestCase):
    def make_dialog(self) -> SettingsDialog:
        dialog = object.__new__(SettingsDialog)
        dialog.window = object()
        dialog.prompt_text = _FakePromptText("custom prompt")  # type: ignore[assignment]
        dialog.status_var = _FakeStringVar()  # type: ignore[assignment]
        return dialog

    def test_prompt_reset_requires_confirmation(self) -> None:
        dialog = self.make_dialog()

        with patch("mq_localizer.ui.messagebox.askyesno", return_value=False) as confirm:
            dialog._reset_prompt()

        self.assertEqual(dialog.prompt_text.value, "custom prompt")  # type: ignore[attr-defined]
        self.assertFalse(dialog.prompt_text.focused)  # type: ignore[attr-defined]
        confirm.assert_called_once()

    def test_confirmed_prompt_reset_restores_default(self) -> None:
        dialog = self.make_dialog()

        with patch("mq_localizer.ui.messagebox.askyesno", return_value=True):
            dialog._reset_prompt()

        self.assertEqual(dialog.prompt_text.value, DEFAULT_TRANSLATION_PROMPT)  # type: ignore[attr-defined]
        self.assertTrue(dialog.prompt_text.focused)  # type: ignore[attr-defined]
        self.assertIn("保存すると反映", dialog.status_var.value)  # type: ignore[attr-defined]

    def test_glossary_scan_limits_can_be_reset_to_defaults(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.glossary_scan_limits_enabled_var = _FakeStringVar(False)  # type: ignore[arg-type,assignment]
        dialog.glossary_max_source_members_var = _FakeStringVar(999)  # type: ignore[arg-type,assignment]
        dialog.glossary_max_language_file_mib_var = _FakeStringVar(99)  # type: ignore[arg-type,assignment]
        dialog.glossary_max_source_language_mib_var = _FakeStringVar(99)  # type: ignore[arg-type,assignment]
        dialog.glossary_max_total_language_mib_var = _FakeStringVar(99)  # type: ignore[arg-type,assignment]
        dialog.glossary_limit_status_var = _FakeStringVar()  # type: ignore[assignment]

        dialog._reset_glossary_scan_limits()

        defaults = GlossaryScanLimits()
        self.assertEqual(
            dialog.glossary_scan_limits_enabled_var.get(),
            defaults.enabled,
        )
        self.assertEqual(
            dialog.glossary_max_source_members_var.get(),
            defaults.max_source_members,
        )
        self.assertEqual(
            dialog.glossary_max_language_file_mib_var.get(),
            defaults.max_language_file_mib,
        )
        self.assertEqual(
            dialog.glossary_max_source_language_mib_var.get(),
            defaults.max_source_language_mib,
        )
        self.assertEqual(
            dialog.glossary_max_total_language_mib_var.get(),
            defaults.max_total_language_mib,
        )
        self.assertIn("保存", dialog.glossary_limit_status_var.get())

    def test_invalid_glossary_scan_limits_select_glossary_tab(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.window = object()
        dialog.api_key_var = _FakeStringVar("")  # type: ignore[assignment]
        dialog.model_var = _FakeStringVar("")  # type: ignore[assignment]
        dialog.source_locale_var = _FakeStringVar("en_us")  # type: ignore[assignment]
        dialog.target_locale_var = _FakeStringVar("ja_jp")  # type: ignore[assignment]
        dialog.glossary_max_source_members_var = _FakeStringVar(0)  # type: ignore[arg-type,assignment]
        dialog.settings = AppSettings()
        dialog.models = []
        selected_tabs: list[str] = []
        dialog._select_tab = selected_tabs.append  # type: ignore[method-assign]

        with patch("mq_localizer.ui.messagebox.showwarning") as warning:
            dialog._save()

        warning.assert_called_once()
        self.assertEqual(selected_tabs, ["glossary"])
        self.assertIn(
            "1〜1,000,000",
            warning.call_args.args[1],
        )

    def test_close_requests_model_fetch_cancellation(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.window = _FakeWindow()  # type: ignore[assignment]
        dialog.model_cancel_event = Event()

        dialog._close()

        self.assertTrue(dialog.model_cancel_event.is_set())
        self.assertTrue(dialog.window.destroyed)  # type: ignore[attr-defined]

    def test_environment_api_key_is_never_preselected_for_dpapi_persistence(self) -> None:
        settings = AppSettings(
            save_api_key=True,
            api_key_ciphertext="existing-dpapi-ciphertext",
        )
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "sk-environment-key"},
            clear=False,
        ):
            self.assertTrue(_api_key_is_environment_value("sk-environment-key"))
            self.assertFalse(_api_key_is_environment_value("sk-session-key"))
        self.assertFalse(
            _save_api_key_initially_selected(settings, True, True)
        )
        self.assertTrue(
            _save_api_key_initially_selected(settings, True, False)
        )
        self.assertFalse(
            _save_api_key_initially_selected(settings, False, False)
        )

    def test_saving_general_settings_preserves_dpapi_key_hidden_by_environment(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.settings = AppSettings(
            save_api_key=True,
            api_key_ciphertext="existing-dpapi-ciphertext",
        )
        dialog.api_key_from_environment = True
        dialog._initial_api_key = "sk-environment-key"
        dialog.save_key_var = _FakeStringVar(False)  # type: ignore[assignment]
        calls: list[tuple[str, bool]] = []

        class RecordingStore:
            def set_api_key(
                self,
                _settings: AppSettings,
                api_key: str,
                persist: bool,
            ) -> None:
                calls.append((api_key, persist))

        dialog.store = RecordingStore()  # type: ignore[assignment]
        dialog._apply_api_key_persistence("sk-environment-key")

        self.assertEqual(calls, [])
        self.assertTrue(dialog.settings.save_api_key)
        self.assertEqual(
            dialog.settings.api_key_ciphertext,
            "existing-dpapi-ciphertext",
        )

        dialog.save_key_var.set(True)
        dialog._apply_api_key_persistence("sk-environment-key")
        self.assertEqual(calls, [("sk-environment-key", True)])

    def test_successful_model_fetch_replaces_candidates_and_selects_first(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.window = _FakeWindow()  # type: ignore[assignment]
        dialog.api_key_var = _FakeStringVar("new-key")  # type: ignore[assignment]
        dialog.model_var = _FakeStringVar("old-model")  # type: ignore[assignment]
        dialog.status_var = _FakeStringVar()  # type: ignore[assignment]
        dialog.models = ["old-model"]
        configured: list[dict[str, object]] = []
        dialog.model_combo = SimpleNamespace(  # type: ignore[assignment]
            configure=lambda **kwargs: configured.append(kwargs)
        )
        fetching: list[bool] = []
        dialog._set_fetching = lambda value: fetching.append(value)  # type: ignore[method-assign]

        dialog._models_loaded(
            "new-key",
            [ModelInfo("gpt-new"), ModelInfo("o3"), ModelInfo("gpt-new")],
        )

        self.assertEqual(dialog.models, ["gpt-new", "o3"])
        self.assertEqual(dialog.model_var.get(), "gpt-new")
        self.assertEqual(configured, [{"values": ["gpt-new", "o3"]}])
        self.assertEqual(fetching, [False])
        self.assertIn("2 件", dialog.status_var.get())

    def test_model_fetch_failure_is_redacted_and_forwarded_to_session_log(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.window = _FakeWindow()  # type: ignore[assignment]
        secret = "custom-secret-value-123456"
        dialog.api_key_var = _FakeStringVar(secret)  # type: ignore[assignment]
        dialog.status_var = _FakeStringVar()  # type: ignore[assignment]
        fetching: list[bool] = []
        dialog._set_fetching = lambda value: fetching.append(value)  # type: ignore[method-assign]
        logged: list[tuple[str, str | None, str]] = []
        dialog.on_log = lambda message, tag, section: logged.append(  # type: ignore[assignment]
            (message, tag, section)
        )

        with patch("mq_localizer.ui.messagebox.showerror") as showerror:
            dialog._models_failed(f"request failed with {secret}")

        self.assertEqual(fetching, [False])
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0][1:], ("error", "OpenAIモデル一覧取得エラー"))
        self.assertIn("[API KEY REDACTED]", logged[0][0])
        self.assertNotIn(secret, logged[0][0])
        shown_message = showerror.call_args.args[1]
        self.assertIn("request failed", shown_message)
        self.assertNotIn(secret, shown_message)

    def test_model_fetch_failure_is_durable_before_dialog_queue_and_rendered_once(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.model_events = Queue()
        secret = "custom-secret-value-123456"
        durable_calls: list[tuple[str, str, str]] = []

        def persist_before_queue(
            message: str,
            *,
            level: str,
            section: str,
        ) -> _DurableLogEvent:
            durable_calls.append((message, level, section))
            return _DurableLogEvent(message=message, persisted=True)

        dialog.on_durable_log = persist_before_queue  # type: ignore[assignment]

        dialog._queue_model_failure("request failed with " + secret, secret)

        self.assertEqual(len(durable_calls), 1)
        self.assertNotIn(secret, durable_calls[0][0])
        self.assertEqual(durable_calls[0][1:], ("ERROR", "OpenAIモデル一覧取得エラー"))
        event, notice = dialog.model_events.get_nowait()
        self.assertEqual(event, "failed")
        self.assertTrue(notice.persisted)

        dialog.window = _FakeWindow()  # type: ignore[assignment]
        dialog.api_key_var = _FakeStringVar(secret)  # type: ignore[assignment]
        dialog.status_var = _FakeStringVar()  # type: ignore[assignment]
        fetching: list[bool] = []
        dialog._set_fetching = lambda value: fetching.append(value)  # type: ignore[method-assign]
        rendered: list[tuple[str, str | None, str, bool]] = []
        dialog.on_log = (  # type: ignore[assignment]
            lambda message, tag, section, persist=True: rendered.append(
                (message, tag, section, persist)
            )
        )

        with patch("mq_localizer.ui.messagebox.showerror"):
            dialog._models_failed(notice)

        self.assertEqual(len(durable_calls), 1)
        self.assertEqual(len(rendered), 1)
        self.assertFalse(rendered[0][3])
        self.assertNotIn(secret, rendered[0][0])
        self.assertEqual(fetching, [False])

    def test_model_fetch_retry_uses_its_own_durable_warning_section(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.model_events = Queue()
        durable_calls: list[tuple[str, str, str]] = []

        def persist_before_queue(
            message: str,
            *,
            level: str,
            section: str,
        ) -> _DurableLogEvent:
            durable_calls.append((message, level, section))
            return _DurableLogEvent(message=message, persisted=True)

        dialog.on_durable_log = persist_before_queue  # type: ignore[assignment]
        event = OpenAIRetryEvent(
            attempt=1,
            max_retries=3,
            delay=1.0,
            endpoint="/models",
            status=None,
            request_id="",
            kind="connection",
        )

        dialog._queue_model_retry(event, "sk-secret-value-123456")

        self.assertEqual(len(durable_calls), 1)
        self.assertEqual(durable_calls[0][1:], ("WARNING", "OpenAI通信再試行"))
        queued_event, notice = dialog.model_events.get_nowait()
        self.assertEqual(queued_event, "retry")
        self.assertTrue(notice.persisted)

        dialog.window = _FakeWindow()  # type: ignore[assignment]
        dialog.status_var = _FakeStringVar()  # type: ignore[assignment]
        rendered: list[tuple[str, str | None, str, bool]] = []
        dialog.on_log = (  # type: ignore[assignment]
            lambda message, tag, section, persist=True: rendered.append(
                (message, tag, section, persist)
            )
        )

        dialog._model_retrying(notice)

        self.assertIn("再試行", dialog.status_var.get())
        self.assertEqual(len(rendered), 1)
        self.assertEqual(
            rendered[0][1:],
            ("warning", "OpenAI通信再試行", False),
        )
        self.assertNotIn("翻訳結果の安全確認", rendered[0][0])

    def test_model_fetch_failure_uses_ui_fallback_when_worker_journal_fails(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.model_events = Queue()
        secret = "custom-secret-value-123456"

        def fail_before_queue(
            _message: str,
            *,
            level: str,
            section: str,
        ) -> _DurableLogEvent:
            del level, section
            raise OSError("cannot persist " + secret)

        dialog.on_durable_log = fail_before_queue  # type: ignore[assignment]

        dialog._queue_model_failure("request failed with " + secret, secret)

        event, notice = dialog.model_events.get_nowait()
        self.assertEqual(event, "failed")
        self.assertFalse(notice.persisted)
        self.assertNotIn(secret, notice.message)
        self.assertNotIn(secret, notice.log_error)

        dialog.window = _FakeWindow()  # type: ignore[assignment]
        dialog.api_key_var = _FakeStringVar(secret)  # type: ignore[assignment]
        dialog.status_var = _FakeStringVar()  # type: ignore[assignment]
        dialog._set_fetching = lambda _value: None  # type: ignore[method-assign]
        fallback: list[tuple[str, str | None, str]] = []
        dialog.on_log = lambda message, tag, section: fallback.append(  # type: ignore[assignment]
            (message, tag, section)
        )

        with patch("mq_localizer.ui.messagebox.showerror"):
            dialog._models_failed(notice)

        self.assertEqual(len(fallback), 1)
        self.assertEqual(fallback[0][1:], ("error", "OpenAIモデル一覧取得エラー"))
        self.assertNotIn(secret, fallback[0][0])

    def test_persisted_model_and_fast_mode_can_be_saved_without_fetching_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dialog = object.__new__(SettingsDialog)
            dialog.window = object()
            dialog.api_key_var = _FakeStringVar("sk-current-key")  # type: ignore[assignment]
            dialog.model_var = _FakeStringVar("gpt-persisted")  # type: ignore[assignment]
            dialog.fast_mode_var = _FakeStringVar(True)  # type: ignore[assignment]
            dialog.batch_var = _FakeStringVar(24)  # type: ignore[assignment]
            dialog.char_limit_var = _FakeStringVar(9000)  # type: ignore[assignment]
            dialog.timeout_var = _FakeStringVar(120)  # type: ignore[assignment]
            dialog.retry_var = _FakeStringVar(0)  # type: ignore[assignment]
            dialog.source_locale_var = _FakeStringVar("fr_fr")  # type: ignore[assignment]
            dialog.target_locale_var = _FakeStringVar("de_de")  # type: ignore[assignment]
            dialog.preserve_var = _FakeStringVar(False)  # type: ignore[assignment]
            dialog.scan_resourcepacks_var = _FakeStringVar(True)  # type: ignore[assignment]
            dialog.skip_glossary_confirmation_var = _FakeStringVar(True)  # type: ignore[assignment]
            dialog.glossary_scan_limits_enabled_var = _FakeStringVar(False)  # type: ignore[arg-type,assignment]
            dialog.glossary_max_source_members_var = _FakeStringVar(250_000)  # type: ignore[arg-type,assignment]
            dialog.glossary_max_language_file_mib_var = _FakeStringVar(32)  # type: ignore[arg-type,assignment]
            dialog.glossary_max_source_language_mib_var = _FakeStringVar(128)  # type: ignore[arg-type,assignment]
            dialog.glossary_max_total_language_mib_var = _FakeStringVar(1024)  # type: ignore[arg-type,assignment]
            dialog.save_key_var = _FakeStringVar(False)  # type: ignore[assignment]
            dialog.prompt_text = _FakePromptText(DEFAULT_TRANSLATION_PROMPT)  # type: ignore[assignment]
            dialog.models = ["gpt-persisted", "o3"]
            dialog.settings = AppSettings()
            dialog.store = SettingsStore(Path(directory) / "settings.json")
            saved: list[tuple[str, AppSettings]] = []
            dialog.on_save = lambda key, settings: saved.append(  # type: ignore[assignment]
                (key, _copy_settings(settings))
            )
            closed: list[bool] = []
            dialog._close = lambda: closed.append(True)  # type: ignore[method-assign]

            with (
                patch("mq_localizer.ui.messagebox.showwarning") as warning,
                patch("mq_localizer.ui.messagebox.showerror") as error,
            ):
                dialog._save()

            warning.assert_not_called()
            error.assert_not_called()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0][0], "sk-current-key")
            self.assertEqual(saved[0][1].model, "gpt-persisted")
            self.assertEqual(saved[0][1].cached_models, ["gpt-persisted", "o3"])
            self.assertTrue(saved[0][1].fast_mode)
            self.assertEqual(saved[0][1].max_retries, 0)
            self.assertEqual(saved[0][1].source_locale, "fr_fr")
            self.assertEqual(saved[0][1].target_locale, "de_de")
            self.assertFalse(saved[0][1].preserve_existing)
            self.assertTrue(saved[0][1].scan_resourcepacks)
            self.assertTrue(saved[0][1].skip_glossary_confirmation)
            self.assertEqual(
                saved[0][1].glossary_scan_limits,
                GlossaryScanLimits(
                    enabled=False,
                    max_source_members=250_000,
                    max_language_file_mib=32,
                    max_source_language_mib=128,
                    max_total_language_mib=1024,
                ),
            )
            self.assertEqual(closed, [True])
            loaded = dialog.store.load()
            self.assertEqual(loaded.cached_models, ["gpt-persisted", "o3"])
            self.assertTrue(loaded.fast_mode)
            self.assertEqual(loaded.max_retries, 0)
            self.assertEqual(loaded.source_locale, "fr_fr")
            self.assertEqual(loaded.target_locale, "de_de")
            self.assertFalse(loaded.preserve_existing)
            self.assertTrue(loaded.scan_resourcepacks)
            self.assertTrue(loaded.skip_glossary_confirmation)
            self.assertFalse(loaded.glossary_scan_limits_enabled)
            self.assertEqual(loaded.glossary_scan_limits, saved[0][1].glossary_scan_limits)

    def test_general_settings_can_be_saved_before_openai_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dialog = object.__new__(SettingsDialog)
            dialog.window = object()
            dialog.api_key_var = _FakeStringVar("")  # type: ignore[assignment]
            dialog.model_var = _FakeStringVar("")  # type: ignore[assignment]
            dialog.fast_mode_var = _FakeStringVar(False)  # type: ignore[assignment]
            dialog.batch_var = _FakeStringVar(24)  # type: ignore[assignment]
            dialog.char_limit_var = _FakeStringVar(9000)  # type: ignore[assignment]
            dialog.timeout_var = _FakeStringVar(120)  # type: ignore[assignment]
            dialog.retry_var = _FakeStringVar(3)  # type: ignore[assignment]
            dialog.source_locale_var = _FakeStringVar("fr_fr")  # type: ignore[assignment]
            dialog.target_locale_var = _FakeStringVar("ja_jp")  # type: ignore[assignment]
            dialog.preserve_var = _FakeStringVar(False)  # type: ignore[assignment]
            dialog.scan_resourcepacks_var = _FakeStringVar(True)  # type: ignore[assignment]
            dialog.skip_glossary_confirmation_var = _FakeStringVar(True)  # type: ignore[assignment]
            dialog.save_key_var = _FakeStringVar(False)  # type: ignore[assignment]
            dialog.prompt_text = _FakePromptText(DEFAULT_TRANSLATION_PROMPT)  # type: ignore[assignment]
            dialog.models = []
            dialog.settings = AppSettings()
            dialog.store = SettingsStore(Path(directory) / "settings.json")
            saved: list[AppSettings] = []
            dialog.on_save = lambda _key, settings: saved.append(  # type: ignore[assignment]
                _copy_settings(settings)
            )
            dialog._close = lambda: None  # type: ignore[method-assign]

            with (
                patch("mq_localizer.ui.messagebox.showwarning") as warning,
                patch("mq_localizer.ui.messagebox.showerror") as error,
            ):
                dialog._save()

            warning.assert_not_called()
            error.assert_not_called()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].model, "")
            self.assertEqual(saved[0].source_locale, "fr_fr")
            self.assertFalse(saved[0].preserve_existing)
            self.assertTrue(saved[0].scan_resourcepacks)
            self.assertTrue(saved[0].skip_glossary_confirmation)

    def test_same_source_and_target_locale_are_rejected_without_mutation(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.window = object()
        dialog.api_key_var = _FakeStringVar("")  # type: ignore[assignment]
        dialog.model_var = _FakeStringVar("")  # type: ignore[assignment]
        dialog.source_locale_var = _FakeStringVar("en_us")  # type: ignore[assignment]
        dialog.target_locale_var = _FakeStringVar("en_us")  # type: ignore[assignment]
        dialog.settings = AppSettings(source_locale="en_us", target_locale="ja_jp")
        dialog.models = []

        with patch("mq_localizer.ui.messagebox.showwarning") as warning:
            dialog._save()

        warning.assert_called_once()
        self.assertIn("異なる値", warning.call_args.args[1])
        self.assertEqual(dialog.settings.source_locale, "en_us")
        self.assertEqual(dialog.settings.target_locale, "ja_jp")

    def test_retry_count_outside_zero_to_ten_or_non_integer_is_rejected(self) -> None:
        for value in (-1, 11, "not-an-integer"):
            with self.subTest(value=value):
                dialog = object.__new__(SettingsDialog)
                dialog.window = object()
                dialog.api_key_var = _FakeStringVar("sk-current-key")  # type: ignore[assignment]
                dialog.model_var = _FakeStringVar("gpt-test")  # type: ignore[assignment]
                dialog.fast_mode_var = _FakeStringVar(False)  # type: ignore[assignment]
                dialog.batch_var = _FakeStringVar(24)  # type: ignore[assignment]
                dialog.char_limit_var = _FakeStringVar(9000)  # type: ignore[assignment]
                dialog.timeout_var = _FakeStringVar(120)  # type: ignore[assignment]
                dialog.retry_var = _FakeStringVar(value)  # type: ignore[arg-type,assignment]
                dialog.save_key_var = _FakeStringVar(False)  # type: ignore[assignment]
                dialog.prompt_text = _FakePromptText(DEFAULT_TRANSLATION_PROMPT)  # type: ignore[assignment]
                dialog.models = ["gpt-test"]
                dialog.settings = AppSettings()
                saved: list[bool] = []
                dialog.on_save = lambda _key, _settings: saved.append(True)  # type: ignore[assignment]
                dialog._close = lambda: saved.append(True)  # type: ignore[method-assign]

                with patch("mq_localizer.ui.messagebox.showerror") as showerror:
                    dialog._save()

                showerror.assert_called_once()
                self.assertEqual(saved, [])
                self.assertEqual(dialog.settings.max_retries, 3)

    def test_settings_save_failure_is_redacted_and_forwarded_to_session_log(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.window = object()
        secret = "custom-secret-value-123456"
        dialog.api_key_var = _FakeStringVar(secret)  # type: ignore[assignment]
        dialog.model_var = _FakeStringVar("gpt-test")  # type: ignore[assignment]
        dialog.fast_mode_var = _FakeStringVar(False)  # type: ignore[assignment]
        dialog.batch_var = _FakeStringVar(24)  # type: ignore[assignment]
        dialog.char_limit_var = _FakeStringVar(9000)  # type: ignore[assignment]
        dialog.timeout_var = _FakeStringVar(120)  # type: ignore[assignment]
        dialog.retry_var = _FakeStringVar(3)  # type: ignore[assignment]
        dialog.source_locale_var = _FakeStringVar("fr_fr")  # type: ignore[assignment]
        dialog.target_locale_var = _FakeStringVar("de_de")  # type: ignore[assignment]
        dialog.preserve_var = _FakeStringVar(False)  # type: ignore[assignment]
        dialog.scan_resourcepacks_var = _FakeStringVar(True)  # type: ignore[assignment]
        dialog.skip_glossary_confirmation_var = _FakeStringVar(True)  # type: ignore[assignment]
        dialog.save_key_var = _FakeStringVar(True)  # type: ignore[assignment]
        dialog.prompt_text = _FakePromptText(DEFAULT_TRANSLATION_PROMPT)  # type: ignore[assignment]
        dialog.models = ["gpt-test"]
        original = AppSettings()
        dialog.settings = _copy_settings(original)

        class FailingStore:
            def set_api_key(
                self,
                _settings: AppSettings,
                _api_key: str,
                _persist: bool,
            ) -> None:
                raise OSError(f"cannot protect {secret}")

            def save(self, _settings: AppSettings) -> None:
                raise AssertionError("save must not run after key protection fails")

        dialog.store = FailingStore()  # type: ignore[assignment]
        logged: list[tuple[str, str | None, str]] = []
        dialog.on_log = lambda message, tag, section: logged.append(  # type: ignore[assignment]
            (message, tag, section)
        )
        saved: list[bool] = []
        dialog.on_save = lambda _key, _settings: saved.append(True)  # type: ignore[assignment]
        dialog._close = lambda: saved.append(True)  # type: ignore[method-assign]

        with (
            patch("mq_localizer.ui.messagebox.showwarning") as showwarning,
            patch("mq_localizer.ui.messagebox.showerror") as showerror,
        ):
            dialog._save()

        showwarning.assert_not_called()
        showerror.assert_called_once()
        self.assertEqual(saved, [])
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0][1:], ("error", "設定保存エラー"))
        self.assertIn("[API KEY REDACTED]", logged[0][0])
        self.assertNotIn(secret, logged[0][0])
        self.assertNotIn(secret, showerror.call_args.args[1])
        self.assertEqual(original.source_locale, "en_us")
        self.assertEqual(original.target_locale, "ja_jp")
        self.assertTrue(original.preserve_existing)
        self.assertFalse(original.scan_resourcepacks)
        self.assertFalse(original.skip_glossary_confirmation)

    def test_main_window_routes_settings_failures_to_existing_session_journal(self) -> None:
        main = object.__new__(MainWindow)
        main.root = object()  # type: ignore[assignment]
        main.settings = AppSettings(model="gpt-test")
        main.session_api_key = "key"
        main.store = object()  # type: ignore[assignment]
        recorded: list[tuple[str, str | None, str]] = []
        main._append_log = (  # type: ignore[method-assign]
            lambda message, tag=None, *, section="": recorded.append(
                (message, tag, section)
            )
        )
        captured: list[tuple[object, ...]] = []
        captured_kwargs: list[dict[str, object]] = []

        with patch(
            "mq_localizer.ui.SettingsDialog",
            side_effect=lambda *args, **kwargs: (
                captured.append(args),
                captured_kwargs.append(kwargs),
            ),
        ):
            main._open_settings()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured_kwargs, [{"initial_tab": "translation"}])
        on_log = captured[0][5]
        self.assertTrue(callable(on_log))
        on_log("failure", "error", "settings")
        self.assertEqual(recorded, [("failure", "error", "settings")])

    def test_settings_button_opens_openai_until_api_key_and_model_exist(self) -> None:
        cases = (
            ("", "", "openai"),
            ("sk-current-key", "", "openai"),
            ("", "gpt-test", "openai"),
            ("sk-current-key", "gpt-test", "translation"),
        )
        for api_key, model, expected_tab in cases:
            with self.subTest(api_key=bool(api_key), model=bool(model)):
                main = object.__new__(MainWindow)
                main.root = object()  # type: ignore[assignment]
                main.settings = AppSettings(model=model)
                main.session_api_key = api_key
                main.store = object()  # type: ignore[assignment]
                main._append_log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
                captured: list[dict[str, object]] = []

                with patch(
                    "mq_localizer.ui.SettingsDialog",
                    side_effect=lambda *_args, **kwargs: captured.append(kwargs),
                ):
                    main._open_settings()

                self.assertEqual(captured, [{"initial_tab": expected_tab}])

    def test_model_outside_saved_or_fetched_candidates_cannot_be_saved(self) -> None:
        dialog = object.__new__(SettingsDialog)
        dialog.window = object()
        dialog.api_key_var = _FakeStringVar("sk-current-key")  # type: ignore[assignment]
        dialog.model_var = _FakeStringVar("unlisted-model")  # type: ignore[assignment]
        dialog.models = ["gpt-persisted"]
        dialog.settings = AppSettings()

        with patch("mq_localizer.ui.messagebox.showwarning") as warning:
            dialog._save()

        warning.assert_called_once()
        self.assertIn("保存済みまたは取得済み", warning.call_args.args[1])

    def test_main_window_accepts_persisted_model_without_current_session_fetch(self) -> None:
        main = object.__new__(MainWindow)
        main.session_api_key = "sk-current-key"
        main.settings = AppSettings(
            model="gpt-persisted",
            cached_models=["gpt-persisted"],
        )
        main._analyzed = None
        main._instance_info = None
        opened: list[bool] = []
        main._open_settings = lambda: opened.append(True)  # type: ignore[method-assign]

        with patch("mq_localizer.ui.messagebox.showwarning") as warning:
            main._translate()

        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[0], "解析")
        self.assertEqual(opened, [])

    def test_missing_openai_fields_open_the_openai_settings_tab(self) -> None:
        cases = (
            ("", AppSettings(model="gpt-test"), "APIキー"),
            ("sk-current-key", AppSettings(model=""), "モデル"),
        )
        for api_key, settings, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                main = object.__new__(MainWindow)
                main.session_api_key = api_key
                main.settings = settings
                opened: list[str] = []
                main._open_settings = lambda tab="translation": opened.append(tab)  # type: ignore[method-assign]

                with patch("mq_localizer.ui.messagebox.showwarning") as warning:
                    main._translate()

                warning.assert_called_once()
                self.assertIn(expected_message, warning.call_args.args[1])
                self.assertEqual(opened, ["openai"])

    def test_saved_settings_update_runtime_and_invalidate_only_analysis_options(self) -> None:
        main = object.__new__(MainWindow)
        main.session_api_key = "old-key"
        main.settings = AppSettings()
        main.settings_summary_var = _FakeStringVar()  # type: ignore[assignment]
        logged: list[str] = []
        main._append_log = lambda message, **_kwargs: logged.append(message)  # type: ignore[method-assign]
        invalidations: list[bool] = []
        main._invalidate_analysis = lambda: invalidations.append(True)  # type: ignore[method-assign]
        updated = AppSettings(
            model="gpt-persisted",
            cached_models=["gpt-persisted", "o3"],
            fast_mode=True,
            source_locale="fr_fr",
            target_locale="de_de",
            preserve_existing=False,
            scan_resourcepacks=True,
            skip_glossary_confirmation=True,
            glossary_scan_limits_enabled=False,
        )

        main._settings_updated("new-key", updated)
        updated.cached_models.append("changed-after-save")

        self.assertEqual(main.session_api_key, "new-key")
        self.assertEqual(main.settings.model, "gpt-persisted")
        self.assertEqual(main.settings.cached_models, ["gpt-persisted", "o3"])
        self.assertTrue(main.settings.fast_mode)
        self.assertFalse(main.settings.preserve_existing)
        self.assertTrue(main.settings.scan_resourcepacks)
        self.assertTrue(main.settings.skip_glossary_confirmation)
        self.assertFalse(main.settings.glossary_scan_limits_enabled)
        self.assertEqual(invalidations, [True])
        self.assertIn("既存翻訳の再利用: OFF", main.settings_summary_var.get())
        self.assertIn("resourcepacks走査: ON", main.settings_summary_var.get())
        self.assertIn("固有名詞保護の確認: 省略", main.settings_summary_var.get())
        self.assertIn("Fast Mode: ON", logged[0])
        self.assertIn("locale: fr_fr → de_de", logged[0])
        self.assertIn("固有名詞保護の走査上限: 無効", logged[0])

        same_analysis_options = _copy_settings(main.settings)
        same_analysis_options.preserve_existing = True
        same_analysis_options.skip_glossary_confirmation = False
        main._settings_updated("new-key", same_analysis_options)

        self.assertEqual(invalidations, [True])
        self.assertTrue(main.settings.preserve_existing)
        self.assertFalse(main.settings.skip_glossary_confirmation)


class WorkerCancellationTests(unittest.TestCase):
    def make_main(self) -> MainWindow:
        main = object.__new__(MainWindow)
        main.cancel_event = Event()
        main._request_values = lambda: {  # type: ignore[method-assign]
            "instance_root": Path("instance"),
            "source_locale": "en_us",
            "target_locale": "ja_jp",
        }
        main._invalidate_analysis = lambda *_args: None  # type: ignore[method-assign]
        main._replace_log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        main._queue_analysis_stage = lambda _message: None  # type: ignore[method-assign]
        main._start_worker = lambda function, _event: function()  # type: ignore[method-assign]
        return main

    def test_analyze_cancelled_after_project_parse_never_starts_mod_scan(self) -> None:
        main = self.make_main()
        scanner_called = False

        class Application:
            def analyze(self, **_request: object) -> object:
                main.cancel_event.set()
                return object()

        class Scanner:
            def scan(self, *_args: object) -> GlossaryCatalog:
                nonlocal scanner_called
                scanner_called = True
                return GlossaryCatalog()

        main.application = Application()  # type: ignore[assignment]
        main.scanner = Scanner()  # type: ignore[assignment]
        instance = SimpleNamespace(
            game_root=Path("instance"),
            minecraft_version="1.21.1",
            mods_path=Path("instance/mods"),
        )

        with (
            patch("mq_localizer.ui.inspect_instance_root", return_value=instance),
            self.assertRaises(CancelledError),
        ):
            main._analyze()
        self.assertFalse(scanner_called)

    def test_analyze_cancelled_during_final_mod_scan_never_reports_success(self) -> None:
        main = self.make_main()
        analyzed = SimpleNamespace(
            adapter=SimpleNamespace(id="ftb_modern_snbt"),
            project=SimpleNamespace(
                source_path=Path("input.snbt"),
                source_locale="en_us",
                target_locale="ja_jp",
            )
        )
        main.application = SimpleNamespace(analyze=lambda **_request: analyzed)  # type: ignore[assignment]

        class Scanner:
            def scan(self, *_args: object, **_kwargs: object) -> GlossaryCatalog:
                main.cancel_event.set()
                return GlossaryCatalog()

        main.scanner = Scanner()  # type: ignore[assignment]

        instance = SimpleNamespace(
            game_root=Path("instance"),
            minecraft_version="1.21.1",
            mods_path=Path("instance/mods"),
        )
        with (
            patch("mq_localizer.ui.inspect_instance_root", return_value=instance),
            self.assertRaises(CancelledError),
        ):
            main._analyze()

    def test_analysis_passes_resourcepack_option_and_derived_game_root_to_scanner(self) -> None:
        main = self.make_main()
        analyzed = SimpleNamespace(
            adapter=SimpleNamespace(id="ftb_modern_snbt"),
            project=SimpleNamespace(
                source_locale="en_us",
                target_locale="ja_jp",
            ),
        )
        main.application = SimpleNamespace(analyze=lambda **_request: analyzed)  # type: ignore[assignment]
        captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

        class Scanner:
            def scan(self, *args: object, **kwargs: object) -> GlossaryCatalog:
                captured.append((args, kwargs))
                return GlossaryCatalog()

        main.scanner = Scanner()  # type: ignore[assignment]
        main.analysis_log = None  # type: ignore[assignment]
        instance = SimpleNamespace(
            instance_root=Path("launcher/instance"),
            game_root=Path("instance/minecraft"),
            minecraft_version="1.21.1",
            mods_path=Path("instance/minecraft/mods"),
        )
        limits = GlossaryScanLimits(
            max_source_members=250_000,
            max_language_file_mib=32,
            max_source_language_mib=128,
            max_total_language_mib=1024,
        )
        request = {
            "instance_root": Path("instance"),
            "source_locale": "en_us",
            "target_locale": "ja_jp",
            "scan_resourcepacks": True,
            "glossary_scan_limits": limits,
        }

        with patch("mq_localizer.ui.inspect_instance_root", return_value=instance):
            result = main._inspect_and_analyze(request)

        self.assertTrue(result.scan_resourcepacks)
        self.assertEqual(len(captured), 1)
        _args, kwargs = captured[0]
        self.assertEqual(kwargs["game_root"], instance.game_root)
        self.assertIs(kwargs["include_resourcepacks"], True)
        self.assertEqual(kwargs["instance_root"], instance.instance_root)
        self.assertIs(kwargs["limits"], limits)
        self.assertIs(result.glossary_scan_limits, limits)

    def test_completed_analysis_writes_uncapped_warnings_to_session_log(self) -> None:
        main = self.make_main()
        project_warnings = [f"project warning {index}" for index in range(150)]
        glossary_warnings = [f"jar warning {index}" for index in range(150)]
        analyzed = SimpleNamespace(
            adapter=SimpleNamespace(id="ftb_modern_snbt"),
            project=SimpleNamespace(
                adapter_label="FTB Quests native locale SNBT",
                source_path=Path("instance/lang/en_us.snbt"),
                default_output=Path("instance/lang/ja_jp.snbt"),
                source_locale="en_us",
                target_locale="ja_jp",
                units=[SimpleNamespace(category="quest_title")],
                existing={},
                warnings=project_warnings,
            ),
        )
        main.application = SimpleNamespace(analyze=lambda **_request: analyzed)  # type: ignore[assignment]
        glossary = GlossaryCatalog(warnings=glossary_warnings)
        main.scanner = SimpleNamespace(scan=lambda *_args, **_kwargs: glossary)  # type: ignore[assignment]
        captured: list[str] = []
        captured_metadata: list[dict[str, str]] = []
        expected_path = Path.cwd() / "logs" / "analysis-test.log"

        class AnalysisLog:
            def write(self, text: str, *_secrets: str, **_metadata: str) -> Path:
                captured.append(text)
                captured_metadata.append(_metadata)
                return expected_path

        main.analysis_log = AnalysisLog()  # type: ignore[assignment]
        instance = SimpleNamespace(
            selected_root=Path("instance"),
            instance_root=Path("instance"),
            game_root=Path("instance"),
            mods_path=Path("instance/mods"),
            minecraft_version="1.21.1",
            detected_by=None,
            evidence=(),
            warnings=tuple(f"instance warning {index}" for index in range(150)),
        )

        with patch("mq_localizer.ui.inspect_instance_root", return_value=instance):
            result = main._inspect_and_analyze(main._request_values())

        self.assertEqual(result.log_path, expected_path)
        self.assertEqual(result.log_error, "")
        self.assertEqual(len(captured), 1)
        self.assertIn("150. instance warning 149", captured[0])
        self.assertIn("300. project warning 149", captured[0])
        self.assertIn("150. jar warning 149", captured[0])
        self.assertIn("resourcepacks走査: 無効", captured[0])
        self.assertIn(
            "走査上限: 1資産の項目数 100,000件",
            captured[0],
        )
        self.assertIn(
            "全資産合計 512 MiB",
            captured[0],
        )
        self.assertIn("資産間の優先度: Minecraft本体 > Mod > KubeJS > resource pack", captured[0])
        self.assertIn("下位候補で上位の訳を無効にしません", captured[0])
        self.assertIn("下位の値で上書きしません", captured[0])
        self.assertEqual(captured_metadata, [{"level": "INFO", "section": "解析結果全文"}])

    def test_request_accepts_only_an_instance_directory_and_locales(self) -> None:
        main = object.__new__(MainWindow)
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory) / "instance"
            instance.mkdir()
            main.instance_var = _FakeStringVar(str(instance))  # type: ignore[assignment]
            main.settings = AppSettings(
                source_locale="en_us",
                target_locale="ja_jp",
                scan_resourcepacks=True,
                glossary_max_source_members=250_000,
                glossary_max_language_file_mib=32,
                glossary_max_source_language_mib=128,
                glossary_max_total_language_mib=1024,
            )

            request = main._request_values()

        self.assertEqual(
            request,
            {
                "instance_root": instance,
                "source_locale": "en_us",
                "target_locale": "ja_jp",
                "scan_resourcepacks": True,
                "glossary_scan_limits": GlossaryScanLimits(
                    max_source_members=250_000,
                    max_language_file_mib=32,
                    max_source_language_mib=128,
                    max_total_language_mib=1024,
                ),
            },
        )

    def test_request_rejects_a_file_instead_of_instance_root(self) -> None:
        main = object.__new__(MainWindow)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.snbt"
            source.write_text("{}", encoding="utf-8")
            main.instance_var = _FakeStringVar(str(source))  # type: ignore[assignment]
            main.settings = AppSettings()

            with patch("mq_localizer.ui.messagebox.showerror") as showerror:
                request = main._request_values()

        self.assertIsNone(request)
        self.assertIn("ファイルではなく", showerror.call_args.args[1])

    def test_progress_updates_are_coalesced_without_dropping_control_events(self) -> None:
        main = object.__new__(MainWindow)
        main.events = Queue()
        main._progress_lock = Lock()
        main._pending_progress = None
        main._progress_event_queued = False

        for index in range(10_000):
            main._queue_progress(index, 10_000, f"step {index}")
        main.events.put(("cancelled", "done"))

        self.assertEqual(main.events.qsize(), 2)
        self.assertEqual(main.events.get_nowait()[0], "progress_latest")
        self.assertEqual(main._take_latest_progress(), (9_999, 10_000, "step 9999"))
        self.assertEqual(main.events.get_nowait(), ("cancelled", "done"))

    def test_retry_progress_is_kept_as_a_readable_log_notice(self) -> None:
        main = object.__new__(MainWindow)
        main.events = Queue()
        main._progress_lock = Lock()
        main._pending_progress = None
        main._progress_event_queued = False

        message = (
            "翻訳結果の保護検証に失敗したため、この1件だけ再試行します: "
            "Quest title（理由: token欠落）"
        )
        main._queue_progress(2, 10, message)

        event, notice = main.events.get_nowait()
        self.assertEqual(event, "translation_notice")
        self.assertEqual(notice.message, message)
        self.assertFalse(notice.persisted)
        self.assertEqual(main.events.get_nowait(), ("progress_latest", None))
        self.assertEqual(main._take_latest_progress(), (2, 10, message))

    def test_event_drain_yields_after_a_bounded_number_of_control_events(self) -> None:
        main = object.__new__(MainWindow)
        main.events = Queue()
        main._drain_after_id = "old"
        scheduled: list[str] = []
        main.root = SimpleNamespace(  # type: ignore[assignment]
            after_idle=lambda _callback: scheduled.append("idle") or "idle-id",
            after=lambda _delay, _callback: scheduled.append("timer") or "timer-id",
        )
        for index in range(200):
            main.events.put((f"unknown-{index}", None))

        main._drain_events()

        self.assertEqual(main.events.qsize(), 120)
        self.assertEqual(scheduled, ["idle"])
        self.assertEqual(main._drain_after_id, "idle-id")

    def test_close_stops_waiting_for_a_read_only_daemon_after_grace(self) -> None:
        main = object.__new__(MainWindow)
        main.worker = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]
        main.worker_write_started = Event()
        main.close_deadline = 10.0
        finished: list[bool] = []
        scheduled: list[tuple[int, object]] = []
        main._finish_close = lambda: finished.append(True)  # type: ignore[method-assign]
        main.root = SimpleNamespace(after=lambda delay, callback: scheduled.append((delay, callback)))  # type: ignore[assignment]

        with patch("mq_localizer.ui.time.monotonic", return_value=10.0):
            main._close_when_worker_stops()

        self.assertEqual(finished, [True])
        self.assertEqual(scheduled, [])

    def test_close_keeps_waiting_after_transactional_write_can_start(self) -> None:
        main = object.__new__(MainWindow)
        main.worker = SimpleNamespace(is_alive=lambda: True)  # type: ignore[assignment]
        main.worker_write_started = Event()
        main.worker_write_started.set()
        main.close_deadline = 10.0
        finished: list[bool] = []
        scheduled: list[tuple[int, object]] = []
        main._finish_close = lambda: finished.append(True)  # type: ignore[method-assign]
        main.root = SimpleNamespace(after=lambda delay, callback: scheduled.append((delay, callback)))  # type: ignore[assignment]

        with patch("mq_localizer.ui.time.monotonic", return_value=20.0):
            main._close_when_worker_stops()

        self.assertEqual(finished, [])
        self.assertEqual(len(scheduled), 1)


class SessionJournalUiTests(unittest.TestCase):
    def make_main(self, directory: str) -> tuple[MainWindow, list[tuple[str, str | None, bool]]]:
        main = object.__new__(MainWindow)
        main.analysis_log = SessionAnalysisLog(Path(directory) / "logs", process_id=456)
        main.session_api_key = "custom-api-key-123456"
        main._session_log_path = None
        main._session_log_errors = set()
        main._session_log_last_error = ""
        main.detected_log_var = _FakeStringVar()  # type: ignore[assignment]
        inserted: list[tuple[str, str | None, bool]] = []
        main._insert_gui_log = (  # type: ignore[method-assign]
            lambda text, tag, *, replace_existing: inserted.append(
                (text, tag, replace_existing)
            )
        )
        return main, inserted

    def test_append_and_gui_replace_both_append_to_the_session_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            main, inserted = self.make_main(directory)

            main._append_log("first durable event", section="first")
            first_path = Path(main.detected_log_var.get())
            main._replace_log("second durable event", "heading", section="second")

            self.assertTrue(first_path.is_file())
            self.assertEqual(Path(main.detected_log_var.get()), first_path)
            text = first_path.read_text(encoding="utf-8")
            self.assertLess(text.index("first durable event"), text.index("second durable event"))
            self.assertIn("first", text)
            self.assertIn("second", text)
            self.assertEqual(inserted[-1], ("second durable event", "heading", True))

    def test_journal_write_failure_is_redacted_and_reported_once_without_recursion(self) -> None:
        main = object.__new__(MainWindow)
        secret = "custom-api-key-123456"

        class FailingJournal:
            directory = Path("C:/unwritable/logs")

            def write(self, _text: str, *_secrets: str, **_metadata: str) -> Path:
                raise OSError(f"cannot save {secret}")

        main.analysis_log = FailingJournal()  # type: ignore[assignment]
        main.session_api_key = secret
        main._session_log_path = None
        main._session_log_errors = set()
        main._session_log_last_error = ""
        main.detected_log_var = _FakeStringVar()  # type: ignore[assignment]
        inserted: list[str] = []
        main._insert_gui_log = (  # type: ignore[method-assign]
            lambda text, _tag, *, replace_existing: inserted.append(text)
        )

        main._append_log("first")
        main._append_log("second")

        combined = "\n".join(inserted) + "\n" + main.detected_log_var.get()
        self.assertNotIn(secret, combined)
        self.assertEqual(combined.count("セッションログを保存できませんでした"), 1)
        self.assertIn("[API KEY REDACTED]", combined)

    def test_retry_notice_is_persisted_but_coalesced_analysis_progress_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            main, _inserted = self.make_main(directory)
            main.events = Queue()
            main._progress_lock = Lock()
            main._pending_progress = None
            main._progress_event_queued = False
            retry_message = (
                "safety retry detail: 再試行 custom-api-key-123456"
            )

            main._queue_progress(1, 2, retry_message)

            path = main._session_log_path
            assert path is not None
            before_drain = path.read_text(encoding="utf-8")
            self.assertIn("safety retry detail", before_drain)
            self.assertNotIn("custom-api-key-123456", before_drain)
            event, notice = main.events.get_nowait()
            self.assertEqual(event, "translation_notice")
            self.assertTrue(notice.persisted)
            self.assertEqual(main.events.get_nowait(), ("progress_latest", None))

            # Draining the already-persisted notice only renders it in the GUI;
            # it must not append a duplicate journal event.
            main.events = Queue()
            main.events.put((event, notice))
            main._drain_after_id = "old"
            main.root = SimpleNamespace(  # type: ignore[assignment]
                after=lambda _delay, _callback: "timer-id",
                after_idle=lambda _callback: "idle-id",
            )

            main._drain_events()

            text = path.read_text(encoding="utf-8")
            self.assertEqual(text, before_drain)

            before = text
            main.events = Queue()
            main._stage_lock = Lock()
            main._pending_analysis_stage = None
            main._stage_event_queued = False
            for index in range(1_000):
                main._queue_analysis_stage(f"archive progress {index}")
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_openai_transport_retry_is_not_misclassified_as_safety_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            main, _inserted = self.make_main(directory)
            main.events = Queue()
            main._stage_lock = Lock()
            main._pending_analysis_stage = None
            main._stage_event_queued = False

            main._queue_openai_retry(
                OpenAIRetryEvent(
                    attempt=1,
                    max_retries=3,
                    delay=1.0,
                    endpoint="/responses",
                    status=None,
                    request_id="",
                    kind="timeout",
                )
            )

            path = main._session_log_path
            assert path is not None
            text = path.read_text(encoding="utf-8")
            self.assertIn("OpenAI通信再試行", text)
            self.assertIn("完了済みの翻訳バッチは保持", text)
            self.assertNotIn("翻訳結果の安全確認", text)
            retry_event, notice = main.events.get_nowait()
            self.assertEqual(retry_event, "openai_retry_notice")
            self.assertTrue(notice.persisted)
            self.assertEqual(main.events.get_nowait(), ("analysis_stage_latest", None))

            before_drain = path.read_text(encoding="utf-8")
            main.events = Queue()
            main.events.put((retry_event, notice))
            main._drain_after_id = "old"
            main.root = SimpleNamespace(  # type: ignore[assignment]
                after=lambda _delay, _callback: "timer-id",
                after_idle=lambda _callback: "idle-id",
            )

            main._drain_events()

            self.assertEqual(path.read_text(encoding="utf-8"), before_drain)

    def test_translation_completion_is_durable_before_success_queue_and_rendered_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            main, inserted = self.make_main(directory)
            main.events = Queue()
            main.worker = None
            main._set_busy = lambda _busy: None  # type: ignore[method-assign]
            main.progress_var = _FakeStringVar()  # type: ignore[assignment]
            main.progress = SimpleNamespace(  # type: ignore[assignment]
                configure=lambda **_kwargs: None,
                start=lambda _interval: None,
            )
            main.status_var = _FakeStringVar()  # type: ignore[assignment]
            outcome = TranslationOutcome(
                output_path=Path("instance/output/ja_jp.json"),
                total=4,
                translated=2,
                reused=1,
                copied_without_translation=1,
                glossary_terms=3,
                skipped_by_selection=5,
            )
            analysis = SimpleNamespace()

            main._start_worker(
                lambda: main._prepare_translation_success(  # type: ignore[arg-type]
                    outcome,
                    analysis,
                ),
                "translated",
            )
            assert main.worker is not None
            main.worker.join(timeout=5)

            self.assertFalse(main.worker.is_alive())
            path = main._session_log_path
            assert path is not None
            before_drain = path.read_text(encoding="utf-8")
            self.assertEqual(before_drain.count("=== 翻訳完了 ==="), 1)
            event, payload = main.events.get_nowait()
            self.assertEqual(event, "translated")
            self.assertIsInstance(payload, _WorkerTranslationEvent)
            self.assertTrue(payload.notice.persisted)

            main._render_durable_log_event(
                payload.notice,
                "ok",
                section="翻訳完了",
            )

            self.assertEqual(path.read_text(encoding="utf-8"), before_drain)
            self.assertTrue(any("翻訳完了" in text for text, _tag, _replace in inserted))

    def test_glossary_skip_is_durable_before_queue_and_rendered_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            main, inserted = self.make_main(directory)
            main.events = Queue()
            main.cancel_event = Event()

            main._await_glossary_confirmation(GlossaryCatalog(), skip=True)

            path = main._session_log_path
            assert path is not None
            before_drain = path.read_text(encoding="utf-8")
            self.assertEqual(before_drain.count("続行確認をスキップしました"), 1)
            event, payload = main.events.get_nowait()
            self.assertEqual(event, "glossary_confirmation_skipped")
            self.assertIsInstance(payload, _DurableLogEvent)
            self.assertTrue(payload.persisted)

            main._render_durable_log_event(
                payload,
                "warning",
                section="固有名詞保護の確認",
            )

            self.assertEqual(path.read_text(encoding="utf-8"), before_drain)
            self.assertTrue(
                any("続行確認をスキップしました" in text for text, _tag, _replace in inserted)
            )

    def test_translation_and_glossary_notices_retry_once_after_worker_log_failure(self) -> None:
        secret = "custom-api-key-123456"

        for section, message, tag in (
            ("翻訳完了", "翻訳完了 " + secret, "ok"),
            ("固有名詞保護の確認", "確認スキップ " + secret, "warning"),
        ):
            with self.subTest(section=section):
                main = object.__new__(MainWindow)

                class FailingOnceJournal:
                    directory = Path("C:/logs")

                    def __init__(self) -> None:
                        self.calls = 0
                        self.saved: list[str] = []

                    def write(
                        self,
                        text: str,
                        *_secrets: str,
                        **_metadata: str,
                    ) -> Path:
                        self.calls += 1
                        if self.calls == 1:
                            raise OSError("temporary failure " + secret)
                        self.saved.append(text)
                        return Path("C:/logs/session.log")

                journal = FailingOnceJournal()
                main.analysis_log = journal  # type: ignore[assignment]
                main.session_api_key = secret
                main._session_log_path = None
                main._session_log_last_error = ""
                main._session_log_errors = set()
                main.detected_log_var = _FakeStringVar()  # type: ignore[assignment]
                inserted: list[str] = []
                main._insert_gui_log = (  # type: ignore[method-assign]
                    lambda text, _tag, *, replace_existing: inserted.append(text)
                )
                reported: list[str] = []
                main._report_session_log_error = (  # type: ignore[method-assign]
                    lambda error: reported.append(error)
                )

                notice = main._persist_worker_log(
                    message,
                    level="WARNING" if tag == "warning" else "SUCCESS",
                    section=section,
                )
                self.assertFalse(notice.persisted)
                self.assertNotIn(secret, notice.message)
                self.assertNotIn(secret, notice.log_error)

                main._render_durable_log_event(notice, tag, section=section)

                self.assertEqual(journal.calls, 2)
                self.assertEqual(len(journal.saved), 1)
                self.assertNotIn(secret, journal.saved[0])
                self.assertEqual(len(inserted), 1)
                self.assertEqual(reported, [notice.log_error])

    def test_worker_terminal_events_are_journaled_before_the_ui_queue_is_drained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("cancelled", "unexpected"):
                with self.subTest(kind=kind):
                    case_directory = Path(directory) / kind
                    main, _inserted = self.make_main(str(case_directory))
                    main.events = Queue()
                    main.worker = None
                    main._set_busy = lambda _busy: None  # type: ignore[method-assign]
                    main.progress_var = _FakeStringVar()  # type: ignore[assignment]
                    main.progress = SimpleNamespace(  # type: ignore[assignment]
                        configure=lambda **_kwargs: None,
                        start=lambda _interval: None,
                    )
                    main.status_var = _FakeStringVar()  # type: ignore[assignment]

                    def fail() -> None:
                        if kind == "cancelled":
                            raise CancelledError("cancelled custom-api-key-123456")
                        raise RuntimeError("unexpected custom-api-key-123456")

                    main._start_worker(fail, "completed")
                    assert main.worker is not None
                    main.worker.join(timeout=5)

                    self.assertFalse(main.worker.is_alive())
                    path = main._session_log_path
                    assert path is not None
                    text = path.read_text(encoding="utf-8")
                    self.assertNotIn("custom-api-key-123456", text)
                    self.assertIn("[API KEY REDACTED]", text)
                    self.assertIn("処理を中止" if kind == "cancelled" else "予期しないエラー", text)
                    event, payload = main.events.get_nowait()
                    self.assertEqual(event, kind)
                    self.assertTrue(payload.persisted)
                    main.events.put((event, payload))
                    main._drain_after_id = "old"
                    main.close_pending = True
                    main.root = SimpleNamespace(  # type: ignore[assignment]
                        after=lambda _delay, _callback: "timer-id",
                        after_idle=lambda _callback: "idle-id",
                    )
                    main._drain_events()
                    after_drain = path.read_text(encoding="utf-8")
                    terminal_heading = (
                        "処理を中止しました:"
                        if kind == "cancelled"
                        else "予期しないエラー"
                    )
                    self.assertEqual(
                        after_drain.count(terminal_heading),
                        text.count(terminal_heading),
                    )


class HighDpiLayoutTests(unittest.TestCase):
    def test_main_initial_size_avoids_scrolling_when_screen_space_allows(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SettingsStore(Path(temporary.name) / "settings.json")
        main: MainWindow | None = None
        try:
            with patch("mq_localizer.ui.SettingsStore", return_value=store):
                main = MainWindow(root)
            root.update()

            available_width = max(1, root.winfo_screenwidth() - 80)
            available_height = max(1, root.winfo_screenheight() - 120)
            x_end = main.main_scroll_pane.canvas.xview()[1]
            y_end = main.main_scroll_pane.canvas.yview()[1]
            if root.winfo_width() < available_width:
                self.assertAlmostEqual(x_end, 1.0)
            if root.winfo_height() < available_height:
                self.assertAlmostEqual(y_end, 1.0)
            self.assertEqual(root.state(), "normal")
        finally:
            if main is not None and main._drain_after_id is not None:
                root.after_cancel(main._drain_after_id)
            root.destroy()

    def test_settings_dialog_top_left_matches_parent_window(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SettingsStore(Path(temporary.name) / "settings.json")
        dialog: SettingsDialog | None = None
        try:
            root.geometry("900x700+123+145")
            root.update()
            dialog = SettingsDialog(
                root,
                AppSettings(model="gpt-test"),
                "",
                store,
                lambda _key, _settings: None,
            )
            dialog.window.update()

            self.assertEqual(
                (dialog.window.winfo_x(), dialog.window.winfo_y()),
                (root.winfo_x(), root.winfo_y()),
            )
            self.assertIs(root.grab_current(), dialog.window)
        finally:
            if dialog is not None:
                dialog._close()
            root.destroy()

    def test_real_tk_analysis_worker_keeps_event_loop_responsive_and_populates_results(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        instance = Path(temporary.name) / "instance"
        shutil.copytree(FIXTURES / "native_snbt", instance)
        (instance / "mods").mkdir()
        (instance / "mmc-pack.json").write_text(
            '{"components":[{"uid":"net.minecraft","version":"1.21.1"}]}',
            encoding="utf-8",
        )
        store = SettingsStore(Path(temporary.name) / "settings.json")
        main: MainWindow | None = None
        try:
            with patch("mq_localizer.ui.SettingsStore", return_value=store):
                main = MainWindow(root)
            main.instance_var.set(str(instance))
            heartbeat = [0]

            def beat() -> None:
                heartbeat[0] += 1
                if main._analyzed is None:
                    root.after(5, beat)

            root.after(0, beat)
            with (
                patch("mq_localizer.ui.messagebox.showerror") as showerror,
                patch("mq_localizer.ui.messagebox.showwarning"),
            ):
                main._analyze()
                deadline = time.monotonic() + 5.0
                while main._analyzed is None and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.002)

            self.assertIsNotNone(main._analyzed, showerror.call_args)
            self.assertGreater(heartbeat[0], 1)
            self.assertEqual(main.detected_version_var.get().split("（", 1)[0], "1.21.1")
            self.assertIn("FTB Quests", main.detected_format_var.get())
            self.assertTrue(main.detected_source_var.get().endswith("en_us.snbt"))
            self.assertTrue(main.detected_output_var.get().endswith("ja_jp.snbt"))
            analysis_log = Path(main.detected_log_var.get())
            self.assertTrue(analysis_log.is_file())
            self.assertEqual(analysis_log.parent, store.path.parent / "logs")
            session_text = analysis_log.read_text(encoding="utf-8")
            self.assertIn("Minecraft Quest Localizer セッションログ", session_text)
            self.assertIn("解析開始", session_text)
            self.assertIn("解析結果全文", session_text)
            self.assertEqual(session_text.count("Mod JARが見つかりませんでした"), 1)
            self.assertEqual(str(main.translate_button.cget("state")), "normal")
            self.assertEqual(str(main.progress.cget("mode")), "determinate")
            self.assertEqual(main.progress_var.get(), 100)
            self.assertIn("解析完了", main.status_var.get())
            showerror.assert_not_called()
            if main.worker is not None:
                main.worker.join(timeout=1.0)
        finally:
            if main is not None:
                main._finish_close()
            else:
                root.destroy()

    def test_small_high_dpi_windows_keep_controls_at_full_size_and_scrollable(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = SettingsStore(Path(temporary.name) / "settings.json")
        dialog: SettingsDialog | None = None
        main: MainWindow | None = None
        try:
            root.tk.call("tk", "scaling", 2.0)
            with patch("mq_localizer.ui.SettingsStore", return_value=store):
                main = MainWindow(root)
            main_widget_texts = [
                str(widget.cget("text"))
                for widget in main.main_scroll_pane.content.winfo_children()
                for widget in _walk_widgets(widget)
                if "text" in widget.keys()
            ]
            self.assertIn("インスタンスルート", main_widget_texts)
            self.assertIn("Minecraftバージョン", main_widget_texts)
            self.assertIn("設定…", main_widget_texts)
            self.assertNotIn(
                "選択した項目の既存翻訳を再利用する",
                main_widget_texts,
            )
            self.assertNotIn(
                "resourcepacksも固有名詞保護用に走査する",
                main_widget_texts,
            )
            self.assertNotIn("固有名詞保護の確認をスキップする", main_widget_texts)
            self.assertTrue(any("locale: en_us → ja_jp" in text for text in main_widget_texts))
            self.assertNotIn("Minecraft版", main_widget_texts)
            self.assertNotIn("Mod / instance", main_widget_texts)
            main_combos = [
                widget
                for child in main.main_scroll_pane.content.winfo_children()
                for widget in _walk_widgets(child)
                if isinstance(widget, ttk.Combobox)
            ]
            self.assertEqual(main_combos, [])
            self.assertEqual(
                sum(
                    type(widget) is ttk.Entry
                    for child in main.main_scroll_pane.content.winfo_children()
                    for widget in _walk_widgets(child)
                ),
                1,  # instance path only; low-frequency controls moved to Settings
            )
            self.assertEqual(str(main.translate_button.cget("state")), "disabled")
            main._analyzed = object()  # type: ignore[assignment]
            main._set_busy(False)
            self.assertEqual(str(main.translate_button.cget("state")), "normal")
            self.assertEqual(str(main.settings_button.cget("state")), "normal")
            main._set_busy(True)
            self.assertEqual(str(main.settings_button.cget("state")), "disabled")
            main._set_busy(False)
            self.assertEqual(str(main.settings_button.cget("state")), "normal")
            main.detected_output_var.set("stale-output")
            main.detected_version_var.set("1.21.1")
            changed_settings = _copy_settings(main.settings)
            changed_settings.scan_resourcepacks = True
            main._settings_updated(main.session_api_key, changed_settings)
            self.assertIsNone(main._analyzed)
            self.assertEqual(main.detected_output_var.get(), "解析後に表示します")
            self.assertEqual(main.detected_version_var.get(), "解析後に表示します")
            main._analyzed = object()  # type: ignore[assignment]
            main.detected_output_var.set("stale-output")
            main.detected_version_var.set("1.21.1")
            changed_settings = _copy_settings(main.settings)
            changed_settings.source_locale = "fr_fr"
            main._settings_updated(main.session_api_key, changed_settings)
            self.assertIsNone(main._analyzed)
            self.assertEqual(main.detected_output_var.get(), "解析後に表示します")
            self.assertEqual(main.detected_version_var.get(), "解析後に表示します")
            self.assertIn("locale: fr_fr → ja_jp", main.settings_summary_var.get())
            main._analyzed = object()  # type: ignore[assignment]
            main.detected_output_var.set("stale-output")
            main.detected_version_var.set("1.21.1")
            changed_settings = _copy_settings(main.settings)
            changed_settings.glossary_max_source_members = 200_000
            main._settings_updated(main.session_api_key, changed_settings)
            self.assertIsNone(main._analyzed)
            self.assertEqual(main.detected_output_var.get(), "解析後に表示します")
            self.assertEqual(main.detected_version_var.get(), "解析後に表示します")
            main._analyzed = object()  # type: ignore[assignment]
            main.detected_output_var.set("stale-output")
            main.detected_version_var.set("1.21.1")
            changed_settings = _copy_settings(main.settings)
            changed_settings.glossary_scan_limits_enabled = False
            main._settings_updated(main.session_api_key, changed_settings)
            self.assertIsNone(main._analyzed)
            self.assertEqual(main.detected_output_var.get(), "解析後に表示します")
            self.assertEqual(main.detected_version_var.get(), "解析後に表示します")
            main._analyzed = object()  # type: ignore[assignment]
            main.detected_output_var.set("stale-output")
            main.detected_version_var.set("1.21.1")
            main.instance_var.set(str(Path(temporary.name) / "new-instance"))
            self.assertIsNone(main._analyzed)
            self.assertEqual(main.detected_output_var.get(), "解析後に表示します")
            self.assertEqual(main.detected_version_var.get(), "解析後に表示します")
            root.geometry("640x480-10000-10000")
            root.update()
            main_bbox = main.main_scroll_pane.canvas.bbox("all")
            assert main_bbox is not None
            self.assertGreater(main_bbox[3], main.main_scroll_pane.canvas.winfo_height())
            self.assertEqual(main.settings_button.winfo_width(), main.settings_button.winfo_reqwidth())
            self.assertEqual(main.translate_button.winfo_width(), main.translate_button.winfo_reqwidth())
            main.main_scroll_pane.canvas.yview_moveto(1.0)
            root.update()
            self.assertAlmostEqual(main.main_scroll_pane.canvas.yview()[1], 1.0)

            initial_setup_dialog = main._open_settings()
            self.assertEqual(
                initial_setup_dialog.notebook.select(),
                str(initial_setup_dialog.openai_tab),
            )
            initial_setup_dialog._close()

            dialog_source_settings = AppSettings(
                model="gpt-test",
                preserve_existing=True,
                scan_resourcepacks=False,
                skip_glossary_confirmation=False,
            )
            dialog_saves: list[AppSettings] = []
            dialog = SettingsDialog(
                root,
                dialog_source_settings,
                "sk-test-value",
                store,
                lambda _key, settings: dialog_saves.append(_copy_settings(settings)),
            )
            self.assertEqual(dialog.window.title(), "設定")
            self.assertEqual(
                [dialog.notebook.tab(tab, "text") for tab in dialog.notebook.tabs()],
                ["翻訳", "固有名詞保護", "OpenAI"],
            )
            dialog_widget_texts = [
                str(widget.cget("text"))
                for pane in dialog.tab_scroll_panes.values()
                for child in pane.content.winfo_children()
                for widget in _walk_widgets(child)
                if "text" in widget.keys()
            ]
            self.assertIn("選択した項目の既存翻訳を再利用する", dialog_widget_texts)
            self.assertIn(
                "resourcepacksも固有名詞保護用に走査する",
                dialog_widget_texts,
            )
            self.assertIn(
                "固有名詞保護の走査上限（Minecraft本体を除く）",
                dialog_widget_texts,
            )
            self.assertEqual(
                dialog_widget_texts.count("走査上限を有効にする"),
                1,
            )
            self.assertIn(
                "1資産の項目数上限",
                dialog_widget_texts,
            )
            self.assertIn(
                "言語ファイル1件の上限（MiB）",
                dialog_widget_texts,
            )
            self.assertIn(
                "1資産の言語ファイル合計上限（MiB）",
                dialog_widget_texts,
            )
            self.assertIn(
                "全資産の言語ファイル合計上限（MiB）",
                dialog_widget_texts,
            )
            self.assertIn("既定値に戻す", dialog_widget_texts)
            self.assertIn("固有名詞保護の確認をスキップする", dialog_widget_texts)
            self.assertTrue(
                any(
                    "Minecraft本体、Mod、KubeJS、resource packの順で優先" in text
                    for text in dialog_widget_texts
                )
            )
            self.assertTrue(
                any("安全確認は無効になりません" in text for text in dialog_widget_texts)
            )
            self.assertTrue(
                any(
                    text.startswith(
                        "チェックを外すと4つの上限をすべて無効にします。\n"
                        "1資産：Mod JAR 1件 / kubejs/assets全体 / resource pack 1件。\n"
                        "項目数："
                    )
                    and "本文の読込数ではなく、内容は読み込みません。\nサイズ：" in text
                    and text.endswith("Minecraft本体は対象外です。")
                    and text.count("\n") == 3
                    for text in dialog_widget_texts
                )
            )
            self.assertFalse(any("README" in text for text in dialog_widget_texts))
            self.assertIn("Fast Modeを使用", dialog_widget_texts)
            self.assertIn("再試行回数（初回を除く）", dialog_widget_texts)
            locale_combos = [
                widget
                for child in dialog.translation_scroll_pane.content.winfo_children()
                for widget in _walk_widgets(child)
                if isinstance(widget, ttk.Combobox)
            ]
            self.assertEqual(len(locale_combos), 2)
            self.assertTrue(
                all(str(combo.cget("state")) == "readonly" for combo in locale_combos)
            )
            self.assertTrue(
                all(
                    {"en_us", "ja_jp", "fr_fr", "zh_cn"}.issubset(
                        set(combo.cget("values"))
                    )
                    for combo in locale_combos
                )
            )
            self.assertEqual(dialog.retry_var.get(), 3)
            self.assertTrue(dialog.glossary_scan_limits_enabled_var.get())
            self.assertEqual(dialog.glossary_max_source_members_var.get(), 100_000)
            self.assertEqual(dialog.glossary_max_language_file_mib_var.get(), 16)
            self.assertEqual(dialog.glossary_max_source_language_mib_var.get(), 64)
            self.assertEqual(dialog.glossary_max_total_language_mib_var.get(), 512)
            self.assertTrue(
                all(
                    not widget.instate(("disabled",))
                    for widget in dialog.glossary_limit_value_widgets
                )
            )
            self.assertFalse(
                dialog.reset_glossary_limits_button.instate(("disabled",))
            )
            dialog.glossary_scan_limits_enabled_check.invoke()
            dialog.window.update_idletasks()
            self.assertFalse(dialog.glossary_scan_limits_enabled_var.get())
            self.assertTrue(
                all(
                    widget.instate(("disabled",))
                    for widget in dialog.glossary_limit_value_widgets
                )
            )
            self.assertTrue(
                dialog.reset_glossary_limits_button.instate(("disabled",))
            )
            self.assertIn("無効", dialog.glossary_limit_status_var.get())
            dialog.glossary_scan_limits_enabled_check.invoke()
            dialog.window.update_idletasks()
            self.assertTrue(dialog.glossary_scan_limits_enabled_var.get())
            self.assertTrue(
                all(
                    not widget.instate(("disabled",))
                    for widget in dialog.glossary_limit_value_widgets
                )
            )
            self.assertEqual(dialog.models, ["gpt-test"])
            self.assertIn("保存済みモデル一覧", dialog.status_var.get())
            dialog.window.geometry("480x360-10000-10000")
            dialog.window.update()
            for tab, pane in (
                (dialog.translation_tab, dialog.translation_scroll_pane),
                (dialog.glossary_tab, dialog.glossary_scroll_pane),
                (dialog.openai_tab, dialog.openai_scroll_pane),
            ):
                dialog.notebook.select(tab)
                dialog.window.update()
                pane_bbox = pane.canvas.bbox("all")
                assert pane_bbox is not None
                if pane_bbox[3] > pane.canvas.winfo_height():
                    pane.canvas.yview_moveto(1.0)
                    dialog.window.update()
                    self.assertAlmostEqual(pane.canvas.yview()[1], 1.0)
                    pane.canvas.yview_moveto(0.0)
            dialog.notebook.select(dialog.openai_tab)
            dialog.window.update()
            dialog_bbox = dialog.scroll_pane.canvas.bbox("all")
            assert dialog_bbox is not None
            self.assertGreater(dialog_bbox[3], dialog.scroll_pane.canvas.winfo_height())
            self.assertEqual(
                dialog.reset_prompt_button.winfo_width(),
                dialog.reset_prompt_button.winfo_reqwidth(),
            )
            self.assertEqual(dialog.save_button.winfo_width(), dialog.save_button.winfo_reqwidth())
            dialog.scroll_pane.canvas.yview_moveto(1.0)
            dialog.window.update()
            self.assertAlmostEqual(dialog.scroll_pane.canvas.yview()[1], 1.0)
            dialog._set_fetching(True)
            self.assertEqual(str(dialog.retry_spin.cget("state")), "disabled")
            self.assertEqual(str(dialog.save_button.cget("state")), "disabled")
            self.assertEqual(str(dialog.source_locale_combo.cget("state")), "readonly")
            dialog._set_fetching(False)
            self.assertEqual(str(dialog.retry_spin.cget("state")), "normal")
            self.assertEqual(str(dialog.save_button.cget("state")), "normal")

            observed_cancel: list[Event] = []
            observed_client_options: list[dict[str, object]] = []

            class FakeOpenAIClient:
                def __init__(self, **kwargs: object) -> None:
                    observed_client_options.append(kwargs)

                def list_models(self, _api_key: str, cancel: Event) -> list[object]:
                    observed_cancel.append(cancel)
                    return []

            class ImmediateThread:
                def __init__(self, target: object, **_kwargs: object) -> None:
                    self.target = target

                def start(self) -> None:
                    assert callable(self.target)
                    self.target()

            with (
                patch("mq_localizer.ui.OpenAIClient", FakeOpenAIClient),
                patch("mq_localizer.ui.threading.Thread", ImmediateThread),
            ):
                dialog._fetch_models()
            self.assertEqual(observed_cancel, [dialog.model_cancel_event])
            self.assertEqual(len(observed_client_options), 1)
            self.assertEqual(observed_client_options[0]["timeout"], 120)
            self.assertEqual(observed_client_options[0]["max_retries"], 3)
            self.assertTrue(callable(observed_client_options[0]["on_retry"]))
            dialog.source_locale_var.set("fr_fr")
            dialog.preserve_var.set(False)
            dialog.scan_resourcepacks_var.set(True)
            dialog.skip_glossary_confirmation_var.set(True)
            dialog._close()
            self.assertEqual(dialog_saves, [])
            self.assertFalse(store.path.exists())
            self.assertEqual(dialog_source_settings.source_locale, "en_us")
            self.assertTrue(dialog_source_settings.preserve_existing)
            self.assertFalse(dialog_source_settings.scan_resourcepacks)
            self.assertFalse(dialog_source_settings.skip_glossary_confirmation)
        finally:
            if dialog is not None:
                dialog._close()
            if main is not None:
                main._finish_close()
            else:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
