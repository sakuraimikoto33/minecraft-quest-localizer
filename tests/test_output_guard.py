from __future__ import annotations

import sys
import stat
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import (  # noqa: E402
    AdapterError,
    CancelledError,
    TranslationProject,
    TranslationUnit,
)
from mq_localizer.glossary import GlossaryCatalog  # noqa: E402
from mq_localizer import output_guard  # noqa: E402
from mq_localizer.output_guard import (  # noqa: E402
    assert_path_unchanged,
    assert_source_unchanged,
    snapshot_path,
)
from mq_localizer.translator import TranslationOptions, TranslationService  # noqa: E402


class _ConcurrentWriterClient:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.calls = 0

    def translate_batch(
        self,
        _api_key: str,
        _model: str,
        items: list[dict[str, str]],
        _source_locale: str,
        _target_locale: str,
        _cancel: object = None,
    ) -> dict[str, str]:
        self.calls += 1
        self.output.write_text("CONCURRENT EDIT", encoding="utf-8")
        return {item["id"]: "訳:" + item["text"] for item in items}


class _RecordingAdapter:
    def __init__(self) -> None:
        self.write_calls = 0

    def write(
        self,
        _project: TranslationProject,
        _translations: dict[str, str],
        output_path: Path,
        selected_unit_ids: frozenset[str] | None = None,
    ) -> None:
        del selected_unit_ids
        self.write_calls += 1
        output_path.write_text("LOCALIZER WRITE", encoding="utf-8")


def _project(root: Path, output: Path) -> TranslationProject:
    return TranslationProject(
        adapter_id="test",
        adapter_label="Test",
        source_path=root / "source.snbt",
        default_output=output,
        source_locale="en_us",
        target_locale="ja_jp",
        units=[
            TranslationUnit(
                id="unit-1",
                key="quest.test.title",
                source="Translate me",
                category="quest_title",
            )
        ],
    )


class OutputSnapshotTests(unittest.TestCase):
    def test_snapshot_and_recheck_honor_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "ja_jp.snbt"
            output.write_text("before", encoding="utf-8")
            snapshot = snapshot_path(output)
            cancel = Event()
            cancel.set()

            with self.assertRaises(CancelledError):
                snapshot_path(output, cancel)
            with self.assertRaises(CancelledError):
                assert_path_unchanged(snapshot, cancel)
            with self.assertRaises(CancelledError):
                assert_source_unchanged(snapshot, cancel)

    def test_windows_directory_reparse_point_is_treated_as_a_non_recursive_junction(self) -> None:
        reparse_flag = 0x400
        fake_stat = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=reparse_flag,
        )
        with patch.object(
            output_guard.stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            reparse_flag,
            create=True,
        ):
            self.assertEqual(output_guard._kind(Path("nested-junction"), fake_stat), "junction")

    def test_missing_file_and_recursive_directory_changes_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "new-output.snbt"
            missing_snapshot = snapshot_path(missing)
            self.assertFalse(missing_snapshot.exists)
            assert_path_unchanged(missing_snapshot)
            missing.write_text("created elsewhere", encoding="utf-8")
            with self.assertRaisesRegex(AdapterError, "確認後に変更"):
                assert_path_unchanged(missing_snapshot)

            output_dir = root / "locale"
            nested = output_dir / "chapters" / "one.snbt"
            nested.parent.mkdir(parents=True)
            nested.write_text("before", encoding="utf-8")
            directory_snapshot = snapshot_path(output_dir)
            assert_path_unchanged(directory_snapshot)
            nested.write_text("after!", encoding="utf-8")
            with self.assertRaisesRegex(AdapterError, "確認後に変更"):
                assert_path_unchanged(directory_snapshot)

            source_snapshot = snapshot_path(output_dir)
            nested.write_text("source changed", encoding="utf-8")
            with self.assertRaisesRegex(AdapterError, "翻訳元が解析後に変更"):
                assert_source_unchanged(source_snapshot)

    def test_concurrent_api_time_edit_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            output.write_text("ORIGINAL", encoding="utf-8")
            snapshot = snapshot_path(output)
            client = _ConcurrentWriterClient(output)
            adapter = _RecordingAdapter()

            with self.assertRaisesRegex(AdapterError, "再度解析"):
                TranslationService(client).translate(
                    _project(root, output),
                    adapter,
                    output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                    pre_write_guard=lambda: assert_path_unchanged(snapshot),
                )

            self.assertEqual(client.calls, 1)
            self.assertEqual(adapter.write_calls, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "CONCURRENT EDIT")


if __name__ == "__main__":
    unittest.main()
