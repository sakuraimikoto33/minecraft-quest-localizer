from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from collections import Counter
from dataclasses import dataclass

from .domain import TranslationError


_NEWLINE_OR_TAB_PATTERN = r"(?:\r\n|\r|\n|\t|\\(?:r\\n|n|r|t))"
_ESCAPED_AMPERSAND_PATTERN = r"(?:\\&)"
_FORMAT_CODE_PATTERN = (
    r"(?:§x(?:§[0-9A-Fa-f]){6}|&x(?:&[0-9A-Fa-f]){6})"
    r"|(?:[§&]#[0-9A-Fa-f]{6})"
    r"|(?:[§&][0-9A-FK-ORZa-fk-orz])"
)
_ASCII_WORD_CLASS = r"A-Za-z0-9_"
# RFC 3986 URIs are ASCII.  Keeping the matcher to URI characters prevents a
# URL from swallowing adjacent CJK prose, Minecraft formatting, or a template.
# ASCII punctuation is valid inside a URI and is therefore conservatively kept
# when directly adjacent; target-language CJK punctuation remains ordinary text.
_URL_PATTERN_TEXT = r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
_HEX_ID_PATTERN_TEXT = (
    rf"(?<![{_ASCII_WORD_CLASS}])[0-9A-F]{{16}}(?:/\d+)?"
    rf"(?![{_ASCII_WORD_CLASS}])"
)
_SLASH_PATH_PATTERN_TEXT = (
    rf"(?<![{_ASCII_WORD_CLASS}])/(?=[A-Za-z0-9_.:-]*[A-Za-z_.:-])"
    r"[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*"
)
_RESOURCE_ID_PATTERN_TEXT = (
    rf"(?<![{_ASCII_WORD_CLASS}])[a-z0-9_.-]+:[a-z0-9_./-]+"
    rf"(?![{_ASCII_WORD_CLASS}])"
)
# A literal space is technically a Formatter flag, but accepting it here
# masks the ``% c`` in ordinary prose such as ``50% complete``.  Minecraft
# language placeholders use compact forms such as ``%s`` and ``%1$s``.
_PRINTF_PATTERN_TEXT = (
    r"%(?:\d+\$)?[-#+0,(<]*\d*(?:\.\d+)?(?:[tT][A-Za-z]|[A-Za-z%])"
)
_TEMPLATE_PATTERN_TEXT = (
    r"(?:\{@[^{}]+\})"
    r"|(?:\{(?:image|link|quest|chapter|task|item|icon|command):[^{}]*\})"
    r"|(?:\$\{[^{}]+\})"
    r"|(?:\{\{[^{}]+\}\})"
    r"|(?:\{(?:\d+|[A-Za-z_][A-Za-z0-9_.:-]*)\})"
)

# Keep grammars separate.  A single alternation loses overlapping candidates;
# for example ``ABCDEF0123456789:x`` can be both a 16-character ID prefix and
# one longer resource ID.  The longest source candidate must be protected, and
# provenance validation must still be able to see every overlapping grammar.
_SPECIAL_COMPONENT_PATTERN_TEXTS = (
    _NEWLINE_OR_TAB_PATTERN,
    _ESCAPED_AMPERSAND_PATTERN,
    _FORMAT_CODE_PATTERN,
    _PRINTF_PATTERN_TEXT,
    _TEMPLATE_PATTERN_TEXT,
    _URL_PATTERN_TEXT,
    _HEX_ID_PATTERN_TEXT,
    _SLASH_PATH_PATTERN_TEXT,
    _RESOURCE_ID_PATTERN_TEXT,
)
_SPECIAL_COMPONENT_PATTERNS = tuple(
    re.compile(rf"(?:{pattern})", re.IGNORECASE)
    for pattern in _SPECIAL_COMPONENT_PATTERN_TEXTS
)
_SPECIAL_COMPONENT_OVERLAP_PATTERNS = tuple(
    re.compile(rf"(?=({pattern}))", re.IGNORECASE)
    for pattern in _SPECIAL_COMPONENT_PATTERN_TEXTS
)
_BOUNDARY_RELAXED_PATTERN_TEXTS = (
    r"[0-9A-F]{16}(?:/\d+)?",
    r"/(?=[A-Za-z0-9_.:-]*[A-Za-z_.:-])[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*",
    r"[a-z0-9_.-]+:[a-z0-9_./-]+",
)
_BOUNDARY_RELAXED_PATTERNS = tuple(
    re.compile(rf"(?:{pattern})", re.IGNORECASE)
    for pattern in _BOUNDARY_RELAXED_PATTERN_TEXTS
)
_URL_PATTERN = re.compile(rf"(?:{_URL_PATTERN_TEXT})\Z", re.IGNORECASE)
_HEX_ID_PATTERN = re.compile(rf"(?:{_HEX_ID_PATTERN_TEXT})\Z", re.IGNORECASE)
_SLASH_PATH_PATTERN = re.compile(rf"(?:{_SLASH_PATH_PATTERN_TEXT})\Z", re.IGNORECASE)
_RESOURCE_ID_PATTERN = re.compile(rf"(?:{_RESOURCE_ID_PATTERN_TEXT})\Z", re.IGNORECASE)
_PRINTF_PATTERN = re.compile(rf"(?:{_PRINTF_PATTERN_TEXT})\Z", re.IGNORECASE)

_LAYOUT_PATTERN = re.compile(
    rf"(?:{_NEWLINE_OR_TAB_PATTERN}|{_ESCAPED_AMPERSAND_PATTERN}|{_FORMAT_CODE_PATTERN})\Z",
    re.IGNORECASE,
)

_PLACEHOLDER_RE = re.compile(r"__MQP_[0-9A-F]{4}__")
_RAW_JSON_TEXT_PREFIX = re.compile(r'^(?:\{\s*"|\[\s*(?:"|\{|\[))')
_FORMAT_CODE_AT_END = re.compile(
    r"(?:§x(?:§[0-9A-F]){6}|&x(?:&[0-9A-F]){6}|[§&]#[0-9A-F]{6}|[§&][0-9A-FK-ORZ])$",
    re.IGNORECASE,
)
_FORMAT_CODE = re.compile(_FORMAT_CODE_PATTERN, re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TermReplacement:
    """One occurrence-specific terminology replacement in source offsets."""

    start: int
    end: int
    replacement: str


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    replacement: str
    kind: str


def _special_spans(text: str) -> tuple[_Span, ...]:
    """Return maximal non-overlapping syntax spans, including adjacent chains.

    A single regex ``finditer`` cannot see ``minecraft:stone`` in
    ``§aminecraft:stone``: after consuming ``§a``, the technical token's
    lookbehind still sees the formatting code's ASCII ``a``.  Mask each round
    of already recognized syntax with spaces and scan the remaining text again.
    Spaces preserve offsets and prevent matches from being joined across a
    removed token.
    """

    working = list(text)
    spans: list[_Span] = []
    while True:
        view = "".join(working)
        candidates = {
            match.span()
            for pattern in _SPECIAL_COMPONENT_PATTERNS
            for match in pattern.finditer(view)
        }
        if not candidates:
            break

        # Use leftmost-longest tokenization.  The left edge matters because the
        # ASCII payload of ``§a``/``%s`` must not become the namespace of one
        # longer resource candidate.  At the same left edge, longest protects a
        # whole URL/resource rather than an inner slash or hexadecimal prefix.
        accepted: list[tuple[int, int]] = []
        accepted_end = -1
        for start, end in sorted(
            candidates,
            key=lambda candidate: (
                candidate[0],
                -(candidate[1] - candidate[0]),
                candidate[1],
            ),
        ):
            if start < accepted_end:
                continue
            accepted.append((start, end))
            accepted_end = end

        for start, end in accepted:
            spans.append(_Span(start, end, text[start:end], "special"))
            working[start:end] = " " * (end - start)
    spans.sort(key=lambda span: span.start)
    return tuple(spans)


def _overlapping_special_candidates(text: str) -> tuple[tuple[int, int], ...]:
    """Return every grammar candidate, including candidates that overlap."""

    candidates = {
        match.span(1)
        for pattern in _SPECIAL_COMPONENT_OVERLAP_PATTERNS
        for match in pattern.finditer(text)
    }
    return tuple(sorted(candidates))


def _relaxed_boundary_candidates(text: str) -> tuple[tuple[int, int], ...]:
    """Return maximal technical candidates even when a neighbor hides a boundary.

    Non-overlapping maximal candidates are sufficient: a nested slash/resource
    cannot cross a boundary that its containing candidate does not also cross.
    Avoiding lookahead at every slash keeps ``/a/a/...`` inputs linear.
    """

    candidates = {
        match.span()
        for pattern in _BOUNDARY_RELAXED_PATTERNS
        for match in pattern.finditer(text)
    }
    return tuple(sorted(candidates))


def _without_special_syntax(text: str) -> str:
    """Replace all protected syntax with spaces while preserving offsets."""

    characters = list(text)
    for span in _special_spans(text):
        characters[span.start : span.end] = " " * (span.end - span.start)
    return "".join(characters)


@dataclass(frozen=True, slots=True)
class _RestoredSpecialSpan:
    placeholder: str
    start: int
    end: int
    value: str
    original_start: int
    original_end: int


@dataclass(frozen=True, slots=True)
class _RestoredTermSpan:
    placeholder: str
    start: int
    end: int
    value: str
    original_start: int
    original_end: int


@dataclass(frozen=True, slots=True)
class _RestoredTrustedSpan:
    placeholder: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _FormattingGroup:
    placeholders: tuple[str, ...]
    internal_segment_signatures: tuple[
        tuple[bool, tuple[tuple[str, int], ...]], ...
    ]


@dataclass(frozen=True, slots=True)
class _FormattingSegment:
    source_sequence: tuple[str, ...]
    movable_groups: tuple[_FormattingGroup, ...]
    strict_segment_signatures: tuple[
        tuple[bool, tuple[tuple[str, int], ...]], ...
    ]


@dataclass(frozen=True, slots=True)
class ProtectedText:
    original: str
    protected: str
    replacements: Mapping[str, str]
    special_values: tuple[str, ...]
    special_placeholders: tuple[str, ...]
    special_source_spans: tuple[tuple[int, int], ...]
    term_placeholders: tuple[str, ...]
    term_source_spans: tuple[tuple[int, int], ...]
    structural_placeholders: tuple[str, ...]
    layout_segment_signatures: tuple[
        tuple[bool, tuple[tuple[str, int], ...]], ...
    ]
    formatting_segments: tuple[_FormattingSegment, ...]

    @property
    def content_placeholders(self) -> tuple[str, ...]:
        """Placeholders that carry content rather than visual layout."""

        non_layout_special = tuple(
            placeholder
            for placeholder in self.special_placeholders
            if not _is_layout_token(self.replacements[placeholder])
        )
        return self.term_placeholders + non_layout_special

    def restore(self, translated: str) -> str:
        observed = _PLACEHOLDER_RE.findall(translated)
        expected = list(self.replacements)
        if Counter(observed) != Counter(expected):
            missing = sorted((Counter(expected) - Counter(observed)).elements())
            extra = sorted((Counter(observed) - Counter(expected)).elements())

            def described(placeholder: str) -> str:
                if placeholder in self.term_placeholders:
                    kind = "固有名詞"
                elif placeholder in self.structural_placeholders:
                    kind = "改行・タブ等の固定配置"
                elif placeholder in self.special_placeholders:
                    value = self.replacements.get(placeholder, "")
                    kind = "装飾" if _is_format_token(value) else "技術token"
                elif placeholder in self.replacements:
                    kind = "原文中のMQP形文字列"
                else:
                    kind = "未知token"
                return f"{placeholder} [{kind}]"

            detail = []
            if missing:
                detail.append(f"欠落: {', '.join(map(described, missing))}")
            if extra:
                detail.append(f"余分: {', '.join(map(described, extra))}")
            raise TranslationError(
                "保護プレースホルダーの種類・個数が一致しません（"
                + "; ".join(detail)
                + "）"
            )
        _validate_provider_segments(translated)
        fixed_layout = frozenset(self.structural_placeholders)
        observed_fixed_layout = tuple(
            token for token in observed if token in fixed_layout
        )
        if observed_fixed_layout != self.structural_placeholders:
            raise TranslationError(
                "改行、タブ、または文字として扱うエスケープ記号の位置・順序が変更されました"
            )
        translated_segments = _split_on_placeholders(
            translated,
            self.structural_placeholders,
        )
        if self.structural_placeholders:
            translated_signatures = tuple(
                _segment_signature(segment) for segment in translated_segments
            )
            if translated_signatures != self.layout_segment_signatures:
                raise TranslationError(
                    "改行やタブをまたいで翻訳本文または保護対象が移動しました"
                )
        _validate_formatting_segments(
            translated_segments,
            self.formatting_segments,
            self.replacements,
        )

        result_chunks: list[str] = []
        restored_term_spans: list[_RestoredTermSpan] = []
        restored_special_spans: list[_RestoredSpecialSpan] = []
        restored_trusted_spans: list[_RestoredTrustedSpan] = []
        term_placeholders = frozenset(self.term_placeholders)
        special_placeholders = frozenset(self.special_placeholders)
        if not (
            len(self.special_source_spans)
            == len(self.special_placeholders)
            == len(self.special_values)
        ):
            raise TranslationError("内部エラー: 原文の保護対象を再検証できません")
        if len(self.term_source_spans) != len(self.term_placeholders):
            raise TranslationError("内部エラー: 原文の固有名詞を再検証できません")
        original_special_by_placeholder = {
            placeholder: source_span
            for placeholder, source_span in zip(
                self.special_placeholders,
                self.special_source_spans,
                strict=True,
            )
        }
        original_term_by_placeholder = {
            placeholder: source_span
            for placeholder, source_span in zip(
                self.term_placeholders,
                self.term_source_spans,
                strict=True,
            )
        }
        result_length = 0
        cursor = 0
        for match in _PLACEHOLDER_RE.finditer(translated):
            prefix = translated[cursor:match.start()]
            result_chunks.append(prefix)
            result_length += len(prefix)
            placeholder = match.group(0)
            value = self.replacements[placeholder]
            result_chunks.append(value)
            restored_trusted_spans.append(
                _RestoredTrustedSpan(
                    placeholder=placeholder,
                    start=result_length,
                    end=result_length + len(value),
                )
            )
            if placeholder in special_placeholders:
                original_start, original_end = original_special_by_placeholder[placeholder]
                restored_special_spans.append(
                    _RestoredSpecialSpan(
                        placeholder=placeholder,
                        start=result_length,
                        end=result_length + len(value),
                        value=value,
                        original_start=original_start,
                        original_end=original_end,
                    )
                )
            if placeholder in term_placeholders:
                original_start, original_end = original_term_by_placeholder[placeholder]
                restored_term_spans.append(
                    _RestoredTermSpan(
                        placeholder=placeholder,
                        start=result_length,
                        end=result_length + len(value),
                        value=value,
                        original_start=original_start,
                        original_end=original_end,
                    )
                )
            result_length += len(value)
            cursor = match.end()
        result_chunks.append(translated[cursor:])
        result = "".join(result_chunks)
        if any(placeholder in result for placeholder in self.replacements):
            raise TranslationError("未復元の保護プレースホルダーが残っています")
        _validate_term_boundaries(self.original, result, restored_term_spans)
        baseline, baseline_trusted_spans = _restore_trusted_template(
            self.protected,
            self.replacements,
        )
        _validate_special_provenance(
            self.original,
            result,
            restored_special_spans,
            restored_trusted_spans,
            baseline,
            baseline_trusted_spans,
            special_placeholders,
        )
        return result


class TokenProtector:
    """Masks codes and glossary terms so a model cannot rewrite them."""

    def protect(
        self,
        text: str,
        terminology: Mapping[str, str] | None = None,
        term_spans: Iterable[TermReplacement] | None = None,
    ) -> ProtectedText:
        spans = list(_special_spans(text))
        occupied = [(span.start, span.end) for span in spans]
        # A glossary hit can legitimately occur inside syntax which is already
        # protected as one immutable token.  The common case is a Mod display
        # name matching the namespace of ``modid:path``.  Keep these source
        # spans separate from term occupancy so a fully-contained term can be
        # discarded safely, while partial intersections and term/term overlap
        # still fail closed below.
        immutable_occupied = list(occupied)
        for match in _PLACEHOLDER_RE.finditer(text):
            if not any(match.start() < end and match.end() > start for start, end in occupied):
                spans.append(_Span(match.start(), match.end(), match.group(0), "literal"))
                occupied.append((match.start(), match.end()))
                immutable_occupied.append((match.start(), match.end()))
        for term in sorted(
            term_spans or (),
            key=lambda item: (item.start, -item.end),
        ):
            if not (0 <= term.start < term.end <= len(text)):
                raise TranslationError("内部エラー: 固有名詞の原文位置が範囲外です")
            if any(
                term.start >= immutable_start and term.end <= immutable_end
                for immutable_start, immutable_end in immutable_occupied
            ):
                # The surrounding special/literal placeholder already restores
                # the exact source bytes, so a nested terminology placeholder
                # would add no protection and cannot apply a safe translation.
                continue
            if any(
                term.start < used_end and term.end > used_start
                for used_start, used_end in occupied
            ):
                raise TranslationError(
                    "内部エラー: 固有名詞の原文位置が装飾・プレースホルダー"
                    "または別の固有名詞と重複しています"
                )
            source = text[term.start : term.end]
            safe_target = (
                term.replacement
                if terminology_target_is_safe(source, term.replacement)
                else source
            )
            spans.append(_Span(term.start, term.end, safe_target, "term"))
            occupied.append((term.start, term.end))
        for source, target in sorted((terminology or {}).items(), key=lambda item: len(item[0]), reverse=True):
            if not source:
                continue
            # Treat a terminology provider as data, not as an authority to add
            # formatting/templates/URLs.  Normal mod translations have the same
            # empty syntax signature; incompatible targets safely keep source.
            safe_target = target if terminology_target_is_safe(source, target) else source
            start = 0
            while True:
                index = text.find(source, start)
                if index < 0:
                    break
                end = index + len(source)
                if _has_word_boundaries(text, index, end, source) and not any(
                    index < used_end and end > used_start for used_start, used_end in occupied
                ):
                    spans.append(_Span(index, end, safe_target, "term"))
                    occupied.append((index, end))
                start = index + max(1, len(source))

        # Literal internal-shaped placeholders and glossary terms are also
        # immutable.  Treat them as boundaries and rescan the remaining source
        # so ``Create/give`` and ``__MQP_0000__/kill`` protect the path too.
        masked_source = list(text)
        for used_start, used_end in occupied:
            masked_source[used_start:used_end] = " " * (used_end - used_start)
        for special in _special_spans("".join(masked_source)):
            spans.append(
                _Span(
                    special.start,
                    special.end,
                    text[special.start:special.end],
                    "special",
                )
            )
            occupied.append((special.start, special.end))

        spans.sort(key=lambda span: span.start)
        chunks: list[str] = []
        replacements: dict[str, str] = {}
        special_values: list[str] = []
        special_placeholders: list[str] = []
        special_source_spans: list[tuple[int, int]] = []
        term_placeholders: list[str] = []
        term_source_spans: list[tuple[int, int]] = []
        fixed_layout_placeholders: list[str] = []
        cursor = 0
        placeholder_index = 0

        def allocate_placeholder() -> str:
            nonlocal placeholder_index
            while placeholder_index <= 0xFFFF:
                placeholder = f"__MQP_{placeholder_index:04X}__"
                placeholder_index += 1
                if placeholder not in text and placeholder not in replacements:
                    return placeholder
            raise TranslationError("1つの文字列に含まれる保護tokenが多すぎます")

        for span in spans:
            if span.start < cursor:
                continue
            placeholder = allocate_placeholder()
            chunks.append(text[cursor:span.start])
            chunks.append(placeholder)
            replacements[placeholder] = span.replacement
            if span.kind == "special":
                special_values.append(span.replacement)
                special_placeholders.append(placeholder)
                special_source_spans.append((span.start, span.end))
                if _is_fixed_layout_token(span.replacement):
                    fixed_layout_placeholders.append(placeholder)
            elif span.kind == "term":
                term_placeholders.append(placeholder)
                term_source_spans.append((span.start, span.end))
            cursor = span.end
        chunks.append(text[cursor:])

        protected = "".join(chunks)
        structural_placeholders = tuple(fixed_layout_placeholders)
        protected_segments = _split_on_placeholders(
            protected,
            structural_placeholders,
        )
        layout_segment_signatures = tuple(
            _segment_signature(segment)
            for segment in protected_segments
        )
        formatting_segments = tuple(
            _build_formatting_segment(segment, replacements)
            for segment in protected_segments
        )

        return ProtectedText(
            text,
            protected,
            replacements,
            tuple(special_values),
            tuple(special_placeholders),
            tuple(special_source_spans),
            tuple(term_placeholders),
            tuple(term_source_spans),
            structural_placeholders,
            layout_segment_signatures,
            formatting_segments,
        )


def _has_word_boundaries(text: str, start: int, end: int, term: str) -> bool:
    def ascii_word(character: str) -> bool:
        return character.isascii() and (character.isalnum() or character == "_")

    before_ok = (
        not ascii_word(term[0])
        or
        start == 0
        or not ascii_word(text[start - 1])
        or _FORMAT_CODE_AT_END.search(text, 0, start) is not None
    )
    after_ok = not ascii_word(term[-1]) or end == len(text) or not ascii_word(text[end])
    return before_ok and after_ok


def terminology_target_is_safe(source: str, target: str) -> bool:
    """Accept a terminology target only when it remains visible and inert."""

    if not target:
        return False
    categories = tuple(unicodedata.category(character) for character in target)
    if any(category in {"Cc", "Cf", "Cs"} for category in categories):
        return False
    if not any(
        not character.isspace()
        and not category.startswith(("C", "M", "Z"))
        for character, category in zip(target, categories, strict=True)
    ):
        return False
    return protected_syntax_signature(source) == protected_syntax_signature(target)


def _validate_term_boundaries(
    original: str,
    restored: str,
    term_spans: list[_RestoredTermSpan],
) -> None:
    """Reject only ASCII attachments that were not present in the source.

    Formatting codes are transparent for this check so a provider cannot hide
    ``SuperCreate`` as ``Super§aCreate``.  The same transparency is applied to
    the original source: an unchanged ``A§aCreate`` must always round-trip.
    """

    if not term_spans:
        return

    def ascii_word(character: str) -> bool:
        return character.isascii() and (character.isalnum() or character == "_")

    def formatting_mask(text: str) -> bytearray:
        mask = bytearray(len(text))
        for match in _FORMAT_CODE.finditer(text):
            mask[match.start() : match.end()] = b"\x01" * (
                match.end() - match.start()
            )
        return mask

    def attachments(
        text: str,
        formatting: bytearray,
        start: int,
        end: int,
        value: str,
    ) -> tuple[bool, bool]:
        if not value:
            return False, False
        before = start - 1
        while before >= 0 and formatting[before]:
            before -= 1
        after = end
        while after < len(text) and formatting[after]:
            after += 1
        return (
            ascii_word(value[0])
            and before >= 0
            and ascii_word(text[before]),
            ascii_word(value[-1])
            and after < len(text)
            and ascii_word(text[after]),
        )

    original_formatting = formatting_mask(original)
    restored_formatting = formatting_mask(restored)
    for span in term_spans:
        source_value = original[span.original_start : span.original_end]
        original_left, original_right = attachments(
            original,
            original_formatting,
            span.original_start,
            span.original_end,
            source_value,
        )
        restored_left, restored_right = attachments(
            restored,
            restored_formatting,
            span.start,
            span.end,
            span.value,
        )
        if (restored_left and not original_left) or (
            restored_right and not original_right
        ):
            raise TranslationError("Mod名または公式用語のplaceholderに文字が連結されました")


_SPECIAL_PROVENANCE_FAILURE = (
    "原文にない装飾コード・改行・URL・ID・テンプレートが追加されたか、"
    "保護対象に英数字が連結されました"
)


def _validate_provider_segments(translated: str) -> None:
    """Reject complete protected syntax authored outside immutable placeholders.

    Splitting first is important for ``__MQP_0000__/kill``.  In the restored
    string a left-boundary lookbehind can be hidden by the source token, while
    the provider-authored segment clearly starts with a new command path.
    Cross-placeholder syntax is checked again after restoration.
    """

    if any(_special_spans(segment) for segment in _PLACEHOLDER_RE.split(translated)):
        raise TranslationError(_SPECIAL_PROVENANCE_FAILURE)


def _restore_trusted_template(
    template: str,
    replacements: Mapping[str, str],
) -> tuple[str, list[_RestoredTrustedSpan]]:
    """Restore one protected template and retain each immutable output interval."""

    chunks: list[str] = []
    spans: list[_RestoredTrustedSpan] = []
    cursor = 0
    result_length = 0
    for match in _PLACEHOLDER_RE.finditer(template):
        prefix = template[cursor : match.start()]
        chunks.append(prefix)
        result_length += len(prefix)
        placeholder = match.group(0)
        try:
            value = replacements[placeholder]
        except KeyError as exc:
            raise TranslationError(
                "内部エラー: 保護プレースホルダーの復元値がありません"
            ) from exc
        chunks.append(value)
        spans.append(
            _RestoredTrustedSpan(
                placeholder=placeholder,
                start=result_length,
                end=result_length + len(value),
            )
        )
        result_length += len(value)
        cursor = match.end()
    chunks.append(template[cursor:])
    return "".join(chunks), spans


def _validate_special_provenance(
    original: str,
    restored: str,
    spans: list[_RestoredSpecialSpan],
    trusted_spans: list[_RestoredTrustedSpan],
    baseline: str,
    baseline_trusted_spans: list[_RestoredTrustedSpan],
    special_placeholders: frozenset[str],
) -> None:
    """Accept syntax only when it is wholly inside one immutable placeholder.

    Placeholder equality already proves that each source token's bytes were
    restored exactly.  Source tokens can contain overlapping grammars (a URL
    can contain a slash path), so containment within a trusted span is the
    correct provenance rule.  Any candidate crossing a placeholder boundary
    necessarily uses provider-authored text or combines formerly separate
    immutable values and is rejected.

    Boundary-relaxed candidates expose syntax hidden behind an ASCII immutable
    value, such as ``Create`` + provider ``/give``.  A cross-boundary candidate
    is accepted only up to the count already present in the protected source
    baseline; this preserves valid source text such as ``Create/Create`` while
    rejecting a model-created equivalent.
    """

    for span in spans:
        if _has_new_ascii_attachment(original, restored, span):
            raise TranslationError(_SPECIAL_PROVENANCE_FAILURE)

    def candidate_sets(text: str) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
        strict = set(_overlapping_special_candidates(text))
        strict.update((span.start, span.end) for span in _special_spans(text))
        all_candidates = strict | set(_relaxed_boundary_candidates(text))
        return strict, all_candidates

    def interval_helpers(
        immutable: list[_RestoredTrustedSpan],
    ) -> tuple[
        tuple[int, ...],
        tuple[_RestoredTrustedSpan, ...],
        tuple[int, ...],
        tuple[_RestoredTrustedSpan, ...],
    ]:
        ordered = tuple(sorted(immutable, key=lambda item: (item.start, item.end)))
        ordered_starts = tuple(item.start for item in ordered)
        special = tuple(
            item for item in ordered if item.placeholder in special_placeholders
        )
        special_starts = tuple(item.start for item in special)
        return ordered_starts, ordered, special_starts, special

    def contained_or_shadowed(
        start: int,
        end: int,
        immutable_starts: tuple[int, ...],
        immutable: tuple[_RestoredTrustedSpan, ...],
        special_starts: tuple[int, ...],
        special: tuple[_RestoredTrustedSpan, ...],
    ) -> bool:
        index = bisect_right(immutable_starts, start) - 1
        if index >= 0:
            owner = immutable[index]
            if owner.start <= start and end <= owner.end:
                return True

        # A later grammar beginning inside an already-tokenized source special
        # is lexically shadowed by that token.  The ``a`` in ``§a`` therefore
        # cannot become the namespace of ``aminecraft:stone``.  A candidate at
        # the same start is still checked, catching ``HEX`` extended to ``HEX:x``.
        index = bisect_right(special_starts, start) - 1
        if index >= 0:
            owner = special[index]
            if owner.start < start < owner.end:
                return True
        return False

    def intersects(
        start: int,
        end: int,
        immutable_starts: tuple[int, ...],
        immutable: tuple[_RestoredTrustedSpan, ...],
    ) -> bool:
        index = bisect_right(immutable_starts, start)
        if index > 0 and immutable[index - 1].end > start:
            return True
        return index < len(immutable) and immutable[index].start < end

    baseline_intervals = interval_helpers(baseline_trusted_spans)
    baseline_strict, baseline_candidates = candidate_sets(baseline)
    allowed_cross_boundary: Counter[tuple[str, bool]] = Counter()
    for start, end in baseline_candidates:
        if contained_or_shadowed(start, end, *baseline_intervals):
            continue
        if (start, end) in baseline_strict or intersects(
            start,
            end,
            baseline_intervals[0],
            baseline_intervals[1],
        ):
            allowed_cross_boundary[
                (baseline[start:end], (start, end) in baseline_strict)
            ] += 1

    restored_intervals = interval_helpers(trusted_spans)
    restored_strict, restored_candidates = candidate_sets(restored)
    for start, end in sorted(restored_candidates):
        if contained_or_shadowed(start, end, *restored_intervals):
            continue
        touches_immutable = intersects(
            start,
            end,
            restored_intervals[0],
            restored_intervals[1],
        )
        if (start, end) not in restored_strict and not touches_immutable:
            # A boundary-relaxed match entirely in ordinary prose (``and/or``)
            # is not protected syntax. Complete provider syntax was checked
            # above with the strict scanner.
            continue
        value = restored[start:end]
        provenance_key = (value, (start, end) in restored_strict)
        if allowed_cross_boundary[provenance_key] > 0:
            allowed_cross_boundary[provenance_key] -= 1
            continue
        raise TranslationError(_SPECIAL_PROVENANCE_FAILURE)


def _has_new_ascii_attachment(
    original: str,
    restored: str,
    span: _RestoredSpecialSpan,
) -> bool:
    """Reject a new grammar character attached to boundary-sensitive syntax."""

    token_kind = next(
        (
            kind
            for kind, pattern in (
                ("url", _URL_PATTERN),
                ("hex", _HEX_ID_PATTERN),
                ("slash", _SLASH_PATH_PATTERN),
                ("resource", _RESOURCE_ID_PATTERN),
            )
            if pattern.fullmatch(span.value)
        ),
        "",
    )
    if not token_kind:
        return False

    def character_at(text: str, index: int) -> str:
        if index < 0 or index >= len(text):
            return ""
        return text[index]

    def extends(character: str, side: str) -> bool:
        ascii_word = character.isascii() and (
            character.isalnum() or character == "_"
        )
        if token_kind == "url":
            return bool(character) and character.isascii() and bool(
                re.fullmatch(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]", character)
            )
        if token_kind == "hex":
            return ascii_word or character == "/"
        if token_kind == "slash":
            return ascii_word or character == "/"
        if token_kind == "resource":
            return ascii_word or (side == "right" and character == ":")
        return False

    original_left = extends(character_at(original, span.original_start - 1), "left")
    original_right = extends(character_at(original, span.original_end), "right")
    restored_left = extends(character_at(restored, span.start - 1), "left")
    restored_right = extends(character_at(restored, span.end), "right")
    return (restored_left and not original_left) or (
        restored_right and not original_right
    )


def should_translate(text: str) -> bool:
    stripped = _PLACEHOLDER_RE.sub("", _without_special_syntax(text)).strip()
    return any(character.isalpha() for character in stripped)


def looks_like_raw_json_text(text: str) -> bool:
    """Recognize object/array JSON text components without treating ``[Label]`` as JSON."""

    return _RAW_JSON_TEXT_PREFIX.match(text.lstrip()) is not None


def special_tokens(text: str) -> tuple[str, ...]:
    """Return formatting/newline/template tokens in their exact order."""

    return tuple(span.replacement for span in _special_spans(text))


def protected_syntax_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return source ranges which terminology matching must not claim.

    Formatting codes are included even though a glossary matcher may bridge
    across them: its projected fragments sit on either side of the formatting
    range and therefore do not overlap it.  Technical tokens such as resource
    IDs and URLs, on the other hand, can contain a Mod display name as their
    namespace or path.  Exposing the exact ranges lets the glossary reject
    that false match before :class:`TokenProtector` receives conflicting
    occurrence spans.

    Literal ``__MQP_0000__``-shaped input is included for the same reason and
    mirrors :meth:`TokenProtector.protect` exactly.
    """

    ranges = [(span.start, span.end) for span in _special_spans(text)]
    for match in _PLACEHOLDER_RE.finditer(text):
        if not any(
            match.start() < used_end and match.end() > used_start
            for used_start, used_end in ranges
        ):
            ranges.append(match.span())
    ranges.sort()
    return tuple(ranges)


def protected_syntax_signature(text: str) -> tuple[tuple[str, str], ...]:
    """Return protected syntax and literal internal-shaped tokens in source order.

    The kind is ``"special"`` for formatting/newline/template/etc. syntax and
    ``"literal_placeholder"`` for an ``__MQP_0000__``-shaped string that was
    already present in the input.  Literal matches nested inside a larger
    special token are omitted, mirroring :meth:`TokenProtector.protect`.
    """

    matches: list[tuple[int, int, str, str]] = [
        (span.start, span.end, "special", span.replacement)
        for span in _special_spans(text)
    ]
    occupied = [(start, end) for start, end, _kind, _value in matches]
    for match in _PLACEHOLDER_RE.finditer(text):
        if not any(match.start() < end and match.end() > start for start, end in occupied):
            matches.append(
                (match.start(), match.end(), "literal_placeholder", match.group(0))
            )
    matches.sort(key=lambda item: item[0])
    return tuple((kind, value) for _start, _end, kind, value in matches)


def terminology_literal_skeleton(text: str) -> tuple[str, bool] | None:
    """Remove only printf display arguments from a possible fixed label.

    Some Mods prefix item/block language values with ``%s`` arguments used to
    render tier or material decoration.  Those arguments are not part of the
    registry object's stable display name.  Any other protected syntax keeps
    the value dynamic and therefore returns ``None``.  The caller must still
    prove that the remaining literal text identifies the language key before
    adding it to a global terminology catalog.
    """

    spans = _special_spans(text)
    occupied = tuple((span.start, span.end) for span in spans)
    if any(
        not any(match.start() >= start and match.end() <= end for start, end in occupied)
        for match in _PLACEHOLDER_RE.finditer(text)
    ):
        return None
    if any(
        _PRINTF_PATTERN.fullmatch(span.replacement) is None
        or span.replacement[-1].casefold() in {"n", "%"}
        for span in spans
    ):
        return None
    if not spans:
        return text, False
    characters = list(text)
    for span in spans:
        characters[span.start : span.end] = " " * (span.end - span.start)
    return "".join(characters), True


def protected_layout_signature(
    text: str,
    terminology: Mapping[str, str] | None = None,
    term_spans: Iterable[TermReplacement] | None = None,
) -> tuple[object, ...]:
    """Describe fixed lines and safely movable closed formatting groups.

    Newlines/tabs/escaped ampersands retain their exact segment.  A simple
    ``format...reset`` group may move as one unit inside that segment, which is
    required for natural Japanese word order.  Orphan resets, unclosed codes,
    and complex mid-body style changes retain strict source positions.
    """

    protected = TokenProtector().protect(text, terminology, term_spans)
    segments = _split_on_placeholders(
        protected.protected,
        protected.structural_placeholders,
    )
    special = frozenset(protected.special_placeholders)
    fixed_values = tuple(
        protected.replacements[placeholder]
        for placeholder in protected.structural_placeholders
    )
    segment_signatures = tuple(
        (
            _canonical_segment_signature(
                segment,
                protected.replacements,
                special,
            ),
            _canonical_formatting_signature(
                segment,
                formatting,
                protected.replacements,
                special,
            ),
        )
        for segment, formatting in zip(
            segments,
            protected.formatting_segments,
            strict=True,
        )
    )
    return fixed_values, segment_signatures


def layout_tokens(text: str) -> tuple[str, ...]:
    """Return formatting, escaped-literal, newline, and tab tokens."""

    return tuple(token for token in special_tokens(text) if _is_layout_token(token))


def _split_on_placeholders(text: str, placeholders: tuple[str, ...]) -> list[str]:
    segments: list[str] = []
    cursor = 0
    for placeholder in placeholders:
        position = text.find(placeholder, cursor)
        if position < 0:
            raise TranslationError("内部エラー: 保護した装飾・改行の位置を特定できません")
        segments.append(text[cursor:position])
        cursor = position + len(placeholder)
    segments.append(text[cursor:])
    return segments


def _formatting_placeholder_matches(
    segment: str,
    replacements: Mapping[str, str],
) -> list[tuple[str, int, int]]:
    matches: list[tuple[str, int, int]] = []
    for match in _PLACEHOLDER_RE.finditer(segment):
        placeholder = match.group(0)
        value = replacements.get(placeholder, "")
        if _is_format_token(value):
            matches.append((placeholder, match.start(), match.end()))
    return matches


def _build_formatting_segment(
    segment: str,
    replacements: Mapping[str, str],
) -> _FormattingSegment:
    matches = _formatting_placeholder_matches(segment, replacements)
    source_sequence = tuple(placeholder for placeholder, _start, _end in matches)
    if not matches:
        return _FormattingSegment((), (), ())

    movable_groups: list[_FormattingGroup] = []
    requires_strict_layout = False
    index = 0
    while index < len(matches):
        first = matches[index]
        if _is_reset_format_token(replacements[first[0]]):
            requires_strict_layout = True
            break

        group = [first]
        index += 1
        # Consecutive opening codes such as ``&l&5`` form one opening stack.
        # A later code after body text (``&5A &3B&r``) is a complex layout and
        # deliberately retains the old fixed-position validation.
        while index < len(matches):
            candidate = matches[index]
            if _is_reset_format_token(replacements[candidate[0]]):
                break
            if segment[group[-1][2] : candidate[1]]:
                requires_strict_layout = True
                break
            group.append(candidate)
            index += 1
        if requires_strict_layout:
            break
        if index >= len(matches):
            requires_strict_layout = True
            break
        reset = matches[index]
        if not _is_reset_format_token(replacements[reset[0]]):
            requires_strict_layout = True
            break
        group.append(reset)
        body_signature = _segment_signature(segment[group[-2][2] : reset[1]])
        if not body_signature[0] and not body_signature[1]:
            requires_strict_layout = True
            break
        movable_groups.append(
            _FormattingGroup(
                placeholders=tuple(part[0] for part in group),
                internal_segment_signatures=tuple(
                    _segment_signature(segment[left[2] : right[1]])
                    for left, right in zip(group, group[1:])
                ),
            )
        )
        index += 1
    if requires_strict_layout:
        return _FormattingSegment(
            source_sequence=source_sequence,
            movable_groups=(),
            strict_segment_signatures=tuple(
                _segment_signature(part)
                for part in _split_on_placeholders(segment, source_sequence)
            ),
        )
    return _FormattingSegment(
        source_sequence=source_sequence,
        movable_groups=tuple(movable_groups),
        strict_segment_signatures=(),
    )


def _validate_formatting_segments(
    translated_segments: list[str],
    expected_segments: tuple[_FormattingSegment, ...],
    replacements: Mapping[str, str],
) -> None:
    if len(translated_segments) != len(expected_segments):
        raise TranslationError("内部エラー: 装飾コードのsegmentを再検証できません")

    for translated, expected in zip(
        translated_segments,
        expected_segments,
        strict=True,
    ):
        matches = _formatting_placeholder_matches(translated, replacements)
        sequence = tuple(placeholder for placeholder, _start, _end in matches)
        if Counter(sequence) != Counter(expected.source_sequence):
            raise TranslationError(
                "装飾コードが元と異なる改行・タブ区間へ移動しました"
            )
        if not expected.source_sequence:
            continue

        if expected.strict_segment_signatures:
            if sequence != expected.source_sequence:
                raise TranslationError(
                    "単純に閉じていない装飾コードの位置・順序が変更されました"
                )
            signatures = tuple(
                _segment_signature(part)
                for part in _split_on_placeholders(translated, sequence)
            )
            if signatures != expected.strict_segment_signatures:
                raise TranslationError(
                    "単純に閉じていない装飾コードをまたいで本文または保護対象が移動しました"
                )
            continue

        sequence_index = {placeholder: index for index, placeholder in enumerate(sequence)}
        match_by_placeholder = {
            placeholder: (start, end) for placeholder, start, end in matches
        }
        for group in expected.movable_groups:
            indexes = tuple(sequence_index[placeholder] for placeholder in group.placeholders)
            first = indexes[0]
            if indexes != tuple(range(first, first + len(indexes))):
                raise TranslationError(
                    "装飾コードの開始とリセットが別の装飾範囲へ分離されました"
                )
            internal_signatures = tuple(
                _segment_signature(
                    translated[
                        match_by_placeholder[left][1] : match_by_placeholder[right][0]
                    ]
                )
                for left, right in zip(group.placeholders, group.placeholders[1:])
            )
            if internal_signatures != group.internal_segment_signatures:
                raise TranslationError(
                    "装飾コードで囲まれた本文または保護対象が別の装飾範囲へ移動しました"
                )


def _canonical_segment_signature(
    segment: str,
    replacements: Mapping[str, str],
    special_placeholders: frozenset[str],
) -> tuple[bool, tuple[tuple[str, str], ...]]:
    syntax: list[tuple[str, str]] = []
    for placeholder in _PLACEHOLDER_RE.findall(segment):
        syntax.append(
            (
                "special" if placeholder in special_placeholders else "literal_placeholder",
                replacements[placeholder],
            )
        )
    visible = _PLACEHOLDER_RE.sub("", segment)
    return (
        any(character.isalnum() for character in visible),
        tuple(sorted(syntax)),
    )


def _canonical_formatting_signature(
    segment: str,
    formatting: _FormattingSegment,
    replacements: Mapping[str, str],
    special_placeholders: frozenset[str],
) -> tuple[object, ...]:
    if not formatting.source_sequence:
        return ("groups", ())
    if formatting.strict_segment_signatures:
        return (
            "strict",
            tuple(replacements[item] for item in formatting.source_sequence),
            tuple(
                _canonical_segment_signature(part, replacements, special_placeholders)
                for part in _split_on_placeholders(
                    segment,
                    formatting.source_sequence,
                )
            ),
        )

    match_by_placeholder = {
        placeholder: (start, end)
        for placeholder, start, end in _formatting_placeholder_matches(
            segment,
            replacements,
        )
    }
    groups: list[tuple[object, ...]] = []
    for group in formatting.movable_groups:
        groups.append(
            (
                tuple(replacements[item] for item in group.placeholders),
                tuple(
                    _canonical_segment_signature(
                        segment[
                            match_by_placeholder[left][1] : match_by_placeholder[right][0]
                        ],
                        replacements,
                        special_placeholders,
                    )
                    for left, right in zip(group.placeholders, group.placeholders[1:])
                ),
            )
        )
    return "groups", tuple(sorted(groups))


def _segment_signature(segment: str) -> tuple[bool, tuple[tuple[str, int], ...]]:
    placeholders = _PLACEHOLDER_RE.findall(segment)
    visible = _PLACEHOLDER_RE.sub("", segment)
    has_meaningful_body = any(character.isalnum() for character in visible)
    return has_meaningful_body, tuple(sorted(Counter(placeholders).items()))


def _is_layout_token(value: str) -> bool:
    return _LAYOUT_PATTERN.fullmatch(value) is not None


def _is_format_token(value: str) -> bool:
    return _FORMAT_CODE.fullmatch(value) is not None


def _is_fixed_layout_token(value: str) -> bool:
    return _is_layout_token(value) and not _is_format_token(value)


def _is_reset_format_token(value: str) -> bool:
    return len(value) == 2 and value[0] in {"§", "&"} and value[1].lower() == "r"
