from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .adapters import AdapterRegistry, QuestAdapter, create_default_registry
from .domain import TranslationProject


@dataclass(frozen=True, slots=True)
class AnalyzedProject:
    adapter: QuestAdapter
    project: TranslationProject


class LocalizerApplication:
    def __init__(self, registry: AdapterRegistry | None = None) -> None:
        self.registry = registry or create_default_registry()

    def analyze(
        self,
        source_path: Path,
        adapter_id: str,
        source_locale: str,
        target_locale: str,
        minecraft_version: str,
        output_override: Path | None = None,
    ) -> AnalyzedProject:
        adapter = (
            self.registry.detect(source_path, source_locale, minecraft_version)
            if not adapter_id or adapter_id == "auto"
            else self.registry.get(adapter_id)
        )
        project = adapter.load(
            source_path,
            source_locale.lower(),
            target_locale.lower(),
            minecraft_version,
            output_override,
        )
        return AnalyzedProject(adapter, project)
