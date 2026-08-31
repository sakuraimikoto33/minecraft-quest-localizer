from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.analysis_log import (  # noqa: E402
    SessionAnalysisLog,
    SessionDebugLog,
    SessionOpenAIJsonLog,
)


_LOG_RECORD_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} "
    r"\[(?:DEBUG|INFO|WARNING|ERROR|SUCCESS)\] \[[^\]\r\n]+\] .*$"
)


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
            self.assertNotIn("ログ記録", text)
            records = [
                line for line in text.splitlines() if _LOG_RECORD_PATTERN.fullmatch(line)
            ]
            self.assertEqual(len(records), 153)
            self.assertTrue(all("[INFO] [実行ログ]" in line for line in records))
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

            records = [
                line for line in text.splitlines() if _LOG_RECORD_PATTERN.fullmatch(line)
            ]
            self.assertEqual(len(records), 2)
            self.assertTrue(records[0].endswith("line one"))
            self.assertTrue(records[1].endswith(r"line\u0000two\u200B"))
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
            records = [
                line for line in text.splitlines() if _LOG_RECORD_PATTERN.fullmatch(line)
            ]
            self.assertEqual(len(records), 80)
            self.assertEqual(sum("[WARNING]" in line for line in records), 4)
            self.assertEqual(sum("[INFO]" in line for line in records), 76)
            self.assertNotIn("=== ログ記録 ", text)
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


class SessionDebugLogTests(unittest.TestCase):
    def test_file_is_created_lazily_and_uses_a_distinct_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            logger = SessionDebugLog(
                log_dir,
                session_started=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
                process_id=321,
            )

            self.assertFalse(log_dir.exists())

            path = logger.write(
                "OpenAI request body",
                level="debug",
                section="OpenAI通信",
            )

            self.assertTrue(path.is_file())
            self.assertEqual(path.name, "debug-20260830T120000.000000-p321.log")
            self.assertEqual(list(log_dir.glob("analysis-*.log")), [])
            text = path.read_text(encoding="utf-8")
            self.assertIn("Minecraft Quest Localizer デバッグログ", text)
            self.assertNotIn("OpenAIへの要求と応答", text)
            self.assertNotIn("翻訳対象本文を省略せず", text)
            self.assertRegex(
                text,
                r"(?m)^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} "
                r"\[DEBUG\] \[OpenAI通信\] OpenAI request body$",
            )

    def test_debug_log_preserves_payload_but_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = SessionDebugLog(Path(directory), process_id=654)
            explicit = "custom-secret-value-123456"

            path = logger.write(
                "\n".join(
                    (
                        "request: Arcane Gold and Soul Gems",
                        f'Authorization: Bearer {explicit}',
                        'response: {"translation":"アーケインゴールド"}',
                        "fallback: sk-proj-abcdefghijklmnopqrstuvwxyz",
                    )
                ),
                explicit,
                level="DEBUG",
                section="OpenAI通信",
            )

            text = path.read_text(encoding="utf-8")
            self.assertIn("Arcane Gold and Soul Gems", text)
            self.assertIn("アーケインゴールド", text)
            self.assertNotIn(explicit, text)
            self.assertNotIn("sk-proj-", text)
            self.assertIn("Bearer [REDACTED]", text)
            self.assertIn("[API KEY REDACTED]", text)

    def test_debug_and_analysis_logs_rotate_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            log_dir.mkdir()
            unrelated = log_dir / "notes.log"
            unrelated.write_text("keep", encoding="utf-8")
            debug_lookalike = log_dir / "debug-20260101T000000.000000-p999.log"
            debug_lookalike.write_text("not an application debug log", encoding="utf-8")
            start = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
            analysis_paths: list[Path] = []
            debug_paths: list[Path] = []

            for index in range(4):
                analysis_paths.append(
                    SessionAnalysisLog(
                        log_dir,
                        max_files=2,
                        session_started=start + timedelta(seconds=index),
                        process_id=100 + index,
                    ).write(f"analysis {index}")
                )
                debug_paths.append(
                    SessionDebugLog(
                        log_dir,
                        max_files=2,
                        session_started=start + timedelta(seconds=index),
                        process_id=200 + index,
                    ).write(f"debug {index}")
                )

            remaining_analysis = sorted(log_dir.glob("analysis-*.log"))
            remaining_debug = sorted(
                path for path in log_dir.glob("debug-*.log") if path != debug_lookalike
            )
            self.assertEqual(
                [path.name for path in remaining_analysis],
                [path.name for path in analysis_paths[-2:]],
            )
            self.assertEqual(
                [path.name for path in remaining_debug],
                [path.name for path in debug_paths[-2:]],
            )
            self.assertTrue(unrelated.exists())
            self.assertEqual(
                debug_lookalike.read_text(encoding="utf-8"),
                "not an application debug log",
            )


class SessionOpenAIJsonLogTests(unittest.TestCase):
    def test_lazy_json_document_appends_events_and_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            logger = SessionOpenAIJsonLog(
                log_dir,
                session_started=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
                process_id=777,
            )
            explicit_key = "custom-api-key-value-123456"
            bearer_secret = "bearer-secret-value-123456"
            patterned_key = "sk-proj-abcdefghijklmnopqrstuvwxyz"

            self.assertFalse(log_dir.exists())

            request = {
                "phase": "REQUEST",
                "method": "POST",
                "headers": {
                    "Authorization": f"Bearer {bearer_secret}",
                    "client-secret": "unregistered-client-secret",
                },
                "payload": {
                    "input": "Arcane Gold and Soul Gems",
                    "echoed_key": explicit_key,
                    "patterned_key": patterned_key,
                },
            }
            first_path = logger.write(
                "OpenAI REQUEST\n" + json.dumps(request, ensure_ascii=False),
                explicit_key,
            )

            self.assertEqual(
                first_path.name,
                "openai-20260830T120000.000000-p777.json",
            )
            self.assertEqual(list(log_dir.glob("analysis-*.log")), [])
            self.assertEqual(list(log_dir.glob("debug-*.log")), [])
            first_document = json.loads(first_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    key: first_document["metadata"][key]
                    for key in ("format", "application", "process_id")
                },
                {
                    "format": "minecraft-quest-localizer-openai-events-v1",
                    "application": "Minecraft Quest Localizer",
                    "process_id": 777,
                },
            )
            self.assertEqual(
                datetime.fromisoformat(
                    first_document["metadata"]["session_started"]
                ).astimezone(timezone.utc),
                datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(len(first_document["events"]), 1)
            self.assertEqual(first_document["events"][0]["sequence"], 1)
            first_recorded_at = datetime.fromisoformat(
                first_document["events"][0]["recorded_at"]
            )
            self.assertIsNotNone(first_recorded_at.tzinfo)
            self.assertEqual(
                first_document["events"][0]["payload"]["input"],
                "Arcane Gold and Soul Gems",
            )
            first_raw = first_path.read_text(encoding="utf-8")
            self.assertNotIn(explicit_key, first_raw)
            self.assertNotIn(bearer_secret, first_raw)
            self.assertNotIn("unregistered-client-secret", first_raw)
            self.assertNotIn(patterned_key, first_raw)
            self.assertEqual(
                first_document["events"][0]["headers"]["Authorization"],
                "[REDACTED]",
            )
            self.assertEqual(
                first_document["events"][0]["headers"]["client-secret"],
                "[REDACTED]",
            )
            self.assertIn("[API KEY REDACTED]", first_raw)

            response = {
                "response": {
                    "output_text": "翻訳結果",
                    "echoed_key": explicit_key,
                }
            }
            second_path = logger.write(
                "OpenAI RESPONSE\n" + json.dumps(response, ensure_ascii=False),
                explicit_key,
            )

            self.assertEqual(first_path, second_path)
            completed_document = json.loads(second_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [event["phase"] for event in completed_document["events"]],
                ["REQUEST", "RESPONSE"],
            )
            self.assertEqual(
                [event["sequence"] for event in completed_document["events"]],
                [1, 2],
            )
            second_recorded_at = datetime.fromisoformat(
                completed_document["events"][1]["recorded_at"]
            )
            self.assertGreaterEqual(second_recorded_at, first_recorded_at)
            self.assertEqual(
                completed_document["events"][1]["response"]["output_text"],
                "翻訳結果",
            )
            self.assertNotIn(
                explicit_key,
                second_path.read_text(encoding="utf-8"),
            )

    def test_filename_collision_uses_suffix_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            started = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
            first = SessionOpenAIJsonLog(
                log_dir,
                session_started=started,
                process_id=77,
            )
            second = SessionOpenAIJsonLog(
                log_dir,
                session_started=started,
                process_id=77,
            )

            first_path = first.write('OpenAI REQUEST\n{"marker":"first"}')
            second_path = second.write('OpenAI RESPONSE\n{"marker":"second"}')

            self.assertEqual(first_path.name, "openai-20260830T120000.000000-p77.json")
            self.assertEqual(second_path.name, "openai-20260830T120000.000000-p77-1.json")
            self.assertEqual(
                json.loads(first_path.read_text(encoding="utf-8"))["events"][0][
                    "marker"
                ],
                "first",
            )
            self.assertEqual(
                json.loads(second_path.read_text(encoding="utf-8"))["events"][0][
                    "marker"
                ],
                "second",
            )

    def test_rotation_is_independent_and_preserves_non_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            log_dir.mkdir()
            unrelated = log_dir / "notes.json"
            unrelated.write_text("keep", encoding="utf-8")
            lookalike = log_dir / "openai-20260101T000000.000000-p999.json"
            lookalike.write_text(
                '{"metadata":{"format":"not-owned"},"events":[]}',
                encoding="utf-8",
            )
            analysis_path = SessionAnalysisLog(log_dir, process_id=10).write("analysis")
            debug_path = SessionDebugLog(log_dir, process_id=11).write("debug")
            start = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
            created: list[Path] = []

            for index in range(4):
                logger = SessionOpenAIJsonLog(
                    log_dir,
                    max_files=2,
                    session_started=start + timedelta(seconds=index),
                    process_id=200 + index,
                )
                created.append(
                    logger.write(
                        "OpenAI REQUEST\n"
                        + json.dumps({"sequence": index}, ensure_ascii=False)
                    )
                )

            remaining = sorted(
                path for path in log_dir.glob("openai-*.json") if path != lookalike
            )
            self.assertEqual(
                [path.name for path in remaining],
                [path.name for path in created[-2:]],
            )
            self.assertTrue(unrelated.exists())
            self.assertEqual(
                lookalike.read_text(encoding="utf-8"),
                '{"metadata":{"format":"not-owned"},"events":[]}',
            )
            self.assertTrue(analysis_path.exists())
            self.assertTrue(debug_path.exists())
            for path in remaining:
                self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_invalid_callback_remains_lazy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            logger = SessionOpenAIJsonLog(log_dir, process_id=99)

            with self.assertRaisesRegex(ValueError, "JSONが不正"):
                logger.write("OpenAI REQUEST\n{not-json}")

            self.assertFalse(log_dir.exists())

    def test_rejects_unknown_or_mismatched_phase_and_outer_duplicate_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            logger = SessionOpenAIJsonLog(log_dir, process_id=100)

            invalid_callbacks = (
                ('OpenAI RETRY\n{"phase":"RETRY"}', "見出しが不正"),
                (
                    'OpenAI REQUEST\n{"phase":"RESPONSE"}',
                    "見出しとphaseが一致しません",
                ),
                (
                    'OpenAI REQUEST\n{"phase":"REQUEST","phase":"REQUEST"}',
                    "重複キーがあります",
                ),
            )
            for message, expected_error in invalid_callbacks:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, expected_error):
                        logger.write(message)

            self.assertFalse(log_dir.exists())

    def test_append_failure_keeps_previous_document_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            logger = SessionOpenAIJsonLog(
                log_dir,
                session_started=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
                process_id=101,
            )
            path = logger.write('OpenAI REQUEST\n{"phase":"REQUEST","value":1}')
            before = path.read_bytes()

            with patch(
                "mq_localizer.analysis_log.os.replace",
                side_effect=OSError("atomic replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "atomic replace failed"):
                    logger.write(
                        'OpenAI RESPONSE\n{"phase":"RESPONSE","value":2}'
                    )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                [event["sequence"] for event in json.loads(before)["events"]],
                [1],
            )
            self.assertEqual(list(log_dir.glob(".*.tmp")), [])

            logger.write('OpenAI RESPONSE\n{"phase":"RESPONSE","value":3}')
            recovered = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                [event["sequence"] for event in recovered["events"]],
                [1, 2],
            )
            self.assertEqual(recovered["events"][1]["value"], 3)

    def test_initial_write_failure_does_not_leave_a_truncated_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            logger = SessionOpenAIJsonLog(
                log_dir,
                session_started=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
                process_id=102,
            )

            with patch(
                "mq_localizer.analysis_log.os.fsync",
                side_effect=OSError("initial flush failed"),
            ):
                with self.assertRaisesRegex(OSError, "initial flush failed"):
                    logger.write('OpenAI REQUEST\n{"phase":"REQUEST","value":1}')

            self.assertEqual(list(log_dir.glob("openai-*.json")), [])
            self.assertEqual(list(log_dir.glob(".*.tmp")), [])

            path = logger.write('OpenAI REQUEST\n{"phase":"REQUEST","value":2}')
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["events"][0]["sequence"], 1)
            self.assertEqual(document["events"][0]["value"], 2)

    def test_atomic_publish_failure_is_not_mistaken_for_a_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "logs"
            logger = SessionOpenAIJsonLog(log_dir, process_id=103)

            with patch(
                "mq_localizer.analysis_log.os.link",
                side_effect=PermissionError("hard links unavailable"),
            ) as publish:
                with self.assertRaisesRegex(PermissionError, "hard links unavailable"):
                    logger.write('OpenAI REQUEST\n{"phase":"REQUEST"}')

            publish.assert_called_once()
            self.assertEqual(list(log_dir.glob("openai-*.json")), [])
            self.assertEqual(list(log_dir.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
