from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer import io_utils  # noqa: E402


class AtomicManyWriteTests(unittest.TestCase):
    def test_rollback_never_overwrites_a_concurrent_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.snbt"
            second = root / "second.snbt"
            first.write_text("original first\n", encoding="utf-8")
            second.write_text("original second\n", encoding="utf-8")
            real_atomic_write = io_utils.atomic_write_text
            calls = 0

            def edit_first_then_fail_second(
                path: Path,
                text: str,
                encoding: str = "utf-8",
                newline: str | None = None,
            ) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second write failure")
                identity = real_atomic_write(path, text, encoding=encoding, newline=newline)
                first.write_text("external concurrent edit\n", encoding="utf-8")
                return identity

            with patch.object(
                io_utils,
                "atomic_write_text",
                side_effect=edit_first_then_fail_second,
            ):
                with self.assertRaisesRegex(OSError, "外部変更.*保持"):
                    io_utils.atomic_write_many_text(
                        [
                            (first, "localizer first\n", "\n"),
                            (second, "localizer second\n", "\n"),
                        ]
                    )

            self.assertEqual(first.read_text(encoding="utf-8"), "external concurrent edit\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "original second\n")

    def test_rollback_preserves_an_identical_byte_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.snbt"
            second = root / "second.snbt"
            external = root / "external.snbt"
            first.write_text("original first\n", encoding="utf-8")
            second.write_text("original second\n", encoding="utf-8")
            real_atomic_write = io_utils.atomic_write_text
            calls = 0

            def replace_first_then_fail_second(
                path: Path,
                text: str,
                encoding: str = "utf-8",
                newline: str | None = None,
            ) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second write failure")
                identity = real_atomic_write(path, text, encoding=encoding, newline=newline)
                external.write_bytes(b"localizer first\n")
                first.unlink()
                os.link(external, first)
                return identity

            with patch.object(
                io_utils,
                "atomic_write_text",
                side_effect=replace_first_then_fail_second,
            ):
                with self.assertRaisesRegex(OSError, "外部置換.*保持"):
                    io_utils.atomic_write_many_text(
                        [
                            (first, "localizer first\n", "\n"),
                            (second, "localizer second\n", "\n"),
                        ]
                    )

            self.assertTrue(os.path.samefile(first, external))
            self.assertEqual(first.read_text(encoding="utf-8"), "localizer first\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "original second\n")


if __name__ == "__main__":
    unittest.main()
