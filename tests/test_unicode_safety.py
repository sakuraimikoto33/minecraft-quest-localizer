from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.unicode_safety import translation_unicode_issue  # noqa: E402


class JapaneseUnicodeSafetyTests(unittest.TestCase):
    def test_reported_armenian_letters_are_rejected(self) -> None:
        issue = translation_unicode_issue(
            "transmutation tricks",
            "変成の հնարみ",
            "ja_jp",
        )

        self.assertIsNotNone(issue)
        self.assertIn("異種文字", issue or "")
        self.assertIn("Armenian", issue or "")
        self.assertIn("U+0570", issue or "")

    def test_reported_zero_width_spaces_are_rejected(self) -> None:
        issue = translation_unicode_issue(
            "Basic Access Port",
            "基本アクセス\u200b\u200bポート",
            "ja_jp",
        )

        self.assertIsNotNone(issue)
        self.assertIn("不可視", issue or "")
        self.assertIn("U+200B ZERO WIDTH SPACE", issue or "")

    def test_source_characters_and_trusted_official_term_are_allowed(self) -> None:
        source = "Use Օննա\u200d"
        candidate = "Օննա\u200dを使う 公式\u200b名"
        official_start = candidate.index("公式")

        self.assertIsNone(
            translation_unicode_issue(
                source,
                candidate,
                "ja_jp",
                trusted_candidate_ranges=(
                    (official_start, official_start + len("公式\u200b名")),
                ),
            )
        )

    def test_other_locales_may_introduce_their_script_and_zwj(self) -> None:
        self.assertIsNone(
            translation_unicode_issue(
                "Scientist",
                "वैज्ञानिक 👩\u200d🔬",
                "hi_in",
            )
        )

    def test_japanese_scripts_greek_and_cjk_extensions_are_allowed(self) -> None:
        self.assertIsNone(
            translation_unicode_issue(
                "Alpha test",
                "αテストで𠀀を使う",
                "ja_jp",
            )
        )

    def test_new_private_use_surrogate_unassigned_and_replacement_are_rejected(self) -> None:
        cases = (
            "",
            "\ud800",
            "\u0378",
            "\N{REPLACEMENT CHARACTER}",
        )
        for character in cases:
            with self.subTest(codepoint=f"U+{ord(character):04X}"):
                issue = translation_unicode_issue("Source", "訳" + character, "ja_jp")
                self.assertIsNotNone(issue)
                self.assertIn(f"U+{ord(character):04X}", issue or "")

    def test_source_private_use_glyph_is_allowed_by_count(self) -> None:
        self.assertIsNone(
            translation_unicode_issue("Open ", "開く ", "ja_jp")
        )


if __name__ == "__main__":
    unittest.main()
