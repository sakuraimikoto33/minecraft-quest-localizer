from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence, Set
from pathlib import Path
from typing import Mapping

from ..domain import AdapterError, TranslationProject


class QuestAdapter(ABC):
    """Extension point for FTB Quests, Better Questing, and mod language data."""

    id: str
    label: str
    description: str

    @abstractmethod
    def probe(self, path: Path, source_locale: str) -> int:
        """Return 0 for unsupported or a larger confidence score for a match."""

    @abstractmethod
    def load(
        self,
        path: Path,
        source_locale: str,
        target_locale: str,
        minecraft_version: str = "",
        output_override: Path | None = None,
    ) -> TranslationProject:
        """Read source data without modifying it."""

    def validate_output(self, project: TranslationProject, output_path: Path) -> None:
        """Reject unsafe output paths before any provider request or write."""

        if output_path.resolve() == project.source_path.resolve():
            raise AdapterError("原文と同じパスには書き込めません")

    @abstractmethod
    def write(
        self,
        project: TranslationProject,
        translations: Mapping[str, str],
        output_path: Path,
        selected_unit_ids: Set[str] | None = None,
    ) -> None:
        """Validate and atomically write the adapter's output artifact(s).

        ``selected_unit_ids`` is ``None`` for a complete catalog.  When the
        user deliberately selects only some text categories, it identifies
        the exact subset that may be emitted into the target locale.
        """


def validate_translation_selection(
    project: TranslationProject,
    translations: Mapping[str, str],
    selected_unit_ids: Set[str] | None,
) -> frozenset[str]:
    """Distinguish an intentional category subset from an incomplete write."""

    all_ids = [unit.id for unit in project.units]
    if len(all_ids) != len(set(all_ids)):
        raise AdapterError("内部エラー: 翻訳単位IDが重複しています")
    known = frozenset(all_ids)
    selected = known if selected_unit_ids is None else frozenset(selected_unit_ids)
    unknown_selected = selected - known
    if unknown_selected:
        raise AdapterError(f"内部エラー: 不明な翻訳単位が選択されています: {next(iter(unknown_selected))}")
    provided = frozenset(translations)
    missing = selected - provided
    if missing:
        unit_by_id = {unit.id: unit for unit in project.units}
        missing_id = next(iter(missing))
        raise AdapterError(f"翻訳がありません: {unit_by_id[missing_id].key}")
    unexpected = provided - selected
    if unexpected:
        raise AdapterError(f"内部エラー: 選択外の翻訳が渡されました: {next(iter(unexpected))}")
    return selected


def array_translation_is_selected(
    key: str,
    unit_ids: Sequence[str],
    selected: Set[str],
    *,
    complete_catalog: bool,
) -> bool:
    """Return whether a language-array value may be emitted as one unit.

    FTB Quests consumes a description/subtitle array as one language value.
    Writing source text for the unselected members would violate the adapter
    contract that a partial target catalog contains translations only.  Empty
    arrays carry no translation unit and therefore belong only to a complete
    catalog write.
    """

    if not unit_ids:
        return complete_catalog
    included = sum(unit_id in selected for unit_id in unit_ids)
    if included == 0:
        return False
    if included != len(unit_ids):
        raise AdapterError(
            "配列型の翻訳項目は一部だけを出力できません。"
            f"配列全体を選択するか除外してください: {key}"
        )
    return True
