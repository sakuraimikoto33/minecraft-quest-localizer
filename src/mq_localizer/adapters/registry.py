from __future__ import annotations

import re
from pathlib import Path

from ..domain import AdapterError
from .base import QuestAdapter


class AdapterRegistry:
    def __init__(self, adapters: list[QuestAdapter] | None = None) -> None:
        self._adapters: dict[str, QuestAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: QuestAdapter) -> None:
        if adapter.id in self._adapters:
            raise ValueError(f"Duplicate adapter id: {adapter.id}")
        self._adapters[adapter.id] = adapter

    def get(self, adapter_id: str) -> QuestAdapter:
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise AdapterError(f"不明な形式アダプターです: {adapter_id}") from exc

    def all(self) -> list[QuestAdapter]:
        return list(self._adapters.values())

    def detect(
        self,
        path: Path,
        source_locale: str,
        minecraft_version: str = "",
    ) -> QuestAdapter:
        parsed_version = _parsed_minecraft_version(minecraft_version)
        scored_candidates: list[tuple[int, QuestAdapter, AdapterError | None]] = []
        for adapter in self._adapters.values():
            probe_error: AdapterError | None = None
            try:
                score = adapter.probe(path, source_locale)
            except AdapterError as exc:
                fallback_score = getattr(adapter, "probe_error_fallback_score", None)
                if fallback_score is None:
                    raise
                score = int(fallback_score)
                probe_error = exc
            managed_rerun_score = getattr(adapter, "managed_rerun_probe_score", None)
            if (
                score == managed_rerun_score
                and parsed_version is not None
                and parsed_version < (1, 21, 0)
            ):
                # A verified quests.bak + keyed quests pair belongs to this
                # tool.  On 1.20.x, keep its rerun on the raw adapter even if
                # another tool also generated lang/en_us.snbt.  1.21+ keeps
                # the native locale adapters' normal higher scores.
                score = 110
            scored_candidates.append((score, adapter, probe_error))
        scored = sorted(scored_candidates, key=lambda item: item[0], reverse=True)
        if not scored or scored[0][0] <= 0:
            raise AdapterError(
                "インスタンス内でFTB Questsの翻訳元を判定できません。"
                "config/ftbquests/quests と原文localeが実際のインスタンス内にあることを確認し、"
                "同じインスタンスルートを再解析してください。"
            )
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            labels = " / ".join(item[1].label for item in scored if item[0] == scored[0][0])
            raise AdapterError(f"入力形式を一意に判定できません: {labels}")
        if scored[0][2] is not None:
            raise scored[0][2]
        winner = scored[0][1]
        if winner.id == "ftb_legacy_raw" and _version_at_least(
            minecraft_version,
            (1, 21, 0),
        ):
            raise AdapterError(
                "Minecraft 1.21以降では旧版raw変換へ自動fallbackしません。"
                "FTB Questsでクエストブックを一度開いてlocaleファイルを生成し、"
                f"lang/{source_locale.lower()}.snbt、lang/{source_locale.lower()}/、"
                "または分割JSON5が生成された同じインスタンスルートを再解析してください"
            )
        return winner


def _version_at_least(value: str, minimum: tuple[int, int, int]) -> bool:
    parsed = _parsed_minecraft_version(value)
    return parsed is not None and parsed >= minimum


def _parsed_minecraft_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?", value.strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def create_default_registry() -> AdapterRegistry:
    # Local imports keep third-party/future adapters independently installable.
    from .ftb_legacy_json import FtbLegacyJsonAdapter
    from .ftb_legacy_raw import FtbLegacyRawAdapter
    from .ftb_modern import FtbModernSnbtAdapter
    from .ftb_split_json5 import FtbSplitJson5Adapter
    from .ftb_split_snbt import FtbSplitSnbtAdapter

    return AdapterRegistry(
        [
            FtbModernSnbtAdapter(),
            FtbSplitSnbtAdapter(),
            FtbSplitJson5Adapter(),
            FtbLegacyJsonAdapter(),
            FtbLegacyRawAdapter(),
        ]
    )
