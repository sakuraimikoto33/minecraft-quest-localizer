from __future__ import annotations

from collections import OrderedDict
from collections.abc import Set
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..categories import classify_ftb_text
from ..domain import AdapterError, TranslationProject, TranslationUnit
from ..io_utils import atomic_write_many_text, read_text_detect
from ..output_guard import PathSnapshot, assert_path_unchanged, assert_source_unchanged, snapshot_path
from ..snbt import (
    SnbtCompound,
    SnbtParseError,
    SnbtScalar,
    SnbtString,
    dump_lang_snbt,
    parse_lang_snbt,
    parse_snbt,
)
from .base import (
    QuestAdapter,
    array_translation_is_selected,
    validate_translation_selection,
)
from .path_safety import (
    find_regular_files_by_suffix_no_reparse,
    reject_nested_reparse_points,
    safe_is_directory,
    safe_is_regular_file,
    validate_split_locale_targets,
)


@dataclass(slots=True)
class _SplitSnbtDocument:
    source_path: Path
    relative_path: Path
    values: OrderedDict[str, str | list[str]]
    unit_ids: OrderedDict[str, str | list[str]]
    newline: str


@dataclass(slots=True)
class _ExistingTargetDocument:
    values: OrderedDict[str, str | list[str]]
    newline: str


@dataclass(slots=True)
class _OutputPlan:
    source_by_relative: dict[Path, _SplitSnbtDocument]
    existing_documents: OrderedDict[Path, _ExistingTargetDocument]
    known_keys: frozenset[str]
    relative_paths: list[Path]
    targets: list[Path]
    output_snapshot: PathSnapshot
    expected_originals: dict[Path, bytes | None]


class FtbSplitSnbtAdapter(QuestAdapter):
    id = "ftb_split_snbt"
    label = "FTB Quests 1.21.x（Lang Splitter SNBT）"
    description = "FTB Quests Lang Splitter の lang/<locale>/**/*.snbt を同じ分割構造で翻訳します。"

    def probe(self, path: Path, source_locale: str) -> int:
        source_dir, _actual_locale = _locate_source_dir(path, source_locale, allow_fallback=True)
        if source_dir and find_regular_files_by_suffix_no_reparse(
            source_dir,
            ".snbt",
            "分割 SNBT locale の原文",
        ):
            return 92
        return 0

    def load(
        self,
        path: Path,
        source_locale: str,
        target_locale: str,
        minecraft_version: str = "",
        output_override: Path | None = None,
    ) -> TranslationProject:
        source_dir, actual_source_locale = _locate_source_dir(path, source_locale, allow_fallback=True)
        if source_dir is None:
            raise AdapterError(f"Lang Splitter の lang/{source_locale}/ が見つかりません: {path}")
        if actual_source_locale.lower() == target_locale.lower():
            raise AdapterError("実際の原文localeと翻訳先localeが同じです")
        reject_nested_reparse_points(source_dir, "分割 locale の原文")
        source_snapshot = snapshot_path(source_dir)
        directly_selected = (
            path.suffix.lower() == ".snbt"
            and safe_is_regular_file(path, "分割 SNBT locale の原文ファイル")
        )
        source_files = (
            [path]
            if directly_selected
            else find_regular_files_by_suffix_no_reparse(
                source_dir,
                ".snbt",
                "分割 SNBT locale の原文",
            )
        )
        documents: list[_SplitSnbtDocument] = []
        units: list[TranslationUnit] = []
        counter = 0
        seen_keys: dict[str, Path] = {}
        for source_file in source_files:
            text, _encoding, newline = read_text_detect(source_file)
            try:
                values: OrderedDict[str, str | list[str]] = OrderedDict(parse_lang_snbt(text).items())
            except SnbtParseError as exc:
                raise AdapterError(f"分割 locale SNBT を解析できません: {source_file} ({exc})") from exc
            duplicate = next((key for key in values if key in seen_keys), None)
            if duplicate:
                raise AdapterError(
                    f"分割 locale SNBT 間でキーが重複しています: {duplicate}\n"
                    f"- {seen_keys[duplicate]}\n- {source_file}"
                )
            seen_keys.update({key: source_file for key in values})
            ids: OrderedDict[str, str | list[str]] = OrderedDict()
            for key, value in values.items():
                if isinstance(value, str):
                    unit_id = f"split-snbt-{counter:07d}"
                    counter += 1
                    ids[key] = unit_id
                    units.append(_unit(unit_id, key, value, source_file, len(units)))
                else:
                    item_ids: list[str] = []
                    for index, item in enumerate(value):
                        unit_id = f"split-snbt-{counter:07d}"
                        counter += 1
                        item_ids.append(unit_id)
                        units.append(
                            _unit(unit_id, f"{key}[{index}]", item, source_file, len(units), key, index)
                        )
                    ids[key] = item_ids
            documents.append(
                _SplitSnbtDocument(source_file, source_file.relative_to(source_dir), values, ids, newline)
            )
        if not units:
            raise AdapterError(f"分割 locale SNBT に翻訳文字列がありません: {source_dir}")
        reject_nested_reparse_points(source_dir, "分割 locale の原文")
        assert_source_unchanged(source_snapshot)
        target_dir = output_override or source_dir.parent / target_locale.lower()
        warnings: list[str] = []
        if actual_source_locale.lower() != source_locale.lower():
            warnings.append(
                f"data.snbt の fallback_locale に従い、原文localeを {actual_source_locale} として読み込みました。"
            )
        project = TranslationProject(
            adapter_id=self.id,
            adapter_label=self.label,
            source_path=source_dir,
            default_output=target_dir,
            source_locale=actual_source_locale,
            target_locale=target_locale,
            units=units,
            documents=documents,
            metadata={"source_dir": source_dir, "source_snapshot": source_snapshot},
            warnings=warnings,
        )
        validate_split_locale_targets(
            source_dir,
            target_dir,
            target_locale,
            (document.relative_path for document in documents),
        )
        project.existing = _load_existing(target_dir, documents)
        return project

    def validate_output(self, project: TranslationProject, output_path: Path) -> None:
        assert_source_unchanged(project.metadata["source_snapshot"])
        plan = _build_output_plan(project, output_path)
        project.existing = _existing_from_documents(
            plan.existing_documents,
            project.documents,
        )

    def write(
        self,
        project: TranslationProject,
        translations: Mapping[str, str],
        output_path: Path,
        selected_unit_ids: Set[str] | None = None,
    ) -> None:
        assert_source_unchanged(project.metadata["source_snapshot"])
        selected = validate_translation_selection(project, translations, selected_unit_ids)
        plan = _build_output_plan(project, output_path)
        prepared: list[tuple[Path, str, str]] = []
        for relative_path, target in zip(plan.relative_paths, plan.targets):
            result: OrderedDict[str, str | list[str]] = OrderedDict()
            document = plan.source_by_relative.get(relative_path)
            if document is not None:
                for key in document.values:
                    unit_id = document.unit_ids[key]
                    if isinstance(unit_id, str):
                        if unit_id in selected:
                            result[key] = translations[unit_id]
                    elif array_translation_is_selected(
                        key,
                        unit_id,
                        selected,
                        complete_catalog=selected_unit_ids is None,
                    ):
                        result[key] = [translations[item] for item in unit_id]
            existing_document = plan.existing_documents.get(relative_path)
            for key, value in (existing_document.values.items() if existing_document else ()):
                if key not in plan.known_keys:
                    result[key] = value
            rendered = dump_lang_snbt(result)
            try:
                verified = parse_lang_snbt(rendered)
            except SnbtParseError as exc:
                raise AdapterError(f"生成した分割 locale SNBT の自己検証に失敗しました: {exc}") from exc
            if list(verified) != list(result) or any(
                isinstance(result[key], list) != isinstance(verified[key], list) for key in result
            ):
                raise AdapterError(
                    f"生成した分割 locale SNBT のキーまたは値型が選択結果と一致しません: "
                    f"{relative_path}"
                )
            newline = (
                document.newline
                if document is not None
                else existing_document.newline if existing_document is not None else "\n"
            )
            prepared.append((target, rendered, newline))
        atomic_write_many_text(
            prepared,
            encoding="utf-8",
            expected_path_snapshot=plan.output_snapshot,
            expected_originals=plan.expected_originals,
        )


def _unit(
    unit_id: str,
    display_key: str,
    value: str,
    source_file: Path,
    ordinal: int,
    base_key: str | None = None,
    index: int | None = None,
) -> TranslationUnit:
    context = f"FTB Quests translation key: {base_key or display_key}"
    if index is not None:
        context += f", line {index + 1}"
    return TranslationUnit(
        unit_id,
        display_key,
        value,
        context,
        str(source_file),
        ordinal,
        classify_ftb_text(base_key or display_key),
    )


def _locate_source_dir(path: Path, source_locale: str, allow_fallback: bool) -> tuple[Path | None, str]:
    locale = source_locale.lower()
    is_directory = safe_is_directory(
        path,
        "分割 SNBT locale の入力ルート",
        anchor=path,
    )
    if not is_directory and safe_is_regular_file(
        path,
        "分割 SNBT locale の原文ファイル",
    ):
        for parent in path.parents:
            if parent.parent.name.lower() != "lang":
                continue
            actual_locale = parent.name.lower()
            if actual_locale == locale:
                return parent, locale
            if allow_fallback:
                quest_root = parent.parent.parent
                if _read_fallback_locale(
                    quest_root / "data.snbt",
                    quest_root,
                ) == actual_locale:
                    return parent, actual_locale
        return None, locale
    if not is_directory:
        return None, locale
    if path.name.lower() == locale and path.parent.name.lower() == "lang":
        return path, locale
    if path.name.lower() == "lang" and safe_is_directory(
        path / locale,
        "分割 SNBT locale の原文ディレクトリ",
        anchor=path,
    ):
        return path / locale, locale
    direct = path / "lang" / locale
    if safe_is_directory(
        direct,
        "分割 SNBT locale の原文ディレクトリ",
        anchor=path,
    ):
        return direct, locale
    quest_root: Path | None = None
    if safe_is_regular_file(
        path / "data.snbt",
        "FTB Quests のfallback設定",
        anchor=path,
    ):
        quest_root = path
    elif path.name.lower() == "lang" and safe_is_regular_file(
        path.parent / "data.snbt",
        "FTB Quests のfallback設定",
    ):
        quest_root = path.parent
    elif (
        path.parent.name.lower() == "lang"
        and safe_is_regular_file(
            path.parent.parent / "data.snbt",
            "FTB Quests のfallback設定",
        )
    ):
        quest_root = path.parent.parent
    for spelling in ("ftbquests", "ftb_quests"):
        nested_root = path / "config" / spelling / "quests"
        nested = nested_root / "lang" / locale
        if safe_is_directory(
            nested,
            "分割 SNBT locale の原文ディレクトリ",
            anchor=path,
        ):
            return nested, locale
        if safe_is_directory(
            nested_root,
            "FTB Quests のquestルート",
            anchor=path,
        ):
            quest_root = nested_root
    if allow_fallback and quest_root:
        fallback = _read_fallback_locale(quest_root / "data.snbt", quest_root)
        candidate = quest_root / "lang" / fallback
        if safe_is_directory(
            candidate,
            "FTB Quests fallback原文localeディレクトリ",
            anchor=quest_root,
        ):
            return candidate, fallback
    return None, locale


def _read_fallback_locale(data_file: Path, quest_root: Path) -> str:
    if not safe_is_regular_file(
        data_file,
        "FTB Quests のfallback設定",
        anchor=quest_root,
    ):
        return "en_us"
    try:
        root = parse_snbt(data_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, SnbtParseError):
        return "en_us"
    if isinstance(root, SnbtCompound):
        for entry in root.entries:
            if entry.key == "fallback_locale" and isinstance(entry.value, (SnbtString, SnbtScalar)):
                return entry.value.value.lower() or "en_us"
    return "en_us"


def _build_output_plan(project: TranslationProject, output_path: Path) -> _OutputPlan:
    source_dir = Path(project.metadata["source_dir"])
    source_by_relative = {
        document.relative_path: document for document in project.documents
    }
    # Reject an unsafe base destination before recursively inspecting it.  A
    # second validation below includes legacy/moved keys found in extra target
    # documents, because those files will also be rewritten.
    validate_split_locale_targets(
        source_dir,
        output_path,
        project.target_locale,
        source_by_relative,
    )
    output_snapshot = snapshot_path(output_path)
    existing_documents = _read_target_documents(output_path)
    known_keys = frozenset(
        key for document in project.documents for key in document.values
    )
    relative_paths = [
        *source_by_relative,
        *(
            relative
            for relative, target_document in existing_documents.items()
            if relative not in source_by_relative
            and any(key in known_keys for key in target_document.values)
        ),
    ]
    targets = validate_split_locale_targets(
        source_dir,
        output_path,
        project.target_locale,
        relative_paths,
    )
    expected_originals = {
        target: target.read_bytes() if target.exists() else None
        for target in targets
    }
    assert_path_unchanged(output_snapshot)
    return _OutputPlan(
        source_by_relative=source_by_relative,
        existing_documents=existing_documents,
        known_keys=known_keys,
        relative_paths=relative_paths,
        targets=targets,
        output_snapshot=output_snapshot,
        expected_originals=expected_originals,
    )


def _load_existing(target_dir: Path, documents: list[_SplitSnbtDocument]) -> dict[str, str]:
    return _existing_from_documents(_read_target_documents(target_dir), documents)


def _existing_from_documents(
    target_documents: OrderedDict[Path, _ExistingTargetDocument],
    documents: list[_SplitSnbtDocument],
) -> dict[str, str]:
    result: dict[str, str] = {}
    by_key = {key: unit_id for document in documents for key, unit_id in document.unit_ids.items()}
    for target_document in target_documents.values():
        for key, value in target_document.values.items():
            unit_id = by_key.get(key)
            if isinstance(unit_id, str) and isinstance(value, str):
                result[unit_id] = value
            elif isinstance(unit_id, list) and isinstance(value, list):
                result.update(zip(unit_id, value))
    return result


def _read_target_documents(
    target_dir: Path,
) -> OrderedDict[Path, _ExistingTargetDocument]:
    documents: OrderedDict[Path, _ExistingTargetDocument] = OrderedDict()
    if not target_dir.exists():
        return documents
    if not target_dir.is_dir():
        raise AdapterError(f"分割 locale の出力先にはフォルダーを指定してください: {target_dir}")
    seen: dict[str, Path] = {}
    for target in find_regular_files_by_suffix_no_reparse(
        target_dir,
        ".snbt",
        "分割 locale の既存出力",
    ):
        try:
            text, _encoding, newline = read_text_detect(target)
            values = OrderedDict(parse_lang_snbt(text).items())
        except (OSError, UnicodeError, SnbtParseError) as exc:
            raise AdapterError(f"既存の翻訳先 locale SNBT を解析できません: {target} ({exc})") from exc
        duplicate = next((key for key in values if key in seen), None)
        if duplicate:
            raise AdapterError(
                f"既存の翻訳先 locale SNBT 間でキーが重複しています: {duplicate}\n"
                f"- {seen[duplicate]}\n- {target}"
            )
        seen.update({key: target for key in values})
        documents[target.relative_to(target_dir)] = _ExistingTargetDocument(values, newline)
    return documents
