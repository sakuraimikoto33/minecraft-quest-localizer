from __future__ import annotations

import copy
import json
import re
import unicodedata
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Any

from .categories import REFERENCE_NAME_CATEGORY_IDS, REFERENCE_PROSE_CATEGORY_IDS
from .domain import CancelledError, TranslationError, TranslationOutcome, TranslationProject, TranslationUnit
from .glossary import GlossaryCatalog, visible_terminology_text
from .json_streams import FlatJsonStreamPlan
from .openai_client import (
    FAST_MODE_SERVICE_TIER,
    OpenAIClient,
    OpenAIResponseProtocolError,
    _redact_sensitive,
)
from .protection import (
    ProtectedText,
    TermReplacement,
    TokenProtector,
    has_protected_possessive_suffix,
    looks_like_raw_json_text,
    protected_layout_signature,
    protected_syntax_ranges,
    should_translate,
    without_leading_styled_article,
)
from .unicode_safety import translation_unicode_issue
from .translation_quality import (
    IMAGE_TITLE_RETRY_INSTRUCTIONS,
    JAPANESE_WORD_ORDER_RETRY_INSTRUCTIONS,
    image_title_translation_issue,
    japanese_word_order_issue,
)
from .quota import ComplimentaryQuotaExhausted, QuotaStatus


ProgressCallback = Callable[[int, int, str], None]


_PROTECTION_RETRY_CONTEXT = (
    "MANDATORY RETRY SAFETY: The previous translation failed local protection validation. "
    "Return every required translated fragment and assign every protected token_key exactly "
    "one distinct token_positions integer. Protected tokens cover only their source spans and "
    "do not translate surrounding source content. Translate all meaningful fragment content, "
    "including actions and modifiers; when such text exists, the retry must contain translated "
    "ordinary text. Natural target-language omission or fusion of articles and determiners is "
    "allowed. Keep layout-token order and keep text and protected values in their original "
    "layout segments. Do not add MQP-shaped strings, formatting codes, physical or escaped "
    "newlines/tabs, template placeholders, URLs, Minecraft resource IDs, command paths, or "
    "quest/chapter/task/item IDs to a fragment."
)
_WORD_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_MQP_PLACEHOLDER = re.compile(r"__MQP_[0-9A-F]{4}__")
_OMISSIBLE_ENGLISH_DETERMINERS = frozenset({"a", "an", "the"})
_ENGLISH_LIST_CONNECTOR = re.compile(r" *,? *and *\Z", re.IGNORECASE)
_DYNAMIC_COMPONENT_FIELDS = frozenset(
    {"translate", "score", "selector", "keybind", "nbt", "object"}
)
# Apply reference names only to prose surfaces. Keep the temporary title
# glossary out of independently selected names/titles so a checked title
# cannot be frozen merely because another title has the same spelling.
_PROJECT_REFERENCE_SOURCE_CATEGORIES = REFERENCE_NAME_CATEGORY_IDS
_PROJECT_REFERENCE_TARGET_CATEGORIES = REFERENCE_PROSE_CATEGORY_IDS


@dataclass(frozen=True, slots=True)
class TranslationOptions:
    batch_size: int = 24
    batch_char_limit: int = 9000
    preserve_existing: bool = True
    selected_categories: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class PartialTranslationState:
    model: str
    quota: QuotaStatus | None
    required_max_tokens: int
    total: int
    completed: int
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class _StyledBodyProjection:
    part_id: str
    provider_token: str
    source_text: str
    opening: str
    reset: str
    source_start: int = -1
    protected_body: str = ""


@dataclass(frozen=True, slots=True)
class _ProjectionCandidate:
    start: int
    end: int
    term_tokens: tuple[str, ...] = ()
    fixed_expansion: str = ""
    body_source: str = ""
    opening: str = ""
    reset: str = ""
    local_suffix: bool = False
    symbol_body: str = ""
    body_source_start: int = -1
    protected_body: str = ""


@dataclass(frozen=True, slots=True)
class _ProviderProjection:
    """Provider-facing text plus exact local-only token expansions.

    A closed formatting span whose entire body consists of protected terms
    and whitespace, or one logical term split by formatting/escaped-ampersand
    syntax, is one semantic unit for translation. Exposing its pieces as
    independent opaque tokens
    lets a model separate the term or put a Japanese particle inside its
    formatting range. Such spans are therefore projected to one otherwise-
    unused MQP token and expanded locally before the existing fail-closed
    restoration checks run.

    Consecutive non-reset formatting stacks at either safe edge are also kept
    local.  An absolute leading stack is restored through ``fixed_prefix``.
    A simple terminal styled suffix is translated as a separate child and
    appended through ``local_suffix_bodies``; a protected-name-only tail after
    closed scopes is restored through ``fixed_suffix``. Neither is represented
    by a movable provider token: moving an unclosed suffix before
    its preceding prose would make its formatting bleed into that prose.
    """

    text: str
    term_token_aliases: tuple[tuple[str, str], ...] = ()
    expansions: tuple[tuple[str, str], ...] = ()
    symbol_bindings: tuple[tuple[str, str], ...] = ()
    styled_bodies: tuple[_StyledBodyProjection, ...] = ()
    local_suffix_bodies: tuple[_StyledBodyProjection, ...] = ()
    fixed_prefix: str = ""
    fixed_suffix: str = ""

    def provider_token_for(self, original_token: str) -> str:
        for original, provider in self.term_token_aliases:
            if original == original_token:
                return provider
        return original_token

    def expand(
        self,
        translated: str,
        translated_parts: Mapping[str, str],
    ) -> str:
        observed_tokens = Counter(_MQP_PLACEHOLDER.findall(translated))
        expected_tokens = Counter(_MQP_PLACEHOLDER.findall(self.text))
        if (
            self.expansions
            or self.styled_bodies
            or self.local_suffix_bodies
            or self.fixed_prefix
            or self.fixed_suffix
        ) and observed_tokens != expected_tokens:
            missing = sorted((expected_tokens - observed_tokens).elements())
            extra = sorted((observed_tokens - expected_tokens).elements())
            details: list[str] = []
            if missing:
                details.append(f"欠落: {', '.join(missing)}")
            if extra:
                details.append(f"余分: {', '.join(extra)}")
            raise TranslationError(
                "装飾範囲の不可分tokenの種類・個数が一致しません（"
                + "; ".join(details)
                + "）"
            )
        for provider_token, original_group in self.expansions:
            translated = translated.replace(provider_token, original_group)
        for styled in self.styled_bodies:
            if styled.part_id not in translated_parts:
                raise TranslationError(
                    "内部エラー: 装飾本文の翻訳結果を親の文章へ対応付けられません"
                )
            translated = translated.replace(
                styled.provider_token,
                styled.opening + translated_parts[styled.part_id] + styled.reset,
            )
        if self.fixed_prefix:
            translated = self.fixed_prefix + translated
        for styled in self.local_suffix_bodies:
            if styled.part_id not in translated_parts:
                raise TranslationError(
                    "内部エラー: 末尾装飾本文の翻訳結果を親の文章へ対応付けられません"
                )
            translated += (
                styled.opening + translated_parts[styled.part_id] + styled.reset
            )
        translated += self.fixed_suffix
        return translated


def _safe_fixed_leading_format_prefix(protected: ProtectedText) -> str:
    """Return a locally fixed prefix for one simple unclosed style span.

    Only an absolute leading stack with no reset, later style switch, or fixed
    layout boundary is eligible here.  Other mid-string codes and formatting
    that crosses newlines/tabs retain the strict provider-response path; the
    separate terminal-suffix helper handles its own narrower safe case.
    """

    if protected.structural_placeholders or len(protected.formatting_segments) != 1:
        return ""
    formatting = protected.formatting_segments[0]
    tokens = formatting.source_sequence
    if (
        not tokens
        or formatting.movable_groups
        or not formatting.strict_segment_signatures
    ):
        return ""

    cursor = 0
    for token in tokens:
        value = protected.replacements.get(token, "")
        if (
            (
                len(value) == 2
                and value[0] in {"&", "§"}
                and value[1].lower() == "r"
            )
            or not protected.protected.startswith(token, cursor)
        ):
            return ""
        cursor += len(token)
    if cursor >= len(protected.protected):
        return ""
    return protected.protected[:cursor]


def _terminal_unclosed_styled_body_candidate(
    protected: ProtectedText,
) -> _ProjectionCandidate | None:
    """Return one simple terminal unclosed style as a local-only suffix.

    A consecutive non-reset formatting stack followed by ordinary text is
    eligible, also after closed scopes. A protected-name-only tail following
    closed scopes is kept as a fixed local expansion instead of a child.
    Newline/tab boundaries, internal style switches, and mixed protected/
    ordinary text retain the provider path.
    Keeping the suffix out of the parent provider item prevents a model from
    moving preceding prose into the unclosed formatting scope.
    """

    if protected.structural_placeholders or len(protected.formatting_segments) != 1:
        return None
    formatting = protected.formatting_segments[0]
    tokens = formatting.trailing_placeholders or formatting.source_sequence
    if not tokens:
        return None
    if not formatting.trailing_placeholders and (
        formatting.movable_groups
        or not formatting.strict_segment_signatures
        or not formatting.strict_segment_signatures[0][0]
    ):
        return None

    source = protected.protected
    start = source.find(tokens[0])
    if start <= 0:
        return None
    cursor = start
    for token in tokens:
        value = protected.replacements.get(token, "")
        if (
            (
                len(value) == 2
                and value[0] in {"&", "§"}
                and value[1].lower() == "r"
            )
            or not source.startswith(token, cursor)
        ):
            return None
        cursor += len(token)

    body = source[cursor:]
    body_tokens = tuple(_MQP_PLACEHOLDER.findall(body))
    visible_body = _MQP_PLACEHOLDER.sub("", body)
    if (
        body_tokens
        and all(token in protected.term_placeholders for token in body_tokens)
        and all(
            character.isspace() or unicodedata.category(character)[0] in {"P", "S"}
            for character in visible_body
        )
        and not has_protected_possessive_suffix(body, protected.term_placeholders)
    ):
        return _ProjectionCandidate(
            start=start,
            end=len(source),
            fixed_expansion=source[start:],
            local_suffix=True,
        )
    if (
        (not any(character.isalnum() for character in visible_body)
         and not has_protected_possessive_suffix(body, protected.term_placeholders))
        or any(token not in protected.term_placeholders for token in body_tokens)
    ):
        return None
    opening = source[start:cursor]
    if (
        tuple(_MQP_PLACEHOLDER.findall(opening)) != tokens
        or _MQP_PLACEHOLDER.sub("", opening)
    ):
        return None

    special_spans = dict(
        zip(
            protected.special_placeholders,
            protected.special_source_spans,
            strict=True,
        )
    )
    last_span = special_spans.get(tokens[-1])
    if last_span is None:
        raise TranslationError("内部エラー: 末尾装飾本文の原文位置を特定できません")
    original_body = protected.original[last_span[1] :]
    return _ProjectionCandidate(
        start=start,
        end=len(source),
        body_source=original_body,
        body_source_start=last_span[1],
        protected_body=body,
        opening=opening,
        local_suffix=True,
    )


def _fixed_unclosed_style_projection(
    protected: ProtectedText, part_id: str,
) -> _ProviderProjection | None:
    """Split strict, reset-free colour changes into locally ordered bodies.

    Unlike a closed span, these bodies cannot move with Japanese word order:
    every later character inherits the preceding codes. Keep the initial
    stack locally, translate the first body, and append the remaining styled
    body as a child. Further switches are handled recursively by that child.
    The complete original layout is still validated after assembly.
    """
    if protected.structural_placeholders or len(protected.formatting_segments) != 1:
        return None
    formatting = protected.formatting_segments[0]
    tokens = formatting.source_sequence
    if (len(tokens) < 2 or formatting.movable_groups
            or not formatting.strict_segment_signatures
            or any(protected.replacements[token].lower() in {"&r", "§r"}
                   for token in tokens)):
        return None

    source = protected.protected
    prefix_end = 0
    index = 0
    while index < len(tokens) and source.startswith(tokens[index], prefix_end):
        prefix_end += len(tokens[index])
        index += 1
    if index == len(tokens):
        return None  # One leading stack already has a simpler projection.
    start = source.index(tokens[index])
    if start <= prefix_end:
        return None
    body_start = start
    while index < len(tokens) and source.startswith(tokens[index], body_start):
        body_start += len(tokens[index])
        index += 1
    if body_start == len(source):
        return None
    spans = dict(zip(protected.special_placeholders, protected.special_source_spans, strict=True))
    original_start = spans[tokens[index - 1]][1]
    return _ProviderProjection(
        text=source[prefix_end:start],
        fixed_prefix=source[:prefix_end],
        local_suffix_bodies=(_StyledBodyProjection(
            part_id=f"{part_id}::styled::0000",
            provider_token="",
            source_text=protected.original[original_start:],
            opening=source[start:body_start],
            reset="",
            source_start=original_start,
            protected_body=source[body_start:],
        ),),
    )


@dataclass(frozen=True, slots=True)
class _PreparedPart:
    id: str
    protected: ProtectedText
    provider_projection: _ProviderProjection
    context: str
    unit_key: str
    source_path: str
    styled_parts: tuple[_PreparedPart, ...] = ()
    raw_json_fragment: bool = False
    parent_token_aliases: tuple[tuple[str, str], ...] = ()
    json_stream_plan: FlatJsonStreamPlan | None = None
    provider_context: str = ""


@dataclass(slots=True)
class _PreparedUnit:
    unit: TranslationUnit
    template: Any | None
    path_by_part: dict[str, tuple[object, ...]]
    parts: list[_PreparedPart]
    glossary: GlossaryCatalog
    json_stream_plan: FlatJsonStreamPlan | None = None
    json_stream_part_id: str = ""

    def assemble(self, translated_parts: dict[str, str]) -> str:
        if self.template is None:
            return translated_parts[self.parts[0].id] if self.parts else self.unit.source
        if self.json_stream_plan is not None:
            rendered = self.json_stream_plan.render(
                self.template,
                translated_parts[self.json_stream_part_id],
                {
                    int(path[0]): translated_parts[part_id]
                    for part_id, path in self.path_by_part.items()
                },
            )
            return json.dumps(rendered, ensure_ascii=False, separators=(",", ":"))
        rendered = copy.deepcopy(self.template)
        for part_id, path in self.path_by_part.items():
            rendered = _set_path(rendered, path, translated_parts[part_id])
        return json.dumps(rendered, ensure_ascii=False, separators=(",", ":"))


class TranslationService:
    def __init__(
        self,
        client: OpenAIClient,
        protector: TokenProtector | None = None,
        *,
        fast_mode: bool = False,
    ) -> None:
        self.client = client
        self.protector = protector or TokenProtector()
        self.fast_mode = fast_mode

    def _translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, Any]],
        source_locale: str,
        target_locale: str,
        cancel: Event | None,
    ) -> dict[str, str]:
        if self.fast_mode:
            return self.client.translate_batch(
                api_key,
                model,
                items,
                source_locale,
                target_locale,
                cancel,
                service_tier=FAST_MODE_SERVICE_TIER,
            )
        return self.client.translate_batch(
            api_key,
            model,
            items,
            source_locale,
            target_locale,
            cancel,
        )

    def _translate_bundles(
        self,
        roots: list[_PreparedPart],
        api_key: str,
        model: str,
        source_locale: str,
        target_locale: str,
        options: TranslationOptions,
        report: Callable[[str], None],
        cancel: Event | None,
        translated_parts: dict[str, str] | None = None,
        on_restored: Callable[[dict[str, str]], None] | None = None,
    ) -> dict[str, str]:
        batches = deque(_make_batches(roots, options.batch_size, options.batch_char_limit))
        translated_parts = translated_parts if translated_parts is not None else {}
        batch_index = 0
        while batches:
            batch = batches.popleft()
            batch_index += 1
            if cancel and cancel.is_set():
                raise CancelledError("処理をキャンセルしました")
            report(f"OpenAI で翻訳中 ({batch_index}/{batch_index + len(batches)})")
            batch_parts = [part for root in batch for part in _bundle_parts(root)]
            request_items = [_provider_item(part) for part in batch_parts]
            restored_root_ids: set[str] = set()
            try:
                response = self._translate_batch(
                    api_key,
                    model,
                    request_items,
                    source_locale,
                    target_locale,
                    cancel,
                )
            except ComplimentaryQuotaExhausted:
                if len(batch) <= 1:
                    raise
                middle = len(batch) // 2
                batches.appendleft(batch[middle:])
                batches.appendleft(batch[:middle])
                report(f"無料枠に合わせてバッチを縮小: {len(batch)} → {middle} + {len(batch) - middle}件")
                continue
            except OpenAIResponseProtocolError as batch_error:
                failed_scope = batch_error.item_id or "batch root"
                report(
                    "構造化翻訳応答の安全確認に失敗したため、このバッチを"
                    "1件ずつ再試行します: "
                    f"{failed_scope} / 理由: {batch_error}"
                )
                response = {}
                for root in batch:
                    bundle = _bundle_parts(root)
                    bundle_items = [_provider_item(part) for part in bundle]
                    if cancel and cancel.is_set():
                        raise CancelledError("処理をキャンセルしました")
                    try:
                        individual = self._translate_batch(
                            api_key,
                            model,
                            bundle_items,
                            source_locale,
                            target_locale,
                            cancel,
                        )
                    except ComplimentaryQuotaExhausted as quota_error:
                        raise TranslationError(
                            "構造化応答の安全確認に失敗し、再試行に必要な無料枠も不足しました。"
                            "翻訳ファイルには書き込んでいません。"
                        ) from quota_error
                    except OpenAIResponseProtocolError as retry_error:
                        failed_part = _part_for_item_id(root, retry_error.item_id)
                        raise TranslationError(
                            "OpenAI の構造化翻訳応答を安全に復元できませんでした。\n"
                            f"{_part_location_details(failed_part)}\n"
                            f"バッチ応答の理由: {batch_error}\n"
                            f"個別再試行後の理由: {retry_error}\n"
                            "この処理では翻訳ファイルへ書き込んでいません。"
                        ) from retry_error
                    response.update(individual)
                    # Do not lose verified individual retries if a later
                    # request fails before the full batch has returned.
                    restored, failure = _restore_bundle_response(
                        root, individual, source_locale, target_locale, translated_parts,
                    )
                    if failure is None:
                        translated_parts.update(restored)
                        restored_root_ids.add(root.id)
                        if on_restored:
                            on_restored(restored)

            for root in batch:
                if root.id in restored_root_ids:
                    continue
                restored, failure = _restore_bundle_response(
                    root,
                    response,
                    source_locale,
                    target_locale,
                    translated_parts,
                )
                if failure is None:
                    translated_parts.update(restored)
                    if on_restored:
                        on_restored(restored)
                    continue

                failed_part, first_error = failure
                first_reason = str(first_error)
                report(
                    "翻訳結果の安全確認に失敗したため、この1件を装飾本文と"
                    "まとめて個別再試行します: "
                    f"{_part_location_summary(failed_part)} / "
                    f"1回目の理由: {first_reason}"
                )
                if cancel and cancel.is_set():
                    raise CancelledError("処理をキャンセルしました")
                retry_context = (
                    f"{_part_location_details(failed_part)}\n{_PROTECTION_RETRY_CONTEXT}\n"
                    f"Previous validation failure: {first_reason}"
                )
                if "日本語の語順" in first_reason:
                    retry_context = (
                        f"{retry_context}\n{JAPANESE_WORD_ORDER_RETRY_INSTRUCTIONS}"
                    )
                if "画像タイトルに説明文" in first_reason:
                    retry_context = f"{retry_context}\n{IMAGE_TITLE_RETRY_INSTRUCTIONS}"
                retry_items = [
                    _provider_item(
                        part,
                        context=retry_context if part.id == failed_part.id else None,
                    )
                    for part in _bundle_parts(root)
                ]
                try:
                    retry = self._translate_batch(
                        api_key,
                        model,
                        retry_items,
                        source_locale,
                        target_locale,
                        cancel,
                    )
                except TranslationError as retry_error:
                    retry_part = _part_for_item_id(
                        root,
                        getattr(retry_error, "item_id", None),
                    )
                    raise TranslationError(
                        "OpenAI の応答を安全に復元できませんでした。\n"
                        f"{_part_location_details(retry_part)}\n"
                        f"1回目の理由: {first_reason}\n"
                        f"再試行後の理由: {retry_error}\n"
                        "この処理では翻訳ファイルへ書き込んでいません。"
                    ) from retry_error
                retried, retry_failure = _restore_bundle_response(
                    root,
                    retry,
                    source_locale,
                    target_locale,
                    translated_parts,
                )
                if retry_failure is not None:
                    retry_part, retry_error = retry_failure
                    raise TranslationError(
                        "OpenAI の応答を安全に復元できませんでした。\n"
                        f"{_part_location_details(retry_part)}\n"
                        f"1回目の理由: {first_reason}\n"
                        f"再試行後の理由: {retry_error}\n"
                        "この処理では翻訳ファイルへ書き込んでいません。"
                    ) from retry_error
                translated_parts.update(retried)
                if on_restored:
                    on_restored(retried)
        return translated_parts

    def translate(
        self,
        project: TranslationProject,
        adapter: object,
        output_path: Path,
        api_key: str,
        model: str,
        glossary: GlossaryCatalog,
        options: TranslationOptions,
        progress: ProgressCallback | None = None,
        cancel: Event | None = None,
        pre_write_guard: Callable[[], None] | None = None,
        confirm_partial: Callable[[PartialTranslationState], bool] | None = None,
    ) -> TranslationOutcome:
        if not model.strip():
            raise TranslationError("使用する OpenAI モデルを選択してください")
        if cancel and cancel.is_set():
            raise CancelledError("処理をキャンセルしました")
        quota_validator = getattr(self.client, "validate_quota_configuration", None)
        if callable(quota_validator):
            quota_validator(model, FAST_MODE_SERVICE_TIER if self.fast_mode else None)
        selected_units = select_translation_units(project, options.selected_categories)
        if not selected_units:
            raise TranslationError(
                "選択した項目に翻訳文字列がありません。翻訳する項目を1つ以上選び、解析結果を確認してください"
            )
        validator = getattr(adapter, "validate_output", None)
        if callable(validator):
            validator(project, output_path)
        total = len(selected_units)
        skipped_by_selection = len(project.units) - total
        done = 0
        translated_count = 0
        reused_count = 0
        copied_count = 0
        resolved: dict[str, str] = {}
        prepared_units: list[_PreparedUnit] = []
        pending: list[_PreparedPart] = []
        terminology_groups = _project_terminology_groups(project)
        reference_term_groups = _project_reference_term_groups(project)
        project_reference_terms = _project_reference_terms(
            project,
            selected_units,
            reference_term_groups,
        )
        project_reference_glossary = glossary.with_source_preserved_terms(
            project_reference_terms
        )
        terminology_spans = _project_terminology_spans(
            project,
            glossary,
            terminology_groups,
            project_reference_glossary,
        )
        existing_group_safety = _project_existing_group_safety(
            project,
            glossary,
            project.source_locale,
            project.target_locale,
            terminology_groups,
            project_reference_glossary,
        )

        def report(message: str) -> None:
            if progress:
                progress(done, total, message)

        for unit in selected_units:
            if cancel and cancel.is_set():
                raise CancelledError("処理をキャンセルしました")
            existing = project.existing.get(unit.id, "")
            unit_glossary = glossary.scoped_for_resources(
                unit.source,
                getattr(unit, "resource_ids", ()),
            )
            if unit.category in _PROJECT_REFERENCE_TARGET_CATEGORIES:
                if unit_glossary is glossary:
                    unit_glossary = project_reference_glossary
                else:
                    # Resource-scoped official names are resolved first.  An
                    # unchecked project title then wins on identical spelling
                    # because that title remains source-language in game.
                    unit_glossary = unit_glossary.with_source_preserved_terms(
                        project_reference_terms
                    )
            if options.preserve_existing and existing and existing != unit.source:
                if _existing_translation_is_safe(
                    unit.source,
                    existing,
                    unit_glossary,
                    project.source_locale,
                    project.target_locale,
                    unit_key=unit.key,
                    treat_as_plain=unit.id in terminology_spans,
                ) and existing_group_safety.get(unit.id, True):
                    resolved[unit.id] = existing
                    reused_count += 1
                    done += 1
                    report(f"既存翻訳を保持: {unit.key}")
                    continue
                quality_issue = japanese_word_order_issue(
                    unit.source, existing, project.source_locale, project.target_locale,
                )
                report(
                    f"既存訳を再翻訳: {unit.key} / 理由: {quality_issue}"
                    if quality_issue else
                    "既存訳の保護コード、JSON構造、またはUnicode安全性が"
                    f"不一致のため再翻訳: {unit.key}"
                )
            try:
                prepared = _prepare_unit(
                    unit,
                    self.protector,
                    unit_glossary,
                    terminology_spans.get(unit.id),
                )
            except TranslationError as exc:
                raise TranslationError(
                    "翻訳前の固有名詞・装飾保護を安全に準備できませんでした。\n"
                    f"{_unit_location_details(unit)}\n"
                    f"理由: {exc}\n"
                    "OpenAI APIは呼び出さず、翻訳ファイルにも書き込んでいません。"
                ) from exc
            if not prepared.parts:
                resolved[unit.id] = unit.source
                copied_count += 1
                done += 1
                report(f"翻訳不要のコードを保持: {unit.key}")
                continue
            prepared_units.append(prepared)
            pending.extend(prepared.parts)

        sources = {unit.id: unit.source for unit in selected_units}
        reused_ids = {unit_id for unit_id in resolved if resolved[unit_id] != sources[unit_id]}
        copied_ids = set(resolved) - reused_ids
        translated_parts: dict[str, str] = {}
        prepared_by_part = {part.id: prepared for prepared in prepared_units for part in prepared.parts}

        def complete_units(restored: dict[str, str]) -> None:
            nonlocal done, translated_count
            for part_id in restored:
                prepared = prepared_by_part.get(part_id)
                if prepared is None or prepared.unit.id in resolved:
                    continue
                if not all(part.id in translated_parts for part in prepared.parts):
                    continue
                assembled = prepared.assemble(translated_parts)
                if not _assembled_translation_preserves_terms(
                    prepared.unit.source, assembled, prepared.glossary,
                    treat_as_plain=prepared.unit.id in terminology_spans,
                ):
                    continue  # Preserve the existing assembly retry below.
                resolved[prepared.unit.id] = assembled
                translated_count += 1
                done += 1
                report(f"翻訳済み: {prepared.unit.key}")

        def finish_pending_unit(prepared: _PreparedUnit) -> None:
            nonlocal translated_count, done
            if prepared.unit.id in resolved:
                return
            assembled = prepared.assemble(translated_parts)
            if not _assembled_translation_preserves_terms(
                prepared.unit.source,
                assembled,
                prepared.glossary,
                treat_as_plain=prepared.unit.id in terminology_spans,
            ):
                term_reason = "組み立て後にMod名または公式用語の綴り・境界が変わりました"
                report(
                    "翻訳結果の固有名詞確認に失敗したため、この1件だけ再試行します: "
                    f"{_unit_location_summary(prepared.unit)} / 理由: {term_reason}"
                )
                if cancel and cancel.is_set():
                    raise CancelledError("処理をキャンセルしました")
                for part in prepared.parts:
                    if cancel and cancel.is_set():
                        raise CancelledError("処理をキャンセルしました")
                    retry_context = (
                        f"{_part_location_details(part)}\n{_PROTECTION_RETRY_CONTEXT}\n"
                        f"Previous assembled terminology failure: {term_reason}"
                    )
                    retry_failure_part = part
                    try:
                        retry_items = [
                            _provider_item(
                                bundle_part,
                                context=(
                                    retry_context
                                    if bundle_part.id == part.id
                                    else None
                                ),
                            )
                            for bundle_part in _bundle_parts(part)
                        ]
                        retry = self._translate_batch(
                            api_key,
                            model,
                            retry_items,
                            project.source_locale,
                            project.target_locale,
                            cancel,
                        )
                        restored, failure = _restore_bundle_response(
                            part,
                            retry,
                            project.source_locale,
                            project.target_locale,
                            translated_parts,
                        )
                        if failure is not None:
                            retry_failure_part, restore_error = failure
                            raise restore_error
                        translated_parts.update(restored)
                    except ComplimentaryQuotaExhausted:
                        raise
                    except TranslationError as retry_error:
                        retry_item_id = getattr(retry_error, "item_id", None)
                        if retry_item_id is not None:
                            retry_failure_part = _part_for_item_id(part, retry_item_id)
                        raise TranslationError(
                            "固有名詞確認後の再試行結果を安全に復元できませんでした。\n"
                            f"{_part_location_details(retry_failure_part)}\n"
                            f"1回目の理由: {term_reason}\n"
                            f"再試行後の理由: {retry_error}\n"
                            "この処理では翻訳ファイルへ書き込んでいません。"
                        ) from retry_error
                assembled = prepared.assemble(translated_parts)
                if not _assembled_translation_preserves_terms(
                    prepared.unit.source,
                    assembled,
                    prepared.glossary,
                    treat_as_plain=prepared.unit.id in terminology_spans,
                ):
                    raise TranslationError(
                        "OpenAI の応答でMod名または公式用語の綴り・境界が変更されました。\n"
                        f"{_unit_location_details(prepared.unit)}\n"
                        f"理由: {term_reason}\n"
                        "この処理では翻訳ファイルへ書き込んでいません。"
                    )
            resolved[prepared.unit.id] = assembled
            translated_count += 1
            done += 1
            report(f"翻訳済み: {prepared.unit.key}")

        stop_error: TranslationError | None = None
        try:
            self._translate_bundles(
                pending, api_key, model, project.source_locale, project.target_locale,
                options, report, cancel, translated_parts, complete_units,
            )
            for prepared in prepared_units:
                finish_pending_unit(prepared)
        except TranslationError as exc:
            # Only the translation phase is recoverable. Cancellation,
            # preparation, input/output guards and writer failures remain
            # outside this catch and must never trigger a partial write.
            stop_error = exc

        selected_ids = frozenset(unit.id for unit in selected_units)
        if stop_error is None and set(resolved) != selected_ids:
            raise TranslationError("内部エラー: 選択したすべての翻訳単位が解決されていません")

        def exclude_incomplete_groups() -> None:
            required_groups = [
                selected_ids.intersection(group)
                for group in (*terminology_groups, *reference_term_groups)
            ]
            required_groups.extend(set(group) for group in project.atomic_output_groups)
            changed = True
            while changed:
                changed = False
                for required in required_groups:
                    if not required.issubset(resolved):
                        for unit_id in required.intersection(resolved):
                            del resolved[unit_id]
                            changed = True

        if stop_error is not None:
            exclude_incomplete_groups()
        while (unsafe_group := _first_unsafe_terminology_group(
            project, resolved, glossary, terminology_groups, project_reference_glossary,
        )) is not None:
            keys = ", ".join(
                next(unit.key for unit in project.units if unit.id == unit_id)
                for unit_id in unsafe_group
            )
            group_error = TranslationError(
                "組み立て後にraw JSONコンポーネントをまたぐMod名または公式用語の"
                "綴り・表示境界が変わりました。\n"
                f"対象キー: {keys}\n"
                "この処理では翻訳ファイルへ書き込んでいません。"
            )
            unsafe_ids = set(unsafe_group).intersection(resolved)
            if not unsafe_ids:
                raise group_error  # Source fallback itself could not be verified.
            # A cross-unit failure invalidates the entire affected group, not
            # other independently completed translations.
            stop_error = stop_error or group_error
            report(f"部分出力から除外: {keys} / 理由: グループ全体の固有名詞確認に失敗しました")
            for unit_id in unsafe_ids:
                del resolved[unit_id]
            exclude_incomplete_groups()

        if cancel and cancel.is_set():
            raise CancelledError("処理をキャンセルしました")
        stop_reason = ""
        error_message = ""
        if stop_error is not None:
            reused_count = len(reused_ids.intersection(resolved))
            copied_count = len(copied_ids.intersection(resolved))
            translated_count = len(resolved) - reused_count - copied_count
            done = len(resolved)
            quota_error = stop_error if isinstance(stop_error, ComplimentaryQuotaExhausted) else None
            stop_reason = "quota" if quota_error is not None else "error"
            error_message = _redact_sensitive(str(stop_error), api_key)
            for boilerplate in (
                "この処理では翻訳ファイルへ書き込んでいません。",
                "翻訳ファイルには書き込んでいません。",
            ):
                error_message = error_message.replace(boilerplate, "")
            error_message = error_message.strip()
            state = PartialTranslationState(
                model, quota_error.status if quota_error else None,
                quota_error.required_max_tokens if quota_error else 0,
                total, len(resolved), error_message,
            )
            report(error_message)
            if quota_error is None and (not resolved or confirm_partial is None):
                raise stop_error
            if not resolved or confirm_partial is None or not confirm_partial(state):
                report("途中終了しました。翻訳ファイルには書き込んでいません")
                return TranslationOutcome(
                    output_path, total, translated_count, reused_count, copied_count,
                    len(glossary.entries), skipped_by_selection=skipped_by_selection,
                    partial=True, written=False,
                    stop_reason=stop_reason, error_message=error_message,
                )
        report("出力を検証して書き込んでいます")
        if pre_write_guard:
            pre_write_guard()
        if cancel and cancel.is_set():
            raise CancelledError("処理をキャンセルしました")
        emitted_ids = frozenset(resolved)
        if emitted_ids != frozenset(unit.id for unit in project.units):
            adapter.write(project, resolved, output_path, selected_unit_ids=emitted_ids)
        else:
            adapter.write(project, resolved, output_path)
        report("部分出力が完了しました" if stop_error is not None else "完了")
        return TranslationOutcome(
            output_path=output_path,
            total=total,
            translated=translated_count,
            reused=reused_count,
            copied_without_translation=copied_count,
            glossary_terms=len(glossary.entries),
            skipped_by_selection=skipped_by_selection,
            preserved_unselected=0,
            partial=stop_error is not None,
            stop_reason=stop_reason, error_message=error_message,
        )


def select_translation_units(
    project: TranslationProject,
    selected_categories: frozenset[str] | None,
) -> list[TranslationUnit]:
    """Return units enabled by the user's immutable category snapshot."""

    if selected_categories is None:
        return list(project.units)
    return [unit for unit in project.units if unit.category in selected_categories]


def _make_batches(
    pending: list[_PreparedPart], batch_size: int, char_limit: int
) -> list[list[_PreparedPart]]:
    batch_size = max(1, batch_size)
    char_limit = max(500, char_limit)
    batches: list[list[_PreparedPart]] = []
    current: list[_PreparedPart] = []
    current_items = 0
    current_chars = 0
    request_ids: set[str] = set()
    for root_part in pending:
        bundle = _bundle_parts(root_part)
        bundle_chars = 0
        for item in bundle:
            if item.id in request_ids:
                raise TranslationError(f"内部エラー: 翻訳入力IDが重複しています: {item.id}")
            request_ids.add(item.id)
            # Semantic bindings are serialized into the provider input as
            # well, so include every child and the owning root in one budget.
            item_chars = len(
                json.dumps(
                    {key: value for key, value in _provider_item(item).items()
                     if key != "_source_text"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            if item_chars > char_limit:
                raise TranslationError(
                    f"1件の翻訳文字列が設定した最大文字数を超えています: "
                    f"{item.context} ({item_chars} > {char_limit})"
                )
            bundle_chars += item_chars
        if bundle_chars > char_limit:
            raise TranslationError(
                "装飾本文を含む1件の翻訳文字列が設定した最大文字数を"
                f"超えています: {root_part.context} ({bundle_chars} > {char_limit})"
            )
        bundle_items = len(bundle)
        if current and (
            current_items + bundle_items > batch_size
            or current_chars + bundle_chars > char_limit
        ):
            batches.append(current)
            current = []
            current_items = 0
            current_chars = 0
        current.append(root_part)
        current_items += bundle_items
        current_chars += bundle_chars
    if current:
        batches.append(current)
    return batches


def _grouped_term_projection_candidates(
    protected: ProtectedText,
) -> list[_ProjectionCandidate]:
    r"""Collapse fragments of one glossary match into one provider token.

    Formatting codes and the literal ``\&`` escape can split a single visible
    project or registry name into several occurrence spans. The local
    protector still tracks every span independently for provenance checks, but
    the provider must see the complete name as one semantic unit so it cannot
    separate those fragments while reordering a sentence.
    """

    if not (
        len(protected.term_placeholders)
        == len(protected.term_source_spans)
        == len(protected.term_group_ids)
    ):
        raise TranslationError("内部エラー: 固有名詞groupを再検証できません")

    terms_by_group: dict[int, list[str]] = {}
    for placeholder, group_id in zip(
        protected.term_placeholders,
        protected.term_group_ids,
        strict=True,
    ):
        if group_id is not None:
            terms_by_group.setdefault(group_id, []).append(placeholder)

    source = protected.protected
    all_terms = frozenset(protected.term_placeholders)
    formatting_tokens = frozenset(
        placeholder
        for segment in protected.formatting_segments
        for placeholder in segment.source_sequence
    )
    eligible_specials = formatting_tokens | frozenset(
        placeholder
        for placeholder in protected.special_placeholders
        if protected.replacements.get(placeholder) in {r"\&", "\\&\\"}
    )
    formatting_positions: dict[str, int] = {}
    formatting_at_start: dict[int, str] = {}
    formatting_at_end: dict[int, str] = {}
    for placeholder in formatting_tokens:
        position = source.find(placeholder)
        if position < 0 or source.find(placeholder, position + len(placeholder)) >= 0:
            raise TranslationError("内部エラー: 装飾tokenの位置を特定できません")
        formatting_positions[placeholder] = position
        formatting_at_start[position] = placeholder
        formatting_at_end[position + len(placeholder)] = placeholder
    strict_formatting_tokens: set[str] = set()
    formatting_group_ranges: list[tuple[int, int]] = []
    for segment in protected.formatting_segments:
        if segment.strict_segment_signatures:
            strict_formatting_tokens.update(segment.source_sequence)
        strict_formatting_tokens.update(segment.trailing_placeholders)
        for formatting_group in segment.movable_groups:
            group_start = formatting_positions[formatting_group.placeholders[0]]
            group_end = (
                formatting_positions[formatting_group.placeholders[-1]]
                + len(formatting_group.placeholders[-1])
            )
            formatting_group_ranges.append((group_start, group_end))
    strict_layout_ranges: list[tuple[int, int]] = []
    layout_ranges: list[tuple[int, int]] = []
    layout_start = 0
    for boundary in protected.structural_placeholders:
        boundary_start = source.find(boundary, layout_start)
        if boundary_start < layout_start:
            raise TranslationError("内部エラー: 固定配置tokenの範囲を特定できません")
        layout_ranges.append((layout_start, boundary_start))
        layout_start = boundary_start + len(boundary)
    layout_ranges.append((layout_start, len(source)))
    if len(layout_ranges) != len(protected.formatting_segments):
        raise TranslationError("内部エラー: 装飾と固定配置の範囲が一致しません")
    strict_layout_ranges.extend(
        layout_range
        for layout_range, segment in zip(
            layout_ranges,
            protected.formatting_segments,
            strict=True,
        )
        if segment.strict_segment_signatures
    )
    strict_layout_ranges.extend(
        (formatting_positions[segment.trailing_placeholders[0]], layout_end)
        for (_layout_start, layout_end), segment in zip(
            layout_ranges, protected.formatting_segments, strict=True
        )
        if segment.trailing_placeholders
    )

    candidates: list[_ProjectionCandidate] = []
    for term_tokens in terms_by_group.values():
        if len(term_tokens) < 2:
            continue
        positions: list[int] = []
        for placeholder in term_tokens:
            position = source.find(placeholder)
            if position < 0 or source.find(placeholder, position + len(placeholder)) >= 0:
                raise TranslationError("内部エラー: 固有名詞groupの位置を特定できません")
            positions.append(position)
        if positions != sorted(positions):
            raise TranslationError("内部エラー: 固有名詞groupの順序を特定できません")

        start = positions[0]
        end = positions[-1] + len(term_tokens[-1])

        # Keep immediately adjacent opening style codes with the grouped name.
        # A reset belongs to preceding prose and must never be pulled in.
        while start in formatting_at_end:
            placeholder = formatting_at_end[start]
            value = protected.replacements[placeholder]
            if len(value) == 2 and value[0] in {"&", "§"} and value[1].lower() == "r":
                break
            start -= len(placeholder)

        # Likewise, include only trailing resets that close formatting used by
        # the name; a following color/style code belongs to later prose.
        while end in formatting_at_start:
            placeholder = formatting_at_start[end]
            value = protected.replacements[placeholder]
            if not (
                len(value) == 2
                and value[0] in {"&", "§"}
                and value[1].lower() == "r"
            ):
                break
            end += len(placeholder)

        original_group = source[start:end]
        observed_tokens = tuple(_MQP_PLACEHOLDER.findall(original_group))
        observed_terms = tuple(
            placeholder for placeholder in observed_tokens if placeholder in all_terms
        )
        observed_formatting = tuple(
            placeholder
            for placeholder in observed_tokens
            if placeholder in formatting_tokens
        )
        if (
            observed_terms != tuple(term_tokens)
            or any(
                placeholder not in all_terms
                and placeholder not in eligible_specials
                for placeholder in observed_tokens
            )
            or _MQP_PLACEHOLDER.sub("", original_group)
        ):
            continue
        if any(
            placeholder in strict_formatting_tokens
            for placeholder in observed_formatting
        ) or any(
            start < group_end
            and end > group_start
            and not (start <= group_start and end >= group_end)
            for group_start, group_end in formatting_group_ranges
        ) or any(
            start < layout_end and end > layout_start
            for layout_start, layout_end in strict_layout_ranges
        ):
            # A semantic atom may contain a whole closed formatting scope, but
            # must not detach an opening code or reset from that scope.
            continue
        if observed_formatting and not any(
            len(protected.replacements[placeholder]) == 2
            and protected.replacements[placeholder][0] in {"&", "§"}
            and protected.replacements[placeholder][1].lower() == "r"
            for placeholder in observed_formatting
        ):
            # Moving an unclosed style with a term could change the scope of
            # following prose. Leave that case to the strict validator.
            continue
        candidates.append(
            _ProjectionCandidate(
                start=start,
                end=end,
                term_tokens=tuple(term_tokens),
                fixed_expansion=original_group,
            )
        )
    return candidates


def _build_provider_projection(
    protected: ProtectedText,
    part_id: str,
) -> _ProviderProjection:
    """Keep eligible formatting boundaries under deterministic local control.

    A scope containing only protected terms and whitespace can be expanded
    locally as a fixed value, even when its names use different colours.
    A simple scope containing only ordinary source text is represented
    by a child translation part; the translated child is inserted between the
    original opening codes and reset.  A simple unclosed style stack at the
    absolute beginning is removed from provider input and restored as an exact
    local prefix.  A simple unclosed suffix is translated independently and
    appended locally so its boundary cannot move.  Groups containing mixed
    terms and ordinary text, technical placeholders, or compound styled prose
    retain their internal boundary checks. Unclosed formatting remains under
    the existing fixed-layout validator.
    """

    source = protected.protected
    term_placeholders = frozenset(protected.term_placeholders)
    if len(protected.special_placeholders) != len(protected.special_source_spans):
        raise TranslationError("内部エラー: 装飾範囲の原文位置を再検証できません")
    special_spans = dict(
        zip(
            protected.special_placeholders,
            protected.special_source_spans,
            strict=True,
        )
    )
    fixed_prefix = _safe_fixed_leading_format_prefix(protected)
    if fixed_prefix:
        return _ProviderProjection(
            text=source[len(fixed_prefix) :],
            fixed_prefix=fixed_prefix,
        )
    fixed_scopes = _fixed_unclosed_style_projection(protected, part_id)
    if fixed_scopes is not None:
        return fixed_scopes
    groups = _grouped_term_projection_candidates(protected)
    if len(protected.formatting_segments) == 1:
        tail = protected.formatting_segments[0].trailing_placeholders
        suffix = "".join(tail)
        if tail and source.endswith(suffix) and all(
            protected.replacements[token].lower() in {"&r", "§r"} for token in tail
        ):
            groups.append(_ProjectionCandidate(
                start=len(source) - len(suffix), end=len(source),
                fixed_expansion=suffix, local_suffix=True,
            ))
    terminal_suffix = _terminal_unclosed_styled_body_candidate(protected)
    if terminal_suffix is not None:
        groups.append(terminal_suffix)

    for segment in protected.formatting_segments:
        for formatting_group in segment.movable_groups:
            formatting_tokens = formatting_group.placeholders
            if len(formatting_tokens) < 2:
                raise TranslationError("内部エラー: 装飾範囲を生成できません")

            positions: list[int] = []
            for token in formatting_tokens:
                position = source.find(token)
                if position < 0 or source.find(token, position + len(token)) >= 0:
                    raise TranslationError("内部エラー: 装飾範囲の位置を特定できません")
                positions.append(position)
            if positions != sorted(positions):
                raise TranslationError("内部エラー: 装飾範囲の順序を特定できません")

            start = positions[0]
            end = positions[-1] + len(formatting_tokens[-1])
            if any(start < group.end and end > group.start for group in groups):
                # A logical glossary term spanning this formatting group has
                # already claimed the complete semantic range.
                continue
            original_group = source[start:end]
            observed_tokens = tuple(_MQP_PLACEHOLDER.findall(original_group))
            terms = tuple(token for token in observed_tokens if token in term_placeholders)

            body_tokens = tuple(token for token in observed_tokens if token not in formatting_tokens)
            if (
                len(body_tokens) == 1
                and body_tokens[0] in protected.special_placeholders
                and protected.replacements[body_tokens[0]].startswith("/")
                and not _MQP_PLACEHOLDER.sub("", original_group).strip()
            ):
                # A literal command (including its protected arguments) and
                # its complete style scope are one immutable semantic unit.
                groups.append(_ProjectionCandidate(
                    start=start, end=end, fixed_expansion=original_group,
                    symbol_body=protected.replacements[body_tokens[0]],
                ))
                continue

            symbol_body = _MQP_PLACEHOLDER.sub("", original_group)
            if (
                observed_tokens == formatting_tokens
                and symbol_body.strip()
                and all(
                    character.isspace()
                    or unicodedata.category(character)[0] in {"P", "S"}
                    for character in symbol_body
                )
            ):
                # UI glyphs such as ``&a+&r`` have no linguistic body to
                # translate. Keep the glyph and every style boundary in one
                # immutable token while translating the surrounding sentence.
                groups.append(
                    _ProjectionCandidate(
                        start=start,
                        end=end,
                        fixed_expansion=original_group,
                        symbol_body=symbol_body,
                    )
                )
                continue

            if (
                terms
                and all(
                    token in term_placeholders or token in formatting_tokens
                    for token in observed_tokens
                )
                and all(
                    character.isspace() or unicodedata.category(character)[0] in {"P", "S"}
                    for character in _MQP_PLACEHOLDER.sub("", original_group)
                )
                and not has_protected_possessive_suffix(original_group, term_placeholders)
            ):
                # A reset-closed scope can contain several differently
                # coloured names, e.g. ``&eMekanism &dFission Reactors&r``.
                # Keep all of its original code/term positions together and
                # expose the complete compound name as one semantic token.
                groups.append(
                    _ProjectionCandidate(
                        start=start,
                        end=end,
                        term_tokens=terms,
                        fixed_expansion=original_group,
                    )
                )
                continue

            # Translate mixed prose/names inside their original scope. Child
            # tokens are mapped back to these exact parent occurrences after
            # validation, never by replacing restored visible name strings.
            if any(token not in term_placeholders and token not in formatting_tokens
                   and protected.replacements.get(token) not in {r"\&", "\\&\\"}
                   for token in observed_tokens):
                continue
            opening_index = 0
            while (
                opening_index + 1 < len(formatting_tokens) - 1
                and positions[opening_index] + len(formatting_tokens[opening_index])
                == positions[opening_index + 1]
            ):
                opening_index += 1
            last_opening = formatting_tokens[opening_index]
            closing_index = len(formatting_tokens) - 1
            # A nested child does not carry its parent's colour contract. In
            # ``&oSentence &cName&f!&r`` it would otherwise see ``&cName&f`` as
            # a movable atom and could put prose into the parent's white ``!``
            # slot. Keep symbol-only closing slots with the local closing
            # codes, just as the opening stack is kept outside the child.
            while closing_index - 1 > opening_index:
                previous = closing_index - 1
                closing_body = source[
                    positions[previous] + len(formatting_tokens[previous])
                    : positions[closing_index]
                ]
                if _MQP_PLACEHOLDER.search(closing_body) or any(
                    character.isalnum() for character in closing_body
                ):
                    break
                closing_index = previous
            reset_token = formatting_tokens[closing_index]
            body_start = positions[opening_index] + len(last_opening)
            body_end = positions[closing_index]
            body = source[body_start:body_end]
            if not any(character.isalnum() for character in _MQP_PLACEHOLDER.sub("", body)) and not (
                has_protected_possessive_suffix(body, term_placeholders)
            ):
                continue
            opening_span = special_spans.get(last_opening)
            reset_span = special_spans.get(reset_token)
            if opening_span is None or reset_span is None:
                raise TranslationError("内部エラー: 装飾本文の原文位置を特定できません")
            original_body = protected.original[opening_span[1] : reset_span[0]]
            opening = source[start:body_start]
            reset = source[body_end:end]
            if (
                tuple(_MQP_PLACEHOLDER.findall(opening))
                != formatting_tokens[:opening_index + 1]
                or tuple(_MQP_PLACEHOLDER.findall(reset))
                != formatting_tokens[closing_index:]
                or _MQP_PLACEHOLDER.sub("", opening)
            ):
                raise TranslationError("内部エラー: 装飾本文の境界を生成できません")
            groups.append(
                _ProjectionCandidate(
                    start=start,
                    end=end,
                    body_source=original_body,
                    body_source_start=opening_span[1],
                    protected_body=body,
                    opening=opening,
                    reset=reset,
                )
            )

    if not groups:
        return _ProviderProjection(source)

    groups.sort(key=lambda item: item.start)
    previous_end = -1
    aliased_terms: set[str] = set()
    for group in groups:
        if group.start < previous_end or any(
            term_token in aliased_terms for term_token in group.term_tokens
        ):
            raise TranslationError("内部エラー: 装飾範囲が重複しています")
        previous_end = group.end
        aliased_terms.update(group.term_tokens)

    # Protector placeholders grow upward from 0000. Allocate projection-only
    # tokens downward and exclude every MQP-shaped value already in scope.
    reserved = set(_MQP_PLACEHOLDER.findall(source))
    reserved.update(_MQP_PLACEHOLDER.findall(protected.original))
    for replacement in protected.replacements.values():
        reserved.update(_MQP_PLACEHOLDER.findall(replacement))

    next_index = 0xFFFF
    projected_groups: list[tuple[_ProjectionCandidate, str]] = []
    for group in groups:
        if group.local_suffix:
            projected_groups.append((group, ""))
            continue
        provider_token = ""
        while next_index >= 0:
            candidate = f"__MQP_{next_index:04X}__"
            next_index -= 1
            if candidate not in reserved:
                provider_token = candidate
                reserved.add(candidate)
                break
        if not provider_token:
            raise TranslationError("内部エラー: 装飾範囲の不可分tokenを割り当てられません")
        projected_groups.append((group, provider_token))

    projected = source
    for group, provider_token in reversed(projected_groups):
        projected = projected[: group.start] + provider_token + projected[group.end :]

    styled_index = 0
    styled_bodies: list[_StyledBodyProjection] = []
    local_suffix_bodies: list[_StyledBodyProjection] = []
    for group, provider_token in projected_groups:
        if not group.body_source:
            continue
        styled = _StyledBodyProjection(
            part_id=f"{part_id}::styled::{styled_index:04d}",
            provider_token=provider_token,
            source_text=group.body_source,
            opening=group.opening,
            reset=group.reset,
            source_start=group.body_source_start,
            protected_body=group.protected_body,
        )
        if group.local_suffix:
            local_suffix_bodies.append(styled)
        else:
            styled_bodies.append(styled)
        styled_index += 1

    return _ProviderProjection(
        text=projected,
        term_token_aliases=tuple(
            (term_token, provider_token)
            for group, provider_token in projected_groups
            for term_token in group.term_tokens
        ),
        expansions=tuple(
            (provider_token, group.fixed_expansion)
            for group, provider_token in projected_groups
            if provider_token and group.fixed_expansion
        ),
        symbol_bindings=tuple(
            (provider_token, group.symbol_body)
            for group, provider_token in projected_groups
            if provider_token and group.symbol_body
        ),
        styled_bodies=tuple(styled_bodies),
        local_suffix_bodies=tuple(local_suffix_bodies),
        fixed_suffix="".join(
            group.fixed_expansion for group, _token in projected_groups
            if group.local_suffix and group.fixed_expansion
        ),
    )


def _provider_item(
    part: _PreparedPart,
    *,
    context: str | None = None,
) -> dict[str, Any]:
    """Build one provider item with semantic tokens documented as reference data.

    The model must output the opaque token, not either reference string.  The
    bindings explain which source phrase each movable term or complete styled
    span represents, preventing adjacent ordinary words from being attached to
    a different token after target-language word-order changes.
    """

    item: dict[str, Any] = {
        "id": part.id,
        "text": part.provider_projection.text,
        "context": (part.context if context is None else context) + part.provider_context,
        # Local-only evidence for binding a protected/nested child to its
        # original scope. The API adapter does not serialize this field.
        "_source_text": part.protected.original,
    }
    bindings: list[dict[str, str]] = []
    if not (
        len(part.protected.term_placeholders)
        == len(part.protected.term_source_spans)
        == len(part.protected.term_group_ids)
    ):
        raise TranslationError("内部エラー: 固有名詞の参照情報を生成できません")
    fragments_by_provider_token: dict[
        str, list[tuple[str, int, int]]
    ] = {}
    for placeholder, (start, end) in zip(
        part.protected.term_placeholders,
        part.protected.term_source_spans,
        strict=True,
    ):
        if placeholder not in part.protected.replacements or not (
            0 <= start < end <= len(part.protected.original)
        ):
            raise TranslationError("内部エラー: 固有名詞の参照情報を生成できません")
        provider_token = part.provider_projection.provider_token_for(placeholder)
        if provider_token not in part.provider_projection.text:
            # Protected terminal names are restored locally and must not be
            # advertised as tokens the provider can output or reposition.
            continue
        fragments_by_provider_token.setdefault(provider_token, []).append(
            (placeholder, start, end)
        )
    for provider_token, fragments in fragments_by_provider_token.items():
        if len(fragments) == 1:
            placeholder, start, end = fragments[0]
            source_term = part.protected.original[start:end]
            approved_output = part.protected.replacements[placeholder]
        else:
            source_start = min(start for _placeholder, start, _end in fragments)
            source_end = max(end for _placeholder, _start, end in fragments)
            source_term = visible_terminology_text(
                [part.protected.original[source_start:source_end]]
            )
            first_position = part.protected.protected.find(fragments[0][0])
            last_placeholder = fragments[-1][0]
            last_position = part.protected.protected.find(last_placeholder)
            if first_position < 0 or last_position < first_position:
                raise TranslationError("内部エラー: 固有名詞groupを参照情報へ対応付けられません")
            approved_template = part.protected.protected[
                first_position : last_position + len(last_placeholder)
            ]
            for placeholder in _MQP_PLACEHOLDER.findall(approved_template):
                replacement = part.protected.replacements.get(placeholder)
                if replacement is None:
                    raise TranslationError(
                        "内部エラー: 固有名詞groupの保護tokenを復元できません"
                    )
                approved_template = approved_template.replace(
                    placeholder,
                    replacement,
                )
            approved_output = visible_terminology_text([approved_template])
            if not source_term or not approved_output:
                raise TranslationError("内部エラー: 固有名詞groupの参照情報が空です")
        bindings.append(
            {
                "token": provider_token,
                "source_term": source_term,
                "approved_output": approved_output,
            }
        )
    bindings.extend(
        {"token": token, "source_term": symbol, "approved_output": symbol}
        for token, symbol in part.provider_projection.symbol_bindings
    )
    if bindings:
        item["term_bindings"] = bindings
    if part.provider_projection.styled_bodies:
        item["styled_bindings"] = [
            {
                "token": styled.provider_token,
                "source_text": styled.source_text,
                "body_item_id": styled.part_id,
            }
            for styled in part.provider_projection.styled_bodies
        ]
    return item


def _restore_translated_part(
    part: _PreparedPart,
    response: str,
    source_locale: str,
    target_locale: str,
    translated_parts: Mapping[str, str],
) -> str:
    response = part.protected.remove_adjacent_term_echoes(response)
    provider_tokens = tuple(_MQP_PLACEHOLDER.findall(part.provider_projection.text))
    provider_replacements = dict.fromkeys(provider_tokens, "")
    source_requires_body = _text_requires_translated_body(
        part.provider_projection.text,
        provider_replacements,
        _provider_content_placeholders(part),
        source_locale,
        has_local_content=bool(part.provider_projection.local_suffix_bodies),
    ) or has_protected_possessive_suffix(
        part.provider_projection.text, part.protected.term_placeholders
    )
    response_has_body = _has_unprotected_meaningful_text(
        response,
        provider_replacements,
    ) or has_protected_possessive_suffix(response, part.protected.term_placeholders)
    connector_punctuation = part.raw_json_fragment and _raw_json_connector_is_safe(
        part.protected.original,
        response,
        source_locale,
        target_locale,
    )
    if source_requires_body and not response_has_body and not connector_punctuation:
        raise TranslationError("OpenAI の応答から翻訳本文が失われました")
    response = part.provider_projection.expand(response, translated_parts)
    response = part.protected.restore_source_term_spacing(response)
    restored = part.protected.restore(
        response, allow_omitted_determiners=_is_english_locale(source_locale),
    )
    if part.json_stream_plan is not None:
        part.json_stream_plan.validate_skeleton(restored)
    unicode_issue = translation_unicode_issue(
        part.protected.protected,
        response,
        target_locale,
    )
    if unicode_issue is not None:
        raise TranslationError(unicode_issue)
    if part.parent_token_aliases:
        aliases = dict(part.parent_token_aliases)
        return _MQP_PLACEHOLDER.sub(lambda match: aliases[match.group()], response)
    return restored


def _bundle_parts(root: _PreparedPart) -> tuple[_PreparedPart, ...]:
    return (*(
        part for child in root.styled_parts for part in _bundle_parts(child)
    ), root)


def _part_for_item_id(
    root: _PreparedPart,
    item_id: str | None,
) -> _PreparedPart:
    if item_id is not None:
        for part in _bundle_parts(root):
            if part.id == item_id:
                return part
    return root


def _restore_bundle_response(
    root: _PreparedPart,
    response: Mapping[str, str],
    source_locale: str,
    target_locale: str,
    translated_parts: Mapping[str, str],
) -> tuple[
    dict[str, str],
    tuple[_PreparedPart, TranslationError] | None,
]:
    local_parts = dict(translated_parts)
    restored_parts: dict[str, str] = {}
    for part in _bundle_parts(root):
        candidate = response.get(part.id)
        if not isinstance(candidate, str):
            return {}, (
                part,
                TranslationError("OpenAI の応答に対応する翻訳文字列がありません"),
            )
        try:
            restored = _restore_translated_part(
                part,
                candidate,
                source_locale,
                target_locale,
                local_parts,
            )
        except TranslationError as exc:
            return {}, (part, exc)
        local_parts[part.id] = restored
        restored_parts[part.id] = restored
    if not root.raw_json_fragment and root.json_stream_plan is None:
        quality_issue = japanese_word_order_issue(
            root.protected.original, restored_parts[root.id], source_locale, target_locale,
        )
        if quality_issue is None:
            quality_issue = image_title_translation_issue(
                root.unit_key, root.protected.original, restored_parts[root.id], target_locale,
            )
        if quality_issue:
            return {}, (root, TranslationError(quality_issue))
    return restored_parts, None


def _source_requires_translated_body(
    protected: ProtectedText,
    source_locale: str,
) -> bool:
    return _text_requires_translated_body(
        protected.protected,
        protected.replacements,
        protected.content_placeholders,
        source_locale,
    )


def _provider_content_placeholders(part: _PreparedPart) -> tuple[str, ...]:
    content = [
        part.provider_projection.provider_token_for(placeholder)
        for placeholder in part.protected.content_placeholders
    ]
    content.extend(
        styled.provider_token for styled in part.provider_projection.styled_bodies
    )
    content.extend(token for token, _symbol in part.provider_projection.symbol_bindings)
    return tuple(dict.fromkeys(content))


def _text_requires_translated_body(
    text: str,
    replacements: Mapping[str, str],
    content_placeholders: tuple[str, ...],
    source_locale: str,
    *,
    has_local_content: bool = False,
) -> bool:
    if not _has_unprotected_meaningful_text(text, replacements):
        return False
    if not _is_english_locale(source_locale):
        return True
    if not content_placeholders and not has_local_content:
        return True
    visible = text
    for placeholder in replacements:
        visible = visible.replace(placeholder, "")
    words = _WORD_TOKEN.findall(visible)
    return not (
        words
        and all(
            word.isascii()
            and word.isalpha()
            and word.casefold() in _OMISSIBLE_ENGLISH_DETERMINERS
            for word in words
        )
    )


def _is_english_locale(locale: str) -> bool:
    normalized = locale.strip().lower().replace("-", "_")
    return normalized == "en" or normalized.startswith("en_")


def _raw_json_connector_is_safe(
    source: str,
    candidate: str,
    source_locale: str,
    target_locale: str,
) -> bool:
    """Allow a Japanese list delimiter for one isolated English ``and`` leaf.

    The caller must prove this is a raw JSON text fragment. A comma can join
    the neighbouring styled list items without any alphanumeric characters;
    empty output, ordinary prose, and disjunctions still require translated
    text. This does not replace the normal syntax/Unicode validation.
    """

    target = target_locale.strip().lower().replace("-", "_")
    return (
        _is_english_locale(source_locale)
        and (target == "ja" or target.startswith("ja_"))
        and _ENGLISH_LIST_CONNECTOR.fullmatch(source) is not None
        and candidate.strip(" \u3000") in {"、", "，"}
    )


def _has_unprotected_meaningful_text(text: str, replacements: Mapping[str, str]) -> bool:
    for placeholder in replacements:
        text = text.replace(placeholder, "")
    return any(character.isalnum() for character in text)


def _part_location_summary(part: _PreparedPart) -> str:
    details = [part.context, f"キー: {part.unit_key}"]
    if part.source_path:
        details.append(f"原文ファイル: {part.source_path}")
    return " / ".join(details)


def _part_location_details(part: _PreparedPart) -> str:
    details = [f"対象: {part.context}", f"キー: {part.unit_key}"]
    if part.source_path:
        details.append(f"原文ファイル: {part.source_path}")
    return "\n".join(details)


def _unit_location_summary(unit: TranslationUnit) -> str:
    details = [unit.context or unit.key, f"キー: {unit.key}"]
    if unit.source_path:
        details.append(f"原文ファイル: {unit.source_path}")
    return " / ".join(details)


def _unit_location_details(unit: TranslationUnit) -> str:
    details = [f"対象: {unit.context or unit.key}", f"キー: {unit.key}"]
    if unit.source_path:
        details.append(f"原文ファイル: {unit.source_path}")
    return "\n".join(details)


def _project_terminology_groups(
    project: TranslationProject,
) -> tuple[tuple[str, ...], ...]:
    """Validate adapter-provided visible-stream groups and return unit IDs."""

    raw_groups = project.metadata.get("terminology_groups", ())
    if raw_groups in (None, ()):
        return ()
    if not isinstance(raw_groups, (list, tuple)):
        raise TranslationError("内部エラー: 固有名詞の表示グループ形式が不正です")

    known_ids = {unit.id for unit in project.units}
    occupied: set[str] = set()
    groups: list[tuple[str, ...]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, (list, tuple)) or not raw_group:
            raise TranslationError("内部エラー: 固有名詞の表示グループが空または不正です")
        group = tuple(raw_group)
        if not all(isinstance(unit_id, str) and unit_id for unit_id in group):
            raise TranslationError("内部エラー: 固有名詞の表示グループIDが不正です")
        if len(set(group)) != len(group) or any(unit_id in occupied for unit_id in group):
            raise TranslationError("内部エラー: 翻訳単位が複数の固有名詞表示グループに重複しています")
        unknown = [unit_id for unit_id in group if unit_id not in known_ids]
        if unknown:
            raise TranslationError(
                "内部エラー: 固有名詞の表示グループに不明な翻訳単位があります: "
                + ", ".join(unknown)
            )
        occupied.update(group)
        groups.append(group)
    return tuple(groups)


def _project_reference_term_groups(
    project: TranslationProject,
) -> tuple[tuple[str, ...], ...]:
    """Validate adapter-provided primary title streams used as reference names."""

    raw_groups = project.metadata.get("reference_term_groups", ())
    if raw_groups in (None, ()):
        return ()
    if not isinstance(raw_groups, (list, tuple)):
        raise TranslationError("内部エラー: 未選択タイトルの参照グループ形式が不正です")

    known_ids = {unit.id for unit in project.units}
    occupied: set[str] = set()
    groups: list[tuple[str, ...]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, (list, tuple)) or not raw_group:
            raise TranslationError(
                "内部エラー: 未選択タイトルの参照グループが空または不正です"
            )
        group = tuple(raw_group)
        if not all(isinstance(unit_id, str) and unit_id for unit_id in group):
            raise TranslationError(
                "内部エラー: 未選択タイトルの参照グループIDが不正です"
            )
        if len(set(group)) != len(group) or any(
            unit_id in occupied for unit_id in group
        ):
            raise TranslationError(
                "内部エラー: 翻訳単位が複数の未選択タイトル参照グループに重複しています"
            )
        unknown = [unit_id for unit_id in group if unit_id not in known_ids]
        if unknown:
            raise TranslationError(
                "内部エラー: 未選択タイトルの参照グループに不明な翻訳単位があります: "
                + ", ".join(unknown)
            )
        occupied.update(group)
        groups.append(group)
    return tuple(groups)


def _project_reference_terms(
    project: TranslationProject,
    selected_units: list[TranslationUnit],
    reference_groups: tuple[tuple[str, ...], ...],
) -> tuple[str, ...]:
    """Collect unchecked title/name spellings without selecting them for output."""

    units = {unit.id: unit for unit in project.units}
    selected_ids = {unit.id for unit in selected_units}
    grouped_ids: set[str] = set()
    terms: list[str] = []
    selected_title_terms: set[str] = set()

    for group in reference_groups:
        grouped_ids.update(group)
        group_units = [units[unit_id] for unit_id in group]
        if not all(
            unit.category in _PROJECT_REFERENCE_SOURCE_CATEGORIES
            for unit in group_units
        ):
            continue
        term = visible_terminology_text(unit.source for unit in group_units)
        if not term:
            continue
        if any(unit.id in selected_ids for unit in group_units):
            selected_title_terms.add(term)
        else:
            terms.append(term)

    for unit in project.units:
        if (
            unit.id in grouped_ids
            or unit.category not in _PROJECT_REFERENCE_SOURCE_CATEGORIES
        ):
            continue
        term = _unit_project_reference_text(unit)
        if not term:
            continue
        if unit.id in selected_ids:
            selected_title_terms.add(term)
        else:
            terms.append(term)

    # Preserve deterministic source order while collapsing duplicate names.
    return tuple(
        term for term in dict.fromkeys(terms) if term not in selected_title_terms
    )


def _unit_project_reference_text(unit: TranslationUnit) -> str:
    """Return one safe primary visible title, excluding auxiliary JSON surfaces."""

    if not looks_like_raw_json_text(unit.source):
        return visible_terminology_text([unit.source])
    try:
        component = json.loads(unit.source)
    except ValueError:
        # The unchecked value is not sent to OpenAI, so a malformed component
        # should only disable reference protection rather than block the job.
        return ""
    main_stream, _auxiliary = _component_main_stream(component, ())
    if None in main_stream:
        return ""
    streams = _split_component_stream(main_stream)
    if len(streams) != 1 or not streams[0]:
        return ""
    parts = [_get_path(component, path) for path in streams[0]]
    if not all(isinstance(part, str) for part in parts):
        return ""
    return visible_terminology_text(parts)


def _glossary_for_terminology_group(
    units: Mapping[str, TranslationUnit],
    group: tuple[str, ...],
    glossary: GlossaryCatalog,
    project_reference_glossary: GlossaryCatalog | None,
) -> GlossaryCatalog:
    if project_reference_glossary is not None and all(
        units[unit_id].category in _PROJECT_REFERENCE_TARGET_CATEGORIES
        for unit_id in group
    ):
        return project_reference_glossary
    return glossary


def _project_terminology_spans(
    project: TranslationProject,
    glossary: GlossaryCatalog,
    groups: tuple[tuple[str, ...], ...],
    project_reference_glossary: GlossaryCatalog | None = None,
) -> dict[str, list[TermReplacement]]:
    units = {unit.id: unit for unit in project.units}
    spans: dict[str, list[TermReplacement]] = {}
    for group in groups:
        group_glossary = _glossary_for_terminology_group(
            units,
            group,
            glossary,
            project_reference_glossary,
        )
        group_spans = group_glossary.replacement_spans_for_parts(
            [units[unit_id].source for unit_id in group]
        )
        spans.update(zip(group, group_spans, strict=True))
    return spans


def _project_existing_group_safety(
    project: TranslationProject,
    glossary: GlossaryCatalog,
    source_locale: str,
    target_locale: str,
    groups: tuple[tuple[str, ...], ...],
    project_reference_glossary: GlossaryCatalog | None = None,
) -> dict[str, bool]:
    units = {unit.id: unit for unit in project.units}
    safety: dict[str, bool] = {}
    for group in groups:
        group_glossary = _glossary_for_terminology_group(
            units,
            group,
            glossary,
            project_reference_glossary,
        )
        source_parts = [units[unit_id].source for unit_id in group]
        candidate_parts: list[str] = []
        individually_safe = True
        for unit_id, source in zip(group, source_parts, strict=True):
            existing = project.existing.get(unit_id, "")
            if existing and existing != source:
                candidate_parts.append(existing)
                individually_safe = individually_safe and _existing_translation_is_safe(
                    source,
                    existing,
                    group_glossary,
                    source_locale,
                    target_locale,
                    treat_as_plain=True,
                )
            else:
                candidate_parts.append(source)
        group_safe = individually_safe and group_glossary.candidate_preserves_term_layout(
            source_parts,
            candidate_parts,
        )
        safety.update((unit_id, group_safe) for unit_id in group)
    return safety


def _first_unsafe_terminology_group(
    project: TranslationProject,
    resolved: Mapping[str, str],
    glossary: GlossaryCatalog,
    groups: tuple[tuple[str, ...], ...],
    project_reference_glossary: GlossaryCatalog | None = None,
) -> tuple[str, ...] | None:
    units = {unit.id: unit for unit in project.units}
    for group in groups:
        group_glossary = _glossary_for_terminology_group(
            units,
            group,
            glossary,
            project_reference_glossary,
        )
        source_parts = [units[unit_id].source for unit_id in group]
        # Missing target entries intentionally use the source-language fallback.
        candidate_parts = [
            resolved.get(unit_id, units[unit_id].source) for unit_id in group
        ]
        if not group_glossary.candidate_preserves_term_layout(
            source_parts,
            candidate_parts,
        ):
            return group
    return None


def _make_prepared_part(
    *,
    part_id: str,
    protected: ProtectedText,
    context: str,
    unit_key: str,
    source_path: str,
    protector: TokenProtector,
    raw_json_fragment: bool = False,
) -> _PreparedPart:
    projection = _build_provider_projection(protected, part_id)
    styled_parts: list[_PreparedPart] = []
    projected_bodies = (
        *projection.styled_bodies,
        *projection.local_suffix_bodies,
    )
    for index, styled in enumerate(projected_bodies, start=1):
        body_end = styled.source_start + len(styled.source_text)
        body_terms = [
            TermReplacement(start - styled.source_start, end - styled.source_start,
                            protected.replacements[token], group_id)
            for token, (start, end), group_id in zip(
                protected.term_placeholders, protected.term_source_spans,
                protected.term_group_ids, strict=True,
            )
            if styled.source_start >= 0 and styled.source_start <= start < end <= body_end
        ]
        body_protected = protector.protect(styled.source_text, term_spans=body_terms)
        aliases: tuple[tuple[str, str], ...] = ()
        if styled.protected_body:
            child_tokens = _MQP_PLACEHOLDER.findall(body_protected.protected)
            parent_tokens = _MQP_PLACEHOLDER.findall(styled.protected_body)
            if len(child_tokens) != len(parent_tokens):
                raise TranslationError("内部エラー: 装飾本文の保護対象を親へ対応付けられません")
            aliases = tuple(zip(child_tokens, parent_tokens, strict=True))
            alias_map = dict(aliases)
            if (
                _MQP_PLACEHOLDER.sub(lambda match: alias_map[match.group()], body_protected.protected)
                != styled.protected_body
                or any(body_protected.replacements[child] != protected.replacements[parent]
                       for child, parent in aliases)
            ):
                raise TranslationError("内部エラー: 装飾本文の保護対象が原文と一致しません")
        elif body_protected.protected != styled.source_text or body_protected.replacements:
            raise TranslationError("内部エラー: 装飾本文に未分離の保護対象が含まれています")
        child = _make_prepared_part(
            part_id=styled.part_id,
            protected=body_protected,
            context=f"{context}, styled text {index}",
            unit_key=unit_key,
            source_path=source_path,
            protector=protector,
        )
        styled_parts.append(_PreparedPart(
            id=child.id, protected=child.protected, provider_projection=child.provider_projection,
            context=child.context, unit_key=child.unit_key, source_path=child.source_path,
            styled_parts=child.styled_parts, parent_token_aliases=aliases,
        ))
    return _PreparedPart(
        id=part_id,
        protected=protected,
        provider_projection=projection,
        context=context,
        unit_key=unit_key,
        source_path=source_path,
        styled_parts=tuple(styled_parts),
        raw_json_fragment=raw_json_fragment,
    )


def _prepare_unit(
    unit: TranslationUnit,
    protector: TokenProtector,
    glossary: GlossaryCatalog,
    terminology_spans: list[TermReplacement] | None = None,
) -> _PreparedUnit:
    # Adapter-provided terminology groups describe already-extracted visible
    # text leaves. A leaf may itself look like JSON, but it is still literal
    # catalog text and must not be reinterpreted as a nested component.
    if terminology_spans is None and looks_like_raw_json_text(unit.source):
        try:
            template = json.loads(unit.source)
        except ValueError as exc:
            raise TranslationError(
                f"raw JSON text を解析できないため安全に翻訳できません: {unit.key}"
            ) from exc
        flat_stream = _flat_json_stream_plan(template, glossary)
        if flat_stream is not None:
            plan, terminology = flat_stream
            return _prepare_flat_json_stream(unit, template, plan, terminology, protector, glossary)
        streams = _component_text_streams(template)
        paths = [path for stream in streams for path in stream]
        terminology_by_path: dict[tuple[object, ...], list[TermReplacement]] = {}
        for stream in streams:
            stream_sources = [_get_path(template, path) for path in stream]
            stream_terminology = glossary.replacement_spans_for_parts(
                [source if isinstance(source, str) else "" for source in stream_sources]
            )
            terminology_by_path.update(zip(stream, stream_terminology, strict=True))
        parts: list[_PreparedPart] = []
        path_by_part: dict[str, tuple[object, ...]] = {}
        for index, path in enumerate(paths):
            source = _get_path(template, path)
            if not isinstance(source, str) or not should_translate(source):
                continue
            part_id = f"{unit.id}-p{index:03d}"
            protected = protector.protect(
                source,
                term_spans=terminology_by_path[path],
            )
            parts.append(
                _make_prepared_part(
                    part_id=part_id,
                    protected=protected,
                    context=f"{unit.context or unit.key}, JSON text part {index + 1}",
                    unit_key=unit.key,
                    source_path=unit.source_path,
                    protector=protector,
                    raw_json_fragment=True,
                )
            )
            path_by_part[part_id] = path
        return _PreparedUnit(unit, template, path_by_part, parts, glossary)

    if not should_translate(unit.source):
        return _PreparedUnit(unit, None, {}, [], glossary)
    protected = protector.protect(
        unit.source,
        term_spans=(
            terminology_spans
            if terminology_spans is not None
            else glossary.replacement_spans_for_parts([unit.source])[0]
        ),
    )
    part = _make_prepared_part(
        part_id=unit.id,
        protected=protected,
        context=unit.context or unit.key,
        unit_key=unit.key,
        source_path=unit.source_path,
        protector=protector,
    )
    return _PreparedUnit(unit, None, {part.id: ()}, [part], glossary)


def _flat_json_stream_plan(
    template: Any, glossary: GlossaryCatalog,
) -> tuple[FlatJsonStreamPlan, list[list[TermReplacement]]] | None:
    plan = FlatJsonStreamPlan.create(template)
    if plan is None:
        return None
    terminology = glossary.replacement_spans_for_parts(list(plan.source_texts))
    # A name split across adjacent styled nodes is safe: both node text and
    # component order stay fixed. A name spanning a freely translated string
    # gap must instead retain the conservative per-leaf layout validation.
    if any(
        span.group_id is not None
        for index, spans in enumerate(terminology)
        if isinstance(template[index], str)
        for span in spans
    ):
        return None
    return plan, terminology


def _prepare_flat_json_stream(
    unit: TranslationUnit,
    template: list[Any],
    plan: FlatJsonStreamPlan,
    terminology: list[list[TermReplacement]],
    protector: TokenProtector,
    glossary: GlossaryCatalog,
) -> _PreparedUnit:
    parts: list[_PreparedPart] = []
    paths: dict[str, tuple[object, ...]] = {}
    for index in plan.node_indices:
        source = plan.source_texts[index]
        if not should_translate(source):
            continue
        part_id = f"{unit.id}-p{index:03d}"
        parts.append(_make_prepared_part(
            part_id=part_id,
            protected=protector.protect(source, term_spans=terminology[index]),
            context=f"{unit.context or unit.key}, JSON styled component {index + 1}",
            unit_key=unit.key, source_path=unit.source_path, protector=protector,
        ))
        paths[part_id] = (index, "text")
    node_markers = plan.node_markers
    skeleton_terms: list[TermReplacement] = []
    offset = 0
    for index, source in enumerate(plan.source_texts):
        if index in node_markers:
            offset += len(node_markers[index])
            continue
        skeleton_terms.extend(
            TermReplacement(offset + span.start, offset + span.end, span.replacement)
            for span in terminology[index]
        )
        offset += len(source)
    protected = protector.protect(plan.skeleton, term_spans=skeleton_terms)
    references = [
        {
            "token": token,
            "source_text": "".join(
                plan.source_texts[index]
                for index in plan.node_groups[plan.markers.index(value)]
            ),
        }
        for token, value in protected.replacements.items() if value in plan.markers
    ]
    root_id = f"{unit.id}-json-stream"
    root = _make_prepared_part(
        part_id=root_id, protected=protected,
        context=f"{unit.context or unit.key}, JSON visible sentence",
        unit_key=unit.key, source_path=unit.source_path, protector=protector,
    )
    parts.append(replace(
        root, json_stream_plan=plan,
        provider_context=(
            ". "
            "Component tokens keep their order and styling; translate the surrounding "
            "sentence together. Adjacent component tokens without source prose between "
            "them must remain adjacent. Component reference data: "
            + json.dumps(references, ensure_ascii=False)
        ),
    ))
    return _PreparedUnit(unit, template, paths, parts, glossary, plan, root_id)


def _existing_flat_json_translation_is_safe(
    template: list[Any], candidate: Any, plan: FlatJsonStreamPlan,
    terminology: list[list[TermReplacement]], glossary: GlossaryCatalog,
    source_locale: str, target_locale: str,
) -> bool:
    skeleton = plan.candidate_skeleton(template, candidate)
    if skeleton is None:
        return False
    try:
        plan.validate_skeleton(skeleton)
    except TranslationError:
        return False
    if not (
        _existing_text_syntax_is_compatible(plan.skeleton, skeleton, glossary, source_locale)
        and _existing_text_unicode_is_compatible(plan.skeleton, skeleton, glossary, target_locale)
        and glossary.candidate_preserves_term_layout(
            list(plan.source_texts), plan.texts(candidate),
        )
    ):
        return False
    for index in plan.node_indices:
        source = plan.source_texts[index]
        translated = candidate[index]["text"]
        if not should_translate(source):
            if translated != source:
                return False
            continue
        # Names split across nodes have occurrence-specific fragment protection;
        # independently looking them up would incorrectly choose standalone
        # glossary translations instead of preserving the original fragments.
        protected = TokenProtector().protect(source, term_spans=terminology[index])
        visible = translated
        for value in protected.replacements.values():
            # Each occurrence was checked by the stream-wide terminology
            # layout validator. Remove only that occurrence, not incidental
            # matching prose elsewhere in the same styled label.
            visible = visible.replace(value, "", 1)
        if (
            protected_syntax_ranges(translated)
            or (_source_requires_translated_body(protected, source_locale)
                and not any(character.isalnum() for character in visible))
            or not _existing_text_unicode_is_compatible(source, translated, glossary, target_locale)
        ):
            return False
    return True


def _component_text_streams(
    value: Any,
    path: tuple[object, ...] = (),
) -> list[list[tuple[object, ...]]]:
    """Return text-leaf paths grouped by one visible concatenation stream.

    A component's ``text`` and ``extra`` children render as one stream. Each
    ``with`` argument is an independent substitution value, and hover text is
    a separate display surface. Keeping those boundaries prevents a Mod name
    from being invented across unrelated leaves while still recognizing names
    split by styled ``extra`` components.
    """

    main, auxiliary = _component_main_stream(value, path)
    return _split_component_stream(main) + auxiliary


def _component_main_stream(
    value: Any,
    path: tuple[object, ...],
) -> tuple[
    list[tuple[object, ...] | None],
    list[list[tuple[object, ...]]],
]:
    if isinstance(value, str):
        # An empty literal renders nothing and therefore must not break a term
        # split across surrounding styled components.
        return ([path] if value else []), []
    if isinstance(value, list):
        main: list[tuple[object, ...] | None] = []
        auxiliary: list[list[tuple[object, ...]]] = []
        for index, item in enumerate(value):
            if not isinstance(item, (str, list, dict)):
                # Invalid or future component values may still render visible
                # content. Never join literal terms across an unknown value.
                main.append(None)
                continue
            child_main, child_auxiliary = _component_main_stream(
                item,
                path + (index,),
            )
            main.extend(child_main)
            auxiliary.extend(child_auxiliary)
        return main, auxiliary
    if not isinstance(value, dict):
        return [], []

    main: list[tuple[object, ...] | None] = []
    auxiliary = []
    has_dynamic_content = any(field in value for field in _DYNAMIC_COMPONENT_FIELDS)
    if isinstance(value.get("text"), str):
        # Minecraft's object component parser gives a literal text member
        # precedence when multiple content discriminators are present. Keep
        # translating that visible leaf while preserving the other metadata.
        if value["text"]:
            main.append(path + ("text",))
    elif has_dynamic_content:
        main.append(None)
    elif "text" in value:
        # A malformed literal discriminator is not a transparent style-only
        # wrapper. Treat it as visible unknown content and fail closed.
        main.append(None)

    if "extra" in value:
        extra_main, extra_auxiliary = _component_main_stream(
            value["extra"],
            path + ("extra",),
        )
        main.extend(extra_main)
        auxiliary.extend(extra_auxiliary)

    if "with" in value:
        auxiliary.extend(
            _independent_component_streams(value["with"], path + ("with",))
        )

    if (
        "separator" in value
        and has_dynamic_content
    ):
        separator_main, separator_auxiliary = _component_main_stream(
            value["separator"],
            path + ("separator",),
        )
        auxiliary.extend(_split_component_stream(separator_main))
        auxiliary.extend(separator_auxiliary)

    hover = value.get("hoverEvent")
    if isinstance(hover, dict) and hover.get("action") == "show_text":
        for key in ("contents", "value"):
            if key in hover:
                hover_main, hover_auxiliary = _component_main_stream(
                    hover[key],
                    path + ("hoverEvent", key),
                )
                auxiliary.extend(_split_component_stream(hover_main))
                auxiliary.extend(hover_auxiliary)
    return main, auxiliary


def _independent_component_streams(
    value: Any,
    path: tuple[object, ...],
) -> list[list[tuple[object, ...]]]:
    items = enumerate(value) if isinstance(value, list) else ((None, value),)
    streams: list[list[tuple[object, ...]]] = []
    for index, item in items:
        if not isinstance(item, (str, list, dict)):
            continue
        item_path = path + ((index,) if index is not None else ())
        item_main, item_auxiliary = _component_main_stream(item, item_path)
        streams.extend(_split_component_stream(item_main))
        streams.extend(item_auxiliary)
    return streams


def _split_component_stream(
    tokens: list[tuple[object, ...] | None],
) -> list[list[tuple[object, ...]]]:
    """Split literal leaves at visible components which have no text leaf."""

    streams: list[list[tuple[object, ...]]] = []
    current: list[tuple[object, ...]] = []
    for token in tokens:
        if token is None:
            if current:
                streams.append(current)
                current = []
        else:
            current.append(token)
    if current:
        streams.append(current)
    return streams


def _get_path(value: Any, path: tuple[object, ...]) -> Any:
    current = value
    for segment in path:
        current = current[segment]
    return current


def _set_path(value: Any, path: tuple[object, ...], replacement: str) -> Any:
    if not path:
        return replacement
    current = value
    for segment in path[:-1]:
        current = current[segment]
    current[path[-1]] = replacement
    return value


def _json_values_match_exactly(source: Any, candidate: Any) -> bool:
    """Compare JSON structure without Python's bool/int and int/float coercion."""

    if type(source) is not type(candidate):
        return False
    if isinstance(source, dict):
        return source.keys() == candidate.keys() and all(
            _json_values_match_exactly(source[key], candidate[key]) for key in source
        )
    if isinstance(source, list):
        return len(source) == len(candidate) and all(
            _json_values_match_exactly(source_item, candidate_item)
            for source_item, candidate_item in zip(source, candidate)
        )
    return source == candidate


def _existing_translation_is_safe(
    source: str,
    candidate: str,
    glossary: GlossaryCatalog | None = None,
    source_locale: str = "en_us",
    target_locale: str = "ja_jp",
    *,
    unit_key: str = "",
    treat_as_plain: bool = False,
) -> bool:
    if treat_as_plain or not looks_like_raw_json_text(source):
        if not should_translate(source):
            return candidate == source
        if japanese_word_order_issue(source, candidate, source_locale, target_locale):
            return False
        if image_title_translation_issue(unit_key, source, candidate, target_locale):
            return False
        return (
            _existing_text_syntax_is_compatible(
                source,
                candidate,
                glossary,
                source_locale,
            )
            and (
                glossary is None
                or glossary.candidate_preserves_terms([source], [candidate])
            )
            and _existing_text_unicode_is_compatible(
                source,
                candidate,
                glossary,
                target_locale,
            )
        )
    try:
        source_component = json.loads(source)
        candidate_component = json.loads(candidate)
    except ValueError:
        return False
    flat_stream = _flat_json_stream_plan(source_component, glossary or GlossaryCatalog())
    if flat_stream is not None:
        plan, terminology = flat_stream
        return _existing_flat_json_translation_is_safe(
            source_component, candidate_component, plan, terminology,
            glossary or GlossaryCatalog(), source_locale, target_locale,
        )
    source_streams = _component_text_streams(source_component)
    candidate_streams = _component_text_streams(candidate_component)
    if source_streams != candidate_streams:
        return False
    source_paths = [path for stream in source_streams for path in stream]
    candidate_paths = [path for stream in candidate_streams for path in stream]
    source_text_parts = [_get_path(source_component, path) for path in source_paths]
    candidate_text_parts = [_get_path(candidate_component, path) for path in candidate_paths]
    if glossary is not None:
        for source_stream, candidate_stream in zip(
            source_streams,
            candidate_streams,
            strict=True,
        ):
            source_strings = [
                _get_path(source_component, path) for path in source_stream
            ]
            candidate_strings = [
                _get_path(candidate_component, path) for path in candidate_stream
            ]
            if not all(
                isinstance(part, str)
                for part in (*source_strings, *candidate_strings)
            ) or not glossary.candidate_preserves_term_layout(
                source_strings,
                candidate_strings,
            ):
                return False
    source_normalized = copy.deepcopy(source_component)
    candidate_normalized = copy.deepcopy(candidate_component)
    for index, path in enumerate(source_paths):
        source_text = _get_path(source_component, path)
        candidate_text = _get_path(candidate_component, path)
        if not isinstance(source_text, str) or not isinstance(candidate_text, str):
            return False
        if not should_translate(source_text):
            if candidate_text != source_text:
                return False
        elif not (
            _existing_text_syntax_is_compatible(
                source_text,
                candidate_text,
                glossary,
                source_locale,
            )
            or _raw_json_connector_is_safe(
                source_text,
                candidate_text,
                source_locale,
                target_locale,
            )
        ):
            return False
        elif not _existing_text_unicode_is_compatible(
            source_text,
            candidate_text,
            glossary,
            target_locale,
        ):
            return False
        marker = f"__MQ_TEXT_{index:04d}__"
        source_normalized = _set_path(source_normalized, path, marker)
        candidate_normalized = _set_path(candidate_normalized, path, marker)
    return _json_values_match_exactly(source_normalized, candidate_normalized)


def _existing_text_unicode_is_compatible(
    source: str,
    candidate: str,
    glossary: GlossaryCatalog | None,
    target_locale: str,
) -> bool:
    source_ranges = list(protected_syntax_ranges(source))
    candidate_ranges = list(protected_syntax_ranges(candidate))
    if glossary is not None:
        source_terms, candidate_terms = glossary.layout_replacement_spans_for_pair(
            source,
            candidate,
        )
        source_ranges.extend((term.start, term.end) for term in source_terms)
        candidate_ranges.extend((term.start, term.end) for term in candidate_terms)
    return (
        translation_unicode_issue(
            source,
            candidate,
            target_locale,
            trusted_source_ranges=source_ranges,
            trusted_candidate_ranges=candidate_ranges,
        )
        is None
    )


def _existing_text_syntax_is_compatible(
    source: str,
    candidate: str,
    glossary: GlossaryCatalog | None,
    source_locale: str,
) -> bool:
    if _protected_syntax_is_compatible(source, candidate, glossary):
        return True
    stripped_source = _without_omissible_source_determiners(
        source,
        glossary,
        source_locale,
    )
    return stripped_source is not None and _protected_syntax_is_compatible(
        stripped_source,
        candidate,
        glossary,
    )


def _without_omissible_source_determiners(
    source: str,
    glossary: GlossaryCatalog | None,
    source_locale: str,
) -> str | None:
    if not _is_english_locale(source_locale):
        return None
    original_source = source
    source = without_leading_styled_article(source)
    term_spans = (
        glossary.replacement_spans_for_parts([source])[0]
        if glossary is not None
        else []
    )
    protected = TokenProtector().protect(source, term_spans=term_spans)
    if not protected.content_placeholders:
        return source if source != original_source else None
    occupied = (*protected.special_source_spans, *protected.term_source_spans)
    # Mask before tokenizing: in ``&5The Name&r``, scanning the raw string
    # sees ``5The`` and would discard the article with the colour code.
    visible = list(source)
    for start, end in occupied:
        visible[start:end] = " " * (end - start)
    words = [
        match
        for match in _WORD_TOKEN.finditer("".join(visible))
    ]
    def is_determiner(match: re.Match[str]) -> bool:
        return match.group(0).casefold() in _OMISSIBLE_ENGLISH_DETERMINERS

    if not words:
        return source if source != original_source else None
    if not all(is_determiner(match) for match in words):
        # A styled name inside a longer sentence can also lose its article.
        # Never strip ordinary words or cross a colour/newline/technical
        # boundary; a name must be present inside the same source slot.
        eligible_words = []
        cursor = 0
        for start, end in (*protected.special_source_spans, (len(source), len(source))):
            slot_words = [word for word in words if cursor <= word.start() < start]
            if slot_words and all(is_determiner(word) for word in slot_words) and any(
                cursor <= term_start < term_end <= start
                for term_start, term_end in protected.term_source_spans
            ):
                eligible_words.extend(slot_words)
            cursor = end
        words = eligible_words
        if not words:
            return source if source != original_source else None
    chunks: list[str] = []
    cursor = 0
    for match in words:
        chunks.append(source[cursor : match.start()])
        cursor = match.end()
    chunks.append(source[cursor:])
    return "".join(chunks)


def _assembled_translation_preserves_terms(
    source: str,
    candidate: str,
    glossary: GlossaryCatalog,
    *,
    treat_as_plain: bool = False,
) -> bool:
    """Validate terminology after assembly without rechecking restored syntax.

    Every translated part has already passed :meth:`ProtectedText.restore`, and
    raw JSON is assembled from a deep copy of the parsed source template.  This
    final pass therefore exists only to catch Mod names or official terms split
    across multiple JSON text components.
    """

    if treat_as_plain or not looks_like_raw_json_text(source):
        return glossary.candidate_preserves_terms([source], [candidate])
    try:
        source_component = json.loads(source)
        candidate_component = json.loads(candidate)
    except ValueError:
        return False
    flat_stream = _flat_json_stream_plan(source_component, glossary)
    if flat_stream is not None:
        plan, _terminology = flat_stream
        skeleton = plan.candidate_skeleton(source_component, candidate_component)
        if skeleton is None:
            return False
        try:
            plan.validate_skeleton(skeleton)
        except TranslationError:
            return False
        return glossary.candidate_preserves_term_layout(
            list(plan.source_texts), plan.texts(candidate_component),
        )
    source_streams = _component_text_streams(source_component)
    candidate_streams = _component_text_streams(candidate_component)
    if source_streams != candidate_streams:
        return False
    for source_stream, candidate_stream in zip(
        source_streams,
        candidate_streams,
        strict=True,
    ):
        source_parts = [_get_path(source_component, path) for path in source_stream]
        candidate_parts = [_get_path(candidate_component, path) for path in candidate_stream]
        if not all(
            isinstance(part, str) for part in (*source_parts, *candidate_parts)
        ) or not glossary.candidate_preserves_terms(source_parts, candidate_parts):
            return False
    return True


def _protected_syntax_is_compatible(
    source: str,
    candidate: str,
    glossary: GlossaryCatalog | None = None,
) -> bool:
    """Preserve every protected value while allowing natural placeholder order."""

    if glossary is None:
        source_terms = None
        candidate_terms = None
    else:
        source_terms, candidate_terms = glossary.layout_replacement_spans_for_pair(
            source,
            candidate,
        )
    return protected_layout_signature(
        source,
        term_spans=source_terms,
    ) == protected_layout_signature(candidate, term_spans=candidate_terms)
