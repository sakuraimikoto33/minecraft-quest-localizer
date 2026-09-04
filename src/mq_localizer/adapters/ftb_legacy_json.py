from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Set
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..categories import classify_ftb_text
from ..domain import AdapterError, TranslationProject, TranslationUnit
from ..io_utils import atomic_write_text, read_text_detect
from ..output_guard import assert_source_unchanged, snapshot_path
from ..snbt import SnbtCompound, SnbtList, SnbtNode, SnbtParseError, SnbtScalar, SnbtString, parse_snbt
from .base import QuestAdapter, validate_translation_selection
from .path_safety import (
    find_regular_files_by_suffix_no_reparse,
    find_regular_files_no_reparse,
    safe_is_directory,
    safe_is_regular_file,
    validate_single_locale_output,
)


_BRACED_TRANSLATION_KEY = re.compile(r"\{([A-Za-z0-9_.:-]+)\}")
_RAW_JSON_TRANSLATION_KEY = re.compile(
    r'(?:\\?")translate(?:\\?")\s*:\s*(?:\\?")([A-Za-z0-9_.:-]+)(?:\\?")'
)
_MAX_QUEST_REFERENCE_FILE_BYTES = 16 * 1024 * 1024
_MAX_QUEST_REFERENCE_FILES = 20_000
_MAX_RESOURCE_ID_LENGTH = 512
_RESOURCE_ID = re.compile(r"[a-z0-9_.-]+:[a-z0-9_./-]+")
_QUEST_REFERENCE_PREFIXES = ("quest.", "quests.")
_RESOURCE_FIELDS = frozenset({"block", "entity", "fluid", "item"})
# Item Filters stores a selector object in the task's ``item`` field.  Its own
# registry ID describes the selector implementation, not the item or tag named
# by the quest title, so it must never be treated as terminology context.
_RESOURCE_SELECTOR_NAMESPACES = frozenset({"itemfilters"})
_QUEST_CHILD_KINDS = {
    "quests": "quest",
    "tasks": "task",
    "rewards": "reward",
    "chapter_groups": "chapter_group",
    "groups": "chapter_group",
    "reward_tables": "reward_table",
    "quest_links": "quest_link",
    "images": "image",
}


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _JsonCandidate:
    path: Path
    confidence: int


class FtbLegacyJsonAdapter(QuestAdapter):
    id = "ftb_legacy_json"
    label = "FTB Quests 1.20.x以前（エクスポート済み言語JSON）"
    description = "FTB Quest Localizer 等が生成した en_us.json を翻訳します。"

    def probe(self, path: Path, source_locale: str) -> int:
        if safe_is_directory(
            path,
            "旧版言語JSONの探索ルート",
            anchor=path,
        ):
            candidates = _find_json_candidates(path, source_locale)
            if not candidates:
                return 0
            # A conventional FTB-specific path is stronger evidence than
            # generic KubeJS language keys.  Keep content-only detection below
            # legacy raw SNBT so an unrelated ``quest.*.title`` UI catalog
            # cannot hide a real quest book during instance-wide auto detect.
            # Multiple equally strong candidates remain positive here so the
            # legacy JSON adapter wins over raw SNBT and ``load`` can report
            # the actual ambiguity instead of silently selecting another
            # input format.
            if candidates[0].confidence >= 3:
                return 70
            return 65 if candidates[0].confidence >= 2 else 55
        if safe_is_regular_file(path, "旧版の原文言語JSON") and (
            path.suffix.lower() == ".json"
            and path.stem.lower() == source_locale.lower()
        ):
            return 75
        return 0

    def load(
        self,
        path: Path,
        source_locale: str,
        target_locale: str,
        minecraft_version: str = "",
        output_override: Path | None = None,
    ) -> TranslationProject:
        source_file = _resolve_source_file(path, source_locale)
        source_snapshot = snapshot_path(source_file)
        text, encoding, newline = read_text_detect(source_file)
        try:
            parsed = json.loads(text, object_pairs_hook=_unique_object)
        except _DuplicateJsonKey as exc:
            raise AdapterError(f"言語 JSON に重複キーがあります: {source_file} ({exc})") from exc
        except ValueError as exc:
            raise AdapterError(f"言語 JSON を解析できません: {source_file} ({exc})") from exc
        if not isinstance(parsed, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()):
            raise AdapterError("言語 JSON は文字列キーと文字列値だけのオブジェクトである必要があります")
        resource_ids_by_key, resource_warnings = _quest_title_resource_ids(path)
        units = [
            TranslationUnit(
                id=f"json-{index:06d}",
                key=key,
                source=value,
                context=f"Minecraft / FTB Quests language key: {key}",
                source_path=str(source_file),
                ordinal=index,
                category=classify_ftb_text(key),
                resource_ids=resource_ids_by_key.get(key, ()),
            )
            for index, (key, value) in enumerate(parsed.items())
        ]
        assert_source_unchanged(source_snapshot)
        default_output = output_override or source_file.with_name(f"{target_locale.lower()}.json")
        # Never inspect an existing translation through a reparse point.  This
        # must precede _partition_existing, which reads the destination.
        validate_single_locale_output(source_file, default_output, target_locale, ".json")
        project = TranslationProject(
            adapter_id=self.id,
            adapter_label=self.label,
            source_path=source_file,
            default_output=default_output,
            source_locale=source_locale,
            target_locale=target_locale,
            units=units,
            documents=[parsed],
            metadata={
                "encoding": encoding,
                "newline": newline,
                "source_snapshot": source_snapshot,
            },
            warnings=resource_warnings,
        )
        if default_output.exists() and default_output.resolve() != source_file.resolve():
            project.existing, project.metadata["existing_unknown"] = _partition_existing(
                default_output,
                units,
            )
        return project

    def validate_output(self, project: TranslationProject, output_path: Path) -> None:
        assert_source_unchanged(project.metadata["source_snapshot"])
        validate_single_locale_output(project.source_path, output_path, project.target_locale, ".json")
        if output_path.exists():
            project.existing, project.metadata["existing_unknown"] = _partition_existing(
                output_path,
                project.units,
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
        values: OrderedDict[str, str] = OrderedDict()
        for unit in project.units:
            if unit.id in selected:
                values[unit.key] = translations[unit.id]
        for key, value in project.metadata.get("existing_unknown", {}).items():
            if key not in values:
                values[key] = value
        rendered = json.dumps(values, ensure_ascii=False, indent=2) + "\n"
        atomic_write_text(output_path, rendered, encoding="utf-8", newline=project.metadata.get("newline", "\n"))


def _find_json_candidates(path: Path, source_locale: str) -> list[_JsonCandidate]:
    expected = f"{source_locale.lower()}.json"
    candidates = find_regular_files_no_reparse(
        path,
        expected,
        "旧版言語JSONの探索ルート",
    )
    quest_references = _quest_translation_references(path)
    ranked = [
        _JsonCandidate(candidate, confidence)
        for candidate in candidates
        if (
            confidence := _quest_catalog_confidence(
                candidate,
                anchor=path,
                quest_references=quest_references,
            )
        )
        > 0
    ]
    return sorted(
        ranked,
        key=lambda candidate: (
            -candidate.confidence,
            str(candidate.path).casefold(),
            str(candidate.path),
        ),
    )


def _looks_like_quest_language_catalog(path: Path) -> bool:
    return _quest_catalog_confidence(path) > 0


def _quest_catalog_confidence(
    path: Path,
    anchor: Path | None = None,
    quest_references: frozenset[str] = frozenset(),
) -> int:
    if not safe_is_regular_file(
        path,
        "旧版の原文言語JSON",
        anchor=anchor,
    ):
        return 0
    lowered_parts = tuple(part.lower() for part in path.parts)
    path_markers = {
        "ftblang",
        "ftbquest",
        "ftbquests",
        "ftb_quests",
        "ftb-quests",
        "ftbquestlocalizer",
    }
    path_confidence = 2 if any(
        part in path_markers for part in lowered_parts[-7:-1]
    ) else 0
    try:
        text, _encoding, _newline = read_text_detect(path)
        parsed = json.loads(text, object_pairs_hook=_unique_object)
    except (AdapterError, ValueError):
        return path_confidence
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        return path_confidence
    matching_references = quest_references.intersection(parsed)
    if matching_references:
        # A key referenced by this instance's FTB quest data is stronger than
        # directory naming and lets a root-only UI select a KubeJS catalog
        # even while the already-keyed raw quest files still exist.  Keep
        # quest-shaped text references separate from ordinary item/material
        # keys used by task titles: the latter identify terminology, not the
        # quest catalog.  Equal authored catalogs remain ambiguous instead of
        # silently selecting the larger half of a split catalog.
        if any(_looks_like_quest_reference(key) for key in matching_references):
            return 4
        return 3
    # One generic ``quest.*.title`` key is common in non-FTB UI language
    # catalogs and is not sufficient evidence during recursive auto-detect.
    # A single weak candidate can still be selected automatically when no
    # stronger FTB-specific source or raw quest book competes with it.
    categorized = sum(classify_ftb_text(key) != "other" for key in parsed)
    return max(path_confidence, 1 if categorized >= 2 else 0)


def _looks_like_quest_reference(key: str) -> bool:
    normalized = key.strip().casefold()
    return normalized.startswith(_QUEST_REFERENCE_PREFIXES) or (
        classify_ftb_text(key) != "other"
    )


def _quest_translation_references(path: Path) -> frozenset[str]:
    quest_root = _quest_root_for_catalog_search(path)
    if quest_root is None:
        return frozenset()
    try:
        source_files = find_regular_files_by_suffix_no_reparse(
            quest_root,
            ".snbt",
            "旧版FTB Questsのtranslation key照合",
        )
    except AdapterError:
        return frozenset()
    references: set[str] = set()
    for source_file in source_files[:_MAX_QUEST_REFERENCE_FILES]:
        try:
            if source_file.stat().st_size > _MAX_QUEST_REFERENCE_FILE_BYTES:
                continue
            text, _encoding, _newline = read_text_detect(source_file)
        except (AdapterError, OSError):
            continue
        references.update(_BRACED_TRANSLATION_KEY.findall(text))
        references.update(_RAW_JSON_TRANSLATION_KEY.findall(text))
    return frozenset(references)


def _quest_title_resource_ids(path: Path) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    """Associate task/reward title keys with resources from the same object.

    These values are deliberately only hints.  Any malformed object, reused
    translation key, or invalid resource location disables the association
    instead of guessing from a chapter name, quest icon, or nearby object.
    """

    quest_root = _quest_root_for_catalog_search(path)
    if quest_root is None:
        return {}, []
    try:
        source_files = find_regular_files_by_suffix_no_reparse(
            quest_root,
            ".snbt",
            "旧版FTB Questsのtask/reward resource ID照合",
        )
    except AdapterError as exc:
        return {}, [
            "task/reward名とresource IDの対応を安全に確認できなかったため、"
            f"resource IDによる用語優先を使用しません ({exc})"
        ]

    warnings: list[str] = []
    if len(source_files) > _MAX_QUEST_REFERENCE_FILES:
        warnings.append(
            "quest SNBTが安全上限を超えたため、上限以降のファイルでは"
            "task/reward名にresource IDによる用語優先を使用しません "
            f"({len(source_files)} > {_MAX_QUEST_REFERENCE_FILES})"
        )
    observed: dict[str, tuple[str, ...]] = {}
    ambiguous: set[str] = set()
    invalid_objects = 0
    for source_file in source_files[:_MAX_QUEST_REFERENCE_FILES]:
        try:
            if source_file.stat().st_size > _MAX_QUEST_REFERENCE_FILE_BYTES:
                warnings.append(
                    f"{_relative_quest_path(source_file, quest_root)}: ファイルサイズが安全上限を"
                    "超えるため、このファイルではtask/reward名にresource IDによる"
                    "用語優先を使用しません"
                )
                continue
            text, _encoding, _newline = read_text_detect(source_file)
            root = parse_snbt(text)
        except (AdapterError, OSError, UnicodeError, SnbtParseError) as exc:
            warnings.append(
                f"{_relative_quest_path(source_file, quest_root)}: quest SNBTを解析できないため、"
                "このファイルではtask/reward名にresource IDによる用語優先を"
                f"使用しません ({exc})"
            )
            continue
        if not isinstance(root, SnbtCompound):
            warnings.append(
                f"{_relative_quest_path(source_file, quest_root)}: top-level compoundではないため、"
                "このファイルではtask/reward名にresource IDによる用語優先を使用しません"
            )
            continue
        invalid_objects += _walk_title_resource_ids(
            root,
            "file",
            observed,
            ambiguous,
        )

    if invalid_objects:
        warnings.append(
            f"task/reward名 {invalid_objects}件はresource IDまたはオブジェクト構造が不正なため、"
            "その名前ではresource IDによる用語優先を使用しません"
        )
    if ambiguous:
        preview_limit = 8
        ordered = sorted(ambiguous)
        preview = ", ".join(ordered[:preview_limit])
        omitted = len(ordered) - preview_limit
        if omitted:
            preview += f"、ほか{omitted}件"
        warnings.append(
            "同じtask/reward title翻訳キーが異なるresource IDまたはresourceなしの"
            "オブジェクトで再利用されているため、そのキーの関連付けを無効にしました: "
            + preview
        )
    return {
        key: resource_ids
        for key, resource_ids in observed.items()
        if resource_ids and key not in ambiguous
    }, warnings


def _walk_title_resource_ids(
    compound: SnbtCompound,
    kind: str,
    observed: dict[str, tuple[str, ...]],
    ambiguous: set[str],
) -> int:
    invalid_objects = 0
    if kind in {"task", "reward"}:
        title_nodes = [entry.value for entry in compound.entries if entry.key == "title"]
        title_keys = {
            key
            for title_node in title_nodes
            for key in _translation_keys_from_title(title_node)
        }
        resource_ids = _resource_ids_from_quest_object(compound)
        invalid = len(title_nodes) > 1 or resource_ids is None
        if title_keys:
            if invalid:
                invalid_objects += 1
                resource_ids = ()
            assert resource_ids is not None
            for key in title_keys:
                _record_title_resource_ids(
                    key,
                    resource_ids,
                    observed,
                    ambiguous,
                )

    for entry in compound.entries:
        child_kind = _QUEST_CHILD_KINDS.get(entry.key)
        if child_kind is None:
            continue
        child = entry.value
        if isinstance(child, SnbtList):
            for item in child.items:
                if isinstance(item, SnbtCompound):
                    invalid_objects += _walk_title_resource_ids(
                        item,
                        child_kind,
                        observed,
                        ambiguous,
                    )
        elif isinstance(child, SnbtCompound):
            for child_entry in child.entries:
                if isinstance(child_entry.value, SnbtCompound):
                    invalid_objects += _walk_title_resource_ids(
                        child_entry.value,
                        child_kind,
                        observed,
                        ambiguous,
                    )
    return invalid_objects


def _translation_keys_from_title(node: SnbtNode) -> frozenset[str]:
    values: list[str] = []
    if isinstance(node, (SnbtString, SnbtScalar)):
        values.append(node.value)
    elif isinstance(node, SnbtList):
        values.extend(
            item.value
            for item in node.items
            if isinstance(item, (SnbtString, SnbtScalar))
        )
    result: set[str] = set()
    for value in values:
        result.update(_BRACED_TRANSLATION_KEY.findall(value))
        result.update(_RAW_JSON_TRANSLATION_KEY.findall(value))
    return frozenset(result)


def _resource_ids_from_quest_object(
    compound: SnbtCompound,
) -> tuple[str, ...] | None:
    """Return direct semantic resources, or ``None`` for an invalid shape."""

    field_nodes: list[SnbtNode] = []
    seen_fields: set[str] = set()
    for entry in compound.entries:
        if entry.key not in _RESOURCE_FIELDS:
            continue
        if entry.key in seen_fields:
            return None
        seen_fields.add(entry.key)
        field_nodes.append(entry.value)
    if not field_nodes:
        return ()

    resource_ids: set[str] = set()
    for node in field_nodes:
        resource_id = _direct_resource_id(node)
        if resource_id is None:
            return None
        if resource_id:
            resource_ids.add(resource_id)
    return tuple(sorted(resource_ids))


def _direct_resource_id(node: SnbtNode) -> str | None:
    if isinstance(node, (SnbtString, SnbtScalar)):
        value = node.value
    elif isinstance(node, SnbtCompound):
        id_nodes = [entry.value for entry in node.entries if entry.key == "id"]
        if len(id_nodes) != 1 or not isinstance(id_nodes[0], (SnbtString, SnbtScalar)):
            return None
        value = id_nodes[0].value
    else:
        return None
    if not (0 < len(value) <= _MAX_RESOURCE_ID_LENGTH) or _RESOURCE_ID.fullmatch(value) is None:
        return None
    namespace, resource_path = value.split(":", 1)
    if not namespace or resource_path.startswith("/") or resource_path.endswith("/") or "//" in resource_path:
        return None
    if namespace in _RESOURCE_SELECTOR_NAMESPACES:
        return ""
    return value


def _record_title_resource_ids(
    key: str,
    resource_ids: tuple[str, ...],
    observed: dict[str, tuple[str, ...]],
    ambiguous: set[str],
) -> None:
    if key in ambiguous:
        return
    if key not in observed:
        observed[key] = resource_ids
    elif observed[key] != resource_ids:
        ambiguous.add(key)


def _relative_quest_path(path: Path, quest_root: Path) -> str:
    try:
        return path.relative_to(quest_root).as_posix()
    except ValueError:
        return str(path)


def _quest_root_for_catalog_search(path: Path) -> Path | None:
    if not safe_is_directory(path, "旧版言語JSONの探索ルート", anchor=path):
        return None
    if path.name.lower() == "quests" and safe_is_directory(
        path / "chapters",
        "旧版FTB Questsのchapters",
        anchor=path,
    ):
        return path
    for spelling in ("ftbquests", "ftb_quests"):
        candidate = path / "config" / spelling / "quests"
        if safe_is_directory(
            candidate,
            "旧版FTB Questsのquestルート",
            anchor=path,
        ):
            return candidate
    return None


def _resolve_source_file(path: Path, source_locale: str) -> Path:
    if safe_is_directory(
        path,
        "旧版言語JSONの探索ルート",
        anchor=path,
    ):
        candidates = _find_json_candidates(path, source_locale)
    elif safe_is_regular_file(path, "旧版の原文言語JSON"):
        return path
    else:
        raise AdapterError(f"旧版の原文言語JSONファイルまたはinstanceを指定してください: {path}")
    if not candidates:
        raise AdapterError(f"{source_locale}.json が見つかりません: {path}")
    highest_confidence = candidates[0].confidence
    strongest = [
        candidate
        for candidate in candidates
        if candidate.confidence == highest_confidence
    ]
    if len(strongest) > 1:
        preview_limit = 8
        preview = "\n".join(
            f"- {_relative_candidate_path(candidate.path, path)}"
            for candidate in strongest[:preview_limit]
        )
        omitted = len(strongest) - preview_limit
        if omitted > 0:
            preview += f"\n- ...ほか {omitted}件"
        raise AdapterError(
            "旧版FTB Questsの原文言語JSONを自動で1つに特定できませんでした。\n"
            f"自動判定で同じ優先度になった候補: {len(strongest)}件\n"
            f"{preview}\n"
            "誤った翻訳先言語JSONへ書き込まないため、解析を停止しました。\n"
            "対処: 翻訳対象ではない候補をインスタンス外へ移すか、"
            f"ファイル名を {source_locale.lower()}.json 以外へ一時的に変更してから再解析してください。"
        )
    return strongest[0].path


def _relative_candidate_path(candidate: Path, root: Path) -> str:
    try:
        return candidate.relative_to(root.absolute()).as_posix()
    except ValueError:
        # This should only be needed for unusual path aliases.  Keeping the
        # absolute path is safer than hiding an otherwise ambiguous source.
        return str(candidate)


def _load_existing(path: Path, units: list[TranslationUnit]) -> dict[str, str]:
    existing, _unknown = _partition_existing(path, units)
    return existing


def _partition_existing(
    path: Path,
    units: list[TranslationUnit],
) -> tuple[dict[str, str], OrderedDict[str, str]]:
    try:
        text, _encoding, _newline = read_text_detect(path)
        parsed = json.loads(text, object_pairs_hook=_unique_object)
    except _DuplicateJsonKey as exc:
        raise AdapterError(f"既存の翻訳先言語 JSON に重複キーがあります: {path} ({exc})") from exc
    except (AdapterError, ValueError) as exc:
        raise AdapterError(f"既存の翻訳先言語 JSON を解析できません: {path} ({exc})") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise AdapterError(
            f"既存の翻訳先言語 JSON は文字列キーと文字列値だけである必要があります: {path}"
        )
    by_key = {unit.key: unit.id for unit in units}
    existing = {
        by_key[key]: value
        for key, value in parsed.items()
        if key in by_key and isinstance(value, str)
    }
    unknown = OrderedDict(
        (key, value)
        for key, value in parsed.items()
        if (
            key not in by_key
            and isinstance(value, str)
            and not key.casefold().startswith("mq_localizer.")
        )
    )
    return existing, unknown


def _unique_object(pairs: list[tuple[str, object]]) -> OrderedDict[str, object]:
    result: OrderedDict[str, object] = OrderedDict()
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result
