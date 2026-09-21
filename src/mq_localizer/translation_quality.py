"""Conservative checks for identifiable translation-quality failures.

These checks do not repair word order or attempt to certify all Japanese
grammar. In particular, intentional text fragments must not be mistaken for
complete sentences.
"""
from __future__ import annotations

import re

from .protection import TokenProtector
from .unicode_safety import is_japanese_locale


JAPANESE_WORD_ORDER_INSTRUCTIONS = (
    "JAPANESE WORD ORDER: For example, for 'Find a [token_0000]!', use "
    "fragment_0000='', token_positions={token_0000:0}, "
    "fragment_0001='を見つけましょう！'. Putting 'を見つけましょう！' in fragment_0000 "
    "and leaving fragment_0001 empty would wrongly append the noun after the sentence. "
    "With multiple semantic tokens, place the correct noun before each associated particle "
    "and keep each action attached to its original object. Read the assembled sentence with "
    "the reference names to verify meaning and grammar. Never shift all prose slots by one "
    "or append unused nouns after the final punctuation. Fixed layout boundaries still apply."
)


_FORMAT = re.compile(r"[&§][0-9a-fk-orz]", re.IGNORECASE)
_TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")
_SENTENCE_END = frozenset("。！？!?")


def japanese_word_order_issue(
    source: str, candidate: str, source_locale: str, target_locale: str,
) -> str | None:
    """Detect a dangling object particle plus a styled noun after the sentence.

    The conjunction is deliberately narrow: ``を...！&6Name&r`` for English
    prose. A normal sentence ending in a name, quoted particles, names at the
    beginning, other locales, and multiline text are not rejected. The caller
    must also skip independent JSON/styled fragments with external context.
    """
    if (not is_japanese_locale(target_locale)
            or source_locale.strip().lower().replace("-", "_").split("_")[0] != "en"
            or not candidate.lstrip().startswith("を")):
        return None
    visible_source = _FORMAT.sub("", source).lstrip()
    if not visible_source or not visible_source[0].isascii() or not visible_source[0].isalpha():
        return None
    protected = TokenProtector().protect(candidate.rstrip())
    if protected.structural_placeholders or len(protected.formatting_segments) != 1:
        return None
    for group in protected.formatting_segments[0].movable_groups:
        first, last = group.placeholders[0], group.placeholders[-1]
        text = protected.protected
        if not text.endswith(last):
            continue
        start = text.index(first)
        prefix = text[:start].rstrip()
        body = _TOKEN.sub("", text[start:])
        if (prefix and prefix[-1] in _SENTENCE_END
                and any(character.isalpha() for character in body)
                and not any(character in _SENTENCE_END for character in body)):
            return (
                "日本語の語順が崩れています。文頭の「を」に対応する装飾付きの語句が"
                "文末の句点・感嘆符の後に置かれています。語句と本文を一文として再翻訳してください"
            )
    return None
