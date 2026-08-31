from __future__ import annotations

import copy
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from .categories import REFERENCE_NAME_CATEGORY_IDS, REFERENCE_PROSE_CATEGORY_IDS
from .domain import CancelledError, TranslationError, TranslationOutcome, TranslationProject, TranslationUnit
from .glossary import GlossaryCatalog, visible_terminology_text
from .openai_client import (
    FAST_MODE_SERVICE_TIER,
    OpenAIClient,
    OpenAIResponseProtocolError,
)
from .protection import (
    ProtectedText,
    TermReplacement,
    TokenProtector,
    looks_like_raw_json_text,
    protected_layout_signature,
    protected_syntax_ranges,
    should_translate,
)
from .unicode_safety import translation_unicode_issue


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
class _StyledBodyProjection:
    part_id: str
    provider_token: str
    source_text: str
    opening: str
    reset: str


@dataclass(frozen=True, slots=True)
class _ProjectionCandidate:
    start: int
    end: int
    term_token: str = ""
    fixed_expansion: str = ""
    body_source: str = ""
    opening: str = ""
    reset: str = ""


@dataclass(frozen=True, slots=True)
class _ProviderProjection:
    """Provider-facing text plus exact local-only token expansions.

    A closed formatting span whose entire body is one protected term is one
    semantic unit for translation.  Exposing its opening code, term, and reset
    as independent opaque tokens lets a model put a Japanese particle inside
    the formatting range even though it cannot know which token is the reset.
    Such spans are therefore projected to one otherwise-unused MQP token and
    expanded locally before the existing fail-closed restoration checks run.

    A consecutive, non-reset formatting stack at the absolute start of an
    otherwise simple unclosed span (for example ``&aTitle``) is also kept
    local.  It applies to the complete provider-visible body, so exposing it as
    a movable token can only create invalid candidates; ``fixed_prefix`` puts
    the exact protected formatting placeholders back before restoration.
    """

    text: str
    term_token_aliases: tuple[tuple[str, str], ...] = ()
    expansions: tuple[tuple[str, str], ...] = ()
    styled_bodies: tuple[_StyledBodyProjection, ...] = ()
    fixed_prefix: str = ""

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
            self.expansions or self.styled_bodies or self.fixed_prefix
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
        return translated


def _safe_fixed_leading_format_prefix(protected: ProtectedText) -> str:
    """Return a locally fixed prefix for one simple unclosed style span.

    Only an absolute leading stack with no reset, later style switch, or fixed
    layout boundary is eligible.  Mid-string codes and formatting that crosses
    newlines/tabs retain the strict provider-response validation path.
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


@dataclass(frozen=True, slots=True)
class _PreparedPart:
    id: str
    protected: ProtectedText
    provider_projection: _ProviderProjection
    context: str
    unit_key: str
    source_path: str
    styled_parts: tuple[_PreparedPart, ...] = ()


@dataclass(slots=True)
class _PreparedUnit:
    unit: TranslationUnit
    template: Any | None
    path_by_part: dict[str, tuple[object, ...]]
    parts: list[_PreparedPart]
    glossary: GlossaryCatalog

    def assemble(self, translated_parts: dict[str, str]) -> str:
        if self.template is None:
            return translated_parts[self.parts[0].id] if self.parts else self.unit.source
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
    ) -> dict[str, str]:
        batches = _make_batches(roots, options.batch_size, options.batch_char_limit)
        translated_parts: dict[str, str] = {}
        for batch_index, batch in enumerate(batches, start=1):
            if cancel and cancel.is_set():
                raise CancelledError("処理をキャンセルしました")
            report(f"OpenAI で翻訳中 ({batch_index}/{len(batches)})")
            batch_parts = [part for root in batch for part in _bundle_parts(root)]
            request_items = [_provider_item(part) for part in batch_parts]
            try:
                response = self._translate_batch(
                    api_key,
                    model,
                    request_items,
                    source_locale,
                    target_locale,
                    cancel,
                )
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

            for root in batch:
                restored, failure = _restore_bundle_response(
                    root,
                    response,
                    source_locale,
                    target_locale,
                    translated_parts,
                )
                if failure is None:
                    translated_parts.update(restored)
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
    ) -> TranslationOutcome:
        if not model.strip():
            raise TranslationError("使用する OpenAI モデルを選択してください")
        if cancel and cancel.is_set():
            raise CancelledError("処理をキャンセルしました")
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
                    treat_as_plain=unit.id in terminology_spans,
                ) and existing_group_safety.get(unit.id, True):
                    resolved[unit.id] = existing
                    reused_count += 1
                    done += 1
                    report(f"既存翻訳を保持: {unit.key}")
                    continue
                report(
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

        translated_parts = self._translate_bundles(
            pending,
            api_key,
            model,
            project.source_locale,
            project.target_locale,
            options,
            report,
            cancel,
        )

        for prepared in prepared_units:
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

        selected_ids = frozenset(unit.id for unit in selected_units)
        if set(resolved) != selected_ids:
            raise TranslationError("内部エラー: 選択したすべての翻訳単位が解決されていません")

        unsafe_group = _first_unsafe_terminology_group(
            project,
            resolved,
            glossary,
            terminology_groups,
            project_reference_glossary,
        )
        if unsafe_group is not None:
            keys = ", ".join(
                next(unit.key for unit in project.units if unit.id == unit_id)
                for unit_id in unsafe_group
            )
            raise TranslationError(
                "組み立て後にraw JSONコンポーネントをまたぐMod名または公式用語の"
                "綴り・表示境界が変わりました。\n"
                f"対象キー: {keys}\n"
                "この処理では翻訳ファイルへ書き込んでいません。"
            )

        if cancel and cancel.is_set():
            raise CancelledError("処理をキャンセルしました")
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
        report("完了")
        return TranslationOutcome(
            output_path=output_path,
            total=total,
            translated=translated_count,
            reused=reused_count,
            copied_without_translation=copied_count,
            glossary_terms=len(glossary.entries),
            skipped_by_selection=skipped_by_selection,
            preserved_unselected=0,
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
        bundle = (*root_part.styled_parts, root_part)
        bundle_chars = 0
        for item in bundle:
            if item.id in request_ids:
                raise TranslationError(f"内部エラー: 翻訳入力IDが重複しています: {item.id}")
            request_ids.add(item.id)
            # Semantic bindings are serialized into the provider input as
            # well, so include every child and the owning root in one budget.
            item_chars = len(
                json.dumps(
                    _provider_item(item),
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


def _build_provider_projection(
    protected: ProtectedText,
    part_id: str,
) -> _ProviderProjection:
    """Keep eligible formatting boundaries under deterministic local control.

    A scope containing exactly one protected term can be expanded locally as a
    fixed value.  A scope containing only ordinary source text is represented
    by a child translation part; the translated child is inserted between the
    original opening codes and reset.  A simple unclosed style stack at the
    absolute beginning is removed from provider input and restored as an exact
    local prefix.  Groups containing mixed terms, technical placeholders, or
    other complex/unclosed formatting remain under the existing fail-closed
    validator instead of being guessed at.
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
    groups: list[_ProjectionCandidate] = []

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
            original_group = source[start:end]
            observed_tokens = tuple(_MQP_PLACEHOLDER.findall(original_group))
            terms = tuple(token for token in observed_tokens if token in term_placeholders)

            if len(terms) == 1:
                term_token = terms[0]
                expected_tokens = (
                    *formatting_tokens[:-1],
                    term_token,
                    formatting_tokens[-1],
                )
                if (
                    observed_tokens == expected_tokens
                    and not _MQP_PLACEHOLDER.sub("", original_group).strip()
                ):
                    groups.append(
                        _ProjectionCandidate(
                            start=start,
                            end=end,
                            term_token=term_token,
                            fixed_expansion=original_group,
                        )
                    )
                continue

            # Plain styled prose is translated as a child node.  Any internal
            # protected token makes the group ineligible for this simple path.
            if terms or observed_tokens != formatting_tokens:
                continue
            last_opening = formatting_tokens[-2]
            reset_token = formatting_tokens[-1]
            body_start = positions[-2] + len(last_opening)
            body_end = positions[-1]
            body = source[body_start:body_end]
            if not any(character.isalnum() for character in body):
                continue
            opening_span = special_spans.get(last_opening)
            reset_span = special_spans.get(reset_token)
            if opening_span is None or reset_span is None:
                raise TranslationError("内部エラー: 装飾本文の原文位置を特定できません")
            original_body = protected.original[opening_span[1] : reset_span[0]]
            if original_body != body:
                raise TranslationError("内部エラー: 装飾本文を原文へ対応付けられません")
            opening = source[start:body_start]
            reset = source[body_end:end]
            if (
                tuple(_MQP_PLACEHOLDER.findall(opening))
                != formatting_tokens[:-1]
                or tuple(_MQP_PLACEHOLDER.findall(reset))
                != (reset_token,)
                or _MQP_PLACEHOLDER.sub("", opening + reset)
            ):
                raise TranslationError("内部エラー: 装飾本文の境界を生成できません")
            groups.append(
                _ProjectionCandidate(
                    start=start,
                    end=end,
                    body_source=original_body,
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
        if group.start < previous_end or (
            group.term_token and group.term_token in aliased_terms
        ):
            raise TranslationError("内部エラー: 装飾範囲が重複しています")
        previous_end = group.end
        if group.term_token:
            aliased_terms.add(group.term_token)

    # Protector placeholders grow upward from 0000. Allocate projection-only
    # tokens downward and exclude every MQP-shaped value already in scope.
    reserved = set(_MQP_PLACEHOLDER.findall(source))
    reserved.update(_MQP_PLACEHOLDER.findall(protected.original))
    for replacement in protected.replacements.values():
        reserved.update(_MQP_PLACEHOLDER.findall(replacement))

    next_index = 0xFFFF
    projected_groups: list[tuple[_ProjectionCandidate, str]] = []
    for group in groups:
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
    for group, provider_token in projected_groups:
        if not group.body_source:
            continue
        styled_bodies.append(
            _StyledBodyProjection(
                part_id=f"{part_id}::styled::{styled_index:04d}",
                provider_token=provider_token,
                source_text=group.body_source,
                opening=group.opening,
                reset=group.reset,
            )
        )
        styled_index += 1

    return _ProviderProjection(
        text=projected,
        term_token_aliases=tuple(
            (group.term_token, provider_token)
            for group, provider_token in projected_groups
            if group.term_token
        ),
        expansions=tuple(
            (provider_token, group.fixed_expansion)
            for group, provider_token in projected_groups
            if group.fixed_expansion
        ),
        styled_bodies=tuple(styled_bodies),
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
        "context": part.context if context is None else context,
    }
    bindings: list[dict[str, str]] = []
    if len(part.protected.term_placeholders) != len(
        part.protected.term_source_spans
    ):
        raise TranslationError("内部エラー: 固有名詞の参照情報を生成できません")
    for placeholder, (start, end) in zip(
        part.protected.term_placeholders,
        part.protected.term_source_spans,
        strict=True,
    ):
        if placeholder not in part.protected.replacements or not (
            0 <= start < end <= len(part.protected.original)
        ):
            raise TranslationError("内部エラー: 固有名詞の参照情報を生成できません")
        bindings.append(
            {
                "token": part.provider_projection.provider_token_for(placeholder),
                "source_term": part.protected.original[start:end],
                "approved_output": part.protected.replacements[placeholder],
            }
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
    provider_tokens = tuple(_MQP_PLACEHOLDER.findall(part.provider_projection.text))
    provider_replacements = dict.fromkeys(provider_tokens, "")
    source_requires_body = _text_requires_translated_body(
        part.provider_projection.text,
        provider_replacements,
        _provider_content_placeholders(part),
        source_locale,
    )
    response_has_body = _has_unprotected_meaningful_text(
        response,
        provider_replacements,
    )
    if source_requires_body and not response_has_body:
        raise TranslationError("OpenAI の応答から翻訳本文が失われました")
    response = part.provider_projection.expand(response, translated_parts)
    restored = part.protected.restore(response)
    unicode_issue = translation_unicode_issue(
        part.protected.protected,
        response,
        target_locale,
    )
    if unicode_issue is not None:
        raise TranslationError(unicode_issue)
    return restored


def _bundle_parts(root: _PreparedPart) -> tuple[_PreparedPart, ...]:
    return (*root.styled_parts, root)


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
    return tuple(dict.fromkeys(content))


def _text_requires_translated_body(
    text: str,
    replacements: Mapping[str, str],
    content_placeholders: tuple[str, ...],
    source_locale: str,
) -> bool:
    if not _has_unprotected_meaningful_text(text, replacements):
        return False
    if not _is_english_locale(source_locale):
        return True
    if not content_placeholders:
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
) -> _PreparedPart:
    projection = _build_provider_projection(protected, part_id)
    styled_parts: list[_PreparedPart] = []
    for index, styled in enumerate(projection.styled_bodies, start=1):
        body_protected = protector.protect(styled.source_text, term_spans=[])
        if body_protected.protected != styled.source_text or body_protected.replacements:
            raise TranslationError(
                "内部エラー: 装飾本文に未分離の保護対象が含まれています"
            )
        body_projection = _build_provider_projection(body_protected, styled.part_id)
        if body_projection.styled_bodies or body_projection.expansions:
            raise TranslationError("内部エラー: 装飾本文の翻訳階層が入れ子になっています")
        styled_parts.append(
            _PreparedPart(
                id=styled.part_id,
                protected=body_protected,
                provider_projection=body_projection,
                context=f"{context}, styled text {index}",
                unit_key=unit_key,
                source_path=source_path,
            )
        )
    return _PreparedPart(
        id=part_id,
        protected=protected,
        provider_projection=projection,
        context=context,
        unit_key=unit_key,
        source_path=source_path,
        styled_parts=tuple(styled_parts),
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
    treat_as_plain: bool = False,
) -> bool:
    if treat_as_plain or not looks_like_raw_json_text(source):
        if not should_translate(source):
            return candidate == source
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
        elif not _existing_text_syntax_is_compatible(
            source_text,
            candidate_text,
            glossary,
            source_locale,
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
    term_spans = (
        glossary.replacement_spans_for_parts([source])[0]
        if glossary is not None
        else []
    )
    protected = TokenProtector().protect(source, term_spans=term_spans)
    if not protected.content_placeholders:
        return None
    occupied = (*protected.special_source_spans, *protected.term_source_spans)
    words = [
        match
        for match in _WORD_TOKEN.finditer(source)
        if not any(
            match.start() < end and match.end() > start
            for start, end in occupied
        )
    ]
    if not words or not all(
        match.group(0).isascii()
        and match.group(0).isalpha()
        and match.group(0).casefold() in _OMISSIBLE_ENGLISH_DETERMINERS
        for match in words
    ):
        return None
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
