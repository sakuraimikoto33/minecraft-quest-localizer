from __future__ import annotations

import hashlib
import re
import shutil
import sys
import tempfile
import unittest
from collections.abc import Mapping, Set
from pathlib import Path
from queue import Queue
from threading import Event, Lock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.adapters import (  # noqa: E402
    AdapterRegistry,
    QuestAdapter,
    create_default_registry,
)
from mq_localizer.adapters.base import validate_translation_selection  # noqa: E402
from mq_localizer.application import LocalizerApplication  # noqa: E402
from mq_localizer.domain import TranslationProject, TranslationUnit  # noqa: E402
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry  # noqa: E402
from mq_localizer.glossary import ModLanguageScanner  # noqa: E402
from mq_localizer.snbt import parse_lang_snbt  # noqa: E402
from mq_localizer.translator import TranslationOptions, TranslationService  # noqa: E402
from mq_localizer.ui import MainWindow  # noqa: E402


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


def _tree_hashes(path: Path) -> dict[str, str]:
    return {
        candidate.relative_to(path).as_posix(): hashlib.sha256(candidate.read_bytes()).hexdigest()
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file()
    }


class FakeTranslationClient:
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        return {
            item["id"]: _prefix_inside_protected_segment(item["text"])
            for item in items
        }


class EndToEndTranslationTests(unittest.TestCase):
    def test_third_party_adapter_runs_through_application_and_shared_translator(self) -> None:
        class FutureQuestAdapter(QuestAdapter):
            id = "future_quest_format"
            label = "Future quest format"
            description = "Adapter extension contract test"

            def __init__(self) -> None:
                self.writes: list[dict[str, str]] = []

            def probe(self, path: Path, source_locale: str) -> int:
                del source_locale
                return 100 if path.suffix == ".future" else 0

            def load(
                self,
                path: Path,
                source_locale: str,
                target_locale: str,
                minecraft_version: str = "",
                output_override: Path | None = None,
            ) -> TranslationProject:
                del minecraft_version
                return TranslationProject(
                    adapter_id=self.id,
                    adapter_label=self.label,
                    source_path=path,
                    default_output=output_override or path.with_suffix(".translated"),
                    source_locale=source_locale,
                    target_locale=target_locale,
                    units=[
                        TranslationUnit(
                            id="future-unit",
                            key="future.quest.title",
                            source=path.read_text(encoding="utf-8"),
                            category="other",
                        )
                    ],
                )

            def write(
                self,
                project: TranslationProject,
                translations: Mapping[str, str],
                output_path: Path,
                selected_unit_ids: Set[str] | None = None,
            ) -> None:
                self.validate_output(project, output_path)
                validate_translation_selection(
                    project,
                    translations,
                    selected_unit_ids,
                )
                self.writes.append(dict(translations))
                output_path.write_text(
                    translations["future-unit"],
                    encoding="utf-8",
                )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "quests.future"
            source.write_text("Future Quest", encoding="utf-8")
            adapter = FutureQuestAdapter()
            application = LocalizerApplication(AdapterRegistry([adapter]))

            analyzed = application.analyze(
                source,
                "auto",
                "en_us",
                "ja_jp",
                "1.21.1",
            )
            outcome = TranslationService(FakeTranslationClient()).translate(
                analyzed.project,
                analyzed.adapter,
                analyzed.project.default_output,
                "sk-test-placeholder",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(preserve_existing=False),
            )

            self.assertIs(analyzed.adapter, adapter)
            self.assertEqual(adapter.writes, [{"future-unit": "訳:Future Quest"}])
            self.assertEqual(
                analyzed.project.default_output.read_text(encoding="utf-8"),
                "訳:Future Quest",
            )
            self.assertEqual(outcome.translated, 1)

    def test_single_instance_root_drives_version_format_source_output_and_mod_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory) / "native_snbt"
            shutil.copytree(FIXTURES / "native_snbt", instance)
            (instance / "mods").mkdir()
            (instance / "mmc-pack.json").write_text(
                '{"components":[{"uid":"net.minecraft","version":"1.21.1"}]}',
                encoding="utf-8",
            )
            main = object.__new__(MainWindow)
            main.cancel_event = Event()
            main.events = Queue()
            main._stage_lock = Lock()
            main._pending_analysis_stage = None
            main._stage_event_queued = False
            main.application = LocalizerApplication()
            main.scanner = ModLanguageScanner()

            analysis = main._inspect_and_analyze(
                {
                    "instance_root": instance,
                    "source_locale": "en_us",
                    "target_locale": "ja_jp",
                }
            )

            expected_source = instance / "config" / "ftbquests" / "quests" / "lang" / "en_us.snbt"
            self.assertEqual(analysis.instance.minecraft_version, "1.21.1")
            self.assertEqual(analysis.instance.game_root, instance)
            self.assertEqual(analysis.analyzed.adapter.id, "ftb_modern_snbt")
            self.assertEqual(analysis.analyzed.project.source_path, expected_source)
            self.assertEqual(analysis.analyzed.project.default_output, expected_source.with_name("ja_jp.snbt"))
            self.assertEqual(analysis.glossary.coverage.scan_state, "no_archives")

    def test_native_fixture_fake_api_glossary_and_atomic_adapter_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory) / "native_snbt"
            shutil.copytree(FIXTURES / "native_snbt", instance)
            source = instance / "config" / "ftbquests" / "quests" / "lang" / "en_us.snbt"
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            target = source.with_name("ja_jp.snbt")

            adapter = create_default_registry().detect(instance, "en_us")
            project = adapter.load(instance, "en_us", "ja_jp", minecraft_version="1.21.1")
            glossary = GlossaryCatalog(
                entries={
                    "Machine": GlossaryEntry(
                        source="Machine",
                        target="機械",
                        key="block.example.machine",
                        mod_id="example",
                        translated=True,
                        provenance="example.jar!/assets/example/lang/en_us.json",
                    )
                }
            )

            outcome = TranslationService(FakeTranslationClient()).translate(
                project,
                adapter,
                target,
                "sk-test-placeholder",
                "gpt-test",
                glossary,
                TranslationOptions(preserve_existing=False),
            )

            rendered = parse_lang_snbt(target.read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)

        self.assertEqual(outcome.total, 5)
        self.assertEqual(outcome.translated, 4)
        self.assertEqual(outcome.copied_without_translation, 1)
        self.assertEqual(rendered["quest.00000000000000A1.title"], "&6訳:Welcome&r")
        self.assertEqual(
            rendered["quest.00000000000000A1.quest_desc"],
            ["&a訳:First line&r", "", "訳:Second line\ncontinued"],
        )
        self.assertEqual(rendered["task.00000000000000B2.title"], "&b機械&r")

    def test_every_supported_format_runs_through_the_shared_translation_pipeline(self) -> None:
        cases = (
            ("split_snbt", "ftb_split_snbt", "1.21.1", "*.snbt"),
            ("split_json5", "ftb_split_json5", "26.1.2", "*.json5"),
            ("legacy_json", "ftb_legacy_json", "1.20.1", "*.json"),
            ("legacy_raw", "ftb_legacy_raw", "1.20.1", "*.json"),
        )
        for fixture, expected_adapter, version, output_pattern in cases:
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as directory:
                instance = Path(directory) / fixture
                shutil.copytree(FIXTURES / fixture, instance)
                adapter = create_default_registry().detect(instance, "en_us")
                self.assertEqual(adapter.id, expected_adapter)
                project = adapter.load(instance, "en_us", "ja_jp", minecraft_version=version)
                before = _tree_hashes(project.source_path)

                outcome = TranslationService(FakeTranslationClient()).translate(
                    project,
                    adapter,
                    project.default_output,
                    "sk-test-placeholder",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(preserve_existing=False),
                )

                preserved_source = (
                    Path(project.metadata["backup_quest_root"])
                    if expected_adapter == "ftb_legacy_raw"
                    else project.source_path
                )
                self.assertEqual(_tree_hashes(preserved_source), before)
                self.assertEqual(outcome.total, len(project.units))
                self.assertEqual(outcome.reused, 0)
                if project.default_output.is_dir():
                    self.assertTrue(any(project.default_output.rglob(output_pattern)))
                else:
                    self.assertTrue(project.default_output.is_file())


if __name__ == "__main__":
    unittest.main()
