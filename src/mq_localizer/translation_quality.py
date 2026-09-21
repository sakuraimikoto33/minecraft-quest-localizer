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
    "or append unused nouns after the final punctuation. Fixed layout boundaries still apply. "
    "In Japanese, if the English sentence begins with a styled term or starts "
    "with 'With', 'Using', 'The', 'A', 'To', 'While', 'They', 'Once', or "
    "'In order to' followed by a styled term, put "
    "the translated term before its particle: 'Using [Time Crystals]' becomes "
    "'[タイムクリスタル]を使うと', and 'With access to [Celestigems]' becomes "
    "'[セレスティジェム]にアクセスできる'. For example, 'While intimidating, "
    "[Modern Industrialization]' becomes '[Modern Industrialization]は威圧的に見えます', "
    "and 'Once you've obtained [Sulfuric Crude Oil]' becomes "
    "'[硫酸性原油]を入手したら'. Do not leave an opening 'を', 'に', 'は', "
    "'の', or similar particle before the term and append that term at the end."
)
JAPANESE_WORD_ORDER_RETRY_INSTRUCTIONS = (
    "JAPANESE WORD-ORDER RETRY: The previous output put a Japanese leading particle "
    "before the first styled semantic token. For this failed item, put the first "
    "styled token before that particle: the first styled token is normally `token_0000`, "
    "so return token_positions {`token_0000`: 0} and set fragment_0000 to an empty "
    "string when the token starts the Japanese sentence. Put the translated connective "
    "(for example 'の力を使えば', 'を使うと', "
    "or 'にアクセスできる') in the fragment after that token. For an "
    "introductory clause, use the same order: '[Modern Industrialization]は威圧的に見えます', "
    "'[安山岩の外装]は装飾用として機能します', '[硫酸性原油]を入手したら', or "
    "'[FTBピラミッド]を完了するには'. Never put that connective in fragment_0000 "
    "and append the styled token at the end."
)
IMAGE_TITLE_RETRY_INSTRUCTIONS = (
    "IMAGE TITLE RETRY: This item is a short image label, not a sentence. Return only "
    "the translated noun phrase corresponding to the source title. Do not append a "
    "predicate, installation instruction, explanation, or sentence-ending expression "
    "such as 'です' or 'を設置します'."
)


_FORMAT = re.compile(r"[&§][0-9a-fk-orz]", re.IGNORECASE)
_TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")
_SENTENCE_END = frozenset("。！？!?")
_JAPANESE_LEADING_PARTICLE = re.compile(
    r"^(?:を|に|で|が|は|と|へ|から|より|まで|の|も)"
)
_CAPITALIZED_TERM = re.compile(
    r"(?<![A-Za-z])(?:[A-Z][A-Za-z0-9]*(?:['’][A-Za-z]+)?"
    r")(?:\s+(?:[A-Z][A-Za-z0-9]*(?:['’][A-Za-z]+)?)){1,}"
)
_CAPITALIZED_NAME = re.compile(
    r"(?<![A-Za-z])(?:[A-Z][A-Za-z0-9]*(?:['’]s)?"
    r")(?:\s+(?:[A-Z][A-Za-z0-9]*(?:['’]s)?))*"
)
_COMMON_CAPITALIZED_WORDS = frozenset({
    "A", "An", "And", "Before", "Because", "But", "Consuming", "Create",
    "Double", "Find", "For", "If", "In", "Inside", "It", "Just", "Most",
    "More", "Once", "One", "Placing", "Seeds", "Since", "Stronger", "That",
    "The", "They", "To", "Using", "While", "When", "You",
})
_ENGLISH_TERM_PRECEDING_WORDS = frozenset({
    "a", "an", "the", "by", "create", "craft", "build", "use", "using",
    "summon", "make", "inscribe", "upgrade", "of", "to", "with", "have",
    "holding", "distilling", "combining", "making", "progressing",
})


def _formatting_group_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return closed formatting groups in source-text coordinates."""
    protected = TokenProtector().protect(text)
    source_spans = dict(
        zip(protected.special_placeholders, protected.special_source_spans, strict=True)
    )
    spans: list[tuple[int, int]] = []
    for segment in protected.formatting_segments:
        for group in segment.movable_groups:
            first = source_spans.get(group.placeholders[0])
            last = source_spans.get(group.placeholders[-1])
            if first is not None and last is not None:
                spans.append((first[0], last[1]))
    return tuple(sorted(spans))


def _strip_formatting(text: str) -> str:
    return _FORMAT.sub("", text)


def _source_term_has_direct_leading_slot(prefix: str) -> bool:
    """Return whether the source prefix leaves the term before a particle.

    A leading Japanese particle is not by itself evidence of a misplaced term:
    ``To progress into more late-game [Create]`` can naturally become
    ``より終盤の[Create]へ``.  The check is limited to source prefixes ending
    in an article, preposition, or verb that directly introduces the styled
    term (``Using the power of [Term]``, ``The [Term]``, and similar forms).
    """
    normalized = prefix.strip().lower()
    if not normalized:
        return False
    # Introductory clauses can end in a descriptive word rather than an
    # article/preposition, but the following styled term is still the clause's
    # subject or object.  These forms are especially easy for a model to
    # detach and append after the Japanese sentence (for example, ``While
    # intimidating, [Modern Industrialization] ...`` or ``They ... to
    # [Andesite Casings]``).
    if normalized in {
        "while", "once", "consuming", "placing", "unlike", "for", "to get",
        "inside of the", "just like the", "seeds harvested by the",
        "most components within", "double byproduct amount in",
        "for those heading down the path of",
    }:
        return True
    if normalized.startswith((
        "while ", "they ", "once ", "after ", "before ", "when ",
        "although ", "if ", "since ", "seeds harvested by the ",
        "most components within ", "inside of the ", "just like the ",
        "double byproduct amount in ", "to use ", "to get ",
        "for those heading down the path of ",
    )):
        return True
        return True
    # ``In order to complete the [Term]`` introduces the term directly, while
    # ``In order to build ... you will need [Term]`` does not.  Require the
    # introductory clause to end in an article so the latter remains valid.
    if normalized.startswith("in order to ") and re.search(r"\b(?:the|a|an)\s*$", normalized):
        return True
    if normalized.endswith(" is the"):
        return True
    if normalized.startswith("it is recommended to use it with ") and re.search(
        r"\b(?:the|a|an)\s*$", normalized,
    ):
        return True
    if normalized.startswith("you'll want") and normalized.endswith(" with"):
        return True
    match = re.search(r"([a-z]+)\s*$", normalized)
    if match is None or match.group(1) not in _ENGLISH_TERM_PRECEDING_WORDS:
        return False
    if normalized in {"the", "a", "an", "using", "by", "with", "to", "with access to"}:
        return True
    # These are source constructions in which the first styled term is the
    # object of the trailing function word.  Do not treat every English
    # sentence ending in an article as a misplaced-term case: for example,
    # ``Find a [Term]`` is also used by the older terminal-label check.
    return normalized.startswith((
        "with ", "using ", "by ", "to ", "when ", "combine ",
        "now that ", "once ", "the ", "you can find ", "there are ",
        "it has ", "the higher the ",
    )) or normalized.endswith(" harvested by")


def _candidate_starts_with_particle(prefix: str) -> bool:
    normalized = prefix.strip()
    if not _JAPANESE_LEADING_PARTICLE.match(normalized):
        return False
    # ``より高度な`` / ``より終盤の`` are comparative modifiers, not a
    # detached case particle.  Keep ``よりも`` (the comparison particle)
    # eligible for the actual misplaced-term check.
    if normalized.startswith("より") and not normalized.startswith("よりも"):
        return False
    # These are ordinary Japanese words, not a case particle followed by a
    # missing protected noun.
    return not normalized.startswith((
        "もちろん", "もっと", "もはや", "もし", "はじめ", "はっ", "でき", "とても", "とき", "のち",
        "はい", "いいえ",
    ))


def _plain_term_word_order_issue(source: str, candidate: str) -> bool:
    """Detect an unstyled multi-word name detached after a Japanese clause."""
    if not _CAPITALIZED_TERM.search(source):
        return False
    visible_candidate = _strip_formatting(candidate).strip()
    if not _candidate_starts_with_particle(visible_candidate):
        return False
    # ``にHoney Treatを`` leaves an English name immediately after the
    # Japanese particle instead of putting the name before it.
    if re.match(
        r"^(?:を|に|で|が|は|と|へ|から|より|まで|の|も)"
        r"(?=[A-Z][A-Za-z0-9]*(?:['’][A-Za-z]+)?(?:\s+[A-Z][A-Za-z0-9]+)+)",
        visible_candidate,
    ):
        return True
    # ``を消費して発電します。ドラゴンブレス。`` and
    # ``は、...！Advanced Beehives`` leave the unstyled name as a detached
    # final sentence.
    segments = re.split(r"[。！？!?]", visible_candidate)
    if len(segments) < 2:
        return False
    if segments[-1].strip():
        return bool(segments[-2].strip())
    return len(segments) >= 3 and bool(segments[-2].strip())


def _detached_unstyled_name_issue(source: str, candidate: str) -> bool:
    """Detect an unstyled proper name retained after a leading particle.

    Some quest prose leaves a mod name unformatted (for example
    ``Mekanism's Lasers`` or ``Teleportation Cores``) while the later object is
    styled.  The model can then shift the unstyled name into the Japanese
    clause and append the styled object at the end.  Compare only names that
    occur before the first source style group and are still verbatim in the
    candidate, so a translated noun or an ordinary English sentence word is
    not enough to trigger the check.
    """
    source_groups = _formatting_group_spans(source)
    candidate_groups = _formatting_group_spans(candidate)
    if not source_groups or not candidate_groups:
        return False
    candidate_prefix = _strip_formatting(candidate[: candidate_groups[0][0]]).strip()
    if not _candidate_starts_with_particle(candidate_prefix):
        return False
    source_prefix = _strip_formatting(source[: source_groups[0][0]])
    names: list[str] = []
    for match in _CAPITALIZED_NAME.finditer(source_prefix):
        value = match.group(0).strip()
        if not value or value in _COMMON_CAPITALIZED_WORDS:
            continue
        if re.sub(r"['’]s$", "", value) in _COMMON_CAPITALIZED_WORDS:
            continue
        names.append(value)
    if not names:
        return False
    visible_candidate = _strip_formatting(candidate)
    for name in names:
        base = re.sub(r"['’]s$", "", name)
        if re.search(rf"(?<![A-Za-z]){re.escape(base)}(?![A-Za-z])", visible_candidate):
            return True
    return False


def image_title_translation_issue(
    key: str, source: str, candidate: str, target_locale: str,
) -> str | None:
    """Reject predicates appended to noun-phrase image titles."""
    if (
        not is_japanese_locale(target_locale)
        or not re.fullmatch(r"image\.[^.]+\.title", key.strip(), re.IGNORECASE)
    ):
        return None
    visible_source = _strip_formatting(source).strip()
    if not re.fullmatch(
        r"(?:[A-Z][A-Za-z0-9]*(?:['’][A-Za-z]+)?)(?:\s+[A-Z][A-Za-z0-9]*(?:['’][A-Za-z]+)?)+",
        visible_source,
    ):
        return None
    visible_candidate = _strip_formatting(candidate).strip()
    if visible_candidate == visible_source:
        return None
    if visible_candidate.startswith(visible_source) and len(visible_candidate) > len(visible_source):
        return (
            "画像タイトルに説明文が追加されています。原文の短いラベルだけを翻訳し、"
            "設置方法や断定表現を付け加えずに再翻訳してください"
        )
    if re.search(r"(?:です|だ|ます|する|を設置|を作成|を使用)[。！？!?]*$", visible_candidate):
        return (
            "画像タイトルに説明文が追加されています。原文の短いラベルだけを翻訳し、"
            "設置方法や断定表現を付け加えずに再翻訳してください"
        )
    return None


def japanese_word_order_issue(
    source: str, candidate: str, source_locale: str, target_locale: str,
) -> str | None:
    """Detect a dangling object particle plus a styled noun after the sentence.

    The conjunction is deliberately narrow: a Japanese leading particle before
    the first styled term when English introduces that term at the start, or
    ``を...！&6Name&r`` for English prose. A normal sentence ending in a name,
    quoted particles, names at the beginning, other locales, and multiline text
    are not rejected. The caller must also skip independent JSON/styled
    fragments with external context.
    """
    if (not is_japanese_locale(target_locale)
            or source_locale.strip().lower().replace("-", "_").split("_")[0] != "en"):
        return None
    source_groups = _formatting_group_spans(source)
    candidate_groups = _formatting_group_spans(candidate)
    if _detached_unstyled_name_issue(source, candidate):
        return (
            "日本語の語順が崩れています。装飾のない固有名詞が助詞の後ろまたは"
            "文末へ移動しています。固有名詞を対応する助詞の前へ置き、本文全体として再翻訳してください"
        )
    if not candidate_groups and _plain_term_word_order_issue(source, candidate):
        return (
            "日本語の語順が崩れています。装飾のない固有名詞が助詞の後ろまたは"
            "文末へ移動しています。固有名詞を対応する助詞の前へ置き、本文全体として再翻訳してください"
        )
    has_line_break = re.search(r"(?:\r\n|\r|\n|\\r\\n|\\n|\\r)", source + candidate)
    if source_groups and candidate_groups and has_line_break is None:
        candidate_prefix = _strip_formatting(candidate[: candidate_groups[0][0]]).strip()
        source_prefix = _strip_formatting(source[: source_groups[0][0]]).strip()
        source_starts_with_styled_term = not source_prefix
        source_term_has_direct_slot = _source_term_has_direct_leading_slot(source_prefix)
        candidate_is_only_particle = len(candidate_prefix) <= 3
        # English starts by introducing a styled noun ("With ...", "The ...",
        # "Using ..."), while Japanese starts with the particle that should
        # follow that noun. This is the signature of every reported output;
        # normal Japanese sentences beginning with a noun or a verb are not
        # affected because they do not begin with a case particle.
        if (
            candidate_prefix
            and _candidate_starts_with_particle(candidate_prefix)
            and (source_term_has_direct_slot or source_starts_with_styled_term or candidate_is_only_particle)
            and (source_prefix or source_starts_with_styled_term)
            and candidate_groups[0][0] > 0
        ):
            return (
                "日本語の語順が崩れています。原文の冒頭側で導入された装飾付きの固有名詞が"
                "助詞の後ろへ移動しています。固有名詞を助詞の前へ置き、本文全体を一文として再翻訳してください"
            )
    # The older check is intentionally narrower: it handles a dangling
    # ``を`` followed by a styled noun after sentence punctuation.  Keep it
    # separate from the general leading-particle check above so ordinary
    # Japanese prose that begins with another particle is not rejected solely
    # because it has a styled label later in the sentence.
    if not candidate.lstrip().startswith("を"):
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
