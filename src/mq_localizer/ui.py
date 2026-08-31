from __future__ import annotations

import os
import queue
import re
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .analysis_log import SessionAnalysisLog, redact_sensitive
from .application import AnalyzedProject, LocalizerApplication
from .categories import FTB_TRANSLATION_CATEGORIES
from .config import AppSettings, MAX_CACHED_MODEL_COUNT, SettingsStore
from .domain import (
    CancelledError,
    LocalizerError,
    TranslationError,
    TranslationOutcome,
    TranslationProject,
)
from .glossary import GlossaryCatalog, ModLanguageScanner
from .glossary_snapshot import assert_glossary_inputs_unchanged
from .instance import InstanceInfo, inspect_instance_root
from .openai_client import (
    DEFAULT_TRANSLATION_PROMPT,
    MAX_TRANSLATION_PROMPT_LENGTH,
    ModelInfo,
    OpenAIClient,
    OpenAIRetryEvent,
)
from .output_guard import (
    PathSnapshot,
    assert_path_unchanged,
    assert_source_unchanged,
    snapshot_path,
)
from .scan_limits import (
    LANGUAGE_FILE_MIB_MAX,
    LANGUAGE_FILE_MIB_MIN,
    SOURCE_LANGUAGE_MIB_MAX,
    SOURCE_LANGUAGE_MIB_MIN,
    SOURCE_MEMBERS_MAX,
    SOURCE_MEMBERS_MIN,
    TOTAL_LANGUAGE_MIB_MAX,
    TOTAL_LANGUAGE_MIB_MIN,
    GlossaryScanLimits,
)
from .translator import TranslationOptions, TranslationService, select_translation_units


_LOCALE_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$", re.IGNORECASE)
_MINECRAFT_LOCALE_CHOICES = (
    # Keep the two most common values first.  The rest mirrors the locale
    # assets shipped by Minecraft 1.20.1 and 1.21.1 and is deliberately kept
    # in one place so later Minecraft releases can extend the dropdown.
    "en_us",
    "ja_jp",
    "af_za",
    "ar_sa",
    "ast_es",
    "az_az",
    "ba_ru",
    "bar",
    "be_by",
    "be_latn",
    "bg_bg",
    "br_fr",
    "brb",
    "bs_ba",
    "ca_es",
    "cs_cz",
    "cv_cu",
    "cy_gb",
    "da_dk",
    "de_at",
    "de_ch",
    "de_de",
    "el_gr",
    "en_au",
    "en_ca",
    "en_gb",
    "en_nz",
    "en_pt",
    "en_ud",
    "enp",
    "enws",
    "eo_uy",
    "es_ar",
    "es_cl",
    "es_ec",
    "es_es",
    "es_mx",
    "es_uy",
    "es_ve",
    "esan",
    "et_ee",
    "eu_es",
    "fa_ir",
    "fi_fi",
    "fil_ph",
    "fo_fo",
    "fr_ca",
    "fr_ch",
    "fr_fr",
    "fra_de",
    "fur_it",
    "fy_nl",
    "ga_ie",
    "gd_gb",
    "gl_es",
    "go_fr",
    "got_de",
    "hal_ua",
    "haw_us",
    "he_il",
    "hi_in",
    "hn_no",
    "hr_hr",
    "hu_hu",
    "hy_am",
    "id_id",
    "ig_ng",
    "io_en",
    "is_is",
    "isv",
    "it_it",
    "jbo_en",
    "ka_ge",
    "kk_kz",
    "kn_in",
    "ko_kr",
    "ksh",
    "kw_gb",
    "ky_kg",
    "la_la",
    "lb_lu",
    "li_li",
    "lmo",
    "lo_la",
    "lol_us",
    "lt_lt",
    "lv_lv",
    "lzh",
    "mk_mk",
    "mn_mn",
    "ms_my",
    "mt_mt",
    "nah",
    "nds_de",
    "nl_be",
    "nl_nl",
    "nn_no",
    "no_no",
    "oc_fr",
    "ovd",
    "pl_pl",
    "pls",
    "pt_br",
    "pt_pt",
    "qcb_es",
    "qid",
    "qya_aa",
    "ro_ro",
    "rpr",
    "ru_ru",
    "ry_ua",
    "sah_sah",
    "se_no",
    "sk_sk",
    "sl_si",
    "so_so",
    "sq_al",
    "sr_cs",
    "sr_sp",
    "sv_se",
    "sxu",
    "szl",
    "ta_in",
    "th_th",
    "tl_ph",
    "tlh_aa",
    "tok",
    "tr_tr",
    "tt_ru",
    "tzo_mx",
    "uk_ua",
    "uz_uz",
    "val_es",
    "vec_it",
    "vi_vn",
    "vp_vl",
    "vro",
    "yi_de",
    "yo_ng",
    "zh_cn",
    "zh_hk",
    "zh_tw",
    "zlm_arab",
)
_CLOSE_WORKER_GRACE_SECONDS = 2.0
_EVENTS_PER_TICK = 80
_DISPLAY_WARNING_LIMIT = 100
_GLOSSARY_SOURCE_POLICY_TEXT = (
    "資産間の優先度: Minecraft本体 > Mod > KubeJS > resource pack。\n"
    "同じnamespace・keyでは、上位に翻訳先の値がある場合、下位の値で上書きしません。\n"
    "上位に翻訳先keyがない場合だけ、同じ安全な原文に対応する下位の値を利用します。\n"
    "別namespace・keyに同じ原文があっても、下位候補で上位の訳を無効にしません。\n"
    "同じ順位で値が競合した場合は原語を保持します。"
)


@dataclass(slots=True)
class _OutputDecision:
    path: Path
    snapshot: PathSnapshot
    title: str = ""
    message: str = ""
    force_confirmation: bool = False
    ready: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


@dataclass(slots=True)
class _ApprovalDecision:
    ready: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


@dataclass(frozen=True, slots=True)
class _InstanceAnalysis:
    analyzed: AnalyzedProject
    instance: InstanceInfo
    glossary: GlossaryCatalog
    scan_resourcepacks: bool = False
    glossary_scan_limits: GlossaryScanLimits = field(
        default_factory=GlossaryScanLimits
    )
    requested_source_locale: str = ""
    requested_target_locale: str = ""
    log_path: Path | None = None
    log_error: str = ""


@dataclass(frozen=True, slots=True)
class _WorkerTerminalEvent:
    """A terminal worker event already journaled before entering the UI queue."""

    message: str
    details: str = ""
    persisted: bool = False
    log_error: str = ""


@dataclass(frozen=True, slots=True)
class _DurableLogEvent:
    """A worker-originated UI notice with its journal persistence state."""

    message: str
    persisted: bool = False
    log_error: str = ""


@dataclass(frozen=True, slots=True)
class _WorkerTranslationEvent:
    """A successful translation whose completion notice is already durable."""

    outcome: TranslationOutcome
    analysis: _InstanceAnalysis
    notice: _DurableLogEvent


def _redact_sensitive(text: str, *secrets: str) -> str:
    return redact_sensitive(text, *secrets)


def _project_output_text(project: TranslationProject) -> str:
    if getattr(project, "adapter_id", "") != "ftb_legacy_raw":
        return str(project.default_output)
    active = project.metadata.get("active_quest_root", project.source_path)
    backup = project.metadata.get("backup_quest_root", "")
    asset = project.metadata.get("asset_output_root", project.default_output)
    delivery = project.metadata.get("legacy_language_delivery", "resourcepack")
    delivery_label = "KubeJS言語資産" if delivery == "kubejs" else "リソースパック"
    return (
        f"クエスト: {active}\n"
        f"原本バックアップ: {backup}\n"
        f"{delivery_label}: {asset}"
    )


def _project_output_line(project: TranslationProject) -> str:
    if getattr(project, "adapter_id", "") == "ftb_legacy_raw":
        return f"出力先:\n{_project_output_text(project)}"
    return f"出力先: {project.default_output}"


def _resourcepack_activation_required(project: TranslationProject) -> bool:
    return (
        getattr(project, "adapter_id", "") == "ftb_legacy_raw"
        and bool(project.metadata.get("resourcepack_activation_required", False))
    )


def _format_translation_completion(
    outcome: TranslationOutcome,
    project: TranslationProject | None = None,
) -> str:
    output_line = (
        _project_output_line(project)
        if project is not None
        and getattr(project, "adapter_id", "") == "ftb_legacy_raw"
        else f"出力先: {outcome.output_path}"
    )
    activation_note = (
        "\n注意: Minecraftのリソースパック画面で生成したパックを有効化してください。"
        if project is not None and _resourcepack_activation_required(project)
        else ""
    )
    return (
        "\n=== 翻訳完了 ===\n"
        f"{output_line}\n"
        f"新規翻訳: {outcome.translated}件\n"
        f"既存訳を保持: {outcome.reused}件\n"
        f"装飾・コードのみ: {outcome.copied_without_translation}件\n"
        f"選択外（翻訳先へ未出力）: {outcome.skipped_by_selection}件"
        f"{activation_note}"
    )


def _translation_completion_dialog(
    project: TranslationProject,
    warning_count: int,
) -> tuple[str, str, str]:
    warning_note = (
        f"\n\n確認事項が {warning_count} 件あります。詳細ログを確認してください。"
        if warning_count
        else ""
    )
    message = (
        "翻訳ファイルを出力しました。\n\n"
        f"{_project_output_text(project)}{warning_note}"
    )
    if _resourcepack_activation_required(project):
        return (
            "warning",
            "翻訳完了・リソースパックを有効化してください",
            message
            + "\n\nMinecraftのリソースパック画面を開き、"
            "生成したmq_localizerリソースパックを必ず有効化してください。",
        )
    return "info", "翻訳完了", message


def _output_confirmation(project: TranslationProject) -> tuple[str, str, bool]:
    if project.adapter_id != "ftb_legacy_raw":
        return "", "", False
    active = Path(project.metadata["active_quest_root"])
    backup = Path(project.metadata["backup_quest_root"])
    asset = Path(project.metadata["asset_output_root"])
    source_kind = str(project.metadata.get("legacy_source_kind", "active"))
    if source_kind == "backup":
        quest_action = (
            f"原本 {backup} はそのまま保持し、{active} をキー化済みクエストで更新します。"
        )
    else:
        quest_action = (
            f"現在の {active} を {backup} へ原本として保存し、"
            f"新しい {active} にキー化済みクエストを出力します。"
        )
    delivery = str(project.metadata.get("legacy_language_delivery", "resourcepack"))
    language_action = (
        f"言語ファイルはKubeJSから自動読込される場所へ出力します:\n{asset}"
        if delivery == "kubejs"
        else f"言語ファイルをリソースパックとして出力します:\n{asset}"
    )
    return (
        "旧版クエストの入替え確認",
        f"{quest_action}\n\n{language_action}\n\n"
        "書き込み前にMinecraftを完全に終了してください。\n\n続行しますか？",
        True,
    )


def _format_openai_retry(event: OpenAIRetryEvent) -> str:
    """Render a bounded, credential-free explanation of one API retry."""

    target = {
        "/responses": "翻訳リクエスト",
        "/models": "モデル一覧取得",
    }.get(event.endpoint, "OpenAI APIリクエスト")
    if event.kind == "timeout":
        reason = "通信timeout"
    elif event.kind == "connection":
        reason = "接続エラー"
    elif event.status is not None:
        reason = f"HTTP {event.status}"
    else:
        reason = "一時的な通信エラー"
    request_id = (
        event.request_id
        if re.fullmatch(r"[\x21-\x7e]{1,128}", event.request_id)
        else ""
    )
    request_id_line = f"\nRequest ID: {request_id}" if request_id else ""
    continuation = (
        "\n完了済みの翻訳バッチは保持し、失敗した現在のAPIリクエストだけを再送します。"
        if event.endpoint == "/responses"
        else ""
    )
    return (
        "OpenAI API通信を再試行します。\n"
        f"対象: {target}\n"
        f"追加試行: {event.attempt}/{event.max_retries}\n"
        f"待機時間: {event.delay:g}秒\n"
        f"理由: {reason}"
        f"{request_id_line}"
        f"{continuation}"
    )


def _format_glossary_confirmation_skipped(
    reason: str,
    has_protection: bool,
) -> str:
    availability = (
        "取得できたMod名と公式用語は引き続き保護します。"
        if has_protection
        else "今回の解析では利用できるMod名・公式用語を取得できませんでした。"
    )
    return (
        "\n=== 固有名詞保護の確認 ===\n"
        "設定により続行確認をスキップしました。"
        "固有名詞の保護処理は無効になっていません。\n"
        f"{availability}\n"
        f"確認を表示する理由: {reason}"
    )


def _is_valid_locale(value: str) -> bool:
    return bool(_LOCALE_PATTERN.fullmatch(value))


def _api_key_is_environment_value(api_key: str) -> bool:
    environment_key = os.getenv("OPENAI_API_KEY", "").strip()
    return bool(environment_key and api_key.strip() == environment_key)


def _save_api_key_initially_selected(
    settings: AppSettings,
    secure_persistence_available: bool,
    api_key_from_environment: bool,
) -> bool:
    """Never opt an environment-provided secret into DPAPI implicitly."""

    return bool(
        settings.save_api_key
        and secure_persistence_available
        and not api_key_from_environment
    )


def _locale_candidates(*saved_values: str) -> tuple[str, ...]:
    """Return readonly dropdown choices without losing valid legacy values."""

    candidates = list(_MINECRAFT_LOCALE_CHOICES)
    known = set(candidates)
    for value in saved_values:
        normalized = value.strip().lower() if isinstance(value, str) else ""
        if not _is_valid_locale(normalized) or normalized in known:
            continue
        candidates.append(normalized)
        known.add(normalized)
    return tuple(candidates)


def _glossary_confirmation_reason(glossary: GlossaryCatalog) -> str:
    coverage = glossary.coverage
    if not coverage.has_protection:
        if coverage.scan_state == "no_archives":
            reason = (
                "インスタンスのmodsフォルダーにMod JARが見つからず、"
                "Minecraft本体の公式言語資産からも保護用語を取得できませんでした。"
            )
        elif coverage.scan_state == "all_failed":
            reason = (
                f"検出したMod JAR {coverage.discovered_archives}件を読み取れず、"
                "Minecraft本体の公式言語資産からも保護用語を取得できませんでした。"
                "警告欄に失敗理由を表示しています。"
            )
        else:
            reason = (
                f"Mod JAR {coverage.scanned_archives}件の走査には成功しましたが、"
                "クエスト内で保護できるMod表示名またはMod・Minecraft公式用語を"
                "取得できませんでした。"
            )
        if coverage.external_sources_discovered:
            reason += (
                " KubeJS・resource packの追加言語資産 "
                f"{coverage.external_sources_discovered}件中"
                f"{coverage.external_sources_scanned}件も走査しましたが、"
                "保護用語を取得できませんでした。"
            )
        elif coverage.resourcepacks_enabled:
            reason += " resourcepacks走査は有効ですが、対象の追加言語資産はありませんでした。"
        if coverage.external_asset_warning_count:
            reason += (
                " 追加言語資産の確認事項が"
                f"{coverage.external_asset_warning_count}件あります。"
            )
        return reason
    issues: list[str] = []
    if coverage.scan_state == "no_archives":
        issues.append(
            "modsフォルダーにMod JARが見つからず、Mod表示名とMod公式用語を確認できませんでした"
        )
    elif coverage.scan_state == "all_failed":
        issues.append(
            f"検出したMod JAR {coverage.discovered_archives}件を読み取れず、"
            "Mod表示名とMod公式用語を確認できませんでした"
        )
    elif coverage.term_state == "official_terms_only":
        issues.append(
            "Mod表示名を取得できなかったため、表示名を持たないJARの製品名は翻訳される可能性があります"
        )
    unavailable = coverage.failed_archives + coverage.skipped_archives
    if unavailable and coverage.scan_state not in {"no_archives", "all_failed"}:
        issues.append(f"{unavailable}件のJARを走査できませんでした")
    if coverage.partial_warning_count:
        issues.append(
            f"{coverage.archives_with_warnings}件のJAR内で部分警告が"
            f"{coverage.partial_warning_count}件ありました"
        )
    if coverage.minecraft_asset_warning_count:
        issues.append(
            "Minecraft本体の公式言語資産を読み取れない警告が"
            f"{coverage.minecraft_asset_warning_count}件ありました"
        )
    external_unavailable = (
        coverage.external_sources_failed + coverage.external_sources_skipped
    )
    if coverage.external_asset_warning_count:
        if external_unavailable:
            issues.append(
                "KubeJS・resource packの追加言語資産 "
                f"{external_unavailable}件を走査できず、追加言語資産の確認事項が計"
                f"{coverage.external_asset_warning_count}件ありました"
            )
        else:
            issues.append(
                "KubeJS・resource packの追加言語資産に確認事項が"
                f"{coverage.external_asset_warning_count}件ありました"
            )
    if issues:
        return (
            f"取得済みのMod表示名 {coverage.mod_display_names}件と公式用語 "
            f"{coverage.official_terms}件は保護します。ただし、"
            + "。".join(issues)
            + "。確認できなかった資産・Modでは一部の固有名詞を保護できない可能性があります。"
        )
    return ""


def _minecraft_version_text(instance: InstanceInfo, analyzed: AnalyzedProject) -> str:
    if instance.minecraft_version:
        evidence = instance.detected_by
        source = evidence.source.name if evidence is not None else "インスタンス情報"
        return f"{instance.minecraft_version}（{source} から自動検出）"
    if analyzed.adapter.id == "ftb_split_json5":
        return "26.1.2以降（FTB Questsの分割JSON5形式から推定）"
    if analyzed.adapter.id in {"ftb_modern_snbt", "ftb_split_snbt"}:
        return "1.21以降（FTB Questsのlocale形式から推定）"
    if analyzed.adapter.id == "ftb_legacy_json":
        return "1.20.x以前向け形式（既存のキー化済みJSONから推定）"
    return "自動検出できませんでした"


def _analysis_identity(
    analyzed: AnalyzedProject,
    instance: InstanceInfo,
    *,
    scan_resourcepacks: bool = False,
) -> tuple[Any, ...]:
    project = analyzed.project
    return (
        str(instance.selected_root),
        str(instance.instance_root),
        str(instance.game_root),
        str(instance.mods_path),
        bool(scan_resourcepacks),
        instance.minecraft_version,
        instance.detected_by,
        instance.evidence,
        instance.warnings,
        analyzed.adapter.id,
        str(project.source_path),
        str(project.default_output),
        project.metadata.get("legacy_source_kind"),
        project.metadata.get("legacy_language_delivery"),
        str(project.metadata.get("active_quest_root", "")),
        str(project.metadata.get("backup_quest_root", "")),
        str(project.metadata.get("asset_output_root", "")),
        project.metadata.get("active_snapshot"),
        project.metadata.get("backup_snapshot"),
        project.metadata.get("asset_snapshot"),
        project.source_locale,
        project.target_locale,
        project.metadata.get("source_snapshot"),
        tuple(
            (unit.id, getattr(unit, "resource_ids", ()))
            for unit in getattr(project, "units", ())
        ),
    )


def _instance_identity(instance: InstanceInfo) -> tuple[Any, ...]:
    """Return the instance facts which must still match a cached analysis."""

    return (
        instance.selected_root,
        instance.instance_root,
        instance.game_root,
        instance.mods_path,
        instance.minecraft_version,
        instance.detected_by,
        instance.evidence,
        instance.warnings,
    )


def _glossary_identity(glossary: GlossaryCatalog) -> tuple[Any, ...]:
    return (
        glossary.coverage,
        glossary.entries,
        glossary.conflicts,
        glossary.evidence,
        tuple(glossary.warnings),
    )


def _glossary_status_text(glossary: GlossaryCatalog) -> str:
    coverage = glossary.coverage
    if not coverage.has_protection:
        state = "利用不可"
    elif (
        coverage.scan_state != "complete"
        or coverage.has_partial_warnings
        or coverage.term_state == "official_terms_only"
    ):
        state = "一部有効"
    else:
        state = "有効"
    return f"{state} — {coverage.summary}"


def _format_warning_block(
    warnings: list[str] | tuple[str, ...],
    *,
    title: str = "確認事項",
    guidance: str = "",
    full_log_path: Path | None = None,
) -> str:
    cleaned = [str(warning).strip() for warning in warnings if str(warning).strip()]
    if not cleaned:
        return ""
    shown = cleaned[:_DISPLAY_WARNING_LIMIT]
    lines = [f"\n=== {title}（{len(cleaned)}件） ==="]
    if guidance:
        lines.append(guidance)
    lines.extend(f"{index}. {warning}" for index, warning in enumerate(shown, start=1))
    if len(cleaned) > len(shown):
        omitted = len(cleaned) - len(shown)
        if full_log_path is not None:
            lines.append(
                f"…残り {omitted}件は画面の応答性を保つため省略しました。"
                f"解析結果全文を含むセッションログ: {full_log_path}"
            )
        else:
            lines.append(f"…残り {omitted}件は画面の応答性を保つため省略しました。")
    return "\n".join(lines)


def _format_full_warning_section(
    warnings: list[str] | tuple[str, ...],
    *,
    title: str,
) -> str:
    cleaned = [str(warning).strip() for warning in warnings if str(warning).strip()]
    lines = [f"--- {title}（{len(cleaned)}件） ---"]
    if not cleaned:
        lines.append("なし")
        return "\n".join(lines)
    for index, warning in enumerate(cleaned, start=1):
        warning_lines = warning.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lines.append(f"{index}. {warning_lines[0]}")
        lines.extend(f"   | {line}" for line in warning_lines[1:])
    return "\n".join(lines)


def _format_analysis_log(analysis: _InstanceAnalysis) -> str:
    """Render an analysis record without applying the GUI's 100-warning cap."""

    analyzed = analysis.analyzed
    project = analyzed.project
    glossary = analysis.glossary
    category_counts = Counter(unit.category for unit in project.units)
    category_lines = [
        f"- {category.label}: {category_counts[category.id]}件"
        for category in FTB_TRANSLATION_CATEGORIES
        if category_counts[category.id]
    ]
    instance_root = str(analysis.instance.game_root)
    if analysis.instance.selected_root != analysis.instance.game_root:
        instance_root += f"\n  選択ルート: {analysis.instance.selected_root}"
    sections = [
        "--- 自動判定 ---",
        f"Minecraftバージョン: {_minecraft_version_text(analysis.instance, analyzed)}",
        f"検出形式: {project.adapter_label}",
        f"インスタンス: {instance_root}",
        f"翻訳元: {project.source_path}",
        _project_output_line(project),
        "",
        "--- 翻訳対象 ---",
        f"全テキスト: {len(project.units)}件",
        f"既存の翻訳: {len(project.existing)}件",
        *(category_lines or ["- 項目なし"]),
        "",
        "--- 固有名詞保護 ---",
        f"状態: {_glossary_status_text(glossary)}",
        f"resourcepacks走査: {'有効' if analysis.scan_resourcepacks else '無効'}",
        f"走査上限: {_glossary_scan_limits_summary(analysis.glossary_scan_limits)}",
        f"用語衝突: {len(glossary.conflicts)}件",
        _GLOSSARY_SOURCE_POLICY_TEXT,
        "",
        _format_full_warning_section(
            [*analysis.instance.warnings, *project.warnings],
            title="インスタンス / クエストの確認事項",
        ),
        "",
        _format_full_warning_section(
            glossary.warnings,
            title="固有名詞資産の確認事項",
        ),
    ]
    return "\n".join(sections)


def _copy_settings(settings: AppSettings) -> AppSettings:
    """Copy settings without sharing either mutable settings list."""

    return replace(
        settings,
        cached_models=list(settings.cached_models),
        translation_categories=list(settings.translation_categories),
    )


def _settings_summary_text(settings: AppSettings) -> str:
    """Return the low-frequency settings shown read-only on the main window."""

    reuse = "ON" if settings.preserve_existing else "OFF"
    resourcepacks = "ON" if settings.scan_resourcepacks else "OFF"
    confirmation = "省略" if settings.skip_glossary_confirmation else "表示"
    return (
        f"locale: {settings.source_locale} → {settings.target_locale} / "
        f"既存翻訳の再利用: {reuse} / resourcepacks走査: {resourcepacks} / "
        f"固有名詞保護の確認: {confirmation}"
    )


def _glossary_scan_limits_summary(limits: GlossaryScanLimits) -> str:
    """Render configured glossary budgets using the same units as Settings."""

    if not limits.enabled:
        return "無効"
    return (
        f"1資産の項目数 {limits.max_source_members:,}件 / "
        f"言語ファイル 1件 {limits.max_language_file_mib:,} MiB・"
        f"1資産合計 {limits.max_source_language_mib:,} MiB・"
        f"全資産合計 {limits.max_total_language_mib:,} MiB"
    )


def _validated_glossary_scan_limits(
    enabled: object,
    max_source_members: object,
    max_language_file_mib: object,
    max_source_language_mib: object,
    max_total_language_mib: object,
) -> GlossaryScanLimits:
    """Validate all four user-facing limits and return one immutable value."""

    if type(enabled) is not bool:
        raise ValueError("走査上限の有効・無効を選択してください。")
    values = (
        (
            "1資産の項目数上限",
            max_source_members,
            SOURCE_MEMBERS_MIN,
            SOURCE_MEMBERS_MAX,
        ),
        (
            "言語ファイル1件の上限（MiB）",
            max_language_file_mib,
            LANGUAGE_FILE_MIB_MIN,
            LANGUAGE_FILE_MIB_MAX,
        ),
        (
            "1資産の言語ファイル合計上限（MiB）",
            max_source_language_mib,
            SOURCE_LANGUAGE_MIB_MIN,
            SOURCE_LANGUAGE_MIB_MAX,
        ),
        (
            "全資産の言語ファイル合計上限（MiB）",
            max_total_language_mib,
            TOTAL_LANGUAGE_MIB_MIN,
            TOTAL_LANGUAGE_MIB_MAX,
        ),
    )
    for label, value, minimum, maximum in values:
        if type(value) is not int:
            raise ValueError(f"{label}には整数を入力してください。")
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{label}は{minimum:,}〜{maximum:,}で指定してください。"
            )
    if max_language_file_mib > max_source_language_mib:
        raise ValueError(
            "言語ファイル1件の上限は、"
            "1資産の言語ファイル合計上限以下にしてください。"
        )
    if max_source_language_mib > max_total_language_mib:
        raise ValueError(
            "1資産の言語ファイル合計上限は、"
            "全資産の言語ファイル合計上限以下にしてください。"
        )
    return GlossaryScanLimits(
        enabled=enabled,
        max_source_members=max_source_members,
        max_language_file_mib=max_language_file_mib,
        max_source_language_mib=max_source_language_mib,
        max_total_language_mib=max_total_language_mib,
    )


def _model_candidates(settings: AppSettings) -> list[str]:
    """Return safe persisted candidates, migrating an older selected model."""

    candidates = [
        model.strip()
        for model in settings.cached_models
        if isinstance(model, str) and model.strip() and len(model) <= 512
    ]
    selected = settings.model.strip()
    if selected and len(selected) <= 512 and selected not in candidates:
        candidates.insert(0, selected)
    return list(dict.fromkeys(candidates))[:MAX_CACHED_MODEL_COUNT]


def _saved_instance_path(settings: AppSettings) -> str:
    """Migrate an old source selection to the nearest instance root."""

    raw = settings.last_source_path.strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if path.is_file():
        path = path.parent
    for candidate in (path, *path.parents):
        if (candidate / "config").is_dir() and (candidate / "mods").is_dir():
            return str(candidate)
    source = Path(settings.last_source_path).expanduser() if settings.last_source_path else None
    return str(source) if source is not None and source.is_dir() else ""


def _bounded_window_size(
    screen_width: int,
    screen_height: int,
    preferred_width: int,
    preferred_height: int,
    *,
    horizontal_margin: int,
    vertical_margin: int,
) -> tuple[int, int]:
    """Fit an initial window inside a small screen while retaining useful space."""

    available_width = max(1, screen_width - horizontal_margin)
    available_height = max(1, screen_height - vertical_margin)
    return min(preferred_width, available_width), min(preferred_height, available_height)


def _fitted_scrollable_window_size(
    content_width: int,
    content_height: int,
    vertical_scrollbar_width: int,
    horizontal_scrollbar_height: int,
    base_width: int,
    base_height: int,
) -> tuple[int, int]:
    """Fit one scrollable window to its current UI without shrinking its design size."""

    return (
        max(base_width, content_width + vertical_scrollbar_width),
        max(base_height, content_height + horizontal_scrollbar_height),
    )


def _window_geometry(
    width: int,
    height: int,
    position: tuple[int, int] | None = None,
) -> str:
    """Build Tk geometry while preserving absolute negative monitor coordinates."""

    geometry = f"{width}x{height}"
    if position is not None:
        x, y = position
        # Tk needs ``+-100`` for absolute -100. ``-100`` alone means an
        # offset measured from the screen's right or bottom edge.
        geometry += f"+{x}+{y}"
    return geometry


def _configure_window_size(
    window: tk.Misc,
    preferred_width: int,
    preferred_height: int,
    minimum_width: int,
    minimum_height: int,
    *,
    horizontal_margin: int,
    vertical_margin: int,
    position: tuple[int, int] | None = None,
) -> None:
    width, height = _bounded_window_size(
        window.winfo_screenwidth(),
        window.winfo_screenheight(),
        preferred_width,
        preferred_height,
        horizontal_margin=horizontal_margin,
        vertical_margin=vertical_margin,
    )
    window.geometry(_window_geometry(width, height, position))
    window.minsize(min(minimum_width, width), min(minimum_height, height))


class _ScrollablePane(ttk.Frame):
    """A frame that preserves requested widget sizes and scrolls when needed."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        padding: int = 0,
        wheel_master: tk.Misc | None = None,
    ) -> None:
        super().__init__(master)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        background = ttk.Style(master).lookup("TFrame", "background") or "#f0f0f0"
        self.canvas = tk.Canvas(
            self,
            borderwidth=0,
            highlightthickness=0,
            background=background,
            takefocus=False,
        )
        self.vertical_scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
        )
        self.horizontal_scrollbar = ttk.Scrollbar(
            self,
            orient="horizontal",
            command=self.canvas.xview,
        )
        self.canvas.configure(
            yscrollcommand=self.vertical_scrollbar.set,
            xscrollcommand=self.horizontal_scrollbar.set,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        self.horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        self.content = ttk.Frame(self.canvas, padding=padding)
        self._content_window = self.canvas.create_window(
            (0, 0),
            window=self.content,
            anchor="nw",
        )
        self._layout_pending = False
        self.canvas.bind("<Configure>", self._schedule_layout, add="+")
        self.content.bind("<Configure>", self._schedule_layout, add="+")
        (wheel_master or master).bind("<MouseWheel>", self._on_mousewheel, add="+")

    def _schedule_layout(self, _event: object = None) -> None:
        if self._layout_pending:
            return
        self._layout_pending = True
        self.after_idle(self._layout_content)

    def _layout_content(self) -> None:
        self._layout_pending = False
        try:
            viewport_width = max(1, self.canvas.winfo_width())
            viewport_height = max(1, self.canvas.winfo_height())
            content_width = max(viewport_width, self.content.winfo_reqwidth())
            content_height = max(viewport_height, self.content.winfo_reqheight())
            self.canvas.itemconfigure(
                self._content_window,
                width=content_width,
                height=content_height,
            )
            self.canvas.configure(
                scrollregion=(0, 0, content_width, content_height),
            )
        except tk.TclError:
            return

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> None:
        try:
            if not self.winfo_ismapped():
                return
        except tk.TclError:
            return
        if isinstance(event.widget, (tk.Text, ttk.Combobox, ttk.Spinbox)):
            return
        delta = int(getattr(event, "delta", 0))
        if not delta:
            return
        steps = -max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120)
        if int(getattr(event, "state", 0)) & 0x0001:
            self.canvas.xview_scroll(steps, "units")
        else:
            self.canvas.yview_scroll(steps, "units")


class MainWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._show_root_after_layout = self.root.state() == "normal"
        if self._show_root_after_layout:
            self.root.withdraw()
        self.root.title("Minecraft Quest Localizer")
        _configure_window_size(
            self.root,
            1020,
            900,
            640,
            480,
            horizontal_margin=80,
            vertical_margin=120,
        )
        self.store = SettingsStore()
        self.settings = self.store.load()
        self.analysis_log = SessionAnalysisLog(self.store.path.parent / "logs")
        self._session_log_path: Path | None = None
        self._session_log_errors: set[str] = set()
        self._session_log_last_error = ""
        self.session_api_key = self.store.read_api_key(self.settings)
        self.application = LocalizerApplication()
        self.scanner = ModLanguageScanner()
        self.cancel_event = threading.Event()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._progress_lock = threading.Lock()
        self._pending_progress: tuple[int, int, str] | None = None
        self._progress_event_queued = False
        self._stage_lock = threading.Lock()
        self._pending_analysis_stage: str | None = None
        self._stage_event_queued = False
        self.worker: threading.Thread | None = None
        self.worker_write_started = threading.Event()
        self.close_pending = False
        self.close_deadline: float | None = None
        self._analysis: _InstanceAnalysis | None = None
        self._analyzed: AnalyzedProject | None = None
        self._instance_info: InstanceInfo | None = None
        self._glossary = GlossaryCatalog()
        self._analysis_scan_resourcepacks = False
        self._analysis_category_counts: dict[str, int] = {}
        self._drain_after_id: str | None = None
        self.request_widgets: list[tuple[Any, str]] = []

        self.instance_var = tk.StringVar(value=_saved_instance_path(self.settings))
        self.settings_summary_var = tk.StringVar(
            value=_settings_summary_text(self.settings)
        )
        saved_categories = set(self.settings.translation_categories)
        self.category_vars = {
            category.id: tk.BooleanVar(value=category.id in saved_categories)
            for category in FTB_TRANSLATION_CATEGORIES
        }
        self.category_status_var = tk.StringVar()
        self.detected_version_var = tk.StringVar(value="解析後に表示します")
        self.detected_format_var = tk.StringVar(value="解析後に表示します")
        self.detected_source_var = tk.StringVar(value="解析後に表示します")
        self.detected_output_var = tk.StringVar(value="解析後に表示します")
        self.detected_glossary_var = tk.StringVar(value="解析後に表示します")
        self.detected_log_var = tk.StringVar(
            value=f"起動ログ作成後に表示します（{self.analysis_log.directory}）"
        )
        self.status_var = tk.StringVar(value="Modpackのインスタンスルートを選択してください")
        self.progress_var = tk.DoubleVar(value=0)

        self._configure_style()
        self._build_ui()
        self._refresh_category_status()
        self.root.update_idletasks()
        preferred_width, preferred_height = _fitted_scrollable_window_size(
            self.main_scroll_pane.content.winfo_reqwidth(),
            self.main_scroll_pane.content.winfo_reqheight(),
            self.main_scroll_pane.vertical_scrollbar.winfo_reqwidth(),
            self.main_scroll_pane.horizontal_scrollbar.winfo_reqheight(),
            1020,
            900,
        )
        _configure_window_size(
            self.root,
            preferred_width,
            preferred_height,
            640,
            480,
            horizontal_margin=80,
            vertical_margin=120,
        )
        self.instance_var.trace_add("write", self._invalidate_analysis)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_after_id = self.root.after(100, self._drain_events)
        if self._show_root_after_layout:
            self.root.deiconify()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Yu Gothic UI", 18, "bold"))
        style.configure("Subtitle.TLabel", foreground="#4b5563")
        style.configure("Section.TLabelframe.Label", font=("Yu Gothic UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Yu Gothic UI", 10, "bold"))

    def _build_ui(self) -> None:
        self.main_scroll_pane = _ScrollablePane(self.root, padding=18)
        self.main_scroll_pane.pack(fill="both", expand=True)
        outer = self.main_scroll_pane.content

        ttk.Label(outer, text="Minecraft Quest Localizer", style="Title.TLabel").pack(anchor="w")

        source_box = ttk.LabelFrame(
            outer,
            text="1. Modpackインスタンス",
            padding=12,
            style="Section.TLabelframe",
        )
        source_box.pack(fill="x")
        source_box.columnconfigure(1, weight=1)
        ttk.Label(source_box, text="インスタンスルート").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=4,
        )
        instance_entry = ttk.Entry(source_box, textvariable=self.instance_var)
        instance_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self._track_request_widget(instance_entry)
        instance_button = ttk.Button(
            source_box,
            text="フォルダー…",
            command=self._choose_instance_directory,
        )
        instance_button.grid(row=0, column=2, padx=(8, 0))
        self._track_request_widget(instance_button)
        ttk.Label(
            source_box,
            text="config・mods が入っているModpackのフォルダーを選択してください。"
            "形式、Minecraftバージョン、翻訳元、出力先は解析時に自動判定します。",
            foreground="#4b5563",
            justify="left",
            wraplength=760,
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 8))

        category_box = ttk.LabelFrame(
            outer,
            text="2. 翻訳する項目",
            padding=10,
            style="Section.TLabelframe",
        )
        category_box.pack(fill="x", pady=(10, 0))
        category_header = ttk.Frame(category_box)
        category_header.pack(fill="x", pady=(0, 4))
        category_header.columnconfigure(0, weight=1)
        category_actions = ttk.Frame(category_header)
        category_actions.grid(row=0, column=0, sticky="e")
        ttk.Label(
            category_header,
            textvariable=self.category_status_var,
            foreground="#4b5563",
            justify="left",
            wraplength=620,
        ).grid(row=1, column=0, sticky="ew", pady=(5, 0))
        select_none_button = ttk.Button(
            category_actions,
            text="全解除",
            command=lambda: self._set_all_categories(False),
        )
        select_none_button.pack(side="right")
        self._track_request_widget(select_none_button)
        select_all_button = ttk.Button(
            category_actions,
            text="全選択",
            command=lambda: self._set_all_categories(True),
        )
        select_all_button.pack(side="right", padx=(0, 6))
        self._track_request_widget(select_all_button)
        category_grid = ttk.Frame(category_box)
        category_grid.pack(fill="x")
        for column in range(4):
            category_grid.columnconfigure(column, weight=1)
        for index, category in enumerate(FTB_TRANSLATION_CATEGORIES):
            check = ttk.Checkbutton(
                category_grid,
                text=category.label,
                variable=self.category_vars[category.id],
                command=self._refresh_category_status,
            )
            check.grid(row=index // 4, column=index % 4, sticky="w", padx=(0, 8), pady=2)
            self._track_request_widget(check)
        result_box = ttk.LabelFrame(
            outer,
            text="3. 解析結果（自動判定）",
            padding=12,
            style="Section.TLabelframe",
        )
        result_box.pack(fill="x", pady=(10, 0))
        result_box.columnconfigure(1, weight=1)

        def add_result_row(row: int, label: str, variable: tk.StringVar) -> None:
            ttk.Label(result_box, text=label).grid(
                row=row,
                column=0,
                sticky="nw",
                padx=(0, 10),
                pady=2,
            )
            ttk.Label(
                result_box,
                textvariable=variable,
                justify="left",
                wraplength=760,
            ).grid(row=row, column=1, sticky="ew", pady=2)

        add_result_row(0, "Minecraftバージョン", self.detected_version_var)
        add_result_row(1, "検出形式", self.detected_format_var)
        add_result_row(2, "翻訳元", self.detected_source_var)
        add_result_row(3, "出力先", self.detected_output_var)
        add_result_row(4, "固有名詞保護", self.detected_glossary_var)
        add_result_row(5, "セッションログ", self.detected_log_var)

        action_frame = ttk.Frame(outer)
        action_frame.pack(fill="x", pady=12)
        settings_frame = ttk.Frame(action_frame)
        settings_frame.pack(side="left", fill="x", expand=True)
        self.settings_button = ttk.Button(
            settings_frame,
            text="設定…",
            command=self._open_settings,
        )
        self.settings_button.pack(side="left")
        ttk.Label(
            settings_frame,
            textvariable=self.settings_summary_var,
            foreground="#4b5563",
            justify="left",
            wraplength=560,
        ).pack(side="left", fill="x", expand=True, padx=(10, 8))
        self.analyze_button = ttk.Button(action_frame, text="解析", command=self._analyze)
        self.analyze_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(action_frame, text="キャンセル", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right")
        self.translate_button = ttk.Button(
            action_frame,
            text="翻訳を開始",
            command=self._translate,
            style="Primary.TButton",
            state="disabled",
        )
        self.translate_button.pack(side="right", padx=(0, 8))

        progress_frame = ttk.Frame(outer)
        progress_frame.pack(fill="x")
        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", pady=(5, 10))

        log_box = ttk.LabelFrame(
            outer,
            text="解析結果の詳細 / ログ",
            padding=8,
            style="Section.TLabelframe",
        )
        log_box.pack(fill="both", expand=True)
        self.log = tk.Text(
            log_box,
            height=12,
            width=1,
            wrap="word",
            state="disabled",
            font=("Yu Gothic UI", 9),
            background="#f8fafc",
            relief="flat",
        )
        scrollbar = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.tag_configure("heading", font=("Yu Gothic UI", 11, "bold"), foreground="#1f2937")
        self.log.tag_configure("section", font=("Yu Gothic UI", 9, "bold"), foreground="#1d4ed8")
        self.log.tag_configure("ok", foreground="#166534")
        self.log.tag_configure("warning", foreground="#92400e")
        self.log.tag_configure("muted", foreground="#64748b")
        self.log.tag_configure("error", foreground="#b91c1c")
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._append_log(
            "Modpackのインスタンスルートを選び、［解析］を押してください。\n"
            f"セッションログの保存先: {self.analysis_log.directory}",
            "muted",
            section="起動",
        )

    def _track_request_widget(self, widget: Any, normal_state: str = "normal") -> None:
        self.request_widgets.append((widget, normal_state))

    def _selected_category_ids(self) -> frozenset[str]:
        return frozenset(
            category_id for category_id, variable in self.category_vars.items() if variable.get()
        )

    def _set_all_categories(self, selected: bool) -> None:
        for variable in self.category_vars.values():
            variable.set(selected)
        self._refresh_category_status()

    def _refresh_category_status(self) -> None:
        selected = self._selected_category_ids()
        category_count = len(FTB_TRANSLATION_CATEGORIES)
        if self._analyzed is None:
            detail = ""
        else:
            unit_count = sum(
                self._analysis_category_counts.get(category_id, 0)
                for category_id in selected
            )
            detail = f" / 現在の入力 {unit_count}件"
        self.category_status_var.set(
            f"{len(selected)}/{category_count}種類を選択{detail}。"
            "選択外の既知キーは翻訳先へ書き込まず、設定済みfallbackを使います。"
        )

    def _invalidate_analysis(self, *_args: object) -> None:
        self._analysis = None
        self._analyzed = None
        self._instance_info = None
        self._glossary = GlossaryCatalog()
        self._analysis_scan_resourcepacks = False
        self._analysis_category_counts = {}
        self.detected_version_var.set("解析後に表示します")
        self.detected_format_var.set("解析後に表示します")
        self.detected_source_var.set("解析後に表示します")
        self.detected_output_var.set("解析後に表示します")
        self.detected_glossary_var.set("解析後に表示します")
        if hasattr(self, "detected_log_var"):
            self.detected_log_var.set(self._session_log_status_text())
        if hasattr(self, "translate_button"):
            self.translate_button.configure(state="disabled")
        if hasattr(self, "status_var"):
            self.status_var.set("入力が変更されました。［解析］を押してください")
        if hasattr(self, "progress"):
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress_var.set(0)
        self._refresh_category_status()

    def _choose_instance_directory(self) -> None:
        path = filedialog.askdirectory(
            title="Modpackのインスタンスルートを選択",
            mustexist=True,
        )
        if path:
            self.instance_var.set(path)

    def _analyze(self) -> None:
        request = self._request_values()
        if request is None:
            return
        settings = getattr(self, "settings", None)
        default_scan_limits = (
            settings.glossary_scan_limits
            if settings is not None
            else GlossaryScanLimits()
        )
        request_scan_limits = request.get(
            "glossary_scan_limits",
            default_scan_limits,
        )

        # A fresh scan supersedes the old immutable analysis even when the
        # visible inputs did not change (for example after mods were updated).
        self._invalidate_analysis()
        self._replace_log(
            "解析を開始しました\n"
            f"  インスタンス: {request['instance_root']}\n"
            "  固有名詞保護の走査上限: "
            f"{_glossary_scan_limits_summary(request_scan_limits)}\n",
            "heading",
            section="解析開始",
        )
        self._start_worker(lambda: self._inspect_and_analyze(request), "analyzed")

    def _inspect_and_analyze(self, request: dict[str, Any]) -> _InstanceAnalysis:
        self._queue_analysis_stage("インスタンス構成を確認しています…")
        instance = inspect_instance_root(Path(request["instance_root"]))
        if self.cancel_event.is_set():
            raise CancelledError("処理をキャンセルしました")

        self._queue_analysis_stage("FTB Questsの形式と翻訳元を解析しています…")
        analyzed = self.application.analyze(
            source_path=instance.game_root,
            adapter_id="auto",
            source_locale=str(request["source_locale"]),
            target_locale=str(request["target_locale"]),
            minecraft_version=instance.minecraft_version,
            output_override=None,
        )
        if self.cancel_event.is_set():
            raise CancelledError("処理をキャンセルしました")
        scan_resourcepacks = bool(request.get("scan_resourcepacks", False))
        scan_limits = request.get("glossary_scan_limits", GlossaryScanLimits())
        if not isinstance(scan_limits, GlossaryScanLimits):
            raise LocalizerError(
                "固有名詞保護の走査上限が不正です。設定を開いて保存し直してください。"
            )
        resourcepack_note = "・resourcepacks" if scan_resourcepacks else ""
        self._queue_analysis_stage(
            "固有名詞資産（Mod・KubeJS・Minecraft本体"
            f"{resourcepack_note}）の言語ファイルを走査しています…"
        )

        def report_archive(progress: Any) -> None:
            current = int(getattr(progress, "current", 0))
            total = int(getattr(progress, "total", 0))
            archive_name = str(getattr(progress, "archive_name", ""))
            phase = str(getattr(progress, "phase", ""))
            if phase == "before":
                message = f"固有名詞資産を確認中 {current}/{total}: {archive_name}"
                self._queue_analysis_stage(message)

        glossary = self.scanner.scan(
            instance.mods_path,
            analyzed.project.source_locale,
            analyzed.project.target_locale,
            self.cancel_event,
            progress=report_archive,
            minecraft_version=instance.minecraft_version,
            instance_root=getattr(instance, "instance_root", instance.game_root),
            game_root=instance.game_root,
            include_resourcepacks=scan_resourcepacks,
            limits=scan_limits,
        )
        if self.cancel_event.is_set():
            raise CancelledError("処理をキャンセルしました")
        analysis = _InstanceAnalysis(
            analyzed=analyzed,
            instance=instance,
            glossary=glossary,
            scan_resourcepacks=scan_resourcepacks,
            glossary_scan_limits=scan_limits,
            requested_source_locale=str(request["source_locale"]).strip().lower(),
            requested_target_locale=str(request["target_locale"]).strip().lower(),
        )
        if getattr(self, "analysis_log", None) is None:
            return analysis
        try:
            log_path = self._write_session_log(
                _format_analysis_log(analysis),
                level="INFO",
                section="解析結果全文",
            )
        except OSError as exc:
            return replace(
                analysis,
                log_path=getattr(self, "_session_log_path", None),
                log_error=_redact_sensitive(str(exc), getattr(self, "session_api_key", "")),
            )
        return replace(analysis, log_path=log_path)

    def _translate(self) -> None:
        if not self.session_api_key:
            messagebox.showwarning("OpenAI設定", "OpenAI APIキーを設定してください。")
            self._open_settings("openai")
            return
        if not self.settings.model.strip():
            messagebox.showwarning("OpenAI設定", "使用するモデルを選択してください。")
            self._open_settings("openai")
            return
        analysis = getattr(self, "_analysis", None)
        if analysis is None:
            messagebox.showwarning(
                "解析",
                "先に［解析］を押し、自動判定したMinecraftバージョン・形式・出力先を確認してください。",
            )
            return
        request = self._request_values()
        if request is None:
            return
        current_scan_limits = request.get(
            "glossary_scan_limits",
            self.settings.glossary_scan_limits,
        )
        selected_categories = self._selected_category_ids()
        if not selected_categories:
            messagebox.showwarning("翻訳する項目", "翻訳する項目を1つ以上選択してください。")
            return
        preserve_existing = self.settings.preserve_existing
        skip_glossary_confirmation = self.settings.skip_glossary_confirmation
        api_key = self.session_api_key
        model = self.settings.model.strip()
        request_timeout = self.settings.request_timeout
        max_retries = self.settings.max_retries
        batch_size = self.settings.batch_size
        batch_char_limit = self.settings.batch_char_limit
        translation_prompt = self.settings.translation_prompt
        fast_mode = self.settings.fast_mode

        selected_labels = [
            category.label
            for category in FTB_TRANSLATION_CATEGORIES
            if category.id in selected_categories
        ]
        self._append_log(
            "\n=== 翻訳開始 ===\n"
            f"モデル: {model}\n"
            f"Fast Mode: {'ON' if fast_mode else 'OFF'}\n"
            f"timeout: {request_timeout}秒\n"
            f"通信再試行: {max_retries}回（初回を除く）\n"
            f"resourcepacks走査: {'ON' if request.get('scan_resourcepacks', False) else 'OFF'}\n"
            "固有名詞保護の走査上限: "
            f"{_glossary_scan_limits_summary(current_scan_limits)}\n"
            "固有名詞保護: 解析済み結果を再利用（再走査なし）\n"
            f"翻訳対象: {', '.join(selected_labels)}",
            "heading",
            section="翻訳開始",
        )

        def work() -> _WorkerTranslationEvent:
            analyzed = analysis.analyzed
            glossary = analysis.glossary

            def invalidate(message: str) -> TranslationError:
                self.events.put(("invalidate_analysis", None))
                return TranslationError(message)

            if self.cancel_event.is_set():
                raise CancelledError("処理をキャンセルしました")
            requested_source_locale = str(request["source_locale"]).strip().lower()
            requested_target_locale = str(request["target_locale"]).strip().lower()
            if (
                requested_source_locale != analysis.requested_source_locale
                or requested_target_locale != analysis.requested_target_locale
                or bool(request.get("scan_resourcepacks", False))
                != analysis.scan_resourcepacks
                or current_scan_limits != analysis.glossary_scan_limits
            ):
                raise invalidate(
                    "解析後にlocale、resourcepacks走査、または固有名詞保護の走査上限が"
                    "変わりました。"
                    "安全のため翻訳を開始していません。もう一度［解析］を押して結果を確認してください。"
                )

            self._queue_analysis_stage(
                "解析済み固有名詞保護結果を再利用（再走査なし）。"
                "インスタンスとファイルの変更有無を確認しています…"
            )
            try:
                current_instance = inspect_instance_root(Path(request["instance_root"]))
            except CancelledError:
                raise
            except LocalizerError as exc:
                raise invalidate(
                    "解析後にインスタンス構成を安全に確認できなくなりました。"
                    "翻訳を開始していません。もう一度［解析］を押して結果を確認してください。"
                ) from exc
            if self.cancel_event.is_set():
                raise CancelledError("処理をキャンセルしました")
            if _instance_identity(current_instance) != _instance_identity(
                analysis.instance
            ):
                raise invalidate(
                    "解析後にインスタンスの場所、Minecraftバージョン、検出根拠、"
                    "または確認事項が変わりました。安全のため翻訳を開始していません。"
                    "もう一度［解析］を押して結果を確認してください。"
                )

            source_snapshot = analyzed.project.metadata.get("source_snapshot")
            expected_source_path = Path(analyzed.project.source_path).expanduser().absolute()
            if (
                not isinstance(source_snapshot, PathSnapshot)
                or source_snapshot.path != expected_source_path
            ):
                raise invalidate(
                    "解析時の翻訳元確認情報が見つからないか、翻訳元と一致しません。"
                    "安全のため翻訳を開始していません。もう一度［解析］を押してください。"
                )
            glossary_input_snapshot = getattr(glossary, "input_snapshot", None)
            if glossary_input_snapshot is None:
                raise invalidate(
                    "解析時の固有名詞資産確認情報が見つかりません。"
                    "安全のため翻訳を開始していません。もう一度［解析］を押してください。"
                )

            def assert_cached_inputs_unchanged(
                *,
                include_glossary: bool = True,
            ) -> None:
                try:
                    assert_source_unchanged(source_snapshot, self.cancel_event)
                    if include_glossary:
                        assert_glossary_inputs_unchanged(
                            glossary_input_snapshot,
                            self.cancel_event,
                        )
                except CancelledError:
                    raise
                except LocalizerError:
                    self.events.put(("invalidate_analysis", None))
                    raise
                except Exception as exc:
                    raise invalidate(
                        "解析済みの翻訳元または固有名詞資産を安全に再確認できませんでした。"
                        "翻訳を開始していません。もう一度［解析］を押してください。"
                    ) from exc

            if not select_translation_units(analyzed.project, selected_categories):
                raise TranslationError(
                    "選択した項目に翻訳文字列がありません。別の項目を選択して再度解析してください"
                )
            output = analyzed.project.default_output
            output_snapshot = snapshot_path(output, self.cancel_event)
            assert_cached_inputs_unchanged()
            if analyzed.adapter.id == "ftb_legacy_raw":
                # Existing target catalogs may legitimately have changed since
                # analysis.  Treat the translation click as the start of the
                # stable-read window while retaining the analysis-time source,
                # active-quest and backup snapshots.
                analyzed.project.metadata["asset_snapshot"] = output_snapshot
            try:
                analyzed.adapter.validate_output(analyzed.project, output)
            except CancelledError:
                raise
            except LocalizerError:
                # If validation observed a concurrent source edit, invalidate
                # the cache.  Output-only validation failures normally keep
                # the analysis because a later click can safely re-read it.
                assert_cached_inputs_unchanged(include_glossary=False)
                if analyzed.adapter.id == "ftb_legacy_raw":
                    # The raw adapter also validates analysis-time active and
                    # backup quest layouts, which are not necessarily the
                    # selected source path.  Any failure there needs a fresh
                    # layout analysis.
                    self.events.put(("invalidate_analysis", None))
                raise
            assert_path_unchanged(output_snapshot, self.cancel_event)
            # validate_output is a short, read-only stable-read step.  Avoid a
            # second full asset inventory here; the post-confirmation check
            # below still runs before OpenAI is called.
            assert_cached_inputs_unchanged(include_glossary=False)
            confirmation_title, confirmation_message, force_confirmation = (
                _output_confirmation(analyzed.project)
            )
            decision = _OutputDecision(
                output,
                output_snapshot,
                title=confirmation_title,
                message=confirmation_message,
                force_confirmation=force_confirmation,
            )
            self.events.put(("confirm_output", decision))
            while not decision.ready.wait(0.1):
                if self.cancel_event.is_set():
                    raise CancelledError("処理をキャンセルしました")
            if not decision.approved:
                raise CancelledError("翻訳を中止しました")
            assert_path_unchanged(output_snapshot, self.cancel_event)
            assert_cached_inputs_unchanged()
            self._await_glossary_confirmation(
                glossary,
                skip=skip_glossary_confirmation,
            )
            client = OpenAIClient(
                timeout=request_timeout,
                max_retries=max_retries,
                translation_prompt=translation_prompt,
                on_retry=self._queue_openai_retry,
            )
            service = TranslationService(client, fast_mode=fast_mode)

            def guard_output_write() -> None:
                assert_path_unchanged(output_snapshot, self.cancel_event)
                assert_cached_inputs_unchanged()
                # Once an atomic/transactional writer may begin, app shutdown
                # must wait for it so the process cannot be killed mid-rollback.
                self.worker_write_started.set()

            outcome = service.translate(
                analyzed.project,
                analyzed.adapter,
                output,
                api_key,
                model,
                glossary,
                TranslationOptions(
                    batch_size=batch_size,
                    batch_char_limit=batch_char_limit,
                    preserve_existing=preserve_existing,
                    selected_categories=selected_categories,
                ),
                progress=self._queue_progress,
                cancel=self.cancel_event,
                pre_write_guard=guard_output_write,
            )
            return self._prepare_translation_success(outcome, analysis)

        self._start_worker(work, "translated")

    def _await_glossary_confirmation(
        self,
        glossary: GlossaryCatalog,
        *,
        skip: bool,
    ) -> None:
        """Wait for the risk acknowledgement unless the user opted out."""

        reason = _glossary_confirmation_reason(glossary)
        if not reason:
            return
        if skip:
            notice = self._persist_worker_log(
                _format_glossary_confirmation_skipped(
                    reason,
                    glossary.coverage.has_protection,
                ),
                level="WARNING",
                section="固有名詞保護の確認",
            )
            self.events.put(
                (
                    "glossary_confirmation_skipped",
                    notice,
                )
            )
            return

        decision = _ApprovalDecision()
        self.events.put(
            (
                "confirm_without_glossary",
                (decision, reason, glossary.coverage.has_protection),
            )
        )
        while not decision.ready.wait(0.1):
            if self.cancel_event.is_set():
                raise CancelledError("処理をキャンセルしました")
        if not decision.approved:
            raise CancelledError("固有名詞保護を確認できないため翻訳を中止しました")

    def _request_values(self) -> dict[str, Any] | None:
        instance_text = self.instance_var.get().strip()
        if not instance_text:
            messagebox.showwarning(
                "インスタンスルート",
                "Modpackのインスタンスルートを選択してください。",
            )
            return None
        instance_root = Path(instance_text).expanduser()
        if not instance_root.exists():
            messagebox.showerror(
                "インスタンスルート",
                f"選択したフォルダーが存在しません:\n{instance_root}",
            )
            return None
        if not instance_root.is_dir():
            messagebox.showerror(
                "インスタンスルート",
                "ファイルではなく、config・mods が入っているModpackのフォルダーを選択してください。",
            )
            return None
        source_locale = self.settings.source_locale.strip().lower()
        target_locale = self.settings.target_locale.strip().lower()
        if not _is_valid_locale(source_locale) or not _is_valid_locale(target_locale):
            messagebox.showerror("locale", "原文localeと翻訳先localeを選択してください。")
            return None
        if source_locale == target_locale:
            messagebox.showerror(
                "locale",
                "原文localeと翻訳先localeには異なる値を選択してください。",
            )
            return None
        scan_resourcepacks = bool(self.settings.scan_resourcepacks)
        return {
            "instance_root": instance_root,
            "source_locale": source_locale,
            "target_locale": target_locale,
            "scan_resourcepacks": scan_resourcepacks,
            "glossary_scan_limits": self.settings.glossary_scan_limits,
        }

    def _queue_progress(self, done: int, total: int, message: str) -> None:
        """Keep only the newest high-frequency progress update."""

        if "再試行" in message:
            # Retry diagnostics are important after a successful recovery too.
            # Journal them before queueing so closing the window cannot discard
            # the reason while the UI thread is still draining other events.
            notice = self._persist_worker_log(
                message,
                level="WARNING",
                section="翻訳結果の安全確認",
            )
            self.events.put(("translation_notice", notice))
            message = notice.message
        with self._progress_lock:
            self._pending_progress = (done, total, message)
            if self._progress_event_queued:
                return
            self._progress_event_queued = True
        self.events.put(("progress_latest", None))

    def _queue_openai_retry(self, event: OpenAIRetryEvent) -> None:
        """Journal API retries separately from translation-safety retries."""

        message = _format_openai_retry(event)
        notice = self._persist_worker_log(
            message,
            level="WARNING",
            section="OpenAI通信再試行",
        )
        self.events.put(("openai_retry_notice", notice))
        self._queue_analysis_stage(
            f"OpenAI通信を再試行します（{event.attempt}/{event.max_retries}、"
            f"{event.delay:g}秒後）…"
        )

    def _queue_analysis_stage(self, message: str) -> None:
        """Coalesce JAR-by-JAR status updates without dropping control events."""

        with self._stage_lock:
            self._pending_analysis_stage = message
            if self._stage_event_queued:
                return
            self._stage_event_queued = True
        self.events.put(("analysis_stage_latest", None))

    def _take_latest_progress(self) -> tuple[int, int, str] | None:
        with self._progress_lock:
            payload = self._pending_progress
            self._pending_progress = None
            self._progress_event_queued = False
            return payload

    def _take_latest_analysis_stage(self) -> str | None:
        with self._stage_lock:
            message = self._pending_analysis_stage
            self._pending_analysis_stage = None
            self._stage_event_queued = False
            return message

    def _start_worker(self, function: Callable[[], Any], success_event: str) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("処理中", "現在の処理が完了するまでお待ちください。")
            return
        self.cancel_event = threading.Event()
        self.worker_write_started = threading.Event()
        self._set_busy(True)
        self.progress_var.set(0)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status_var.set("処理を開始しています…")

        def queue_terminal(
            event: str,
            message: str,
            *,
            details: str = "",
            level: str,
            section: str,
        ) -> None:
            body = {
                "cancelled": f"処理を中止しました: {message}",
                "error": f"エラー\n{message}",
                "unexpected": f"予期しないエラー\n{message}\n{details}",
            }[event]
            persisted = False
            log_error = ""
            try:
                persisted = self._write_session_log(
                    body,
                    level=level,
                    section=section,
                ) is not None
            except OSError as exc:
                log_error = _redact_sensitive(
                    str(exc),
                    getattr(self, "session_api_key", ""),
                )
            self.events.put(
                (
                    event,
                    _WorkerTerminalEvent(
                        message=message,
                        details=details,
                        persisted=persisted,
                        log_error=log_error,
                    ),
                )
            )

        def target() -> None:
            try:
                self.events.put((success_event, function()))
            except CancelledError as exc:
                queue_terminal(
                    "cancelled",
                    _redact_sensitive(str(exc), self.session_api_key),
                    level="WARNING",
                    section="処理中止",
                )
            except LocalizerError as exc:
                queue_terminal(
                    "error",
                    _redact_sensitive(str(exc), self.session_api_key),
                    level="ERROR",
                    section="処理エラー",
                )
            except BaseException as exc:
                queue_terminal(
                    "unexpected",
                    _redact_sensitive(str(exc), self.session_api_key),
                    details=_redact_sensitive(traceback.format_exc(), self.session_api_key),
                    level="ERROR",
                    section="予期しないエラー",
                )

        self.worker = threading.Thread(target=target, name="mq-localizer-worker", daemon=True)
        self.worker.start()

    def _drain_events(self) -> None:
        self._drain_after_id = None
        processed = 0
        try:
            while processed < _EVENTS_PER_TICK:
                event, payload = self.events.get_nowait()
                processed += 1
                if event == "progress_latest":
                    progress = self._take_latest_progress()
                    if progress is not None:
                        done, total, message = progress
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                        self.progress_var.set((done / total * 100) if total else 100)
                        self.status_var.set(message)
                elif event == "analysis_stage_latest":
                    message = self._take_latest_analysis_stage()
                    if message:
                        self.status_var.set(message)
                elif event == "translation_notice":
                    notice = (
                        payload
                        if isinstance(payload, _DurableLogEvent)
                        else _DurableLogEvent(message=str(payload))
                    )
                    notice = replace(
                        notice,
                        message="\n=== 翻訳結果の安全確認 ===\n" + notice.message,
                    )
                    self._render_durable_log_event(
                        notice,
                        "warning",
                        section="翻訳結果の安全確認",
                    )
                elif event == "openai_retry_notice":
                    notice = (
                        payload
                        if isinstance(payload, _DurableLogEvent)
                        else _DurableLogEvent(message=str(payload))
                    )
                    notice = replace(
                        notice,
                        message="\n=== OpenAI通信再試行 ===\n" + notice.message,
                    )
                    self._render_durable_log_event(
                        notice,
                        "warning",
                        section="OpenAI通信再試行",
                    )
                elif event == "invalidate_analysis":
                    self._invalidate_analysis()
                elif event == "confirm_output":
                    decision: _OutputDecision = payload
                    approved = not self.close_pending and not self.cancel_event.is_set()
                    try:
                        if approved and (
                            decision.snapshot.exists or decision.force_confirmation
                        ):
                            approved = messagebox.askyesno(
                                decision.title or "既存出力",
                                decision.message
                                or (
                                    "自動判定した出力先には既存データがあります。"
                                    "原文は変更しませんが、対象の翻訳ファイルを更新します。\n\n"
                                    f"{decision.path}\n\n続行しますか？"
                                ),
                            )
                    except tk.TclError:
                        approved = False
                    finally:
                        decision.approved = approved
                        decision.ready.set()
                elif event == "confirm_without_glossary":
                    decision, reason, has_protection = payload
                    approved = not self.close_pending and not self.cancel_event.is_set()
                    try:
                        if approved:
                            if has_protection:
                                question = (
                                    "取得できたMod名と公式用語は保護します。"
                                    "確認できなかった資産・Modでは保護が不足する可能性を"
                                    "承知して続けますか？"
                                )
                            else:
                                question = (
                                    "Mod名と公式用語を自動保護できない状態で翻訳を続けますか？"
                                )
                            approved = messagebox.askyesno(
                                "固有名詞保護の確認",
                                f"{reason}\n\n{question}",
                            )
                    except tk.TclError:
                        approved = False
                    finally:
                        decision.approved = approved
                        decision.ready.set()
                elif event == "glossary_confirmation_skipped":
                    if isinstance(payload, _DurableLogEvent):
                        notice = payload
                    else:
                        reason, has_protection = payload
                        notice = _DurableLogEvent(
                            message=_format_glossary_confirmation_skipped(
                                reason,
                                has_protection,
                            )
                        )
                    self._render_durable_log_event(
                        notice,
                        "warning",
                        section="固有名詞保護の確認",
                    )
                elif event == "analyzed":
                    analysis: _InstanceAnalysis = payload
                    self._analysis = analysis
                    self._analyzed = analysis.analyzed
                    self._instance_info = analysis.instance
                    self._glossary = analysis.glossary
                    self._analysis_scan_resourcepacks = analysis.scan_resourcepacks
                    self._show_analysis(analysis)
                    self._set_busy(False)
                    self.root.after_idle(self._save_settings)
                elif event == "translated":
                    if isinstance(payload, _WorkerTranslationEvent):
                        outcome = payload.outcome
                        analysis = payload.analysis
                        completion_notice = payload.notice
                    else:
                        outcome, analysis = payload
                        completion_notice = _DurableLogEvent(
                            message=_format_translation_completion(
                                outcome,
                                analysis.analyzed.project,
                            )
                        )
                    self._analysis = analysis
                    self._analyzed = analysis.analyzed
                    self._instance_info = analysis.instance
                    self._glossary = analysis.glossary
                    self._analysis_scan_resourcepacks = analysis.scan_resourcepacks
                    self._set_detected_results(analysis)
                    warnings = [
                        *analysis.instance.warnings,
                        *analysis.analyzed.project.warnings,
                        *analysis.glossary.warnings,
                    ]
                    self.progress_var.set(100)
                    self.status_var.set("翻訳が完了しました")
                    self._render_durable_log_event(
                        completion_notice,
                        "ok",
                        section="翻訳完了",
                    )
                    warning_block = _format_warning_block(
                        warnings,
                        full_log_path=analysis.log_path,
                    )
                    if warning_block:
                        # The uncapped warnings were already written as the
                        # pre-translation analysis event by the worker.
                        self._append_log(warning_block, "warning", persist=False)
                    if analysis.log_error:
                        self._report_session_log_error(analysis.log_error)
                    self._set_busy(False)
                    self.root.after_idle(self._save_settings)
                    if not self.close_pending:
                        project = analysis.analyzed.project
                        dialog_kind, dialog_title, dialog_message = (
                            _translation_completion_dialog(project, len(warnings))
                        )
                        if dialog_kind == "warning":
                            messagebox.showwarning(dialog_title, dialog_message)
                        else:
                            messagebox.showinfo(dialog_title, dialog_message)
                elif event == "cancelled":
                    terminal = (
                        payload
                        if isinstance(payload, _WorkerTerminalEvent)
                        else _WorkerTerminalEvent(message=str(payload))
                    )
                    self.status_var.set(terminal.message)
                    self._append_log(
                        f"\n処理を中止しました: {terminal.message}",
                        "warning",
                        persist=not terminal.persisted,
                        section="処理中止",
                    )
                    if terminal.log_error:
                        self._report_session_log_error(terminal.log_error)
                    self._set_busy(False)
                elif event == "error":
                    terminal = (
                        payload
                        if isinstance(payload, _WorkerTerminalEvent)
                        else _WorkerTerminalEvent(message=str(payload))
                    )
                    self.status_var.set("解析または翻訳を完了できませんでした")
                    self._append_log(
                        f"\nエラー\n{terminal.message}",
                        "error",
                        persist=not terminal.persisted,
                        section="処理エラー",
                    )
                    if terminal.log_error:
                        self._report_session_log_error(terminal.log_error)
                    self._set_busy(False)
                    if not self.close_pending:
                        messagebox.showerror("処理できません", terminal.message)
                elif event == "unexpected":
                    if isinstance(payload, _WorkerTerminalEvent):
                        terminal = payload
                    else:
                        message, details = payload
                        terminal = _WorkerTerminalEvent(
                            message=str(message),
                            details=str(details),
                        )
                    self.status_var.set("予期しないエラー")
                    self._append_log(
                        "\n予期しないエラー\n"
                        f"{terminal.message}\n{terminal.details}",
                        "error",
                        persist=not terminal.persisted,
                        section="予期しないエラー",
                    )
                    if terminal.log_error:
                        self._report_session_log_error(terminal.log_error)
                    self._set_busy(False)
                    if not self.close_pending:
                        messagebox.showerror("予期しないエラー", terminal.message)
        except queue.Empty:
            pass
        if not self.events.empty():
            self._drain_after_id = self.root.after_idle(self._drain_events)
        else:
            self._drain_after_id = self.root.after(100, self._drain_events)

    def _set_detected_results(self, analysis: _InstanceAnalysis) -> None:
        analyzed = analysis.analyzed
        project = analyzed.project
        self.detected_version_var.set(_minecraft_version_text(analysis.instance, analyzed))
        self.detected_format_var.set(project.adapter_label)
        self.detected_source_var.set(str(project.source_path))
        self.detected_output_var.set(_project_output_text(project))
        self.detected_glossary_var.set(_glossary_status_text(analysis.glossary))
        if analysis.log_path is not None:
            self._session_log_path = analysis.log_path
        if analysis.log_error:
            self._session_log_last_error = analysis.log_error
        self.detected_log_var.set(self._session_log_status_text())

    def _show_analysis(self, analysis: _InstanceAnalysis) -> None:
        analyzed = analysis.analyzed
        project = analyzed.project
        glossary = analysis.glossary
        selected_categories = self._selected_category_ids()
        category_counts = Counter(unit.category for unit in project.units)
        self._analysis_category_counts = dict(category_counts)
        selected_count = sum(category_counts.get(category_id, 0) for category_id in selected_categories)
        self.status_var.set(
            f"解析完了: {len(project.units)}件を検出、選択項目の{selected_count}件が翻訳対象です"
        )
        self.progress_var.set(100)
        self._set_detected_results(analysis)
        category_summary = " / ".join(
            f"{category.label} {category_counts[category.id]}"
            for category in FTB_TRANSLATION_CATEGORIES
            if category_counts[category.id]
        )
        root_detail = str(analysis.instance.game_root)
        if analysis.instance.selected_root != analysis.instance.game_root:
            root_detail += f"\n  選択ルート: {analysis.instance.selected_root}"
        self._replace_log(
            "=== 解析結果 ===\n"
            f"Minecraftバージョン: {_minecraft_version_text(analysis.instance, analyzed)}\n"
            f"検出形式: {project.adapter_label}\n"
            f"インスタンス: {root_detail}\n"
            f"翻訳元: {project.source_path}\n"
            f"{_project_output_line(project)}\n\n"
            "=== 翻訳対象 ===\n"
            f"全テキスト: {len(project.units)}件\n"
            f"現在の選択対象: {selected_count}件\n"
            f"選択外: {len(project.units) - selected_count}件\n"
            f"既存の翻訳: {len(project.existing)}件\n"
            f"項目別: {category_summary or 'なし'}\n\n"
            "=== 固有名詞保護 ===\n"
            f"状態: {_glossary_status_text(glossary)}\n"
            f"resourcepacks走査: {'有効' if analysis.scan_resourcepacks else '無効'}\n"
            "走査上限: "
            f"{_glossary_scan_limits_summary(analysis.glossary_scan_limits)}\n"
            f"用語衝突: {len(glossary.conflicts)}件\n"
            "意味: 読み取れたMod表示名は原文のまま固定し、Mod・KubeJS・Minecraft本体・"
            "有効時はresourcepacksの言語資産から、安全に対応付けられる訳だけ使います。\n"
            f"{_GLOSSARY_SOURCE_POLICY_TEXT}\n\n"
            "=== セッションログ ===\n"
            + (
                str(analysis.log_path)
                if analysis.log_path is not None
                else f"保存できませんでした: {analysis.log_error or '保存処理が利用できません'}"
            ),
            persist=False,
        )
        instance_and_project_warnings = [*analysis.instance.warnings, *project.warnings]
        warning_block = _format_warning_block(
            instance_and_project_warnings,
            title="インスタンス / クエストの確認事項",
            guidance="意味: 自動判定または既存データで注意が必要な点です。原文は変更していません。",
            full_log_path=analysis.log_path,
        )
        if warning_block:
            self._append_log(warning_block, "warning", persist=False)
        glossary_warning_block = _format_warning_block(
            glossary.warnings,
            title="固有名詞資産の確認事項",
            guidance=(
                "意味: 読み取り失敗は記載した固有名詞資産・ファイルの範囲、"
                "重複keyはそのkeyだけが"
                "保護候補から外れます。動的な原文値は固定名として登録せず、利用できない"
                "公式訳は採用せず原語を保持します。同じファイル内の安全な用語は利用します。"
                "上の『状態』が有効または一部有効なら、取得済みの固有名詞は引き続き保護します。"
            ),
            full_log_path=analysis.log_path,
        )
        if glossary_warning_block:
            self._append_log(glossary_warning_block, "warning", persist=False)
        if analysis.log_error:
            self._report_session_log_error(analysis.log_error)
        self._refresh_category_status()

    def _settings_snapshot_from_ui(self) -> AppSettings:
        """Copy settings while retaining edits that remain on the main window."""

        settings = _copy_settings(self.settings)
        category_vars = getattr(self, "category_vars", {})
        if category_vars:
            settings.translation_categories = [
                category.id
                for category in FTB_TRANSLATION_CATEGORIES
                if category.id in category_vars and category_vars[category.id].get()
            ]
        instance_var = getattr(self, "instance_var", None)
        if instance_var is not None:
            settings.last_source_path = instance_var.get().strip()
        instance_info = getattr(self, "_instance_info", None)
        settings.minecraft_version = (
            instance_info.minecraft_version if instance_info is not None else ""
        )
        settings.adapter_id = "auto"
        return settings

    def _open_settings(self, initial_tab: str | None = None) -> SettingsDialog:
        if initial_tab is None:
            initial_tab = (
                "openai"
                if not self.session_api_key or not self.settings.model.strip()
                else "translation"
            )

        def show_settings_log(
            message: str,
            tag: str | None,
            section: str,
            persist: bool = True,
        ) -> None:
            if persist:
                self._append_log(message, tag, section=section)
            else:
                self._append_log(message, tag, persist=False, section=section)

        return SettingsDialog(
            self.root,
            self._settings_snapshot_from_ui(),
            self.session_api_key,
            self.store,
            lambda api_key, settings: self._settings_updated(api_key, settings),
            show_settings_log,
            self._persist_worker_log,
            initial_tab=initial_tab,
        )

    def _settings_updated(self, api_key: str, settings: AppSettings) -> None:
        previous_analysis_options = (
            self.settings.source_locale,
            self.settings.target_locale,
            self.settings.scan_resourcepacks,
            self.settings.glossary_scan_limits,
        )
        self.session_api_key = api_key
        self.settings = _copy_settings(settings)
        settings_summary_var = getattr(self, "settings_summary_var", None)
        if settings_summary_var is not None:
            settings_summary_var.set(_settings_summary_text(self.settings))
        current_analysis_options = (
            self.settings.source_locale,
            self.settings.target_locale,
            self.settings.scan_resourcepacks,
            self.settings.glossary_scan_limits,
        )
        if current_analysis_options != previous_analysis_options:
            self._invalidate_analysis()
        mode = "ON" if settings.fast_mode else "OFF"
        reuse = "ON" if settings.preserve_existing else "OFF"
        resourcepacks = "ON" if settings.scan_resourcepacks else "OFF"
        confirmation = "省略" if settings.skip_glossary_confirmation else "表示"
        self._append_log(
            f"設定を更新しました（locale: {settings.source_locale} → {settings.target_locale}、"
            f"既存翻訳の再利用: {reuse}、resourcepacks走査: {resourcepacks}、"
            f"固有名詞保護の確認: {confirmation}、モデル: {settings.model or '未選択'}、"
            f"Fast Mode: {mode}、timeout: {settings.request_timeout}秒、"
            f"通信再試行: {settings.max_retries}回、固有名詞保護の走査上限: "
            f"{_glossary_scan_limits_summary(settings.glossary_scan_limits)}）。",
            section="設定更新",
        )

    def _session_log_status_text(self) -> str:
        path = getattr(self, "_session_log_path", None)
        error = getattr(self, "_session_log_last_error", "")
        if path is not None and error:
            return f"{path}\n一部のログを保存できませんでした: {error}"
        if path is not None:
            return str(path)
        if error:
            return f"保存できませんでした: {error}"
        analysis_log = getattr(self, "analysis_log", None)
        directory = getattr(analysis_log, "directory", "保存先不明")
        return f"未作成（{directory}）"

    @staticmethod
    def _log_level(tag: str | None) -> str:
        return {
            "error": "ERROR",
            "warning": "WARNING",
            "ok": "SUCCESS",
        }.get(tag or "", "INFO")

    def _write_session_log(
        self,
        text: str,
        *,
        level: str,
        section: str,
    ) -> Path | None:
        """Persist an event without touching Tk; safe to call from a worker."""

        analysis_log = getattr(self, "analysis_log", None)
        if analysis_log is None:
            return None
        path = analysis_log.write(
            text,
            getattr(self, "session_api_key", ""),
            level=level,
            section=section,
        )
        self._session_log_path = path
        return path

    def _persist_worker_log(
        self,
        message: str,
        *,
        level: str,
        section: str,
    ) -> _DurableLogEvent:
        """Write a worker notice before queueing it, without touching Tk."""

        safe_message = _redact_sensitive(
            message,
            getattr(self, "session_api_key", ""),
        )
        persisted = False
        log_error = ""
        try:
            persisted = self._write_session_log(
                safe_message,
                level=level,
                section=section,
            ) is not None
        except OSError as exc:
            log_error = _redact_sensitive(
                str(exc),
                getattr(self, "session_api_key", ""),
            )
        return _DurableLogEvent(
            message=safe_message,
            persisted=persisted,
            log_error=log_error,
        )

    def _prepare_translation_success(
        self,
        outcome: TranslationOutcome,
        analysis: _InstanceAnalysis,
    ) -> _WorkerTranslationEvent:
        """Make translation completion durable before the worker can exit."""

        analyzed = getattr(analysis, "analyzed", None)
        project = getattr(analyzed, "project", None)
        notice = self._persist_worker_log(
            _format_translation_completion(outcome, project),
            level="SUCCESS",
            section="翻訳完了",
        )
        return _WorkerTranslationEvent(
            outcome=outcome,
            analysis=analysis,
            notice=notice,
        )

    def _render_durable_log_event(
        self,
        notice: _DurableLogEvent,
        tag: str | None,
        *,
        section: str,
    ) -> None:
        """Render a worker notice and retry persistence only when needed."""

        self._append_log(
            notice.message,
            tag,
            persist=not notice.persisted,
            section=section,
        )
        if notice.log_error:
            self._report_session_log_error(notice.log_error)

    def _insert_gui_log(
        self,
        text: str,
        tag: str | None,
        *,
        replace_existing: bool,
    ) -> None:
        self.log.configure(state="normal")
        if replace_existing:
            self.log.delete("1.0", "end")
        self.log.insert("end", text.rstrip() + "\n", tag or ())
        if replace_existing:
            self.log.see("1.0")
        else:
            line_count = int(self.log.index("end-1c").split(".", 1)[0])
            if line_count > 4000:
                self.log.delete("1.0", f"{line_count - 3000}.0")
            self.log.see("end")
        self.log.configure(state="disabled")

    def _report_session_log_error(self, error: str) -> None:
        """Report persistence failure directly, without recursively logging it."""

        safe_error = _redact_sensitive(error, getattr(self, "session_api_key", ""))
        self._session_log_last_error = safe_error
        if hasattr(self, "detected_log_var"):
            self.detected_log_var.set(self._session_log_status_text())
        reported = getattr(self, "_session_log_errors", None)
        if reported is None:
            reported = set()
            self._session_log_errors = reported
        if safe_error in reported:
            return
        reported.add(safe_error)
        if hasattr(self, "log") or "_insert_gui_log" in getattr(self, "__dict__", {}):
            self._insert_gui_log(
                "\nセッションログを保存できませんでした: "
                + safe_error,
                "error",
                replace_existing=False,
            )

    def _append_log(
        self,
        text: str,
        tag: str | None = None,
        *,
        persist: bool = True,
        section: str = "実行ログ",
    ) -> None:
        log_error = ""
        if persist:
            try:
                self._write_session_log(
                    text,
                    level=self._log_level(tag),
                    section=section,
                )
            except OSError as exc:
                log_error = _redact_sensitive(
                    str(exc),
                    getattr(self, "session_api_key", ""),
                )
        self._insert_gui_log(text, tag, replace_existing=False)
        if hasattr(self, "detected_log_var"):
            self.detected_log_var.set(self._session_log_status_text())
        if log_error:
            self._report_session_log_error(log_error)

    def _replace_log(
        self,
        text: str,
        tag: str | None = None,
        *,
        persist: bool = True,
        section: str = "実行ログ",
    ) -> None:
        log_error = ""
        if persist:
            try:
                self._write_session_log(
                    text,
                    level=self._log_level(tag),
                    section=section,
                )
            except OSError as exc:
                log_error = _redact_sensitive(
                    str(exc),
                    getattr(self, "session_api_key", ""),
                )
        self._insert_gui_log(text, tag, replace_existing=True)
        if hasattr(self, "detected_log_var"):
            self.detected_log_var.set(self._session_log_status_text())
        if log_error:
            self._report_session_log_error(log_error)

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set("キャンセルを要求しました…")
        self._append_log(
            "\nキャンセルを要求しました。安全に停止できる点まで待機します。",
            "warning",
            section="キャンセル要求",
        )

    def _set_busy(self, busy: bool) -> None:
        effective_busy = busy or self.close_pending
        if not busy:
            self.progress.stop()
            self.progress.configure(mode="determinate")
        state = "disabled" if effective_busy else "normal"
        self.analyze_button.configure(state=state)
        self.translate_button.configure(
            state="disabled" if effective_busy or self._analyzed is None else "normal"
        )
        self.settings_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy and not self.close_pending else "disabled")
        for widget, normal_state in self.request_widgets:
            widget.configure(state="disabled" if effective_busy else normal_state)

    def _save_settings(self) -> None:
        self.settings.minecraft_version = (
            self._instance_info.minecraft_version if self._instance_info is not None else ""
        )
        self.settings.adapter_id = "auto"
        self.settings.translation_categories = [
            category.id
            for category in FTB_TRANSLATION_CATEGORIES
            if self.category_vars[category.id].get()
        ]
        self.settings.last_source_path = self.instance_var.get().strip()
        try:
            self.store.save(self.settings)
        except OSError as exc:
            self._append_log(
                f"警告: 設定を保存できませんでした ({exc})",
                "warning",
                section="設定保存エラー",
            )

    def _on_close(self) -> None:
        if self.close_pending:
            return
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("終了", "処理中です。キャンセルして終了しますか？"):
                return
            self.close_pending = True
            self.cancel_event.set()
            self.close_deadline = time.monotonic() + _CLOSE_WORKER_GRACE_SECONDS
            self.status_var.set("処理の停止を待っています…")
            self._append_log(
                "\n終了要求を受け付けました。実行中の処理をキャンセルします。",
                "warning",
                section="終了要求",
            )
            self._set_busy(True)
            self.root.after(100, self._close_when_worker_stops)
            return
        self._finish_close()

    def _close_when_worker_stops(self) -> None:
        if self.worker and self.worker.is_alive():
            if (
                not self.worker_write_started.is_set()
                and self.close_deadline is not None
                and time.monotonic() >= self.close_deadline
            ):
                # Analysis, output snapshotting, JAR scanning, and API waits
                # cannot write before their subsequent cancellation checks.
                # The worker is a daemon, so a stuck read must not trap the GUI.
                self._finish_close()
                return
            self.root.after(100, self._close_when_worker_stops)
            return
        self._finish_close()

    def _finish_close(self) -> None:
        if self._drain_after_id is not None:
            try:
                self.root.after_cancel(self._drain_after_id)
            except tk.TclError:
                pass
            self._drain_after_id = None
        self._save_settings()
        try:
            self._write_session_log(
                "アプリケーションを終了します。",
                level="INFO",
                section="終了",
            )
        except OSError:
            # The window is about to close.  Do not recurse through the same
            # failing journal or delay shutdown with another dialog.
            pass
        self.root.destroy()


class SettingsDialog:
    def __init__(
        self,
        parent: tk.Tk,
        settings: AppSettings,
        api_key: str,
        store: SettingsStore,
        on_save: Callable[[str, AppSettings], None],
        on_log: Callable[..., None] | None = None,
        on_durable_log: Callable[..., _DurableLogEvent] | None = None,
        *,
        initial_tab: str = "translation",
    ) -> None:
        self.parent = parent
        # Keep dialog edits isolated until persistence succeeds and Save is
        # pressed; otherwise a failed save would still mutate the main window.
        self.settings = _copy_settings(settings)
        self.store = store
        self.on_save = on_save
        self.on_log = on_log
        self.on_durable_log = on_durable_log
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("設定")
        self.window.transient(parent)
        self.parent.update_idletasks()
        _configure_window_size(
            self.window,
            800,
            720,
            520,
            400,
            horizontal_margin=80,
            vertical_margin=140,
            position=(self.parent.winfo_x(), self.parent.winfo_y()),
        )
        self.source_locale_var = tk.StringVar(value=self.settings.source_locale)
        self.target_locale_var = tk.StringVar(value=self.settings.target_locale)
        self.preserve_var = tk.BooleanVar(value=self.settings.preserve_existing)
        self.scan_resourcepacks_var = tk.BooleanVar(
            value=self.settings.scan_resourcepacks
        )
        self.skip_glossary_confirmation_var = tk.BooleanVar(
            value=self.settings.skip_glossary_confirmation
        )
        self.glossary_scan_limits_enabled_var = tk.BooleanVar(
            value=self.settings.glossary_scan_limits_enabled
        )
        self.glossary_max_source_members_var = tk.IntVar(
            value=self.settings.glossary_max_source_members
        )
        self.glossary_max_language_file_mib_var = tk.IntVar(
            value=self.settings.glossary_max_language_file_mib
        )
        self.glossary_max_source_language_mib_var = tk.IntVar(
            value=self.settings.glossary_max_source_language_mib
        )
        self.glossary_max_total_language_mib_var = tk.IntVar(
            value=self.settings.glossary_max_total_language_mib
        )
        self.glossary_limit_status_var = tk.StringVar(value="")
        self._initial_api_key = api_key.strip()
        self.api_key_from_environment = _api_key_is_environment_value(api_key)
        self.api_key_var = tk.StringVar(value=api_key)
        self.save_key_var = tk.BooleanVar(
            value=_save_api_key_initially_selected(
                settings,
                store.secure_persistence_available,
                self.api_key_from_environment,
            )
        )
        self.model_var = tk.StringVar(value=settings.model)
        self.fast_mode_var = tk.BooleanVar(value=settings.fast_mode)
        self.batch_var = tk.IntVar(value=settings.batch_size)
        self.char_limit_var = tk.IntVar(value=settings.batch_char_limit)
        self.timeout_var = tk.IntVar(value=settings.request_timeout)
        self.retry_var = tk.IntVar(value=settings.max_retries)
        self.models = _model_candidates(settings)
        self.status_var = tk.StringVar(
            value=(
                "保存済みモデル一覧を読み込みました。必要な場合だけ再取得してください。"
                if self.models
                else "APIキーを入力し、モデル一覧を取得してください。"
            )
        )
        self.fetching = False
        self.model_cancel_event = threading.Event()
        self.model_events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._build()
        self._select_tab(initial_tab)
        self.window.bind("<Escape>", lambda _event: self._close())
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self._drain_after_id: str | None = self.window.after(
            100,
            self._drain_model_events,
        )
        self.window.deiconify()
        self.window.grab_set()

    def _build(self) -> None:
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self.window)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=14, pady=(14, 8))
        self.notebook.enable_traversal()

        self.translation_tab = ttk.Frame(self.notebook)
        self.glossary_tab = ttk.Frame(self.notebook)
        self.openai_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.translation_tab, text="翻訳")
        self.notebook.add(self.glossary_tab, text="固有名詞保護")
        self.notebook.add(self.openai_tab, text="OpenAI")

        self.translation_scroll_pane = _ScrollablePane(
            self.translation_tab,
            padding=18,
            wheel_master=self.window,
        )
        self.glossary_scroll_pane = _ScrollablePane(
            self.glossary_tab,
            padding=18,
            wheel_master=self.window,
        )
        self.openai_scroll_pane = _ScrollablePane(
            self.openai_tab,
            padding=18,
            wheel_master=self.window,
        )
        self.tab_scroll_panes = {
            "translation": self.translation_scroll_pane,
            "glossary": self.glossary_scroll_pane,
            "openai": self.openai_scroll_pane,
        }
        for pane in self.tab_scroll_panes.values():
            pane.pack(fill="both", expand=True)
        # Keep the established attribute for callers that only need the
        # OpenAI tab's long, scrollable content.
        self.scroll_pane = self.openai_scroll_pane

        self._build_translation_tab(self.translation_scroll_pane.content)
        self._build_glossary_tab(self.glossary_scroll_pane.content)
        self._build_openai_tab(self.openai_scroll_pane.content)

        buttons = ttk.Frame(self.window, padding=(14, 0, 14, 14))
        buttons.grid(row=1, column=0, sticky="e")
        self.cancel_settings_button = ttk.Button(
            buttons,
            text="キャンセル",
            command=self._close,
        )
        self.cancel_settings_button.pack(side="left", padx=(0, 8))
        self.save_button = ttk.Button(buttons, text="保存", command=self._save)
        self.save_button.pack(side="left")

    def _select_tab(self, tab_name: str) -> None:
        notebook = getattr(self, "notebook", None)
        tab = {
            "translation": getattr(self, "translation_tab", None),
            "glossary": getattr(self, "glossary_tab", None),
            "openai": getattr(self, "openai_tab", None),
        }.get(tab_name)
        if notebook is not None and tab is not None:
            notebook.select(tab)

    def _build_translation_tab(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        ttk.Label(
            frame,
            text="翻訳言語",
            font=("Yu Gothic UI", 10, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        locale_choices = _locale_candidates(
            self.settings.source_locale,
            self.settings.target_locale,
        )
        ttk.Label(frame, text="原文locale").grid(
            row=1,
            column=0,
            sticky="w",
            padx=(0, 12),
            pady=6,
        )
        self.source_locale_combo = ttk.Combobox(
            frame,
            textvariable=self.source_locale_var,
            values=locale_choices,
            state="readonly",
            height=16,
            width=14,
        )
        self.source_locale_combo.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="翻訳先locale").grid(
            row=2,
            column=0,
            sticky="w",
            padx=(0, 12),
            pady=6,
        )
        self.target_locale_combo = ttk.Combobox(
            frame,
            textvariable=self.target_locale_var,
            values=locale_choices,
            state="readonly",
            height=16,
            width=14,
        )
        self.target_locale_combo.grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Label(
            frame,
            text=(
                "locale、resourcepacks走査設定、または走査上限を変更して保存すると、"
                "安全のため現在の解析結果を無効にします。"
            ),
            foreground="#4b5563",
            justify="left",
            wraplength=620,
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 18))

        existing_box = ttk.LabelFrame(frame, text="既存翻訳", padding=12)
        existing_box.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.preserve_check = ttk.Checkbutton(
            existing_box,
            text="選択した項目の既存翻訳を再利用する",
            variable=self.preserve_var,
        )
        self.preserve_check.pack(anchor="w")
        ttk.Label(
            existing_box,
            text=(
                "安全確認に合格した既存訳だけを再利用します。無効にすると、"
                "選択した項目をOpenAIであらためて翻訳します。"
            ),
            foreground="#4b5563",
            justify="left",
            wraplength=620,
        ).pack(anchor="w", padx=(24, 0), pady=(4, 0))

    def _build_glossary_tab(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(0, weight=1)
        ttk.Label(
            frame,
            text="走査対象",
            font=("Yu Gothic UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.scan_resourcepacks_check = ttk.Checkbutton(
            frame,
            text="resourcepacksも固有名詞保護用に走査する",
            variable=self.scan_resourcepacks_var,
        )
        self.scan_resourcepacks_check.grid(row=1, column=0, sticky="w")
        ttk.Label(
            frame,
            text=(
                "Mod、Minecraft本体、kubejs/assetsは常に自動検出します。"
                "resourcepacksは走査対象が増えるため、必要な場合だけ有効にしてください。\n"
                "同じnamespace・keyはMinecraft本体、Mod、KubeJS、resource packの順で"
                "優先し、下位の値では上書きしません。"
            ),
            foreground="#4b5563",
            justify="left",
            wraplength=650,
        ).grid(row=2, column=0, sticky="ew", padx=(24, 0), pady=(4, 20))

        ttk.Separator(frame, orient="horizontal").grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(0, 18),
        )

        limit_box = ttk.LabelFrame(
            frame,
            text="固有名詞保護の走査上限（Minecraft本体を除く）",
            padding=12,
        )
        limit_box.grid(row=4, column=0, sticky="ew")
        limit_box.columnconfigure(0, weight=1)

        self.glossary_scan_limits_enabled_check = ttk.Checkbutton(
            limit_box,
            text="走査上限を有効にする",
            variable=self.glossary_scan_limits_enabled_var,
            command=self._update_glossary_limit_states,
        )
        self.glossary_scan_limits_enabled_check.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 8),
        )

        limit_rows = (
            (
                "1資産の項目数上限",
                self.glossary_max_source_members_var,
                SOURCE_MEMBERS_MIN,
                SOURCE_MEMBERS_MAX,
                10_000,
                "glossary_max_source_members_spin",
            ),
            (
                "言語ファイル1件の上限（MiB）",
                self.glossary_max_language_file_mib_var,
                LANGUAGE_FILE_MIB_MIN,
                LANGUAGE_FILE_MIB_MAX,
                1,
                "glossary_max_language_file_mib_spin",
            ),
            (
                "1資産の言語ファイル合計上限（MiB）",
                self.glossary_max_source_language_mib_var,
                SOURCE_LANGUAGE_MIB_MIN,
                SOURCE_LANGUAGE_MIB_MAX,
                8,
                "glossary_max_source_language_mib_spin",
            ),
            (
                "全資産の言語ファイル合計上限（MiB）",
                self.glossary_max_total_language_mib_var,
                TOTAL_LANGUAGE_MIB_MIN,
                TOTAL_LANGUAGE_MIB_MAX,
                64,
                "glossary_max_total_language_mib_spin",
            ),
        )
        self.glossary_limit_value_widgets: list[tk.Misc] = []
        for row, (label, variable, minimum, maximum, increment, attribute) in enumerate(
            limit_rows,
            start=1,
        ):
            label_widget = ttk.Label(
                limit_box,
                text=label,
                justify="left",
                wraplength=470,
            )
            label_widget.grid(
                row=row,
                column=0,
                sticky="ew",
                padx=(0, 12),
                pady=4,
            )
            spin = ttk.Spinbox(
                limit_box,
                from_=minimum,
                to=maximum,
                increment=increment,
                textvariable=variable,
                width=12,
            )
            spin.grid(row=row, column=1, sticky="w", pady=4)
            setattr(self, attribute, spin)
            self.glossary_limit_value_widgets.extend((label_widget, spin))

        ttk.Label(
            limit_box,
            text=(
                "チェックを外すと4つの上限をすべて無効にします。\n"
                "1資産：Mod JAR 1件 / kubejs/assets全体 / resource pack 1件。\n"
                "項目数：探索で確認するファイル・フォルダー名の数です。本文の読込数ではなく、"
                "内容は読み込みません。\n"
                "サイズ：原文・翻訳先言語ファイルの展開後サイズです。Minecraft本体は対象外です。"
            ),
            foreground="#4b5563",
            justify="left",
            wraplength=610,
        ).grid(
            row=len(limit_rows) + 1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(8, 8),
        )
        limit_actions = ttk.Frame(limit_box)
        limit_actions.grid(
            row=len(limit_rows) + 2,
            column=0,
            columnspan=2,
            sticky="ew",
        )
        self.reset_glossary_limits_button = ttk.Button(
            limit_actions,
            text="既定値に戻す",
            command=self._reset_glossary_scan_limits,
        )
        self.reset_glossary_limits_button.pack(side="left")
        ttk.Label(
            limit_actions,
            textvariable=self.glossary_limit_status_var,
            foreground="#4b5563",
        ).pack(side="left", padx=(10, 0))
        self._update_glossary_limit_states()

        ttk.Separator(frame, orient="horizontal").grid(
            row=5,
            column=0,
            sticky="ew",
            pady=(18, 18),
        )
        ttk.Label(
            frame,
            text="続行確認",
            font=("Yu Gothic UI", 10, "bold"),
        ).grid(row=6, column=0, sticky="w", pady=(0, 8))
        self.skip_glossary_confirmation_check = ttk.Checkbutton(
            frame,
            text="固有名詞保護の確認をスキップする",
            variable=self.skip_glossary_confirmation_var,
        )
        self.skip_glossary_confirmation_check.grid(row=7, column=0, sticky="w")
        ttk.Label(
            frame,
            text=(
                "保護資産が不足または一部失敗した場合の続行確認だけを省略します。"
                "固有名詞の走査・保護処理と翻訳結果の安全確認は無効になりません。"
            ),
            foreground="#4b5563",
            justify="left",
            wraplength=650,
        ).grid(row=8, column=0, sticky="ew", padx=(24, 0), pady=(4, 0))

    def _build_openai_tab(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(5, weight=1)
        ttk.Label(frame, text="OpenAI APIキー").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=6)
        self.api_key_entry = ttk.Entry(frame, textvariable=self.api_key_var, show="●")
        self.api_key_entry.grid(row=0, column=1, sticky="ew", pady=6)
        self.save_key_check = ttk.Checkbutton(
            frame,
            text=(
                "環境変数のAPIキーをWindows DPAPIへ暗号化保存（明示時のみ）"
                if self.api_key_from_environment
                else "Windows DPAPIでこのユーザー用に暗号化保存"
            ),
            variable=self.save_key_var,
            state="normal" if self.store.secure_persistence_available else "disabled",
        )
        self.save_key_check.grid(row=1, column=1, sticky="w")

        ttk.Label(frame, text="モデル").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(16, 6))
        self.model_combo = ttk.Combobox(
            frame,
            textvariable=self.model_var,
            values=self.models,
            state="readonly",
        )
        self.model_combo.grid(row=2, column=1, sticky="ew", pady=(16, 6))
        self.fetch_button = ttk.Button(frame, text="利用可能なモデルを取得", command=self._fetch_models)
        self.fetch_button.grid(row=3, column=1, sticky="w")

        advanced = ttk.LabelFrame(frame, text="翻訳リクエスト", padding=10)
        advanced.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 6))
        for column in range(6):
            advanced.columnconfigure(column, weight=1 if column % 2 else 0)
        ttk.Label(advanced, text="1バッチ件数").grid(row=0, column=0, padx=(0, 6))
        self.batch_spin = ttk.Spinbox(advanced, from_=1, to=100, textvariable=self.batch_var, width=7)
        self.batch_spin.grid(row=0, column=1, sticky="w")
        ttk.Label(advanced, text="最大文字数").grid(row=0, column=2, padx=(14, 6))
        self.char_limit_spin = ttk.Spinbox(
            advanced,
            from_=500,
            to=50000,
            increment=500,
            textvariable=self.char_limit_var,
            width=9,
        )
        self.char_limit_spin.grid(row=0, column=3, sticky="w")
        ttk.Label(advanced, text="timeout秒").grid(row=0, column=4, padx=(14, 6))
        self.timeout_spin = ttk.Spinbox(advanced, from_=10, to=600, textvariable=self.timeout_var, width=7)
        self.timeout_spin.grid(row=0, column=5, sticky="w")
        ttk.Label(advanced, text="再試行回数（初回を除く）").grid(
            row=1,
            column=0,
            padx=(0, 6),
            pady=(10, 0),
        )
        self.retry_spin = ttk.Spinbox(
            advanced,
            from_=0,
            to=10,
            textvariable=self.retry_var,
            width=7,
        )
        self.retry_spin.grid(row=1, column=1, sticky="w", pady=(10, 0))
        ttk.Label(
            advanced,
            text="0で通信エラー・timeoutの自動再試行を無効にします。",
            foreground="#4b5563",
            justify="left",
        ).grid(
            row=1,
            column=2,
            columnspan=4,
            sticky="w",
            padx=(14, 0),
            pady=(10, 0),
        )
        self.fast_mode_check = ttk.Checkbutton(
            advanced,
            text="Fast Modeを使用",
            variable=self.fast_mode_var,
        )
        self.fast_mode_check.grid(
            row=2,
            column=0,
            columnspan=6,
            sticky="w",
            pady=(10, 0),
        )
        self.fast_mode_help = ttk.Label(
            advanced,
            text=(
                "翻訳POSTだけにpriority tierを指定します。"
                "利用可否・追加料金はOpenAIの契約に依存します。"
            ),
            foreground="#4b5563",
            justify="left",
            wraplength=540,
        )
        self.fast_mode_help.grid(
            row=3,
            column=0,
            columnspan=6,
            sticky="w",
            pady=(2, 0),
        )

        prompt_box = ttk.LabelFrame(frame, text="カスタム翻訳プロンプト", padding=10)
        prompt_box.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(10, 6))
        prompt_box.columnconfigure(0, weight=1)
        prompt_box.rowconfigure(1, weight=1)
        prompt_header = ttk.Frame(prompt_box)
        prompt_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        prompt_header.columnconfigure(0, weight=1)
        ttk.Label(
            prompt_header,
            text="{source_locale} と {target_locale} は翻訳時に実際のlocaleへ置換されます。",
            foreground="#4b5563",
            justify="left",
            wraplength=360,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.reset_prompt_button = ttk.Button(
            prompt_header,
            text="既定に戻す…",
            command=self._reset_prompt,
        )
        self.reset_prompt_button.grid(row=0, column=1, sticky="e")
        self.prompt_text = tk.Text(
            prompt_box,
            height=10,
            width=1,
            wrap="word",
            undo=True,
            font=("Yu Gothic UI", 9),
        )
        prompt_scrollbar = ttk.Scrollbar(
            prompt_box,
            orient="vertical",
            command=self.prompt_text.yview,
        )
        self.prompt_text.configure(yscrollcommand=prompt_scrollbar.set)
        self.prompt_text.grid(row=1, column=0, sticky="nsew")
        prompt_scrollbar.grid(row=1, column=1, sticky="ns")
        initial_prompt = self.settings.translation_prompt.strip() or DEFAULT_TRANSLATION_PROMPT
        self.prompt_text.insert("1.0", initial_prompt)

        ttk.Label(frame, textvariable=self.status_var, foreground="#4b5563", wraplength=560).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(8, 14)
        )

    def _fetch_models(self) -> None:
        api_key = self.api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("APIキー", "OpenAI APIキーを入力してください。", parent=self.window)
            return
        try:
            timeout = int(self.timeout_var.get())
            max_retries = int(self.retry_var.get())
            if not 10 <= timeout <= 600:
                raise ValueError("timeout秒は10〜600で指定してください")
            if not 0 <= max_retries <= 10:
                raise ValueError("再試行回数は0〜10で指定してください")
        except (ValueError, tk.TclError) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "timeout秒と再試行回数に整数を入力してください。"
            )
            messagebox.showwarning("リクエスト設定", message, parent=self.window)
            return
        self._set_fetching(True)
        self.status_var.set("OpenAIからモデル一覧を取得しています…")
        self.model_cancel_event.set()
        self.model_cancel_event = threading.Event()
        cancel_event = self.model_cancel_event

        def work() -> None:
            try:
                client = OpenAIClient(
                    timeout=timeout,
                    max_retries=max_retries,
                    on_retry=lambda event: self._queue_model_retry(event, api_key),
                )
                models = client.list_models(api_key, cancel_event)
                self.model_events.put(("loaded", (api_key, models)))
            except CancelledError:
                return
            except LocalizerError as exc:
                self._queue_model_failure(str(exc), api_key)
            except BaseException as exc:
                self._queue_model_failure(str(exc), api_key)

        threading.Thread(target=work, name="mq-model-loader", daemon=True).start()

    def _queue_model_retry(self, event: OpenAIRetryEvent, api_key: str) -> None:
        """Persist a model-list retry without reading or calling Tk."""

        safe_message = _redact_sensitive(_format_openai_retry(event), api_key)
        notice = _DurableLogEvent(message=safe_message)
        on_durable_log = getattr(self, "on_durable_log", None)
        if on_durable_log is not None:
            try:
                persisted_notice = on_durable_log(
                    safe_message,
                    level="WARNING",
                    section="OpenAI通信再試行",
                )
                if isinstance(persisted_notice, _DurableLogEvent):
                    notice = replace(
                        persisted_notice,
                        message=_redact_sensitive(persisted_notice.message, api_key),
                        log_error=_redact_sensitive(persisted_notice.log_error, api_key),
                    )
            except Exception as exc:
                notice = replace(
                    notice,
                    log_error=_redact_sensitive(str(exc), api_key),
                )
        self.model_events.put(("retry", notice))

    def _queue_model_failure(self, message: str, api_key: str) -> None:
        """Persist a model-loader failure without reading or calling Tk."""

        safe_message = _redact_sensitive(
            "OpenAIのモデル一覧を取得できませんでした。\n" + message,
            api_key,
        )
        notice = _DurableLogEvent(message=safe_message)
        on_durable_log = getattr(self, "on_durable_log", None)
        if on_durable_log is not None:
            try:
                persisted_notice = on_durable_log(
                    safe_message,
                    level="ERROR",
                    section="OpenAIモデル一覧取得エラー",
                )
                if isinstance(persisted_notice, _DurableLogEvent):
                    notice = replace(
                        persisted_notice,
                        message=_redact_sensitive(persisted_notice.message, api_key),
                        log_error=_redact_sensitive(persisted_notice.log_error, api_key),
                    )
            except Exception as exc:
                notice = replace(
                    notice,
                    log_error=_redact_sensitive(str(exc), api_key),
                )
        self.model_events.put(("failed", notice))

    def _drain_model_events(self) -> None:
        self._drain_after_id = None
        try:
            exists = self.window.winfo_exists()
        except tk.TclError:
            return
        if not exists:
            return
        try:
            while True:
                event, payload = self.model_events.get_nowait()
                if event == "loaded":
                    api_key, models = payload
                    self._models_loaded(api_key, models)
                elif event == "retry":
                    self._model_retrying(payload)
                else:
                    self._models_failed(payload)
        except queue.Empty:
            pass
        self._drain_after_id = self.window.after(100, self._drain_model_events)

    def _models_loaded(self, api_key: str, models: list[ModelInfo]) -> None:
        if not self.window.winfo_exists():
            return
        if self.api_key_var.get().strip() != api_key:
            self.status_var.set("APIキーが変更されたため、取得結果を破棄しました。")
            self._set_fetching(False)
            return
        loaded_models = list(
            dict.fromkeys(
                model.id.strip()
                for model in models
                if model.id.strip() and len(model.id) <= 512
            )
        )[:MAX_CACHED_MODEL_COUNT]
        if not loaded_models:
            if self.models:
                self.status_var.set(
                    "テキスト生成モデルを取得できなかったため、保存済み候補を維持します。"
                )
            else:
                self.status_var.set("利用できるテキスト生成モデルが見つかりませんでした。")
            self._set_fetching(False)
            return
        self.models = loaded_models
        self.model_combo.configure(values=self.models)
        if self.model_var.get() not in self.models:
            self.model_var.set(self.models[0] if self.models else "")
        self.status_var.set(f"{len(self.models)} 件のテキスト生成モデルを取得しました。")
        self._set_fetching(False)

    def _model_retrying(self, notice: _DurableLogEvent) -> None:
        if not self.window.winfo_exists():
            return
        self.status_var.set("OpenAI通信を再試行しています…")
        on_log = getattr(self, "on_log", None)
        if on_log is not None:
            on_log(
                "\n=== OpenAI通信再試行 ===\n" + notice.message,
                "warning",
                "OpenAI通信再試行",
                not notice.persisted,
            )
            if notice.log_error:
                on_log(
                    "セッションログを保存できませんでした: " + notice.log_error,
                    "error",
                    "セッションログ保存エラー",
                    False,
                )

    def _models_failed(self, payload: str | _DurableLogEvent) -> None:
        if not self.window.winfo_exists():
            return
        if isinstance(payload, _DurableLogEvent):
            notice = payload
        else:
            notice = _DurableLogEvent(
                message="OpenAIのモデル一覧を取得できませんでした。\n" + payload
            )
        safe_message = self._log_settings_failure(
            notice.message,
            section="OpenAIモデル一覧取得エラー",
            persist=not notice.persisted,
        )
        self.status_var.set("モデル一覧を取得できませんでした。")
        self._set_fetching(False)
        messagebox.showerror("OpenAI API", safe_message.split("\n", 1)[-1], parent=self.window)

    def _log_settings_failure(
        self,
        message: str,
        *,
        section: str,
        persist: bool = True,
    ) -> str:
        """Redact dialog credentials and forward a durable failure event."""

        api_key_var = getattr(self, "api_key_var", None)
        api_key = api_key_var.get().strip() if api_key_var is not None else ""
        safe_message = _redact_sensitive(message, api_key)
        on_log = getattr(self, "on_log", None)
        if on_log is not None:
            if persist:
                on_log(safe_message, "error", section)
            else:
                on_log(safe_message, "error", section, False)
        return safe_message

    def _set_fetching(self, fetching: bool) -> None:
        self.fetching = fetching
        state = "disabled" if fetching else "normal"
        self.api_key_entry.configure(state=state)
        self.save_key_check.configure(
            state="disabled" if fetching or not self.store.secure_persistence_available else "normal"
        )
        self.model_combo.configure(state="disabled" if fetching else "readonly")
        self.batch_spin.configure(state=state)
        self.char_limit_spin.configure(state=state)
        self.timeout_spin.configure(state=state)
        self.retry_spin.configure(state=state)
        self.fast_mode_check.configure(state=state)
        self.prompt_text.configure(state=state)
        self.reset_prompt_button.configure(state=state)
        self.fetch_button.configure(state=state)
        self.save_button.configure(state=state)

    def _reset_prompt(self) -> None:
        if not messagebox.askyesno(
            "プロンプトをリセット",
            "現在の編集内容を破棄し、既定の翻訳プロンプトに戻しますか？",
            parent=self.window,
        ):
            return
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", DEFAULT_TRANSLATION_PROMPT)
        self.prompt_text.focus_set()
        self.status_var.set("翻訳プロンプトを既定値に戻しました。保存すると反映されます。")

    def _update_glossary_limit_states(self) -> None:
        enabled_var = getattr(self, "glossary_scan_limits_enabled_var", None)
        enabled = True if enabled_var is None else bool(enabled_var.get())
        state = "normal" if enabled else "disabled"
        for widget in getattr(self, "glossary_limit_value_widgets", ()):
            widget.configure(state=state)
        reset_button = getattr(self, "reset_glossary_limits_button", None)
        if reset_button is not None:
            reset_button.configure(state=state)
        status_var = getattr(self, "glossary_limit_status_var", None)
        if status_var is not None:
            status_var.set(
                "4つの上限は有効です。［保存］で反映します。"
                if enabled
                else "4つの上限は無効です。［保存］で反映します。"
            )

    def _reset_glossary_scan_limits(self) -> None:
        defaults = GlossaryScanLimits()
        enabled_var = getattr(self, "glossary_scan_limits_enabled_var", None)
        if enabled_var is not None:
            enabled_var.set(defaults.enabled)
        self.glossary_max_source_members_var.set(defaults.max_source_members)
        self.glossary_max_language_file_mib_var.set(
            defaults.max_language_file_mib
        )
        self.glossary_max_source_language_mib_var.set(
            defaults.max_source_language_mib
        )
        self.glossary_max_total_language_mib_var.set(
            defaults.max_total_language_mib
        )
        self._update_glossary_limit_states()
        self.glossary_limit_status_var.set(
            "既定値を入力しました。［保存］で反映します。"
        )

    def _save(self) -> None:
        def value(name: str, fallback: object) -> object:
            variable = getattr(self, name, None)
            return variable.get() if variable is not None else fallback

        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        source_locale = str(
            value("source_locale_var", self.settings.source_locale)
        ).strip().lower()
        target_locale = str(
            value("target_locale_var", self.settings.target_locale)
        ).strip().lower()
        if not _is_valid_locale(source_locale) or not _is_valid_locale(target_locale):
            self._select_tab("translation")
            messagebox.showwarning(
                "locale",
                "原文localeと翻訳先localeを選択してください。",
                parent=self.window,
            )
            return
        if source_locale == target_locale:
            self._select_tab("translation")
            messagebox.showwarning(
                "locale",
                "原文localeと翻訳先localeには異なる値を選択してください。",
                parent=self.window,
            )
            return
        try:
            glossary_scan_limits = _validated_glossary_scan_limits(
                value(
                    "glossary_scan_limits_enabled_var",
                    self.settings.glossary_scan_limits_enabled,
                ),
                value(
                    "glossary_max_source_members_var",
                    self.settings.glossary_max_source_members,
                ),
                value(
                    "glossary_max_language_file_mib_var",
                    self.settings.glossary_max_language_file_mib,
                ),
                value(
                    "glossary_max_source_language_mib_var",
                    self.settings.glossary_max_source_language_mib,
                ),
                value(
                    "glossary_max_total_language_mib_var",
                    self.settings.glossary_max_total_language_mib,
                ),
            )
        except (ValueError, tk.TclError) as exc:
            self._select_tab("glossary")
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "走査上限の4項目には整数を入力してください。"
            )
            messagebox.showwarning(
                "固有名詞保護の走査上限",
                message,
                parent=self.window,
            )
            return
        if model and model not in self.models:
            self._select_tab("openai")
            messagebox.showwarning(
                "モデル",
                "保存済みまたは取得済みのモデル一覧から選択してください。",
                parent=self.window,
            )
            return
        try:
            batch_size = int(self.batch_var.get())
            char_limit = int(self.char_limit_var.get())
            timeout = int(self.timeout_var.get())
            max_retries = int(self.retry_var.get())
            translation_prompt = self.prompt_text.get("1.0", "end-1c").strip()
            if not 1 <= batch_size <= 100:
                raise ValueError("1バッチ件数は1〜100で指定してください")
            if not 500 <= char_limit <= 50000:
                raise ValueError("最大文字数は500〜50000で指定してください")
            if not 10 <= timeout <= 600:
                raise ValueError("timeout秒は10〜600で指定してください")
            if not 0 <= max_retries <= 10:
                raise ValueError("再試行回数は0〜10で指定してください")
            if not translation_prompt:
                raise ValueError("翻訳プロンプトを入力してください")
            if len(translation_prompt) > MAX_TRANSLATION_PROMPT_LENGTH:
                raise ValueError(
                    f"翻訳プロンプトは{MAX_TRANSLATION_PROMPT_LENGTH}文字以内で入力してください"
                )
            self.settings.source_locale = source_locale
            self.settings.target_locale = target_locale
            self.settings.preserve_existing = bool(
                value("preserve_var", self.settings.preserve_existing)
            )
            self.settings.scan_resourcepacks = bool(
                value("scan_resourcepacks_var", self.settings.scan_resourcepacks)
            )
            self.settings.skip_glossary_confirmation = bool(
                value(
                    "skip_glossary_confirmation_var",
                    self.settings.skip_glossary_confirmation,
                )
            )
            self.settings.glossary_scan_limits_enabled = glossary_scan_limits.enabled
            self.settings.glossary_max_source_members = (
                glossary_scan_limits.max_source_members
            )
            self.settings.glossary_max_language_file_mib = (
                glossary_scan_limits.max_language_file_mib
            )
            self.settings.glossary_max_source_language_mib = (
                glossary_scan_limits.max_source_language_mib
            )
            self.settings.glossary_max_total_language_mib = (
                glossary_scan_limits.max_total_language_mib
            )
            self.settings.model = model
            self.settings.cached_models = list(self.models)
            self.settings.fast_mode = bool(self.fast_mode_var.get())
            self.settings.translation_prompt = translation_prompt
            self.settings.batch_size = batch_size
            self.settings.batch_char_limit = char_limit
            self.settings.request_timeout = timeout
            self.settings.max_retries = max_retries
            self._apply_api_key_persistence(api_key)
            self.store.save(self.settings)
        except (OSError, ValueError, tk.TclError) as exc:
            if not isinstance(exc, OSError):
                self._select_tab("openai")
            safe_message = self._log_settings_failure(
                "設定を保存できませんでした。\n" + str(exc),
                section="設定保存エラー",
            )
            messagebox.showerror(
                "設定保存",
                safe_message.split("\n", 1)[-1],
                parent=self.window,
            )
            return
        self.on_save(api_key, self.settings)
        self._close()

    def _apply_api_key_persistence(self, api_key: str) -> None:
        persist = bool(self.save_key_var.get())
        if (
            getattr(self, "api_key_from_environment", False)
            and api_key == getattr(self, "_initial_api_key", "")
            and not persist
        ):
            # OPENAI_API_KEY overrides a previously DPAPI-saved key at runtime.
            # Saving unrelated settings must neither overwrite that saved key
            # with the environment value nor erase it.  Checking the box is an
            # explicit opt-in; editing/clearing the field restores normal
            # session-key semantics.
            return
        self.store.set_api_key(self.settings, api_key, persist)

    def _close(self) -> None:
        self.model_cancel_event.set()
        after_id = getattr(self, "_drain_after_id", None)
        if after_id is not None:
            try:
                self.window.after_cancel(after_id)
            except tk.TclError:
                pass
            self._drain_after_id = None
        try:
            if self.window.winfo_exists():
                self.window.destroy()
        except tk.TclError:
            return
