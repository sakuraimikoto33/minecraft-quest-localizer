from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.analysis_log import SessionAnalysisLog  # noqa: E402


class SessionAnalysisLogTests(unittest.TestCase):
    def test_one_utf8_file_per_session_keeps_every_entry_and_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = SessionAnalysisLog(
                Path(directory) / "logs",
                session_started=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
                process_id=123,
            )
            explicit = "custom-secret-value-123456"
            first = "\n".join(["最初の解析", *(f"警告 {index}" for index in range(150))])

            first_path = logger.write(first + "\n" + explicit, explicit)
            second_path = logger.write(
                "再解析 Authorization: Bearer abc.def_123 sk-proj-abcdefghijklmnopqrstuvwxyz"
            )

            self.assertEqual(first_path, second_path)
            self.assertTrue(first_path.is_absolute())
            self.assertEqual(list(first_path.parent.glob("analysis-*.log")), [first_path])
            raw = first_path.read_bytes()
            text = raw.decode("utf-8")
            self.assertIn("最初の解析", text)
            self.assertIn("警告 149", text)
            self.assertIn("ログ記録 1", text)
            self.assertIn("ログ記録 2", text)
            self.assertNotIn(explicit, text)
            self.assertNotIn("abc.def_123", text)
            self.assertNotIn("sk-proj-", text)
            self.assertIn("[API KEY REDACTED]", text)
            self.assertIn("Bearer [REDACTED]", text)

    def test_rotation_keeps_latest_sessions_and_never_removes_unrelated_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            unrelated = log_dir / "notes.log"
            log_dir.mkdir()
            unrelated.write_text("keep", encoding="utf-8")
            lookalike = log_dir / "analysis-20260101T000000.000000-p999.log"
            lookalike.write_text("not an application log", encoding="utf-8")
            start = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
            created: list[Path] = []
            for index in range(5):
                logger = SessionAnalysisLog(
                    log_dir,
                    max_files=3,
                    session_started=start + timedelta(seconds=index),
                    process_id=os.getpid() + index,
                )
                created.append(logger.write(f"session {index}"))

            remaining = sorted(path for path in log_dir.glob("analysis-*.log") if path != lookalike)
            expected = created[-3:]
            self.assertEqual(
                [path.name for path in remaining],
                [path.name for path in expected],
            )
            for actual, expected_path in zip(remaining, expected, strict=True):
                self.assertTrue(actual.samefile(expected_path))
            self.assertTrue(unrelated.exists())
            self.assertEqual(lookalike.read_text(encoding="utf-8"), "not an application log")

    def test_filename_collision_uses_a_second_file_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
            first = SessionAnalysisLog(
                Path(directory), session_started=started, process_id=77
            )
            second = SessionAnalysisLog(
                Path(directory), session_started=started, process_id=77
            )

            first_path = first.write("first")
            second_path = second.write("second")

            self.assertNotEqual(first_path, second_path)
            self.assertIn("first", first_path.read_text(encoding="utf-8"))
            self.assertIn("second", second_path.read_text(encoding="utf-8"))

    def test_control_characters_are_escaped_but_newlines_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = SessionAnalysisLog(Path(directory), process_id=99)

            path = logger.write("line one\nline\x00two\u200b")
            text = path.read_text(encoding="utf-8")

            self.assertIn("line one\nline\\u0000two\\u200B", text)
            self.assertNotIn("\x00", text)
            self.assertNotIn("\u200b", text)

    def test_sections_levels_and_concurrent_events_share_one_append_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = SessionAnalysisLog(Path(directory), process_id=100)

            def write_events(worker: int) -> None:
                for index in range(20):
                    logger.write(
                        f"worker {worker} event {index}",
                        level="warning" if index == 0 else "info",
                        section=f"worker {worker}",
                    )

            threads = [Thread(target=write_events, args=(worker,)) for worker in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            paths = list(Path(directory).glob("analysis-*.log"))
            self.assertEqual(len(paths), 1)
            text = paths[0].read_text(encoding="utf-8")
            self.assertEqual(text.count("=== ログ記録 "), 80)
            self.assertIn("レベル: WARNING", text)
            for worker in range(4):
                for index in range(20):
                    self.assertEqual(text.count(f"worker {worker} event {index}\n"), 1)

    def test_rotation_recognizes_legacy_application_header_but_not_lookalikes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            legacy = log_dir / "analysis-20260101T000000.000000-p1.log"
            legacy.write_text(
                "Minecraft Quest Localizer 解析ログ\nold\n",
                encoding="utf-8",
            )
            lookalike = log_dir / "analysis-20260101T000001.000000-p2.log"
            lookalike.write_text(
                "Minecraft Quest Localizer 解析ログではない\n",
                encoding="utf-8",
            )
            old_time = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
            os.utime(legacy, (old_time, old_time))
            os.utime(lookalike, (old_time, old_time))

            logger = SessionAnalysisLog(
                log_dir,
                max_files=1,
                session_started=datetime(2026, 8, 30, tzinfo=timezone.utc),
                process_id=3,
            )
            current = logger.write("current")

            self.assertFalse(legacy.exists())
            self.assertTrue(lookalike.exists())
            self.assertTrue(current.exists())


if __name__ == "__main__":
    unittest.main()
