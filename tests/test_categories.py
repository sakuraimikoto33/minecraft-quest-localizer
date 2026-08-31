from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.categories import (  # noqa: E402
    DEFAULT_TRANSLATION_CATEGORY_IDS,
    FTB_TRANSLATION_CATEGORIES,
    classify_ftb_text,
)


class FtbCategoryTests(unittest.TestCase):
    def test_official_object_and_translation_key_pairs(self) -> None:
        expected = {
            "mq_localizer.file.2722bacc0e7ccf74.title": "quest_book_title",
            "quest_book.main.title": "quest_book_title",
            "chapter_group.0000000000000001.title": "chapter_group_title",
            "chapter.0000000000000002.title": "chapter_title",
            "chapter.0000000000000002.chapter_subtitle": "chapter_subtitle",
            "quest.0000000000000003.title": "quest_title",
            "quest.0000000000000003.quest_subtitle": "quest_subtitle",
            "quest.0000000000000003.quest_desc": "quest_description",
            "task.0000000000000004.title": "task_title",
            "reward.0000000000000005.title": "reward_title",
            "reward_table.0000000000000006.title": "reward_table_title",
            "quest_link.0000000000000007.title": "quest_link_title",
            "image.0000000000000008.hover": "image_hover",
        }
        self.assertEqual(
            {key: classify_ftb_text(key) for key in expected},
            expected,
        )

    def test_lists_legacy_aliases_and_generated_raw_keys(self) -> None:
        cases = {
            "mq_localizer.file.1.title": "quest_book_title",
            "mq_localizer.file.1.title.part.1": "quest_book_title",
            "mq_localizer.file.1.title.2.part.3": "quest_book_title",
            "mq_localizer.file.1.title.part.1.2": "quest_book_title",
            "quest.1.quest_desc[12]": "quest_description",
            "atm9.quest.welcome.description": "quest_description",
            "atm9.quest.welcome.subtitle": "quest_subtitle",
            "mq_localizer.chapter.1.subtitle.2": "chapter_subtitle",
            "mq_localizer.quest.1.description.3.part.1": "quest_description",
            "mq_localizer.image.1.hover.2.part.1": "image_hover",
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                self.assertEqual(classify_ftb_text(key), expected)

    def test_unknown_and_substrings_are_conservative(self) -> None:
        for key in (
            "atm9.quest.machine",
            "myquest.foo.title",
            "taskmaster.name",
            "custom.file.foo.title",
            "custom.key",
        ):
            with self.subTest(key=key):
                self.assertEqual(classify_ftb_text(key), "other")
        self.assertEqual(
            classify_ftb_text("custom.key", "chapter image hover text"),
            "image_hover",
        )
        self.assertEqual(
            classify_ftb_text("custom.key", "file title"),
            "quest_book_title",
        )

    def test_key_suffix_and_nearest_object_win_over_ambiguous_words_or_context(self) -> None:
        self.assertEqual(classify_ftb_text("pack.quest.reward.foo.title"), "reward_title")
        self.assertEqual(classify_ftb_text("pack.quest.description.title"), "quest_title")
        self.assertEqual(
            classify_ftb_text("reward.0000000000000001.title", "quest title"),
            "reward_title",
        )

    def test_default_selection_contains_every_declared_category_once(self) -> None:
        ids = [category.id for category in FTB_TRANSLATION_CATEGORIES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(DEFAULT_TRANSLATION_CATEGORY_IDS, frozenset(ids))


if __name__ == "__main__":
    unittest.main()
