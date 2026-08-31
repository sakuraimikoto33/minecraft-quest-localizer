from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable


JAPANESE_UNICODE_INSTRUCTIONS = (
    "JAPANESE OUTPUT SAFETY: Do not introduce invisible Unicode format/control "
    "characters that are absent from the source (for example zero-width spaces). "
    "Do not introduce letters from an unrelated writing system that is absent from "
    "the source; Japanese scripts, CJK ideographs, ordinary Latin text, and Greek "
    "technical symbols are allowed. Preserve source characters when they are intentional."
)

_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_UNSAFE_GENERATED_CATEGORIES = frozenset({"Cn", "Co", "Cs"})
_JAPANESE_OR_COMMON_LETTER_MARKERS = (
    "LATIN",
    "GREEK",
    "CJK",
    "HIRAGANA",
    "KATAKANA",
    "KANA",
    "IDEOGRAPH",
)


def is_japanese_locale(locale: str) -> bool:
    normalized = locale.strip().casefold().replace("-", "_")
    return normalized == "ja" or normalized.startswith("ja_")


def translation_unicode_issue(
    source: str,
    candidate: str,
    target_locale: str,
    *,
    trusted_source_ranges: Iterable[tuple[int, int]] = (),
    trusted_candidate_ranges: Iterable[tuple[int, int]] = (),
) -> str | None:
    """Describe unsafe Unicode newly generated for a Japanese translation.

    The check deliberately does not apply to other target locales.  Scripts and
    format characters can be linguistically required elsewhere (for example a
    ZWJ in an Indic-script word or an emoji sequence).

    Trusted ranges let callers exclude immutable Minecraft syntax and official
    glossary terms.  A protected API request already represents those values as
    ASCII placeholders; existing translations pass their exact matched ranges.
    """

    if not is_japanese_locale(target_locale):
        return None

    source_characters = tuple(
        _untrusted_characters(source, trusted_source_ranges)
    )
    candidate_characters = tuple(
        _untrusted_characters(candidate, trusted_candidate_ranges)
    )

    source_invisible = Counter(
        character
        for character in source_characters
        if unicodedata.category(character) in _INVISIBLE_CATEGORIES
    )
    candidate_invisible = Counter(
        character
        for character in candidate_characters
        if unicodedata.category(character) in _INVISIBLE_CATEGORIES
    )
    introduced_invisible = candidate_invisible - source_invisible
    if introduced_invisible:
        details = ", ".join(
            _codepoint_description(character)
            for character in sorted(introduced_invisible)
        )
        return (
            "日本語訳に原文または公式用語由来でない不可視のUnicode文字が"
            f"含まれています: {details}"
        )

    source_unsafe = Counter(
        character
        for character in source_characters
        if unicodedata.category(character) in _UNSAFE_GENERATED_CATEGORIES
        or character == "\N{REPLACEMENT CHARACTER}"
    )
    candidate_unsafe = Counter(
        character
        for character in candidate_characters
        if unicodedata.category(character) in _UNSAFE_GENERATED_CATEGORIES
        or character == "\N{REPLACEMENT CHARACTER}"
    )
    introduced_unsafe = candidate_unsafe - source_unsafe
    if introduced_unsafe:
        details = ", ".join(
            _codepoint_description(character)
            for character in sorted(introduced_unsafe)
        )
        return (
            "日本語訳に原文または公式用語由来でない"
            f"未定義・私用または置換Unicode文字が含まれています: {details}"
        )

    source_foreign_families = {
        family
        for character in source_characters
        if (family := _foreign_letter_family(character)) is not None
    }
    for character in candidate_characters:
        family = _foreign_letter_family(character)
        if family is not None and family not in source_foreign_families:
            return (
                "日本語訳に原文または公式用語由来でない異種文字が"
                f"含まれています: {family} "
                f"({_codepoint_description(character)})"
            )
    return None


def _untrusted_characters(
    text: str,
    trusted_ranges: Iterable[tuple[int, int]],
) -> Iterable[str]:
    ranges = _merged_ranges(len(text), trusted_ranges)
    range_index = 0
    for index, character in enumerate(text):
        while range_index < len(ranges) and index >= ranges[range_index][1]:
            range_index += 1
        if (
            range_index < len(ranges)
            and ranges[range_index][0] <= index < ranges[range_index][1]
        ):
            continue
        yield character


def _merged_ranges(
    text_length: int,
    ranges: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    normalized = sorted(
        (max(0, start), min(text_length, end))
        for start, end in ranges
        if isinstance(start, int)
        and isinstance(end, int)
        and start < end
        and end > 0
        and start < text_length
    )
    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _foreign_letter_family(character: str) -> str | None:
    if not unicodedata.category(character).startswith("L"):
        return None
    name = unicodedata.name(character, "")
    if not name:
        # Do not reject code points added after the Python Unicode database.
        # This is important for newer CJK extensions.
        return None
    if any(marker in name for marker in _JAPANESE_OR_COMMON_LETTER_MARKERS):
        return None
    return name.split(" ", 1)[0].title()


def _codepoint_description(character: str) -> str:
    width = max(4, len(f"{ord(character):X}"))
    name = unicodedata.name(character, "UNNAMED CHARACTER")
    return f"U+{ord(character):0{width}X} {name}"
