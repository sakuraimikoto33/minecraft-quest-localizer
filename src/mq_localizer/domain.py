from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class LocalizerError(Exception):
    """An error that can be shown to an end user without a traceback."""


class AdapterError(LocalizerError):
    """The selected quest format could not be read or written."""


class TranslationError(LocalizerError):
    """A translation response was missing, invalid, or unsafe to write."""


class CancelledError(LocalizerError):
    """The user cancelled the current operation."""


@dataclass(frozen=True, slots=True)
class TranslationUnit:
    """One independently validated string sent to a translation provider."""

    id: str
    key: str
    source: str
    context: str = ""
    source_path: str = ""
    ordinal: int = 0
    category: str = "other"
    # Adapter-supplied, immutable context only.  A translator may use these
    # exact resource locations to prefer the matching Mod language entry for
    # this unit without changing terminology decisions for any other unit.
    resource_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class TranslationProject:
    """Adapter-neutral representation of one localization job."""

    adapter_id: str
    adapter_label: str
    source_path: Path
    default_output: Path
    source_locale: str
    target_locale: str
    units: list[TranslationUnit]
    documents: list[Any] = field(default_factory=list, repr=False)
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    existing: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TranslationOutcome:
    output_path: Path
    total: int
    translated: int
    reused: int
    copied_without_translation: int
    glossary_terms: int
    skipped_by_selection: int = 0
    preserved_unselected: int = 0
