from __future__ import annotations

from collections import OrderedDict
from collections.abc import Set
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..categories import classify_ftb_text
from ..domain import AdapterError, TranslationProject, TranslationUnit
from ..io_utils import atomic_write_text, read_text_detect
from ..output_guard import assert_source_unchanged, snapshot_path
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
from .path_safety import safe_is_directory, safe_is_regular_file, validate_single_locale_output


@dataclass(slots=True)
class _ModernDocument:
    source_path: Path
    values: OrderedDict[str, str | list[str]]
    unit_ids: OrderedDict[str, str | list[str]]
    newline: str


class FtbModernSnbtAdapter(QuestAdapter):
    id = "ftb_modern_snbt"
    label = "FTB Quests 1.21～1.21.x（locale SNBT）"
    description = "ネイティブの lang/<locale>.snbt を対象localeのSNBTへ翻訳します。"

    def probe(self, path: Path, source_locale: str) -> int:
        source_file, _locale = _locate_source_file(path, source_locale, allow_fallback=True)
        if source_file is None:
            return 0
        directly_selected = (
            path.suffix.lower() == ".snbt" and path.parent.name.lower() == "lang"
        )
        return 95 if directly_selected else 90

    def load(
        self,
        path: Path,
        source_locale: str,
        target_locale: str,
        minecraft_version: str = "",
        output_override: Path | None = None,
    ) -> TranslationProject:
        source_file, actual_source_locale = _locate_source_file(path, source_locale, allow_fallback=True)
        if source_file is None:
            raise AdapterError(
                f"FTB Quests の lang/{source_locale}.snbt が見つかりません。"
                "1.21系では一度クエストブックを読み込み、locale SNBTを生成してください。"
            )
        if actual_source_locale.lower() == target_locale.lower():
            raise AdapterError("実際の原文localeと翻訳先localeが同じです")
        source_snapshot = snapshot_path(source_file)
        text, _encoding, newline = read_text_detect(source_file)
        try:
            parsed = parse_lang_snbt(text)
        except SnbtParseError as exc:
            raise AdapterError(f"locale SNBT を解析できません: {source_file} ({exc})") from exc
        values: OrderedDict[str, str | list[str]] = OrderedDict(parsed.items())
        ids: OrderedDict[str, str | list[str]] = OrderedDict()
        units: list[TranslationUnit] = []
        counter = 0
        for key, value in values.items():
            if isinstance(value, str):
                unit_id = f"snbt-{counter:07d}"
                counter += 1
                ids[key] = unit_id
                units.append(
                    TranslationUnit(
                        id=unit_id,
                        key=key,
                        source=value,
                        context=f"FTB Quests translation key: {key}",
                        source_path=str(source_file),
                        ordinal=len(units),
                        category=classify_ftb_text(key),
                    )
                )
            else:
                item_ids: list[str] = []
                for line_index, line in enumerate(value):
                    unit_id = f"snbt-{counter:07d}"
                    counter += 1
                    item_ids.append(unit_id)
                    units.append(
                        TranslationUnit(
                            id=unit_id,
                            key=f"{key}[{line_index}]",
                            source=line,
                            context=f"FTB Quests translation list {key}, line {line_index + 1}",
                            source_path=str(source_file),
                            ordinal=len(units),
                            category=classify_ftb_text(key),
                        )
                    )
                ids[key] = item_ids
        assert_source_unchanged(source_snapshot)
        default_output = output_override or source_file.with_name(f"{target_locale.lower()}.snbt")
        # Validate before inspecting an existing translation.  Otherwise a
        # symlinked target can disclose an external file during analysis.
        validate_single_locale_output(source_file, default_output, target_locale, ".snbt")
        document = _ModernDocument(source_file, values, ids, newline)
        warnings: list[str] = []
        if actual_source_locale.lower() != source_locale.lower():
            warnings.append(
                f"data.snbt の fallback_locale に従い、原文localeを {actual_source_locale} として読み込みました。"
            )
        project = TranslationProject(
            adapter_id=self.id,
            adapter_label=self.label,
            source_path=source_file,
            default_output=default_output,
            source_locale=actual_source_locale,
            target_locale=target_locale,
            units=units,
            documents=[document],
            metadata={"newline": newline, "source_snapshot": source_snapshot},
            warnings=warnings,
        )
        if default_output.exists() and default_output.resolve() != source_file.resolve():
            project.existing, project.metadata["existing_unknown"] = _partition_existing(
                default_output,
                document,
            )
        return project

    def validate_output(self, project: TranslationProject, output_path: Path) -> None:
        assert_source_unchanged(project.metadata["source_snapshot"])
        document: _ModernDocument = project.documents[0]
        validate_single_locale_output(document.source_path, output_path, project.target_locale, ".snbt")
        if output_path.exists():
            project.existing, project.metadata["existing_unknown"] = _partition_existing(
                output_path,
                document,
            )
        else:
            project.existing = {}
            project.metadata["existing_unknown"] = OrderedDict()

    def write(
        self,
        project: TranslationProject,
        translations: Mapping[str, str],
        output_path: Path,
        selected_unit_ids: Set[str] | None = None,
    ) -> None:
        self.validate_output(project, output_path)
        selected = validate_translation_selection(project, translations, selected_unit_ids)
        document: _ModernDocument = project.documents[0]
        rendered_values: OrderedDict[str, str | list[str]] = OrderedDict()
        for key in document.values:
            unit_id = document.unit_ids[key]
            if isinstance(unit_id, str):
                if unit_id in selected:
                    rendered_values[key] = translations[unit_id]
            elif array_translation_is_selected(
                key,
                unit_id,
                selected,
                complete_catalog=selected_unit_ids is None,
            ):
                rendered_values[key] = [translations[item] for item in unit_id]
        for key, value in project.metadata.get("existing_unknown", {}).items():
            if key not in rendered_values:
                rendered_values[key] = value
        rendered = dump_lang_snbt(rendered_values)
        try:
            verified = parse_lang_snbt(rendered)
        except SnbtParseError as exc:
            raise AdapterError(f"生成した locale SNBT の自己検証に失敗しました: {exc}") from exc
        if list(verified) != list(rendered_values) or any(
            isinstance(rendered_values[key], list) != isinstance(verified[key], list)
            for key in rendered_values
        ):
            raise AdapterError("生成した locale SNBT のキーまたは値型が選択結果と一致しません")
        atomic_write_text(output_path, rendered, encoding="utf-8", newline=document.newline)


def _locate_source_file(path: Path, source_locale: str, allow_fallback: bool) -> tuple[Path | None, str]:
    if safe_is_directory(
        path,
        "FTB Quests の入力ルート",
        anchor=path,
    ):
        quest_root = _locate_quest_root(path)
        if quest_root is None:
            return None, source_locale
    elif safe_is_regular_file(path, "FTB Quests の原文localeファイル"):
        if path.suffix.lower() == ".snbt" and path.parent.name.lower() == "lang":
            return path, path.stem.lower()
        return None, source_locale
    else:
        return None, source_locale
    direct = quest_root / "lang" / f"{source_locale.lower()}.snbt"
    if safe_is_regular_file(
        direct,
        "FTB Quests の原文localeファイル",
        anchor=quest_root,
    ):
        return direct, source_locale.lower()
    # A locale directory belongs to one of the split adapters.  Do not inspect
    # the single-file fallback metadata in that case: it is unused by this
    # adapter and a link there must not abort otherwise valid split detection.
    split_direct = quest_root / "lang" / source_locale.lower()
    if safe_is_directory(
        split_direct,
        "FTB Quests の分割原文localeディレクトリ",
        anchor=quest_root,
    ):
        return None, source_locale
    if allow_fallback:
        fallback = _read_fallback_locale(quest_root / "data.snbt", quest_root)
        fallback_file = quest_root / "lang" / f"{fallback}.snbt"
        if safe_is_regular_file(
            fallback_file,
            "FTB Quests fallback原文localeファイル",
            anchor=quest_root,
        ):
            return fallback_file, fallback
    return None, source_locale


def _locate_quest_root(path: Path) -> Path | None:
    if not safe_is_directory(
        path,
        "FTB Quests の入力ルート",
        anchor=path,
    ):
        return None
    if safe_is_directory(
        path / "lang",
        "FTB Quests のlangディレクトリ",
        anchor=path,
    ) and safe_is_directory(
        path / "chapters",
        "FTB Quests のchaptersディレクトリ",
        anchor=path,
    ):
        return path
    for spelling in ("ftbquests", "ftb_quests"):
        nested = path / "config" / spelling / "quests"
        if safe_is_directory(
            nested,
            "FTB Quests のquestルート",
            anchor=path,
        ):
            return nested
    if path.name.lower() == "lang" and safe_is_directory(
        path.parent,
        "FTB Quests のquestルート",
    ):
        return path.parent
    return None


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


def _load_existing(path: Path, document: _ModernDocument) -> dict[str, str]:
    existing, _unknown = _partition_existing(path, document)
    return existing


def _partition_existing(
    path: Path,
    document: _ModernDocument,
) -> tuple[dict[str, str], OrderedDict[str, str | list[str]]]:
    try:
        text, _encoding, _newline = read_text_detect(path)
        values = parse_lang_snbt(text)
    except (AdapterError, SnbtParseError) as exc:
        raise AdapterError(f"既存の翻訳先 locale SNBT を解析できません: {path} ({exc})") from exc
    existing: dict[str, str] = {}
    for key, unit_id in document.unit_ids.items():
        value = values.get(key)
        if isinstance(unit_id, str) and isinstance(value, str):
            existing[unit_id] = value
        elif isinstance(unit_id, list) and isinstance(value, list):
            for item_id, item in zip(unit_id, value):
                existing[item_id] = item
    unknown = OrderedDict(
        (key, value)
        for key, value in values.items()
        if key not in document.unit_ids
    )
    return existing, unknown
