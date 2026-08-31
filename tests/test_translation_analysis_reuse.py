from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from queue import Queue
from threading import Event
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.application import AnalyzedProject  # noqa: E402
from mq_localizer.config import AppSettings  # noqa: E402
from mq_localizer.domain import (  # noqa: E402
    AdapterError,
    CancelledError,
    TranslationError,
    TranslationOutcome,
    TranslationProject,
    TranslationUnit,
)
from mq_localizer.glossary import GlossaryCatalog  # noqa: E402
from mq_localizer.instance import InstanceInfo  # noqa: E402
from mq_localizer.output_guard import snapshot_path  # noqa: E402
from mq_localizer.ui import MainWindow, _InstanceAnalysis  # noqa: E402


class _AutoApprovalQueue(Queue[tuple[str, Any]]):
    """Record worker events and answer UI confirmations without a Tk loop."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, Any]] = []

    def put(
        self,
        item: tuple[str, Any],
        block: bool = True,
        timeout: float | None = None,
    ) -> None:
        self.seen.append(item)
        event, payload = item
        if event == "confirm_output":
            payload.approved = True
            payload.ready.set()
        elif event == "confirm_without_glossary":
            decision = payload[0]
            decision.approved = True
            decision.ready.set()
        super().put(item, block=block, timeout=timeout)

    @property
    def event_names(self) -> list[str]:
        return [event for event, _payload in self.seen]


class _RecordingAdapter:
    id = "test_adapter"

    def __init__(self) -> None:
        self.validate_calls = 0
        self.write_calls = 0
        self.refresh_existing = False
        self.mutate_during_validate: str | None = None

    def validate_output(
        self,
        project: TranslationProject,
        output_path: Path,
    ) -> None:
        self.validate_calls += 1
        if self.refresh_existing:
            project.existing = {
                "unit": output_path.read_text(encoding="utf-8")
                if output_path.exists()
                else ""
            }
        if self.mutate_during_validate is not None:
            output_path.write_text(self.mutate_during_validate, encoding="utf-8")

    def write(self, *_args: object, **_kwargs: object) -> None:
        self.write_calls += 1


class _CountingApplication:
    def __init__(self, analyzed: AnalyzedProject) -> None:
        self.analyzed = analyzed
        self.calls = 0

    def analyze(self, **_request: object) -> AnalyzedProject:
        self.calls += 1
        return self.analyzed


class _CountingScanner:
    def __init__(self, glossary: GlossaryCatalog) -> None:
        self.glossary = glossary
        self.calls = 0

    def scan(self, *_args: object, **_kwargs: object) -> GlossaryCatalog:
        self.calls += 1
        return self.glossary


class TranslationAnalysisReuseTests(unittest.TestCase):
    def _analyze_once(
        self,
        directory: str,
        *,
        output_text: str | None = "analyzed output",
    ) -> SimpleNamespace:
        root = Path(directory) / "instance"
        root.mkdir()
        mods = root / "mods"
        mods.mkdir()
        source = root / "en_us.snbt"
        source.write_text('quest: "Source"\n', encoding="utf-8")
        output = root / "ja_jp.snbt"
        if output_text is not None:
            output.write_text(output_text, encoding="utf-8")

        adapter = _RecordingAdapter()
        project = TranslationProject(
            adapter_id=adapter.id,
            adapter_label="Test adapter",
            source_path=source,
            default_output=output,
            source_locale="en_us",
            target_locale="ja_jp",
            units=[
                TranslationUnit(
                    id="unit",
                    key="quest.title",
                    source="Source",
                    category="quest_title",
                )
            ],
            metadata={"source_snapshot": snapshot_path(source)},
            existing={"unit": output_text or ""},
        )
        analyzed = AnalyzedProject(adapter=adapter, project=project)  # type: ignore[arg-type]
        instance = InstanceInfo(
            selected_root=root,
            instance_root=root,
            game_root=root,
            mods_path=mods,
            minecraft_version="1.21.1",
            detected_by=None,
            evidence=(),
            warnings=(),
        )
        glossary_snapshot = object()
        glossary = GlossaryCatalog(input_snapshot=glossary_snapshot)  # type: ignore[arg-type]
        application = _CountingApplication(analyzed)
        scanner = _CountingScanner(glossary)
        request = {
            "instance_root": root,
            "source_locale": "en_us",
            "target_locale": "ja_jp",
            "scan_resourcepacks": False,
        }

        main = object.__new__(MainWindow)
        main.cancel_event = Event()
        main.application = application  # type: ignore[assignment]
        main.scanner = scanner  # type: ignore[assignment]
        main.analysis_log = object()  # type: ignore[assignment]
        main.session_api_key = "test-api-key"
        main._queue_analysis_stage = lambda _message: None  # type: ignore[method-assign]
        log_calls: list[tuple[str, dict[str, str]]] = []

        def write_log(text: str, **metadata: str) -> Path:
            log_calls.append((text, metadata))
            return root / "session.log"

        main._write_session_log = write_log  # type: ignore[method-assign]
        with patch("mq_localizer.ui.inspect_instance_root", return_value=instance):
            analysis = main._inspect_and_analyze(request)

        # Exercise the real analyzed-event branch: this is the branch which
        # must retain the complete immutable analysis, not only its pieces.
        main._analysis = None  # type: ignore[attr-defined]
        main._analyzed = None
        main._instance_info = None
        main._glossary = GlossaryCatalog()
        main._analysis_scan_resourcepacks = False
        main.events = Queue()
        main.events.put(("analyzed", analysis))
        main._drain_after_id = None
        main.root = SimpleNamespace(  # type: ignore[assignment]
            after=lambda _delay, _callback: "timer-id",
            after_idle=lambda _callback: "idle-id",
        )
        main._show_analysis = lambda _analysis: None  # type: ignore[method-assign]
        main._set_busy = lambda _busy: None  # type: ignore[method-assign]
        main._save_settings = lambda: None  # type: ignore[method-assign]
        main._drain_events()

        return SimpleNamespace(
            main=main,
            analysis=analysis,
            analyzed=analyzed,
            instance=instance,
            glossary=glossary,
            glossary_snapshot=glossary_snapshot,
            application=application,
            scanner=scanner,
            adapter=adapter,
            project=project,
            request=request,
            source=source,
            output=output,
            log_calls=log_calls,
        )

    def _prepare_translation(self, case: SimpleNamespace) -> list[Callable[[], Any]]:
        main: MainWindow = case.main
        main.settings = AppSettings(
            model="gpt-test",
            preserve_existing=True,
            skip_glossary_confirmation=True,
        )
        main._request_values = lambda: dict(case.request)  # type: ignore[method-assign]
        main._selected_category_ids = lambda: frozenset({"quest_title"})  # type: ignore[method-assign]
        main._append_log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        main._queue_analysis_stage = lambda _message: None  # type: ignore[method-assign]
        main._queue_openai_retry = lambda _event: None  # type: ignore[method-assign]
        main._queue_progress = lambda *_args: None  # type: ignore[method-assign]
        main._await_glossary_confirmation = (  # type: ignore[method-assign]
            lambda _glossary, *, skip: None
        )
        main._prepare_translation_success = (  # type: ignore[method-assign]
            lambda outcome, analysis: SimpleNamespace(outcome=outcome, analysis=analysis)
        )
        main.worker_write_started = Event()
        main.events = _AutoApprovalQueue()  # type: ignore[assignment]
        workers: list[Callable[[], Any]] = []
        main._start_worker = (  # type: ignore[method-assign]
            lambda function, _success_event: workers.append(function)
        )
        return workers

    @staticmethod
    def _service(
        calls: list[dict[str, Any]],
        *,
        before_guard: Callable[[], None] | None = None,
    ) -> type:
        class Service:
            def __init__(self, client: object, *, fast_mode: bool = False) -> None:
                calls.append(
                    {
                        "constructed": True,
                        "client": client,
                        "fast_mode": fast_mode,
                    }
                )

            def translate(
                self,
                project: TranslationProject,
                adapter: object,
                output_path: Path,
                api_key: str,
                model: str,
                glossary: GlossaryCatalog,
                options: object,
                **kwargs: Any,
            ) -> TranslationOutcome:
                call = calls[-1]
                call.update(
                    project=project,
                    adapter=adapter,
                    output_path=output_path,
                    api_key=api_key,
                    model=model,
                    glossary=glossary,
                    options=options,
                    existing=dict(project.existing),
                )
                if before_guard is not None:
                    before_guard()
                kwargs["pre_write_guard"]()
                call["guard_completed"] = True
                return TranslationOutcome(
                    output_path=output_path,
                    total=1,
                    translated=1,
                    reused=0,
                    copied_without_translation=0,
                    glossary_terms=len(glossary.entries),
                )

        return Service

    def test_analyze_then_translate_reuses_exact_project_and_glossary_without_rescan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory)
            workers = self._prepare_translation(case)
            service_calls: list[dict[str, Any]] = []
            glossary_checks: list[tuple[object, Event | None]] = []

            def check_glossary(snapshot: object, cancel: Event | None = None) -> None:
                glossary_checks.append((snapshot, cancel))

            with (
                patch.object(
                    case.main,
                    "_inspect_and_analyze",
                    side_effect=AssertionError("translate must not re-analyze"),
                ),
                patch("mq_localizer.ui.inspect_instance_root", return_value=case.instance),
                patch(
                    "mq_localizer.ui.assert_glossary_inputs_unchanged",
                    side_effect=check_glossary,
                ),
                patch("mq_localizer.ui.OpenAIClient", return_value=object()),
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(service_calls),
                ),
            ):
                case.main._translate()
                self.assertEqual(len(workers), 1)
                result = workers[0]()

            self.assertIs(case.main._analysis, case.analysis)
            self.assertEqual(case.application.calls, 1)
            self.assertEqual(case.scanner.calls, 1)
            self.assertEqual(
                sum(
                    metadata.get("section") == "解析結果全文"
                    for _text, metadata in case.log_calls
                ),
                1,
            )
            self.assertEqual(len(service_calls), 1)
            self.assertIs(service_calls[0]["project"], case.project)
            self.assertIs(service_calls[0]["glossary"], case.glossary)
            self.assertIs(result.analysis, case.analysis)
            self.assertGreaterEqual(len(glossary_checks), 2)
            self.assertTrue(
                all(snapshot is case.glossary_snapshot for snapshot, _ in glossary_checks)
            )
            self.assertTrue(
                all(cancel is case.main.cancel_event for _, cancel in glossary_checks)
            )

    def test_source_changed_after_analysis_is_rejected_before_confirmation_or_api(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory)
            case.source.write_text('quest: "Changed"\n', encoding="utf-8")
            workers = self._prepare_translation(case)
            service_calls: list[dict[str, Any]] = []

            with (
                patch.object(
                    case.main,
                    "_inspect_and_analyze",
                    side_effect=AssertionError("translate must not re-analyze"),
                ),
                patch("mq_localizer.ui.inspect_instance_root", return_value=case.instance),
                patch("mq_localizer.ui.assert_glossary_inputs_unchanged"),
                patch("mq_localizer.ui.OpenAIClient") as openai_client,
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(service_calls),
                ),
            ):
                case.main._translate()
                with self.assertRaises(AdapterError):
                    workers[0]()

            self.assertEqual(case.application.calls, 1)
            self.assertEqual(case.scanner.calls, 1)
            self.assertEqual(case.adapter.validate_calls, 0)
            self.assertEqual(case.adapter.write_calls, 0)
            self.assertEqual(service_calls, [])
            openai_client.assert_not_called()
            self.assertIn("invalidate_analysis", case.main.events.event_names)
            self.assertNotIn("confirm_output", case.main.events.event_names)

    def test_scan_limit_change_after_analysis_requires_a_fresh_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory)
            workers = self._prepare_translation(case)
            case.main.settings.glossary_max_source_members = 200_000

            with (
                patch("mq_localizer.ui.inspect_instance_root") as inspect_instance,
                patch("mq_localizer.ui.OpenAIClient") as openai_client,
            ):
                case.main._translate()
                self.assertEqual(len(workers), 1)
                with self.assertRaisesRegex(TranslationError, "走査上限"):
                    workers[0]()

            self.assertEqual(case.scanner.calls, 1)
            inspect_instance.assert_not_called()
            openai_client.assert_not_called()
            self.assertIn("invalidate_analysis", case.main.events.event_names)
            self.assertNotIn("confirm_output", case.main.events.event_names)

    def test_instance_detection_change_is_rejected_without_rescanning_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory)
            workers = self._prepare_translation(case)
            changed_instance = replace(case.instance, minecraft_version="1.20.1")
            service_calls: list[dict[str, Any]] = []

            with (
                patch.object(
                    case.main,
                    "_inspect_and_analyze",
                    side_effect=AssertionError("translate must not re-analyze"),
                ),
                patch(
                    "mq_localizer.ui.inspect_instance_root",
                    return_value=changed_instance,
                ),
                patch("mq_localizer.ui.assert_glossary_inputs_unchanged"),
                patch("mq_localizer.ui.OpenAIClient") as openai_client,
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(service_calls),
                ),
            ):
                case.main._translate()
                with self.assertRaises(TranslationError):
                    workers[0]()

            self.assertEqual(case.application.calls, 1)
            self.assertEqual(case.scanner.calls, 1)
            self.assertEqual(service_calls, [])
            openai_client.assert_not_called()
            self.assertIn("invalidate_analysis", case.main.events.event_names)

    def test_glossary_asset_mismatch_is_rejected_before_output_or_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory)
            workers = self._prepare_translation(case)
            checked: list[object] = []
            service_calls: list[dict[str, Any]] = []

            def changed(snapshot: object, _cancel: Event | None = None) -> None:
                checked.append(snapshot)
                raise TranslationError(
                    "固有名詞保護に使用したファイルが解析後に変更されました。再解析してください"
                )

            with (
                patch.object(
                    case.main,
                    "_inspect_and_analyze",
                    side_effect=AssertionError("translate must not re-analyze"),
                ),
                patch("mq_localizer.ui.inspect_instance_root", return_value=case.instance),
                patch(
                    "mq_localizer.ui.assert_glossary_inputs_unchanged",
                    side_effect=changed,
                ),
                patch("mq_localizer.ui.OpenAIClient") as openai_client,
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(service_calls),
                ),
            ):
                case.main._translate()
                with self.assertRaises(TranslationError):
                    workers[0]()

            self.assertEqual(checked, [case.glossary_snapshot])
            self.assertEqual(case.application.calls, 1)
            self.assertEqual(case.scanner.calls, 1)
            self.assertEqual(case.adapter.validate_calls, 0)
            self.assertEqual(service_calls, [])
            openai_client.assert_not_called()
            self.assertIn("invalidate_analysis", case.main.events.event_names)

    def test_output_changed_since_analysis_is_refreshed_from_latest_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory, output_text="old translation")
            case.output.write_text("latest translation", encoding="utf-8")
            case.adapter.refresh_existing = True
            workers = self._prepare_translation(case)
            service_calls: list[dict[str, Any]] = []

            with (
                patch.object(
                    case.main,
                    "_inspect_and_analyze",
                    side_effect=AssertionError("translate must not re-analyze"),
                ),
                patch("mq_localizer.ui.inspect_instance_root", return_value=case.instance),
                patch("mq_localizer.ui.assert_glossary_inputs_unchanged"),
                patch("mq_localizer.ui.OpenAIClient", return_value=object()),
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(service_calls),
                ),
            ):
                case.main._translate()
                workers[0]()

            self.assertEqual(case.adapter.validate_calls, 1)
            self.assertEqual(
                service_calls[0]["existing"],
                {"unit": "latest translation"},
            )
            self.assertIn("confirm_output", case.main.events.event_names)
            self.assertNotIn("invalidate_analysis", case.main.events.event_names)

    def test_output_changed_during_validation_is_rejected_by_confirmation_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory, output_text="latest translation")
            case.adapter.mutate_during_validate = "raced translation"
            workers = self._prepare_translation(case)
            service_calls: list[dict[str, Any]] = []

            with (
                patch.object(
                    case.main,
                    "_inspect_and_analyze",
                    side_effect=AssertionError("translate must not re-analyze"),
                ),
                patch("mq_localizer.ui.inspect_instance_root", return_value=case.instance),
                patch("mq_localizer.ui.assert_glossary_inputs_unchanged"),
                patch("mq_localizer.ui.OpenAIClient") as openai_client,
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(service_calls),
                ),
            ):
                case.main._translate()
                with self.assertRaises(AdapterError):
                    workers[0]()

            self.assertEqual(case.adapter.validate_calls, 1)
            self.assertEqual(case.adapter.write_calls, 0)
            self.assertEqual(service_calls, [])
            openai_client.assert_not_called()
            self.assertNotIn("confirm_output", case.main.events.event_names)
            self.assertIs(case.main._analysis, case.analysis)

    def test_output_changed_after_confirmation_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory, output_text="latest translation")
            workers = self._prepare_translation(case)
            service_calls: list[dict[str, Any]] = []

            def race_after_confirmation() -> None:
                case.output.write_text("post-confirmation race", encoding="utf-8")

            with (
                patch.object(
                    case.main,
                    "_inspect_and_analyze",
                    side_effect=AssertionError("translate must not re-analyze"),
                ),
                patch("mq_localizer.ui.inspect_instance_root", return_value=case.instance),
                patch("mq_localizer.ui.assert_glossary_inputs_unchanged"),
                patch("mq_localizer.ui.OpenAIClient", return_value=object()),
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(
                        service_calls,
                        before_guard=race_after_confirmation,
                    ),
                ),
            ):
                case.main._translate()
                with self.assertRaises(AdapterError):
                    workers[0]()

            self.assertEqual(case.adapter.write_calls, 0)
            self.assertEqual(len(service_calls), 1)
            self.assertNotIn("guard_completed", service_calls[0])
            self.assertIs(case.main._analysis, case.analysis)

    def test_source_changed_during_openai_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory)
            workers = self._prepare_translation(case)
            service_calls: list[dict[str, Any]] = []

            def change_source() -> None:
                case.source.write_text('quest: "Changed during OpenAI"\n', encoding="utf-8")

            with (
                patch("mq_localizer.ui.inspect_instance_root", return_value=case.instance),
                patch("mq_localizer.ui.assert_glossary_inputs_unchanged"),
                patch("mq_localizer.ui.OpenAIClient", return_value=object()),
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(service_calls, before_guard=change_source),
                ),
            ):
                case.main._translate()
                with self.assertRaises(AdapterError):
                    workers[0]()

            self.assertEqual(len(service_calls), 1)
            self.assertNotIn("guard_completed", service_calls[0])
            self.assertEqual(case.adapter.write_calls, 0)
            self.assertIn("invalidate_analysis", case.main.events.event_names)

    def test_glossary_changed_during_openai_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory)
            workers = self._prepare_translation(case)
            service_calls: list[dict[str, Any]] = []
            checks = 0

            def check_glossary(
                _snapshot: object,
                _cancel: Event | None = None,
            ) -> None:
                nonlocal checks
                checks += 1
                if checks == 3:
                    raise TranslationError(
                        "固有名詞保護に使用したファイルが解析後に変更されました。"
                        "再解析してください"
                    )

            with (
                patch("mq_localizer.ui.inspect_instance_root", return_value=case.instance),
                patch(
                    "mq_localizer.ui.assert_glossary_inputs_unchanged",
                    side_effect=check_glossary,
                ),
                patch("mq_localizer.ui.OpenAIClient", return_value=object()),
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(service_calls),
                ),
            ):
                case.main._translate()
                with self.assertRaises(TranslationError):
                    workers[0]()

            self.assertEqual(checks, 3)
            self.assertEqual(len(service_calls), 1)
            self.assertNotIn("guard_completed", service_calls[0])
            self.assertEqual(case.adapter.write_calls, 0)
            self.assertIn("invalidate_analysis", case.main.events.event_names)

    def test_cancel_during_glossary_freshness_check_keeps_cached_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case = self._analyze_once(directory)
            workers = self._prepare_translation(case)
            service_calls: list[dict[str, Any]] = []

            def cancel(_snapshot: object, cancel_event: Event | None = None) -> None:
                assert cancel_event is not None
                cancel_event.set()
                raise CancelledError("処理をキャンセルしました")

            with (
                patch.object(
                    case.main,
                    "_inspect_and_analyze",
                    side_effect=AssertionError("translate must not re-analyze"),
                ),
                patch("mq_localizer.ui.inspect_instance_root", return_value=case.instance),
                patch(
                    "mq_localizer.ui.assert_glossary_inputs_unchanged",
                    side_effect=cancel,
                ),
                patch("mq_localizer.ui.OpenAIClient") as openai_client,
                patch(
                    "mq_localizer.ui.TranslationService",
                    self._service(service_calls),
                ),
            ):
                case.main._translate()
                with self.assertRaises(CancelledError):
                    workers[0]()

            self.assertIs(case.main._analysis, case.analysis)
            self.assertNotIn("invalidate_analysis", case.main.events.event_names)
            self.assertEqual(case.scanner.calls, 1)
            self.assertEqual(service_calls, [])
            openai_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
