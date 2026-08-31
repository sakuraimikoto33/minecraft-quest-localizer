from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TranslationCategory:
    """A user-selectable kind of FTB Quests text."""

    id: str
    label: str
    description: str


FTB_TRANSLATION_CATEGORIES: tuple[TranslationCategory, ...] = (
    TranslationCategory("quest_book_title", "クエストブック名", "クエストブック全体のタイトル"),
    TranslationCategory("chapter_group_title", "チャプターグループ名", "チャプターグループのタイトル"),
    TranslationCategory("chapter_title", "チャプター名", "チャプターのタイトル"),
    TranslationCategory("chapter_subtitle", "チャプター副題", "チャプターの説明・サブタイトル"),
    TranslationCategory("quest_title", "クエスト名", "クエストのタイトル"),
    TranslationCategory("quest_subtitle", "クエスト副題", "クエストのサブタイトル"),
    TranslationCategory("quest_description", "クエスト詳細", "クエスト本文・description"),
    TranslationCategory("task_title", "タスク名", "タスクのカスタムタイトル"),
    TranslationCategory("reward_title", "報酬名", "報酬のカスタムタイトル"),
    TranslationCategory("reward_table_title", "報酬テーブル名", "報酬テーブルのタイトル"),
    TranslationCategory("quest_link_title", "クエストリンク名", "クエストリンクのタイトル"),
    TranslationCategory("image_hover", "画像ホバー文", "チャプター画像に表示するホバーテキスト"),
    TranslationCategory("other", "その他のテキスト", "独自キーなど、自動分類できない翻訳文字列"),
)

DEFAULT_TRANSLATION_CATEGORY_IDS: frozenset[str] = frozenset(
    category.id for category in FTB_TRANSLATION_CATEGORIES
)
TRANSLATION_CATEGORY_BY_ID = {category.id: category for category in FTB_TRANSLATION_CATEGORIES}

# Categories that represent stable project names, and prose surfaces where
# those names may be referenced.  Keeping these adapter-neutral lets future
# quest adapters participate without duplicating FTB-specific string lists.
REFERENCE_NAME_CATEGORY_IDS: frozenset[str] = frozenset(
    {
        "quest_book_title",
        "chapter_group_title",
        "chapter_title",
        "quest_title",
        "task_title",
        "reward_title",
        "reward_table_title",
        "quest_link_title",
    }
)
REFERENCE_PROSE_CATEGORY_IDS: frozenset[str] = frozenset(
    {
        "chapter_subtitle",
        "quest_subtitle",
        "quest_description",
        "image_hover",
    }
)


_ARRAY_INDEX = re.compile(r"\[\d+\]$")
_SEGMENT_SPLIT = re.compile(r"[.:/\\\s]+")
_LEGACY_RAW_BOOK_TITLE = re.compile(
    r"^mq_localizer\.file\..+\.title"
    r"(?:\.\d+)?(?:\.part\.\d+)?(?:\.\d+)?$",
    re.IGNORECASE,
)
_OBJECT_ALIASES = {
    "quest_book": "quest_book",
    "questbook": "quest_book",
    "chapter_group": "chapter_group",
    "chaptergroup": "chapter_group",
    "reward_table": "reward_table",
    "rewardtable": "reward_table",
    "quest_link": "quest_link",
    "questlink": "quest_link",
    "chapter_image": "image",
    "image": "image",
    "chapter": "chapter",
    "quest": "quest",
    "task": "task",
    "reward": "reward",
}
_FIELD_ALIASES = {
    "chapter_subtitle": "chapter_subtitle",
    "quest_subtitle": "quest_subtitle",
    "quest_desc": "quest_desc",
    "description": "description",
    "subtitle": "subtitle",
    "hover": "hover",
    "desc": "desc",
    "title": "title",
}


def classify_ftb_text(key: str, context: str = "") -> str:
    """Classify official and commonly exported FTB Quests translation keys.

    Official locale keys use ``<object-type>.<id>.<sub-key>``.  Older
    exporters are less consistent, so the classifier also accepts field
    aliases and the explicit context produced by the raw-SNBT adapter.
    Unknown keys deliberately remain selectable through ``other``.
    """

    normalized_key = _ARRAY_INDEX.sub("", key.strip().lower())
    # Preserve the legacy raw adapter's existing generated key while avoiding
    # a broad ``file.*.title`` rule that could capture unrelated custom keys.
    if _LEGACY_RAW_BOOK_TITLE.fullmatch(normalized_key) or context.strip().lower() == "file title":
        return "quest_book_title"

    key_parts = _segments(normalized_key)
    context_parts = _segments(context.strip().lower(), combine_phrases=True)
    object_type, field = _object_and_field(key_parts)
    context_object, context_field = _object_and_field(context_parts)
    object_type = object_type or context_object
    field = field or context_field

    if object_type == "quest_book" and field == "title":
        return "quest_book_title"
    if object_type == "chapter_group" and field == "title":
        return "chapter_group_title"
    if object_type == "chapter":
        if field in {"subtitle", "chapter_subtitle"}:
            return "chapter_subtitle"
        if field == "title":
            return "chapter_title"
        return "other"
    if object_type == "quest":
        if field in {"description", "desc", "quest_desc"}:
            return "quest_description"
        if field in {"subtitle", "quest_subtitle"}:
            return "quest_subtitle"
        if field == "title":
            return "quest_title"
        return "other"
    if object_type == "task" and field == "title":
        return "task_title"
    if object_type == "reward" and field == "title":
        return "reward_title"
    if object_type == "reward_table" and field == "title":
        return "reward_table_title"
    if object_type == "quest_link" and field == "title":
        return "quest_link_title"
    if object_type == "image" and field == "hover":
        return "image_hover"
    return "other"


def _segments(value: str, combine_phrases: bool = False) -> list[str]:
    normalized = value.replace("-", "_")
    if combine_phrases:
        normalized = re.sub(r"\bchapter[ _]+group\b", "chapter_group", normalized)
        normalized = re.sub(r"\breward[ _]+table\b", "reward_table", normalized)
        normalized = re.sub(r"\bquest[ _]+link\b", "quest_link", normalized)
        normalized = re.sub(r"\bchapter[ _]+image\b", "chapter_image", normalized)
        normalized = re.sub(r"\bchapter[ _]+subtitle\b", "chapter_subtitle", normalized)
        normalized = re.sub(r"\bquest[ _]+subtitle\b", "quest_subtitle", normalized)
        normalized = re.sub(r"\bquest[ _]+desc(?:ription)?\b", "quest_desc", normalized)
    return [segment for segment in _SEGMENT_SPLIT.split(normalized) if segment]


def _object_and_field(segments: list[str]) -> tuple[str, str]:
    field_index = next(
        (index for index in range(len(segments) - 1, -1, -1) if segments[index] in _FIELD_ALIASES),
        None,
    )
    if field_index is None:
        return "", ""
    field = _FIELD_ALIASES[segments[field_index]]
    object_type = next(
        (
            _OBJECT_ALIASES[segments[index]]
            for index in range(field_index - 1, -1, -1)
            if segments[index] in _OBJECT_ALIASES
        ),
        "",
    )
    return object_type, field
