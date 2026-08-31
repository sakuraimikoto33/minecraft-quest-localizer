from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections import OrderedDict
from collections.abc import Set
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..categories import REFERENCE_NAME_CATEGORY_IDS, classify_ftb_text
from ..domain import AdapterError, TranslationProject, TranslationUnit
from ..io_utils import atomic_write_text, read_text_detect
from ..output_guard import (
    PathSnapshot,
    assert_path_unchanged,
    assert_relocated_path_unchanged,
    assert_source_unchanged,
    snapshot_path,
)
from ..protection import looks_like_raw_json_text
from ..snbt import SnbtCompound, SnbtList, SnbtNode, SnbtParseError, SnbtScalar, SnbtString, parse_snbt
from .base import QuestAdapter, validate_translation_selection
from .path_safety import (
    paths_overlap,
    reject_reparse_ancestors,
    safe_is_directory,
)


_OLD_TRANSLATION = re.compile(r"^\{[A-Za-z0-9_.:-]+\}$")
_DIRECTIVE = re.compile(r"^\{(?:@pagebreak|image:.*)\}$", re.IGNORECASE)
_SAFE_KEY = re.compile(r"[^a-z0-9_.-]+")
_DYNAMIC_COMPONENT_FIELDS = frozenset(
    {"translate", "score", "selector", "keybind", "nbt", "object"}
)
class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Replacement:
    start: int
    end: int
    text: str


@dataclass(slots=True)
class _LegacyDocument:
    source_path: Path
    relative_path: Path
    source_text: str
    replacements: list[_Replacement]
    newline: str


@dataclass(frozen=True, slots=True)
class _LegacyInstallPlan:
    instance_root: Path
    kubejs_root: Path
    config_root: Path
    active_quest_root: Path
    backup_quest_root: Path
    source_quest_root: Path
    source_kind: str
    delivery: str
    target_locale: str
    asset_root: Path
    lang_root: Path

    @property
    def resourcepack_activation_required(self) -> bool:
        return self.delivery == "resourcepack"


@dataclass(frozen=True, slots=True)
class _CommittedDirectorySwap:
    destination: Path
    original_snapshot: PathSnapshot
    installed_snapshot: PathSnapshot
    backup: Path | None


@dataclass(frozen=True, slots=True)
class _CommittedQuestSwap:
    active_root: Path
    backup_root: Path
    source_kind: str
    original_active_snapshot: PathSnapshot
    original_backup_snapshot: PathSnapshot
    installed_snapshot: PathSnapshot


@dataclass(slots=True)
class _ExtractionState:
    relative_path: Path
    units: list[TranslationUnit] = field(default_factory=list)
    replacements: list[_Replacement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_keys: set[str] = field(default_factory=set)
    terminology_groups: list[list[str]] = field(default_factory=list)
    reference_term_groups: list[list[str]] = field(default_factory=list)

    def add_unit(self, key: str, source: str, context: str, source_path: Path) -> TranslationUnit:
        key = self.unique_key(key)
        unit = TranslationUnit(
            id=f"legacy-{len(self.units):07d}-{hashlib.sha1((str(self.relative_path) + key).encode()).hexdigest()[:8]}",
            key=key,
            source=_escape_percent(source),
            context=context,
            source_path=str(source_path),
            ordinal=len(self.units),
            category=classify_ftb_text(key, context),
        )
        self.units.append(unit)
        return unit

    def unique_key(self, key: str) -> str:
        base = key
        suffix = 2
        while key in self.used_keys:
            key = f"{base}.{suffix}"
            suffix += 1
        self.used_keys.add(key)
        return key


class FtbLegacyRawAdapter(QuestAdapter):
    id = "ftb_legacy_raw"
    managed_rerun_probe_score = 89
    probe_error_fallback_score = 60
    label = "FTB Quests 1.20.x以前（生のquest SNBT）"
    description = (
        "原本questをquests.bakへ保存し、キー化済みquestsと"
        "KubeJSまたはresource packの言語JSONを生成します。"
    )

    def probe(self, path: Path, source_locale: str) -> int:
        layout = _locate_quest_layout(path, allow_ambiguous=True)
        if layout is None:
            return 0
        if _is_reparse_point(layout.source_quest_root):
            # Keep probing read-only and let load report the actionable safety
            # error without following the redirected quest root.
            return 80 if layout.backup_quest_root.exists() else 60
        if not (layout.source_quest_root / "chapters").is_dir():
            return 0
        active = layout.active_quest_root
        if layout.source_kind == "backup" and active.is_dir():
            return self.managed_rerun_probe_score
        if (active / "lang" / f"{source_locale.lower()}.snbt").exists() or (
            active / "lang" / source_locale.lower()
        ).is_dir():
            return 0
        # After the first installation the generated language JSON is also a
        # valid legacy-JSON source.  Prefer the canonical quests.bak raw source
        # so re-running this conversion never silently changes adapters.
        return 80 if layout.backup_quest_root.exists() else 60

    def load(
        self,
        path: Path,
        source_locale: str,
        target_locale: str,
        minecraft_version: str = "",
        output_override: Path | None = None,
    ) -> TranslationProject:
        if _parsed_minecraft_version(minecraft_version) is None:
            raise AdapterError(
                "Minecraftバージョンを自動検出できず、"
                "旧版raw SNBTを安全に判定できません。"
                "ランチャーが作成した実際のインスタンスルート"
                "（mmc-pack.json等を含む）を選び、modsにFTB Quests本体が"
                "あることを確認してください"
            )
        if _version_at_least(minecraft_version, (1, 21, 0)):
            raise AdapterError(
                "Minecraft 1.21以降では旧版raw変換を使用できません。"
                "FTB Questsでクエストブックを一度開いてlocaleファイルを生成し、"
                f"lang/{source_locale.lower()}.snbt、lang/{source_locale.lower()}/、"
                "または分割JSON5が生成された同じインスタンスルートを再解析してください"
            )
        plan = _locate_quest_layout(path, target_locale=target_locale)
        if plan is None:
            raise AdapterError("FTB Quests の config/ftbquests/quests ディレクトリを判定できません")
        quest_root = plan.source_quest_root
        instance_root = plan.instance_root
        _reject_reparse_points(quest_root, "原文 quest")
        source_snapshot = snapshot_path(quest_root)
        active_snapshot = snapshot_path(plan.active_quest_root)
        backup_snapshot = snapshot_path(plan.backup_quest_root)
        source_files = _legacy_source_files(quest_root)
        if not source_files:
            raise AdapterError(f"翻訳対象の quest SNBT がありません: {quest_root}")

        all_units: list[TranslationUnit] = []
        terminology_groups: list[list[str]] = []
        reference_term_groups: list[list[str]] = []
        documents: list[_LegacyDocument] = []
        warnings: list[str] = []
        global_keys: set[str] = set()
        for source_file in source_files:
            text, _encoding, newline = read_text_detect(source_file)
            relative = source_file.relative_to(quest_root)
            try:
                root = parse_snbt(text)
            except SnbtParseError as exc:
                raise AdapterError(f"quest SNBT を解析できません: {source_file} ({exc})") from exc
            if not isinstance(root, SnbtCompound):
                warnings.append(f"{relative}: top-level compound ではないためスキップしました")
                continue
            state = _ExtractionState(relative_path=relative, used_keys=global_keys)
            root_kind = _root_kind(relative)
            _walk_object(root, root_kind, state, source_file, text)
            all_units.extend(state.units)
            terminology_groups.extend(state.terminology_groups)
            reference_term_groups.extend(state.reference_term_groups)
            warnings.extend(state.warnings)
            documents.append(_LegacyDocument(source_file, relative, text, state.replacements, newline))

        if not all_units:
            raise AdapterError(
                "翻訳可能な直書きテキストが見つかりません。既に {translation.key} 化されている場合は、"
                "対応する原文言語JSONがインスタンス内にあることを確認して、"
                "同じインスタンスルートを再解析してください。"
            )
        default_output = plan.asset_root
        if output_override is not None and not _same_lexical_path(
            Path(output_override),
            default_output,
        ):
            raise AdapterError(
                "旧版rawの出力先はクエスト原本の退避先と連動するため変更できません: "
                f"{default_output}"
            )
        _validate_install_plan(plan)
        asset_snapshot = snapshot_path(plan.asset_root)
        assert_source_unchanged(source_snapshot)
        assert_path_unchanged(active_snapshot)
        assert_path_unchanged(backup_snapshot)
        project = TranslationProject(
            adapter_id=self.id,
            adapter_label=self.label,
            source_path=quest_root,
            default_output=default_output,
            source_locale=source_locale,
            target_locale=target_locale,
            units=all_units,
            documents=documents,
            metadata={
                "quest_root": quest_root,
                "active_quest_root": plan.active_quest_root,
                "backup_quest_root": plan.backup_quest_root,
                "config_root": plan.config_root,
                "instance_root": instance_root,
                "kubejs_root": plan.kubejs_root,
                "legacy_source_kind": plan.source_kind,
                "legacy_language_delivery": plan.delivery,
                "language_output_root": plan.lang_root,
                "asset_output_root": plan.asset_root,
                "resourcepack_activation_required": plan.resourcepack_activation_required,
                "minecraft_version": minecraft_version.strip(),
                "source_snapshot": source_snapshot,
                "active_snapshot": active_snapshot,
                "backup_snapshot": backup_snapshot,
                "asset_snapshot": asset_snapshot,
                "terminology_groups": terminology_groups,
                "reference_term_groups": reference_term_groups,
            },
            warnings=warnings,
        )
        (
            project.existing,
            project.metadata["existing_unknown_catalogs"],
        ) = _load_existing_catalog_state(
            plan.lang_root,
            source_locale,
            target_locale,
            all_units,
        )
        return project

    def validate_output(self, project: TranslationProject, output_path: Path) -> None:
        output_path = Path(output_path)
        source_snapshot = project.metadata.get("source_snapshot")
        if source_snapshot is not None:
            assert_source_unchanged(source_snapshot)
        plan = _install_plan_from_project(project)
        if not _same_lexical_path(output_path, plan.asset_root):
            raise AdapterError(
                "旧版rawの出力先は解析時に決定した場所から変更できません: "
                f"{plan.asset_root}"
            )
        _validate_install_plan(plan)
        assert_path_unchanged(project.metadata["active_snapshot"])
        assert_path_unchanged(project.metadata["backup_snapshot"])
        assert_path_unchanged(project.metadata["asset_snapshot"])
        (
            project.existing,
            project.metadata["existing_unknown_catalogs"],
        ) = _load_existing_catalog_state(
            plan.lang_root,
            project.source_locale,
            project.target_locale,
            project.units,
        )

    def write(
        self,
        project: TranslationProject,
        translations: Mapping[str, str],
        output_path: Path,
        selected_unit_ids: Set[str] | None = None,
    ) -> None:
        self.validate_output(project, output_path)
        selected = validate_translation_selection(project, translations, selected_unit_ids)
        plan = _install_plan_from_project(project)

        prepared_documents: list[tuple[_LegacyDocument, str]] = []
        for document in project.documents:
            rendered = _apply_replacements(document.source_text, document.replacements)
            try:
                parse_snbt(rendered)
            except SnbtParseError as exc:
                raise AdapterError(f"キー化した quest SNBT の自己検証に失敗しました: {document.relative_path} ({exc})") from exc
            prepared_documents.append((document, rendered))

        unknown_catalogs: dict[str, OrderedDict[str, str]] = project.metadata.get(
            "existing_unknown_catalogs",
            {},
        )
        source_catalog: OrderedDict[str, str] = OrderedDict((unit.key, unit.source) for unit in project.units)
        target_catalog: OrderedDict[str, str] = OrderedDict(
            (unit.key, translations[unit.id]) for unit in project.units if unit.id in selected
        )
        source_json = _render_catalog(
            source_catalog,
            unknown_catalogs.get("en_us", OrderedDict()),
        )
        target_json = _render_catalog(
            target_catalog,
            unknown_catalogs.get(project.target_locale.lower(), OrderedDict()),
        )
        language_writes: list[tuple[Path, str, str | None]] = [
            (plan.lang_root.relative_to(plan.asset_root) / "en_us.json", source_json, "\n")
        ]
        if project.source_locale.lower() != "en_us":
            source_locale_json = _render_catalog(
                source_catalog,
                unknown_catalogs.get(project.source_locale.lower(), OrderedDict()),
            )
            language_writes.append(
                (
                    plan.lang_root.relative_to(plan.asset_root)
                    / f"{project.source_locale.lower()}.json",
                    source_locale_json,
                    "\n",
                )
            )
        language_writes.append(
            (
                plan.lang_root.relative_to(plan.asset_root)
                / f"{project.target_locale.lower()}.json",
                target_json,
                "\n",
            )
        )
        if plan.resourcepack_activation_required:
            pack_meta = {
                "pack": {
                    "pack_format": _pack_format(
                        str(project.metadata.get("minecraft_version", "1.20.1"))
                    ),
                    "description": (
                        f"FTB Quests localization ({project.target_locale}) - "
                        "Minecraft Quest Localizer"
                    ),
                }
            }
            language_writes.append(
                (
                    Path("pack.mcmeta"),
                    json.dumps(pack_meta, ensure_ascii=False, indent=2) + "\n",
                    "\n",
                )
            )
        _write_install_transaction(
            plan,
            prepared_documents,
            language_writes,
            expected_source=project.metadata["source_snapshot"],
            expected_active=project.metadata["active_snapshot"],
            expected_backup=project.metadata["backup_snapshot"],
            expected_asset=project.metadata["asset_snapshot"],
        )


def _reject_reparse_points(root: Path, description: str) -> None:
    stack = [root]
    try:
        while stack:
            current = stack.pop()
            if _is_reparse_point(current):
                raise AdapterError(
                    f"{description}にsymlinkまたはjunctionがあるため、"
                    f"安全に処理できません: {current}"
                )
            if not current.is_dir():
                continue
            with os.scandir(current) as entries:
                for entry in entries:
                    candidate = Path(entry.path)
                    if _is_reparse_point(candidate):
                        raise AdapterError(
                            f"{description}にsymlinkまたはjunctionがあるため、"
                            f"安全に処理できません: {candidate}"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(candidate)
    except AdapterError:
        raise
    except OSError as exc:
        raise AdapterError(f"{description}を安全確認できません: {root} ({exc})") from exc


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _install_plan_from_project(project: TranslationProject) -> _LegacyInstallPlan:
    return _LegacyInstallPlan(
        instance_root=Path(project.metadata["instance_root"]),
        kubejs_root=Path(project.metadata["kubejs_root"]),
        config_root=Path(project.metadata["config_root"]),
        active_quest_root=Path(project.metadata["active_quest_root"]),
        backup_quest_root=Path(project.metadata["backup_quest_root"]),
        source_quest_root=Path(project.metadata["quest_root"]),
        source_kind=str(project.metadata["legacy_source_kind"]),
        delivery=str(project.metadata["legacy_language_delivery"]),
        target_locale=project.target_locale.lower(),
        asset_root=Path(project.metadata["asset_output_root"]),
        lang_root=Path(project.metadata["language_output_root"]),
    )


def _validate_install_plan(plan: _LegacyInstallPlan) -> None:
    reject_reparse_ancestors(
        plan.config_root,
        "FTB Quests設定",
        anchor=plan.instance_root,
    )
    if plan.active_quest_root != plan.config_root / "quests":
        raise AdapterError("内部エラー: active questの配置が不正です")
    if plan.backup_quest_root != plan.config_root / "quests.bak":
        raise AdapterError("内部エラー: quest原本バックアップの配置が不正です")
    if plan.source_quest_root not in {plan.active_quest_root, plan.backup_quest_root}:
        raise AdapterError("内部エラー: quest原本の配置が不正です")
    active_exists = _path_lexically_exists(
        plan.active_quest_root,
        "FTB Questsのquests",
    )
    backup_exists = _path_lexically_exists(
        plan.backup_quest_root,
        "quest原本バックアップ",
    )
    if active_exists:
        reject_reparse_ancestors(
            plan.active_quest_root,
            "FTB Questsのquests",
            anchor=plan.instance_root,
        )
        if not plan.active_quest_root.is_dir():
            raise AdapterError(
                f"FTB Questsのquestsがフォルダーではありません: {plan.active_quest_root}"
            )
        _reject_reparse_points(plan.active_quest_root, "FTB Questsのquests")
    if backup_exists:
        reject_reparse_ancestors(
            plan.backup_quest_root,
            "quest原本バックアップ",
            anchor=plan.instance_root,
        )
        if not plan.backup_quest_root.is_dir():
            raise AdapterError(
                f"quests.bakがフォルダーではありません: {plan.backup_quest_root}"
            )
        _reject_reparse_points(plan.backup_quest_root, "quest原本バックアップ")
    if not plan.source_quest_root.is_dir():
        raise AdapterError(f"quest原本が見つかりません: {plan.source_quest_root}")
    if plan.source_kind == "active" and backup_exists:
        raise AdapterError(
            "quests.bakが既に存在するため、原本を上書きできません。"
            f"内容を確認してください: {plan.backup_quest_root}"
        )
    if plan.source_kind == "backup" and not backup_exists:
        raise AdapterError(f"quest原本バックアップが見つかりません: {plan.backup_quest_root}")
    if plan.source_kind not in {"active", "backup"}:
        raise AdapterError("内部エラー: quest原本の状態が不正です")
    if plan.delivery not in {"kubejs", "resourcepack"}:
        raise AdapterError("内部エラー: 旧版rawの言語出力方式が不正です")
    kubejs_exists = _path_lexically_exists(plan.kubejs_root, "KubeJSフォルダー")
    if plan.delivery == "kubejs":
        if not kubejs_exists:
            raise AdapterError(
                "解析後にKubeJSフォルダーがなくなったため、出力方式を再判定できません。"
                "インスタンスを再解析してください"
            )
        reject_reparse_ancestors(
            plan.kubejs_root,
            "KubeJSフォルダー",
            anchor=plan.instance_root,
        )
        if not plan.kubejs_root.is_dir():
            raise AdapterError(f"KubeJSの出力先がフォルダーではありません: {plan.kubejs_root}")
        expected_asset_root = plan.kubejs_root / "assets" / "mq_localizer"
        expected_lang_root = expected_asset_root / "lang"
    else:
        if kubejs_exists:
            raise AdapterError(
                "解析後にKubeJSフォルダーが検出されたため、言語ファイルの出力方式が変わります。"
                "インスタンスを再解析してください"
            )
        expected_asset_root = (
            plan.instance_root
            / "resourcepacks"
            / f"mq_localizer_{plan.target_locale}"
        )
        expected_lang_root = expected_asset_root / "assets" / "minecraft" / "lang"
    if plan.asset_root != expected_asset_root or plan.lang_root != expected_lang_root:
        raise AdapterError("内部エラー: 旧版rawの言語出力先が不正です")
    reject_reparse_ancestors(
        plan.asset_root,
        "旧版rawの言語出力先",
        anchor=plan.instance_root,
    )
    _require_directory_or_missing(plan.asset_root.parent, "旧版rawの言語出力先の親フォルダー")
    _require_directory_or_missing(plan.lang_root, "旧版rawの言語ファイル出力先")
    if plan.asset_root.exists():
        if not plan.asset_root.is_dir():
            raise AdapterError(f"言語出力先にはフォルダーが必要です: {plan.asset_root}")
        _reject_reparse_points(plan.asset_root, "旧版rawの既存言語出力")
    if paths_overlap(plan.config_root, plan.asset_root):
        raise AdapterError("クエスト設定と翻訳言語の出力先が重なっています")


def _require_directory_or_missing(path: Path, description: str) -> None:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    if candidate.exists() and not candidate.is_dir():
        raise AdapterError(f"{description}がディレクトリではありません: {candidate}")


def _write_install_transaction(
    plan: _LegacyInstallPlan,
    prepared_documents: list[tuple[_LegacyDocument, str]],
    language_writes: list[tuple[Path, str, str | None]],
    *,
    expected_source: PathSnapshot,
    expected_active: PathSnapshot,
    expected_backup: PathSnapshot,
    expected_asset: PathSnapshot,
) -> None:
    _validate_install_plan(plan)
    assert_source_unchanged(expected_source)
    assert_path_unchanged(expected_active)
    assert_path_unchanged(expected_backup)
    assert_path_unchanged(expected_asset)
    quest_stage: Path | None = None
    asset_stage: Path | None = None
    asset_created_parents: tuple[Path, ...] = ()
    asset_commit: _CommittedDirectorySwap | None = None
    quest_commit: _CommittedQuestSwap | None = None
    committed = False
    # A managed re-run already has the exact deterministic keyed tree that the
    # current converter would produce from quests.bak. Translation values live
    # only in the language JSON, so avoid touching that live quest tree (and
    # preserve any runtime-generated quests/lang content) on such runs.
    quest_write_required = plan.source_kind == "active" or not expected_active.exists
    try:
        if quest_write_required:
            quest_stage = _stage_quest_copy(plan, expected_source)
        asset_stage, asset_created_parents = _stage_directory_copy(
            plan.asset_root,
            "旧版rawの言語出力",
            required_existing_root=(
                plan.kubejs_root if plan.delivery == "kubejs" else None
            ),
        )
        if expected_asset.exists:
            assert_relocated_path_unchanged(expected_asset, asset_stage)
        if quest_stage is not None:
            for document, rendered in prepared_documents:
                relative = document.relative_path
                if relative.is_absolute() or ".." in relative.parts:
                    raise AdapterError(f"内部エラー: 不正なquest相対パスです: {relative}")
                atomic_write_text(
                    quest_stage / relative,
                    rendered,
                    encoding="utf-8",
                    newline=document.newline,
                )
        for relative, text, newline in language_writes:
            if relative.is_absolute() or ".." in relative.parts:
                raise AdapterError(f"内部エラー: 不正な言語出力相対パスです: {relative}")
            atomic_write_text(
                asset_stage / relative,
                text,
                encoding="utf-8",
                newline=newline,
            )
        if quest_stage is not None:
            _reject_reparse_points(quest_stage, "キー化questのstaging出力")
        _reject_reparse_points(asset_stage, "言語資産のstaging出力")
        _validate_install_plan(plan)
        assert_source_unchanged(expected_source)
        assert_path_unchanged(expected_active)
        assert_path_unchanged(expected_backup)
        assert_path_unchanged(expected_asset)

        # Install language assets before activating a keyed quest tree. If the
        # quest rename is locked, the asset swap is rolled back below.
        asset_commit = _commit_staged_directory(
            asset_stage,
            plan.asset_root,
            expected_asset,
        )
        asset_stage = None
        if quest_stage is not None:
            quest_commit = _commit_staged_quests(
                quest_stage,
                plan,
                expected_active,
                expected_backup,
            )
            quest_stage = None
        else:
            # The managed quest tree is intentionally retained on re-runs, but
            # still reject a concurrent edit before accepting the new catalog.
            assert_source_unchanged(expected_source)
            assert_path_unchanged(expected_active)
            assert_path_unchanged(expected_backup)
        assert_relocated_path_unchanged(
            asset_commit.installed_snapshot,
            plan.asset_root,
        )
        committed = True
    except BaseException as write_error:
        rollback_errors: list[BaseException] = []
        if quest_commit is not None:
            try:
                _rollback_committed_quests(quest_commit)
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        if asset_commit is not None:
            try:
                _rollback_committed_directory(asset_commit)
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            details = "\n".join(f"- {error}" for error in rollback_errors)
            lock_note = (
                "\nMinecraftを完全に終了し、対象フォルダーを使用している"
                "エディター、同期ソフト、バックアップソフト等を閉じてから、"
                "表示された保持先を確認してください。"
                if _is_filesystem_lock_error(write_error)
                or any(_is_filesystem_lock_error(error) for error in rollback_errors)
                else ""
            )
            raise AdapterError(
                "旧版rawの出力に失敗し、次の場所を完全には復元できませんでした:\n"
                f"{details}{lock_note}"
            ) from write_error
        raise
    finally:
        for stage in (quest_stage, asset_stage):
            if stage is not None and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        if not committed:
            _remove_empty_directories(asset_created_parents)

    assert asset_commit is not None
    _finalize_committed_directory(asset_commit)


def _stage_quest_copy(
    plan: _LegacyInstallPlan,
    expected_source: PathSnapshot,
) -> Path:
    """Copy only the canonical quest source to a same-volume staging tree."""

    # Keep staging outside config/ftbquests. This means unrelated FTB Quests
    # settings are neither copied nor renamed and their open handles cannot
    # block the final child-directory rename on Windows.
    parent = plan.config_root.parent
    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=".quests.stage-", dir=parent))
        shutil.copytree(
            plan.source_quest_root,
            stage,
            dirs_exist_ok=True,
            symlinks=True,
        )
        _reject_reparse_points(stage, "quest原本のstagingコピー")
        assert_relocated_path_unchanged(expected_source, stage)
        assert_source_unchanged(expected_source)
    except BaseException:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    assert stage is not None
    return stage


def _commit_staged_quests(
    stage: Path,
    plan: _LegacyInstallPlan,
    expected_active: PathSnapshot,
    expected_backup: PathSnapshot,
) -> _CommittedQuestSwap:
    """Activate a staged quest tree without ever renaming config/ftbquests."""

    assert_path_unchanged(expected_active)
    assert_path_unchanged(expected_backup)
    installed_snapshot = snapshot_path(stage)
    try:
        if plan.source_kind == "active":
            if not expected_active.exists or expected_backup.exists:
                raise AdapterError("内部エラー: 初回quest入替えの確認状態が不正です")
            _replace_path(plan.active_quest_root, plan.backup_quest_root)
            assert_relocated_path_unchanged(
                expected_active,
                plan.backup_quest_root,
            )
        elif plan.source_kind == "backup":
            if expected_active.exists or not expected_backup.exists:
                raise AdapterError("内部エラー: quest復旧時の確認状態が不正です")
            assert_path_unchanged(expected_backup)
        else:
            raise AdapterError("内部エラー: quest原本の状態が不正です")

        _replace_path(stage, plan.active_quest_root)
        assert_relocated_path_unchanged(
            installed_snapshot,
            plan.active_quest_root,
        )
        if plan.source_kind == "active":
            assert_relocated_path_unchanged(
                expected_active,
                plan.backup_quest_root,
            )
        else:
            assert_path_unchanged(expected_backup)
    except BaseException as commit_error:
        rollback_error: BaseException | None = None
        try:
            original_moved = _quest_rollback_mode(
                plan,
                expected_active,
                expected_backup,
            )
        except BaseException as error:
            rollback_error = error
            original_moved = None
        if rollback_error is None and original_moved is not None:
            try:
                _rollback_quest_state(
                    plan.active_quest_root,
                    plan.backup_quest_root,
                    plan.config_root,
                    expected_active,
                    expected_backup,
                    installed_snapshot,
                    original_moved=original_moved,
                )
            except BaseException as error:
                rollback_error = error
        if rollback_error is not None:
            lock_note = (
                "\nMinecraftを完全に終了し、questsフォルダーを使用している"
                "エディター、同期ソフト、バックアップソフト等を閉じてから、"
                "表示された原本・復元元を確認してください。"
                if _is_filesystem_lock_error(commit_error)
                or _is_filesystem_lock_error(rollback_error)
                else ""
            )
            raise AdapterError(
                "クエストフォルダーの入替えに失敗し、自動復元も完了できませんでした。\n"
                f"quests: {plan.active_quest_root}\n"
                f"quests.bak: {plan.backup_quest_root}\n"
                f"復元エラー: {rollback_error}{lock_note}"
            ) from commit_error
        if isinstance(commit_error, OSError):
            if _is_filesystem_lock_error(commit_error):
                raise AdapterError(
                    "クエストフォルダーを入れ替えられませんでした。"
                    "原本と既存出力は自動復元しました。Minecraftを完全に終了し、"
                    "questsフォルダーを開いているエディター、同期ソフト、"
                    "バックアップソフト等を閉じてから、再解析して再試行してください。\n"
                    f"対象: {plan.active_quest_root}\n詳細: {commit_error}"
                ) from commit_error
            raise AdapterError(
                "クエストフォルダーの入替えに失敗しました。"
                "原本と既存出力は自動復元しました。再解析して再試行してください。\n"
                f"対象: {plan.active_quest_root}\n詳細: {commit_error}"
            ) from commit_error
        raise
    return _CommittedQuestSwap(
        active_root=plan.active_quest_root,
        backup_root=plan.backup_quest_root,
        source_kind=plan.source_kind,
        original_active_snapshot=expected_active,
        original_backup_snapshot=expected_backup,
        installed_snapshot=installed_snapshot,
    )


def _quest_rollback_mode(
    plan: _LegacyInstallPlan,
    expected_active: PathSnapshot,
    expected_backup: PathSnapshot,
) -> bool | None:
    """Return whether the original was moved, or None when nothing changed."""

    active_unchanged = _path_snapshot_matches(expected_active)
    backup_unchanged = _path_snapshot_matches(expected_backup)
    if active_unchanged and backup_unchanged:
        return None
    if plan.source_kind == "active":
        if active_unchanged:
            raise AdapterError(
                "quests.bakが入替え中に外部作成されたため、その内容を上書きせず停止しました: "
                f"{plan.backup_quest_root}"
            )
        if _relocated_snapshot_matches(expected_active, plan.backup_quest_root):
            return True
    elif plan.source_kind == "backup":
        if backup_unchanged:
            return False
    raise AdapterError(
        "クエスト入替え中の状態を安全に復元できません。"
        "外部変更を上書きせず停止しました。\n"
        f"quests: {plan.active_quest_root}\n"
        f"quests.bak: {plan.backup_quest_root}"
    )


def _rollback_committed_quests(swap: _CommittedQuestSwap) -> None:
    _rollback_quest_state(
        swap.active_root,
        swap.backup_root,
        swap.active_root.parent,
        swap.original_active_snapshot,
        swap.original_backup_snapshot,
        swap.installed_snapshot,
        original_moved=swap.source_kind == "active",
    )


def _rollback_quest_state(
    active_root: Path,
    backup_root: Path,
    config_root: Path,
    expected_active: PathSnapshot,
    expected_backup: PathSnapshot,
    installed_snapshot: PathSnapshot,
    *,
    original_moved: bool,
) -> None:
    """Restore the pre-commit quest state without deleting external edits."""

    if original_moved:
        assert_relocated_path_unchanged(expected_active, backup_root)
    else:
        assert_path_unchanged(expected_backup)

    displaced: Path | None = None
    displaced_is_external = False
    if _path_lexically_exists(active_root, "rollback対象のquests"):
        matched_before_move = _relocated_snapshot_matches(
            installed_snapshot,
            active_root,
        )
        displaced = config_root / f".quests.discard-{uuid.uuid4().hex}"
        _replace_path(active_root, displaced)
        matched_after_move = _relocated_snapshot_matches(
            installed_snapshot,
            displaced,
        )
        displaced_is_external = not (matched_before_move and matched_after_move)
        if displaced_is_external:
            recovery = config_root / f".quests.recovery-{uuid.uuid4().hex}"
            _replace_path(displaced, recovery)
            displaced = recovery

    try:
        if original_moved:
            _replace_path(backup_root, active_root)
            assert_relocated_path_unchanged(expected_active, active_root)
            assert_path_unchanged(expected_backup)
        else:
            assert_path_unchanged(expected_active)
            assert_path_unchanged(expected_backup)
    except BaseException as restore_error:
        # If restoration itself fails, put the displaced live tree back when
        # possible. The original remains recoverable at quests.bak.
        if (
            displaced is not None
            and _path_lexically_exists(displaced, "復元待ちのquests")
            and not _path_lexically_exists(active_root, "quests復元先")
        ):
            try:
                _replace_path(displaced, active_root)
                displaced = None
            except OSError:
                pass
        raise OSError(
            "questsを自動復元できませんでした。原本または復元元は次の場所に保持しています: "
            f"{backup_root} ({restore_error})"
        ) from restore_error

    if displaced is None:
        return
    if displaced_is_external:
        raise OSError(
            "処理中に外部から作成または変更されたquestsを別名で保持しました: "
            f"{displaced}"
        )
    if not _relocated_snapshot_matches(installed_snapshot, displaced):
        recovery = config_root / f".quests.recovery-{uuid.uuid4().hex}"
        _replace_path(displaced, recovery)
        raise OSError(
            "rollback中に外部変更されたquestsを別名で保持しました: "
            f"{recovery}"
        )
    try:
        shutil.rmtree(displaced)
    except OSError:
        # This is only the converter's generated tree. The requested pre-write
        # state is already restored, so a locked recoverable discard is benign.
        pass


def _is_filesystem_lock_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PermissionError):
            return True
        if getattr(current, "winerror", None) in {5, 32, 33}:
            return True
        current = current.__cause__ or current.__context__
    return False


def _stage_directory_copy(
    path: Path,
    description: str,
    *,
    required_existing_root: Path | None = None,
) -> tuple[Path, tuple[Path, ...]]:
    parent = path.parent
    created_parents: list[Path] = []
    candidate = parent
    while not candidate.exists():
        created_parents.append(candidate)
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    stage: Path | None = None
    try:
        if required_existing_root is None:
            parent.mkdir(parents=True, exist_ok=True)
        else:
            required_root = Path(required_existing_root)
            if not parent.is_relative_to(required_root):
                raise AdapterError("内部エラー: staging先が必須ルート外です")
            if (
                not _path_lexically_exists(required_root, "必須の出力元フォルダー")
                or _is_reparse_point(required_root)
                or not required_root.is_dir()
            ):
                raise AdapterError(
                    "解析後に必須の出力元フォルダーがなくなったか、"
                    f"安全でなくなりました: {required_root}"
                )
            current = required_root
            for part in parent.relative_to(required_root).parts:
                current = current / part
                current.mkdir(exist_ok=True)
                if _is_reparse_point(current) or not current.is_dir():
                    raise AdapterError(
                        f"言語出力先の親が安全なフォルダーではありません: {current}"
                    )
        stage = Path(
            tempfile.mkdtemp(
                prefix=f".{path.name or 'mq-localizer'}.stage-",
                dir=parent,
            )
        )
        if path.exists():
            shutil.copytree(path, stage, dirs_exist_ok=True, symlinks=True)
            _reject_reparse_points(stage, f"{description}のstagingコピー")
    except BaseException:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        _remove_empty_directories(created_parents)
        raise
    assert stage is not None
    return stage, tuple(created_parents)


def _commit_staged_directory(
    stage: Path,
    destination: Path,
    expected: PathSnapshot,
) -> _CommittedDirectorySwap:
    assert_path_unchanged(expected)
    installed_snapshot = snapshot_path(stage)
    backup: Path | None = None
    try:
        if expected.exists:
            backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
            _replace_path(destination, backup)
            assert_relocated_path_unchanged(expected, backup)
        _replace_path(stage, destination)
        assert_relocated_path_unchanged(installed_snapshot, destination)
    except BaseException as commit_error:
        try:
            if _directory_commit_needs_rollback(destination, expected, backup):
                _rollback_committed_directory(
                    _CommittedDirectorySwap(
                        destination,
                        expected,
                        installed_snapshot,
                        backup,
                    )
                )
        except BaseException as rollback_error:
            raise OSError(
                "出力フォルダーの入替えに失敗し、既存内容を自動復元できませんでした。"
                f"バックアップ: {backup} ({rollback_error})"
            ) from commit_error
        raise
    return _CommittedDirectorySwap(destination, expected, installed_snapshot, backup)


def _directory_commit_needs_rollback(
    destination: Path,
    expected: PathSnapshot,
    backup: Path | None,
) -> bool:
    if _path_snapshot_matches(expected):
        return False
    if expected.exists and backup is not None:
        if _relocated_snapshot_matches(expected, backup):
            return True
    elif not expected.exists:
        return _path_lexically_exists(destination, "入替え途中の出力先")
    raise OSError(
        "出力フォルダーの入替え途中に外部変更を検出し、"
        "自動復元できる状態を確認できません。"
        f"出力先: {destination} / 一時バックアップ: {backup}"
    )


def _rollback_committed_directory(swap: _CommittedDirectorySwap) -> None:
    if swap.backup is not None:
        assert_relocated_path_unchanged(swap.original_snapshot, swap.backup)
    displaced: Path | None = None
    displaced_is_external = False
    if _path_lexically_exists(swap.destination, "rollback対象の出力先"):
        matched_before_move = _relocated_snapshot_matches(
            swap.installed_snapshot,
            swap.destination,
        )
        displaced = swap.destination.with_name(
            f".{swap.destination.name}.discard-{uuid.uuid4().hex}"
        )
        _replace_path(swap.destination, displaced)
        matched_after_move = _relocated_snapshot_matches(
            swap.installed_snapshot,
            displaced,
        )
        displaced_is_external = not (matched_before_move and matched_after_move)
        if displaced_is_external:
            recovery = swap.destination.with_name(
                f".{swap.destination.name}.recovery-{uuid.uuid4().hex}"
            )
            _replace_path(displaced, recovery)
            displaced = recovery
    if swap.backup is not None:
        _restore_directory_backup(swap.backup, swap.destination)
    if displaced is None:
        return
    if displaced_is_external:
        raise OSError(
            "rollback中に外部変更を検出したため、その内容を別名で保持しました: "
            f"{displaced}"
        )
    if not _relocated_snapshot_matches(swap.installed_snapshot, displaced):
        recovery = swap.destination.with_name(
            f".{swap.destination.name}.recovery-{uuid.uuid4().hex}"
        )
        _replace_path(displaced, recovery)
        raise OSError(
            "rollback中に外部変更された出力を別名で保持しました: "
            f"{recovery}"
        )
    try:
        shutil.rmtree(displaced)
    except OSError:
        # The requested pre-write state is already restored. Keep a locked
        # converter-owned discard rather than turning success into data loss.
        pass


def _finalize_committed_directory(swap: _CommittedDirectorySwap) -> None:
    if swap.backup is None or not _path_lexically_exists(
        swap.backup,
        "一時バックアップ",
    ):
        return
    discard = swap.backup.with_name(
        f".{swap.destination.name}.discard-{uuid.uuid4().hex}"
    )
    try:
        assert_relocated_path_unchanged(swap.original_snapshot, swap.backup)
        _replace_path(swap.backup, discard)
        assert_relocated_path_unchanged(swap.original_snapshot, discard)
        shutil.rmtree(discard)
    except (AdapterError, OSError):
        # A locked or externally modified temporary backup is recoverable.  Do
        # not turn a completed installation into a reported translation error.
        pass


def _restore_directory_backup(backup: Path, destination: Path) -> None:
    if _path_lexically_exists(destination, "既存内容の復元先"):
        raise OSError(
            "既存内容の復元先が外部で再作成されたため、上書きしません。"
            f"バックアップ: {backup}"
        )
    _replace_path(backup, destination)


def _remove_empty_directories(paths: tuple[Path, ...] | list[Path]) -> None:
    for directory in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


def _replace_path(source: Path, destination: Path) -> None:
    """Rename a filesystem object without intentionally replacing a race."""

    if _path_lexically_exists(destination, "フォルダー入替え先"):
        raise FileExistsError(f"入替え先が既に存在するため上書きしません: {destination}")
    # On Windows (the supported desktop target) os.rename does not replace an
    # existing directory. The lstat check also gives other platforms explicit
    # no-overwrite behavior for all ordinary, non-racing cases.
    os.rename(source, destination)


def _path_snapshot_matches(expected: PathSnapshot) -> bool:
    return snapshot_path(expected.path) == expected


def _relocated_snapshot_matches(expected: PathSnapshot, path: Path) -> bool:
    current = snapshot_path(path)
    return (
        current.root_kind == expected.root_kind
        and current.root_link_target == expected.root_link_target
        and current.entries == expected.entries
    )


def _same_lexical_path(left: Path, right: Path) -> bool:
    """Compare selected paths without following a symlink or junction."""

    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def _path_lexically_exists(path: Path, description: str) -> bool:
    """Check existence with ``lstat`` so dangling links are not ignored."""

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdapterError(f"{description}を安全確認できません: {path} ({exc})") from exc
    return True


def _locate_quest_layout(
    path: Path,
    *,
    allow_ambiguous: bool = False,
    target_locale: str = "ja_jp",
) -> _LegacyInstallPlan | None:
    if not safe_is_directory(path, "FTB Quests の入力ルート", anchor=path):
        return None
    selected = Path(path)
    config_root: Path | None = None
    instance_root: Path | None = None
    if selected.name.lower() in {"quests", "quests.bak"} and selected.parent.name.lower() in {
        "ftbquests",
        "ftb_quests",
    }:
        config_root = selected.parent
        instance_root = selected.parents[2]
    else:
        for spelling in ("ftbquests", "ftb_quests"):
            candidate = selected / "config" / spelling
            if (candidate / "quests").is_dir() or (candidate / "quests.bak").is_dir():
                config_root = candidate
                instance_root = selected
                break
    if config_root is None or instance_root is None:
        return None
    reject_reparse_ancestors(
        config_root,
        "FTB Quests設定",
        anchor=instance_root,
    )

    active = config_root / "quests"
    backup = config_root / "quests.bak"
    active_exists = _path_lexically_exists(active, "FTB Questsのquests")
    backup_exists = _path_lexically_exists(backup, "quest原本バックアップ")
    if active_exists and not active.is_dir():
        raise AdapterError(f"FTB Questsのquestsがフォルダーではありません: {active}")
    if backup_exists and not backup.is_dir():
        raise AdapterError(f"quests.bakがフォルダーではありません: {backup}")
    if backup_exists:
        _reject_reparse_points(backup, "quest原本バックアップ")
        if not (backup / "chapters").is_dir():
            raise AdapterError(f"quests.bakにchaptersがありません: {backup}")
        if active_exists and not _quest_tree_matches_generated_backup(active, backup):
            if not allow_ambiguous:
                raise AdapterError(
                    "questsとquests.bakの両方が原本形式に見えるため、どちらも上書きしません。"
                    "内容を確認し、原本として残す方をquests.bakにしてください。"
                )
            source_kind = "ambiguous"
        else:
            source_kind = "backup"
        source = backup
    elif active_exists:
        source = active
        source_kind = "active"
    else:
        return None

    kubejs_root = instance_root / "kubejs"
    if _path_lexically_exists(kubejs_root, "KubeJSフォルダー"):
        reject_reparse_ancestors(kubejs_root, "KubeJSフォルダー", anchor=instance_root)
        if not kubejs_root.is_dir():
            raise AdapterError(f"KubeJSの出力先がフォルダーではありません: {kubejs_root}")
        delivery = "kubejs"
        asset_root = kubejs_root / "assets" / "mq_localizer"
        lang_root = asset_root / "lang"
    else:
        delivery = "resourcepack"
        asset_root = (
            instance_root
            / "resourcepacks"
            / f"mq_localizer_{target_locale.lower()}"
        )
        lang_root = asset_root / "assets" / "minecraft" / "lang"
    return _LegacyInstallPlan(
        instance_root=instance_root,
        kubejs_root=kubejs_root,
        config_root=config_root,
        active_quest_root=active,
        backup_quest_root=backup,
        source_quest_root=source,
        source_kind=source_kind,
        delivery=delivery,
        target_locale=target_locale.lower(),
        asset_root=asset_root,
        lang_root=lang_root,
    )


def _quest_tree_matches_generated_backup(active: Path, backup: Path) -> bool:
    """Prove that ``active`` is the deterministic keyed copy of ``backup``."""

    if not active.is_dir() or not backup.is_dir():
        return False
    _reject_reparse_points(active, "既存quests")
    _reject_reparse_points(backup, "quest原本バックアップ")
    backup_sources = _legacy_source_files(backup)
    active_sources = _legacy_source_files(active)
    backup_relatives = [path.relative_to(backup) for path in backup_sources]
    active_relatives = [path.relative_to(active) for path in active_sources]
    if active_relatives != backup_relatives:
        return False

    expected_sources: dict[Path, str] = {}
    global_keys: set[str] = set()
    for source_file in backup_sources:
        source_text, _encoding, newline = read_text_detect(source_file)
        relative = source_file.relative_to(backup)
        try:
            parsed = parse_snbt(source_text)
        except SnbtParseError as exc:
            raise AdapterError(
                f"quest原本バックアップを解析できません: {source_file} ({exc})"
            ) from exc
        if isinstance(parsed, SnbtCompound):
            state = _ExtractionState(relative_path=relative, used_keys=global_keys)
            _walk_object(
                parsed,
                _root_kind(relative),
                state,
                source_file,
                source_text,
            )
            expected_sources[relative] = _written_newline_text(
                _apply_replacements(source_text, state.replacements),
                newline,
            )
        else:
            expected_sources[relative] = source_text

    for relative, expected_text in expected_sources.items():
        active_text, _encoding, _newline = read_text_detect(active / relative)
        if active_text != expected_text:
            return False

    backup_other = _quest_tree_file_hashes(
        backup,
        excluded=set(backup_relatives),
    )
    active_other = _quest_tree_file_hashes(
        active,
        excluded=set(active_relatives),
    )
    for relative in tuple(active_other):
        if relative.is_relative_to(Path("lang")) and relative not in backup_other:
            del active_other[relative]
    return active_other == backup_other


def _quest_tree_file_hashes(
    root: Path,
    *,
    excluded: set[Path],
) -> dict[Path, str]:
    result: dict[Path, str] = {}
    try:
        candidates = sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda item: (str(item).casefold(), str(item)),
        )
        for candidate in candidates:
            relative = candidate.relative_to(root)
            if relative in excluded:
                continue
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    digest.update(block)
            result[relative] = digest.hexdigest()
    except OSError as exc:
        raise AdapterError(f"既存questsを安全確認できません: {root} ({exc})") from exc
    return result


def _written_newline_text(text: str, newline: str | None) -> str:
    if newline and newline != "\n":
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    return text


def _legacy_source_files(quest_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for name in ("data.snbt", "chapter_groups.snbt"):
        candidate = quest_root / name
        if candidate.is_file():
            candidates.append(candidate)
    for directory in ("chapters", "reward_tables"):
        folder = quest_root / directory
        if folder.is_dir():
            candidates.extend(sorted(folder.rglob("*.snbt")))
    return candidates


def _root_kind(relative: Path) -> str:
    if relative.parts and relative.parts[0].lower() == "chapters":
        return "chapter"
    if relative.parts and relative.parts[0].lower() == "reward_tables":
        return "reward_table"
    if relative.name.lower() == "chapter_groups.snbt":
        return "file"
    return "file"


def _walk_object(
    compound: SnbtCompound,
    kind: str,
    state: _ExtractionState,
    source_path: Path,
    source_text: str,
) -> None:
    entries = {entry.key: entry.value for entry in compound.entries}
    object_id = _object_id(entries.get("id"), state.relative_path, compound)
    prefix = f"mq_localizer.{kind}.{object_id}"

    if kind in {"file", "chapter", "chapter_group", "quest", "task", "reward", "reward_table", "quest_link"}:
        _extract_field(entries.get("title"), f"{prefix}.title", f"{kind} title", state, source_path)
    if kind == "chapter":
        _extract_field(entries.get("subtitle"), f"{prefix}.subtitle", "chapter subtitle", state, source_path)
    elif kind == "quest":
        _extract_field(entries.get("subtitle"), f"{prefix}.subtitle", "quest subtitle", state, source_path)
        _extract_field(entries.get("description"), f"{prefix}.description", "quest description", state, source_path)
    elif kind == "image":
        _extract_field(entries.get("hover"), f"{prefix}.hover", "chapter image hover text", state, source_path)

    child_fields = {
        "quests": "quest",
        "tasks": "task",
        "rewards": "reward",
        "chapter_groups": "chapter_group",
        "groups": "chapter_group",
        "reward_tables": "reward_table",
        "quest_links": "quest_link",
        "images": "image",
    }
    for field_name, child_kind in child_fields.items():
        child = entries.get(field_name)
        if isinstance(child, SnbtList):
            for item in child.items:
                if isinstance(item, SnbtCompound):
                    _walk_object(item, child_kind, state, source_path, source_text)
        elif isinstance(child, SnbtCompound):
            for entry in child.entries:
                if isinstance(entry.value, SnbtCompound):
                    _walk_object(entry.value, child_kind, state, source_path, source_text)


def _extract_field(
    node: SnbtNode | None,
    base_key: str,
    context: str,
    state: _ExtractionState,
    source_path: Path,
) -> None:
    if isinstance(node, SnbtString):
        _extract_string(node, base_key, context, state, source_path)
    elif isinstance(node, SnbtList):
        for index, item in enumerate(node.items, start=1):
            if isinstance(item, SnbtString):
                _extract_string(item, f"{base_key}.{index}", f"{context}, line {index}", state, source_path)


def _extract_string(
    node: SnbtString,
    key: str,
    context: str,
    state: _ExtractionState,
    source_path: Path,
) -> None:
    value = node.value
    stripped = value.strip()
    if not stripped or _DIRECTIVE.fullmatch(stripped) or _OLD_TRANSLATION.fullmatch(stripped):
        return
    if looks_like_raw_json_text(stripped):
        try:
            component = json.loads(value)
        except ValueError:
            state.warnings.append(
                f"{state.relative_path}:{node.span.start_line}: raw JSON textを解析できないため、その行は変更していません"
            )
            return
        created: list[TranslationUnit] = []
        transformed, main_stream, auxiliary_groups = _transform_component(
            component,
            key,
            context,
            state,
            source_path,
            created,
        )
        if not created:
            return
        main_groups = _split_unit_stream(main_stream)
        state.terminology_groups.extend(main_groups)
        state.terminology_groups.extend(auxiliary_groups)
        if (
            classify_ftb_text(key, context) in REFERENCE_NAME_CATEGORY_IDS
            and len(main_groups) == 1
            and main_groups[0]
            and None not in main_stream
        ):
            # Only the component's primary visible title is a project-name
            # candidate. Hover text and ``with`` arguments are independent
            # prose. A dynamic/unknown visible barrier, even when it leaves
            # only one literal group on one side, means the complete name
            # cannot be reconstructed safely.
            state.reference_term_groups.append(list(main_groups[0]))
        rendered_component = json.dumps(transformed, ensure_ascii=False, separators=(",", ":"))
        state.replacements.append(_Replacement(node.span.start, node.span.end, _quote_snbt(rendered_component)))
        return
    unit = state.add_unit(key, value, context, source_path)
    state.replacements.append(_Replacement(node.span.start, node.span.end, _quote_snbt("{" + unit.key + "}")))


def _transform_component(
    value: Any,
    base_key: str,
    context: str,
    state: _ExtractionState,
    source_path: Path,
    created: list[TranslationUnit],
) -> tuple[Any, list[str | None], list[list[str]]]:
    if isinstance(value, str):
        if not value:
            return value, [], []
        unit = state.add_unit(f"{base_key}.part.{len(created) + 1}", value, context, source_path)
        created.append(unit)
        return {"translate": unit.key}, [unit.id], []
    if isinstance(value, list):
        transformed_items: list[Any] = []
        main_stream: list[str | None] = []
        auxiliary_groups: list[list[str]] = []
        for item in value:
            if not isinstance(item, (str, list, dict)):
                transformed_items.append(item)
                # Keep unknown/invalid entries unchanged, but do not pretend
                # they are invisible when grouping literal terms around them.
                main_stream.append(None)
                continue
            transformed, item_main, item_auxiliary = _transform_component(
                item,
                base_key,
                context,
                state,
                source_path,
                created,
            )
            transformed_items.append(transformed)
            main_stream.extend(item_main)
            auxiliary_groups.extend(item_auxiliary)
        return transformed_items, main_stream, auxiliary_groups
    if not isinstance(value, dict):
        return value, [], []

    result: dict[str, Any] = {}
    text_stream: list[str | None] = []
    extra_stream: list[str | None] = []
    auxiliary_groups = []
    # Any pre-existing ``translate`` field owns that component. Checking key
    # presence (rather than only a valid string value) prevents a malformed or
    # future-shaped field from overwriting a generated translation key later
    # in serialized field order and leaving an unreachable catalog entry.
    has_dynamic_content = _legacy_component_uses_dynamic_content(value)
    if _legacy_component_is_visible_barrier(value):
        text_stream.append(None)
    for field_name, field_value in value.items():
        if (
            field_name == "text"
            and isinstance(field_value, str)
            and field_value
            and not has_dynamic_content
        ):
            unit = state.add_unit(
                f"{base_key}.part.{len(created) + 1}", field_value, context, source_path
            )
            created.append(unit)
            result["translate"] = unit.key
            text_stream.append(unit.id)
        elif field_name == "extra":
            transformed, extra_main, extra_auxiliary = _transform_component(
                field_value, base_key, context, state, source_path, created
            )
            result[field_name] = transformed
            extra_stream.extend(extra_main)
            auxiliary_groups.extend(extra_auxiliary)
        elif field_name == "with":
            transformed, argument_groups = _transform_independent_components(
                field_value,
                base_key,
                context,
                state,
                source_path,
                created,
            )
            result[field_name] = transformed
            auxiliary_groups.extend(argument_groups)
        elif (
            field_name == "separator"
            and _legacy_component_uses_dynamic_content(value)
        ):
            transformed, separator_main, separator_auxiliary = _transform_component(
                field_value,
                base_key,
                context + " separator",
                state,
                source_path,
                created,
            )
            result[field_name] = transformed
            auxiliary_groups.extend(_split_unit_stream(separator_main))
            auxiliary_groups.extend(separator_auxiliary)
        elif field_name == "hoverEvent" and isinstance(field_value, dict):
            hover = dict(field_value)
            if hover.get("action") == "show_text":
                for hover_key in ("contents", "value"):
                    if hover_key in hover:
                        transformed, hover_main, hover_auxiliary = _transform_component(
                            hover[hover_key],
                            base_key,
                            context + " hover",
                            state,
                            source_path,
                            created,
                        )
                        hover[hover_key] = transformed
                        auxiliary_groups.extend(_split_unit_stream(hover_main))
                        auxiliary_groups.extend(hover_auxiliary)
            result[field_name] = hover
        else:
            result[field_name] = field_value
    return result, text_stream + extra_stream, auxiliary_groups


def _transform_independent_components(
    value: Any,
    base_key: str,
    context: str,
    state: _ExtractionState,
    source_path: Path,
    created: list[TranslationUnit],
) -> tuple[Any, list[list[str]]]:
    values = value if isinstance(value, list) else [value]
    transformed_values: list[Any] = []
    groups: list[list[str]] = []
    for item in values:
        if not isinstance(item, (str, list, dict)):
            transformed_values.append(item)
            continue
        transformed, main_group, auxiliary_groups = _transform_component(
            item,
            base_key,
            context,
            state,
            source_path,
            created,
        )
        transformed_values.append(transformed)
        groups.extend(_split_unit_stream(main_group))
        groups.extend(auxiliary_groups)
    return (transformed_values if isinstance(value, list) else transformed_values[0]), groups


def _split_unit_stream(tokens: list[str | None]) -> list[list[str]]:
    """Split extracted text leaves at non-literal visible components."""

    groups: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token is None:
            if current:
                groups.append(current)
                current = []
        else:
            current.append(token)
    if current:
        groups.append(current)
    return groups


def _legacy_component_uses_dynamic_content(value: dict[str, Any]) -> bool:
    """Return whether this component's base content is not a literal string."""

    return any(field in value for field in _DYNAMIC_COMPONENT_FIELDS)


def _legacy_component_is_visible_barrier(value: dict[str, Any]) -> bool:
    if _legacy_component_uses_dynamic_content(value):
        return True
    # A malformed literal discriminator is not a transparent style wrapper.
    return "text" in value and not isinstance(value.get("text"), str)


def _object_id(node: SnbtNode | None, relative: Path, compound: SnbtCompound) -> str:
    if isinstance(node, (SnbtString, SnbtScalar)) and node.value:
        value = node.value.lower().removesuffix("l")
    else:
        value = hashlib.sha1(f"{relative}:{compound.span.start}".encode()).hexdigest()[:16]
    value = _SAFE_KEY.sub("_", value).strip("_.-")
    return value or hashlib.sha1(f"{relative}:{compound.span.start}".encode()).hexdigest()[:16]


def _quote_snbt(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _escape_percent(value: str) -> str:
    return value.replace("%", "%%")


def _apply_replacements(source: str, replacements: list[_Replacement]) -> str:
    rendered = source
    previous_start = len(source) + 1
    for replacement in sorted(replacements, key=lambda item: item.start, reverse=True):
        if replacement.end > previous_start:
            raise AdapterError("内部エラー: SNBT置換範囲が重複しています")
        rendered = rendered[: replacement.start] + replacement.text + rendered[replacement.end :]
        previous_start = replacement.start
    return rendered


def _load_existing_catalog_state(
    lang_root: Path,
    source_locale: str,
    target_locale: str,
    units: list[TranslationUnit],
) -> tuple[dict[str, str], dict[str, OrderedDict[str, str]]]:
    locales = {"en_us", source_locale.lower(), target_locale.lower()}
    catalogs = {
        locale: _read_existing_json_catalog(lang_root / f"{locale}.json")
        for locale in locales
    }
    by_key = {unit.key: unit.id for unit in units}
    target_values = catalogs[target_locale.lower()]
    existing = {
        by_key[key]: value
        for key, value in target_values.items()
        if key in by_key
    }
    unknown = {
        locale: OrderedDict(
            (key, value)
            for key, value in values.items()
            if key not in by_key and not key.casefold().startswith("mq_localizer.")
        )
        for locale, values in catalogs.items()
    }
    return existing, unknown


def _read_existing_json_catalog(path: Path) -> OrderedDict[str, str]:
    if not path.exists():
        return OrderedDict()
    try:
        text, _encoding, _newline = read_text_detect(path)
        values = json.loads(text, object_pairs_hook=_unique_json_object)
    except _DuplicateJsonKey as exc:
        raise AdapterError(f"既存の言語 JSON に重複キーがあります: {path} ({exc})") from exc
    except (AdapterError, ValueError) as exc:
        raise AdapterError(f"既存の言語 JSON を解析できません: {path} ({exc})") from exc
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        raise AdapterError(
            f"既存の言語 JSON は文字列キーと文字列値だけである必要があります: {path}"
        )
    return OrderedDict(values.items())


def _render_catalog(
    known: OrderedDict[str, str],
    unknown: OrderedDict[str, str],
) -> str:
    merged = OrderedDict(known)
    for key, value in unknown.items():
        if key not in merged:
            merged[key] = value
    return json.dumps(merged, ensure_ascii=False, indent=2) + "\n"


def _unique_json_object(pairs: list[tuple[str, object]]) -> OrderedDict[str, object]:
    result: OrderedDict[str, object] = OrderedDict()
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _pack_format(version: str) -> int:
    parts = version.strip().split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(re.match(r"\d+", parts[2]).group()) if len(parts) > 2 and re.match(r"\d+", parts[2]) else 0
    except (ValueError, IndexError):
        return 15
    if (major, minor, patch) >= (1, 20, 5):
        return 32
    if (major, minor, patch) >= (1, 20, 3):
        return 22
    if (major, minor, patch) >= (1, 20, 2):
        return 18
    if (major, minor, patch) >= (1, 20, 0):
        return 15
    if (major, minor, patch) >= (1, 19, 4):
        return 13
    if (major, minor, patch) >= (1, 19, 3):
        return 12
    if (major, minor, patch) >= (1, 19, 0):
        return 9
    if (major, minor, patch) >= (1, 18, 0):
        return 8
    if (major, minor, patch) >= (1, 17, 0):
        return 7
    if (major, minor, patch) >= (1, 16, 2):
        return 6
    if (major, minor, patch) >= (1, 15, 0):
        return 5
    if (major, minor, patch) >= (1, 13, 0):
        return 4
    if (major, minor, patch) >= (1, 11, 0):
        return 3
    if (major, minor, patch) >= (1, 9, 0):
        return 2
    return 1


def _version_at_least(value: str, minimum: tuple[int, int, int]) -> bool:
    parsed = _parsed_minecraft_version(value)
    if parsed is None:
        return False
    return parsed >= minimum


def _parsed_minecraft_version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.(\d+))?\s*", value)
    if not match:
        return None
    parsed = tuple(int(part or 0) for part in match.groups())
    return parsed if parsed[0] >= 1 else None
