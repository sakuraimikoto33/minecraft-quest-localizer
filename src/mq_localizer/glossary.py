from __future__ import annotations

import json
import os
import re
import stat
import struct
import tomllib
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Iterable, Literal

from .domain import CancelledError
from .glossary_snapshot import GlossaryInputRecorder, GlossaryInputSnapshot
from .minecraft_assets import MinecraftLanguageBundle, load_minecraft_language_bundle
from .protection import (
    TermReplacement,
    protected_syntax_ranges,
    protected_syntax_signature,
    terminology_literal_skeleton,
    terminology_target_is_safe,
)
from .scan_limits import GlossaryScanLimits


_LANG_PATH = re.compile(r"^assets/([^/]+)/lang/([^/]+)\.(json|lang)$", re.IGNORECASE)
_FORMAT_CODE_PATTERN = re.compile(
    r"§x(?:§[0-9A-Fa-f]){6}|&x(?:&[0-9A-Fa-f]){6}"
    r"|[§&]#[0-9A-Fa-f]{6}|[§&][0-9A-FK-ORZa-fk-orz]",
    re.IGNORECASE,
)
_STRING_PRINTF_TOKEN = re.compile(
    r"%(?:\d+\$)?[-#]*\d*(?:\.\d+)?[sS]\Z"
)
_JAPANESE_CLAUSE_PARTICLE = re.compile(
    r"(?<=[A-Za-z0-9\u30A0-\u30FF\u3400-\u9FFF々〆ヵヶ])"
    r"(?:が|を|は|も|に|へ|で|と)"
    r"(?=[A-Za-z0-9\u3040-\u30FF\u3400-\u9FFF々〆ヵヶ])"
)
_JAPANESE_STATUS_ENDING = re.compile(
    r"(?:中|済み|待ち|完了|失敗|成功|有効|無効|必要|不要|不足|過剰|可能|不可能)\s*\Z"
)
_JAPANESE_PREDICATE_ENDING = re.compile(
    r"(?:"
    r"ください|下さい|しました|されました|なりました|できました|"
    r"しています|されています|なっています|できています|"
    r"します|されます|なります|できます|あります|ありません|"
    r"でした|です|である|している|されている|なっている|できている|"
    r"する|される|なる|できる|した|された|なった|いる|います|ます"
    r")\s*\Z"
)
_JAPANESE_SENTENCE_PUNCTUATION = re.compile(r"[。！？.!?]+\s*\Z")
_ENGLISH_IMPERATIVE_OBJECT = re.compile(
    r"\s+(?:(?:a|an|the)\s+(?=\S)|(?=(?:%|\{|\$|/|https?://|[a-z0-9_.-]+:)))",
    re.IGNORECASE,
)
_ENGLISH_TECHNICAL_OBJECT = re.compile(
    r"\s+(?:(?:a|an|the)\s+)?(?=(?:%|\{|\$|/|https?://|[a-z0-9_.-]+:))",
    re.IGNORECASE,
)
_ENGLISH_ARTICLE_OBJECT = re.compile(r"\s+(?:a|an|the)\s+", re.IGNORECASE)
_ENGLISH_GERUND_OBJECT = re.compile(r"\s+(?:a|an|the)\b", re.IGNORECASE)
_ENGLISH_FOLLOWING_WORD = re.compile(r"\s+[A-Za-z]")
_ASCII_WORD_PATTERN = re.compile(r"[A-Za-z]+")
_RESOURCE_ALIAS_SAFE_NAME = re.compile(r"[A-Za-z0-9()\s]+\Z")
_RESOURCE_ALIAS_TOKEN = re.compile(r"[A-Za-z0-9]+")
_RIGHT_NAME_ATOM = re.compile(
    r"\s+(?P<atom>(?:[A-Z][A-Za-z0-9]*(?:['’][A-Za-z]+)?|[A-Z]{2,}))\b"
)
_RIGHT_CONNECTED_NAME_ATOM = re.compile(
    r"\s+(?:(?:\\?&|[+/:])\s+)+"
    r"(?P<atom>(?:[A-Z][A-Za-z0-9]*(?:['’][A-Za-z]+)?|[A-Z]{2,}|[0-9]+))\b",
)
_LEFT_NAME_ATOM = re.compile(
    r"(?P<atom>[A-Za-z][A-Za-z0-9]*(?:['’](?:s|S)?)?)\s*\Z"
)
_LEFT_CONNECTED_SEPARATOR = re.compile(r"(?:\\?&|[+/:])\s*\Z")
_VISIBLE_VALUE_DECORATION = re.compile(
    r'''[\s"'`“”‘’()\[\]{}<>:;,.!?…\-–—+*•‣◦▪]*\Z'''
)
_SENTENCE_BOUNDARIES = frozenset({"\r", "\n", ".", "!", "?", "。", "！", "？"})
_SENTENCE_START_DECORATION = re.compile(
    r'''[\s"'“”‘’()\[\]{}]*'''
    r'''(?:(?:[-*+•‣◦▪]|\[[ xX]\]|(?:\d+|[A-Za-z])[.)])\s+)?'''
    r'''["'“”‘’()\[\]{}\s]*\Z'''
)
_PROJECT_REFERENCE_LABEL_BEFORE = re.compile(
    r"(?:\b(?:book|chapter|group|quest|task|reward|title)s?"
    r"(?:\s+(?:named|called))?\s+)$",
    re.IGNORECASE,
)
_PROJECT_REFERENCE_LABEL_AFTER = re.compile(
    r"^\s+(?:book|chapter|group|quest|task|reward|title)s?\b",
    re.IGNORECASE,
)
_PROJECT_REFERENCE_QUOTED_LABEL_AFTER = re.compile(
    r'''^["'`”’\])}\s]+(?:book|chapter|group|quest|task|reward|title)s?\b''',
    re.IGNORECASE,
)
_PROJECT_REFERENCE_QUOTED_ITEM_BEFORE = re.compile(
    r'''(?:^|["'`“‘(\[]\s*|(?:\\?&|,|\band\b|\bor\b)\s+)\Z''',
    re.IGNORECASE,
)
_PROJECT_REFERENCE_COORDINATED_BEFORE = re.compile(
    r"\b[A-Z][A-Za-z0-9]*(?:['’][A-Za-z]+)?\s+(?:and|or|&)\s+$"
)
_PROJECT_REFERENCE_COORDINATED_AFTER = re.compile(
    r"^\s+(?:and|or|&)\s+[A-Z][A-Za-z0-9]*(?:['’][A-Za-z]+)?\b"
)
_PROJECT_REFERENCE_ACTION_CONTINUATION = re.compile(
    r"^\s+(?i:and|or|then|to)\s+[a-z]",
)
_ENGLISH_ACTION_TITLE_VERBS = frozenset(
    {
        "activate",
        "assemble",
        "break",
        "build",
        "collect",
        "complete",
        "craft",
        "create",
        "defeat",
        "destroy",
        "discover",
        "enter",
        "explore",
        "find",
        "finish",
        "get",
        "grow",
        "kill",
        "make",
        "obtain",
        "place",
        "reach",
        "return",
        "smelt",
        "start",
        "travel",
        "upgrade",
        "use",
        "visit",
    }
)
_MOD_NAME_KEY_PREFIX = "mod.display_name."
_PROJECT_REFERENCE_KEY_PREFIX = "mq_localizer.project_reference."
_PROJECT_REFERENCE_MOD_DISPLAY_KEY_PREFIX = (
    _PROJECT_REFERENCE_KEY_PREFIX + "mod_display."
)
_MAX_PROJECT_REFERENCE_CHARS = 512
_MATCH_PREFIX_LENGTH = 8
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_MAX_LANGUAGE_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_LANGUAGE_BYTES = 64 * 1024 * 1024
_MAX_SCAN_LANGUAGE_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_LANGUAGE_ENTRIES_PER_MEMBER = 250_000
_MAX_EXTERNAL_ZIP_CENTRAL_DIRECTORY_BYTES = _MAX_ARCHIVE_MEMBERS * 46
_MAX_LANGUAGE_COMPRESSED_MEMBER_BYTES = 16 * 1024 * 1024
_DEFAULT_SCAN_LIMITS = GlossaryScanLimits()
_PRIMARY_METADATA_READERS: tuple[
    tuple[str, Callable[[bytes, str], list[tuple[str, str]]]], ...
] = (
    ("META-INF/neoforge.mods.toml", lambda data, locale: _read_forge_metadata(data)),
    ("META-INF/mods.toml", lambda data, locale: _read_forge_metadata(data)),
    ("fabric.mod.json", lambda data, locale: _read_fabric_metadata(data, locale)),
    ("quilt.mod.json", lambda data, locale: _read_quilt_metadata(data, locale)),
    ("mcmod.info", lambda data, locale: _read_legacy_forge_metadata(data, locale)),
    ("META-INF/mcmod.info", lambda data, locale: _read_legacy_forge_metadata(data, locale)),
)
_TERM_KEY_PREFIXES = (
    "item.",
    "block.",
    "entity.",
    "fluid.",
    "biome.",
    "effect.",
    "enchantment.",
    "potion.",
    "trim_pattern.",
    "trim_material.",
    "itemgroup.",
    "item_group.",
    "creative_tab.",
    "dimension.",
    "structure.",
    "attribute.name.",
    "painting.",
    "instrument.",
    "jukebox_song.",
    "tag.item.",
    "tag.block.",
    "tag.fluid.",
    "tag.entity_type.",
    "key.categories.",
)
_REGISTRY_TERM_KEY_PREFIXES = (
    "item.",
    "block.",
    "entity.",
    "fluid.",
    "biome.",
    "effect.",
    "enchantment.",
    "potion.",
    "trim_pattern.",
    "trim_material.",
    "dimension.",
    "structure.",
    "painting.",
    "instrument.",
    "jukebox_song.",
)
_DESCRIPTIVE_TERM_KEY_SEGMENTS = frozenset(
    {
        "desc",
        "description",
        "detail",
        "details",
        "help",
        "info",
        "tooltip",
        "usage",
    }
)
_DOTTED_DESCRIPTIVE_TERM_KEY_SEGMENTS = frozenset({"flavor", "flavour", "lore"})
_DERIVED_TERM_KEY_SEGMENTS = frozenset({"form", "profession", "type", "variant"})
_TERM_KEY_TOKEN_SPLIT = re.compile(r"[._-]+")
_ITEM_MODEL_PATH = re.compile(
    r"^assets/([^/]+)/(?:models/item|items)/(.+)\.json$",
    re.IGNORECASE,
)
_BLOCKSTATE_PATH = re.compile(
    r"^assets/([^/]+)/blockstates/(.+)\.json$",
    re.IGNORECASE,
)
_RESOURCE_ID = re.compile(
    r"^(?P<namespace>[a-z0-9_.-]+):(?P<path>[a-z0-9_./-]+)$"
)
_RESOURCE_LANGUAGE_PREFIXES = (
    "item",
    "block",
    "fluid",
    "entity",
    "biome",
    "effect",
)
_SourceTier = Literal["minecraft", "mod", "kubejs", "resourcepack", "project"]
_SOURCE_TIER_PRIORITY: dict[str, int] = {
    "minecraft": 0,
    "mod": 1,
    "kubejs": 2,
    "resourcepack": 3,
    "project": 4,
}


class _ScanLanguageBudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class _SharedScanBudget:
    remaining_language_bytes: int | None
    language_byte_limit: int | None = _MAX_SCAN_LANGUAGE_BYTES
    source_member_limit: int | None = _MAX_ARCHIVE_MEMBERS
    language_file_limit: int | None = _MAX_LANGUAGE_MEMBER_BYTES
    source_language_limit: int | None = _MAX_ARCHIVE_LANGUAGE_BYTES
    compressed_language_file_limit: int | None = _MAX_LANGUAGE_COMPRESSED_MEMBER_BYTES
    scan_limits: GlossaryScanLimits = field(default_factory=GlossaryScanLimits)

    def reserve_language_bytes(self, amount: int) -> None:
        amount = max(0, amount)
        if self.remaining_language_bytes is None or self.language_byte_limit is None:
            return
        if amount > self.remaining_language_bytes:
            self.remaining_language_bytes = 0
            raise _ScanLanguageBudgetExceeded(
                f"対象言語ファイルの全走査上限 {self.language_byte_limit} bytes に達しました"
            )
        self.remaining_language_bytes -= amount


def _minimum_enabled_budget(*budgets: int | None) -> int | None:
    enabled = tuple(budget for budget in budgets if budget is not None)
    return min(enabled) if enabled else None


@dataclass(frozen=True, slots=True)
class _ParsedLanguage:
    values: dict[str, str]
    keys: frozenset[str]
    duplicate_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ExternalAssetSource:
    path: Path
    label: str
    storage: Literal["directory", "zip"]
    source_kind: Literal["kubejs", "resourcepack"]
    safety_anchor: Path


@dataclass(frozen=True, slots=True)
class _ExternalAssetDiscovery:
    sources: tuple[_ExternalAssetSource, ...] = ()
    warnings: tuple[str, ...] = ()
    discovered_sources: int = 0
    failed_sources: int = 0
    kubejs_sources: int = 0
    resourcepack_sources: int = 0


@dataclass(frozen=True, slots=True)
class _TargetOnlyLanguageEvidence:
    mod_id: str
    key: str
    raw_target: str
    provenance: str
    container_label: str
    independent_fixed_targets: frozenset[str]
    source_tier: _SourceTier


class _JsonObjectPairs(list[tuple[str, object]]):
    """Distinguish JSON objects from arrays while retaining duplicate keys."""


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    source: str
    target: str
    key: str
    mod_id: str
    translated: bool
    provenance: str
    target_state: Literal[
        "auto",
        "missing",
        "translated",
        "explicit_source",
        "rejected",
    ] = "auto"
    source_had_printf: bool = False
    source_tier: _SourceTier = "mod"

    def __post_init__(self) -> None:
        if self.target_state == "auto":
            object.__setattr__(
                self,
                "target_state",
                "translated" if self.translated else "missing",
            )


@dataclass(frozen=True, slots=True)
class _PendingTerminologyWarning:
    evidence: GlossaryEntry
    message: str
    container_label: str


@dataclass(frozen=True, slots=True)
class GlossaryScanProgress:
    """Progress reported immediately before and after an archive scan attempt.

    ``current`` is the one-based index of ``archive_name`` within ``total``.
    Callback exceptions are deliberately not swallowed: they abort the scan and
    propagate to the caller, just like cancellation.
    """

    current: int
    total: int
    archive_name: str
    phase: Literal["before", "after"]
    source_kind: Literal["mod_archive", "kubejs", "resourcepack"] = "mod_archive"


GlossaryProgressCallback = Callable[[GlossaryScanProgress], None]


@dataclass(frozen=True, slots=True)
class GlossaryCoverage:
    """Unambiguous archive and terminology coverage for user-facing decisions."""

    discovered_archives: int
    scanned_archives: int
    failed_archives: int
    skipped_archives: int
    archives_with_warnings: int
    partial_warning_count: int
    warning_count: int
    mod_display_names: int
    official_terms: int
    minecraft_asset_warning_count: int = 0
    external_sources_discovered: int = 0
    external_sources_scanned: int = 0
    external_sources_failed: int = 0
    external_sources_skipped: int = 0
    external_sources_with_warnings: int = 0
    external_asset_warning_count: int = 0
    kubejs_sources_scanned: int = 0
    resourcepack_sources_scanned: int = 0
    resourcepacks_enabled: bool = False

    @property
    def scan_state(
        self,
    ) -> Literal["no_archives", "all_failed", "partial_failure", "complete"]:
        if self.discovered_archives == 0:
            return "no_archives"
        if self.scanned_archives == 0:
            return "all_failed"
        if self.failed_archives or self.skipped_archives:
            return "partial_failure"
        return "complete"

    @property
    def term_state(
        self,
    ) -> Literal[
        "none",
        "mod_names_only",
        "official_terms_only",
        "mod_names_and_official_terms",
    ]:
        if self.mod_display_names and self.official_terms:
            return "mod_names_and_official_terms"
        if self.mod_display_names:
            return "mod_names_only"
        if self.official_terms:
            return "official_terms_only"
        return "none"

    @property
    def has_protection(self) -> bool:
        return bool(self.mod_display_names or self.official_terms)

    @property
    def has_partial_warnings(self) -> bool:
        return bool(
            self.partial_warning_count
            or self.minecraft_asset_warning_count
            or self.external_asset_warning_count
            or self.external_sources_failed
            or self.external_sources_skipped
        )

    @property
    def summary(self) -> str:
        archive_detail = (
            f"Mod JAR: {self.discovered_archives}件検出 / "
            f"{self.scanned_archives}件走査成功 / {self.failed_archives}件失敗"
        )
        if self.skipped_archives:
            archive_detail += f" / {self.skipped_archives}件未走査"
        if self.partial_warning_count:
            archive_detail += (
                f" / 部分警告 {self.partial_warning_count}件"
                f"（{self.archives_with_warnings} JAR）"
            )
        if self.minecraft_asset_warning_count:
            archive_detail += (
                " / Minecraft公式言語資産の読取警告 "
                f"{self.minecraft_asset_warning_count}件"
            )
        if self.external_sources_discovered or self.resourcepacks_enabled:
            external_detail = (
                f"追加言語資産: {self.external_sources_discovered}件検出 / "
                f"{self.external_sources_scanned}件走査成功 / "
                f"{self.external_sources_failed}件失敗"
            )
            if self.external_sources_skipped:
                external_detail += f" / {self.external_sources_skipped}件未走査"
            external_detail += (
                f"（KubeJS {self.kubejs_sources_scanned}件 / "
                f"resource pack {self.resourcepack_sources_scanned}件"
                f"・走査{'有効' if self.resourcepacks_enabled else '無効'}）"
            )
            if self.external_asset_warning_count:
                external_detail += f" / 警告 {self.external_asset_warning_count}件"
            archive_detail += f" / {external_detail}"
        return (
            f"{archive_detail} / Mod表示名: {self.mod_display_names}件 / "
            f"公式用語: {self.official_terms}件"
        )


@dataclass(frozen=True, slots=True)
class _VisibleProjection:
    text: str
    locations: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class _TermMatch:
    entry: GlossaryEntry
    visible: str
    fragments: tuple[tuple[int, str], ...]
    fragment_ranges: tuple[tuple[int, int, int], ...]
    contiguous: bool


@dataclass(slots=True)
class GlossaryCatalog:
    entries: dict[str, GlossaryEntry] = field(default_factory=dict)
    conflicts: dict[str, list[GlossaryEntry]] = field(default_factory=dict)
    evidence: dict[str, tuple[GlossaryEntry, ...]] = field(default_factory=dict, repr=False)
    scanned_archives: int = 0
    warnings: list[str] = field(default_factory=list)
    discovered_archives: int = 0
    failed_archives: int = 0
    archives_with_warnings: int = 0
    partial_warning_count: int = 0
    minecraft_asset_warning_count: int = 0
    external_sources_discovered: int = 0
    external_sources_scanned: int = 0
    external_sources_failed: int = 0
    external_sources_skipped: int = 0
    external_sources_with_warnings: int = 0
    external_asset_warning_count: int = 0
    kubejs_sources_scanned: int = 0
    resourcepack_sources_scanned: int = 0
    resourcepacks_enabled: bool = False
    input_snapshot: GlossaryInputSnapshot | None = field(default=None, repr=False)
    _prefix_index: dict[tuple[int, str], list[str]] = field(default_factory=dict, repr=False)
    _ascii_mod_prefix_index: dict[tuple[int, str], list[str]] = field(default_factory=dict, repr=False)
    _match_rank: dict[str, int] = field(default_factory=dict, repr=False)
    _prefix_lengths: tuple[int, ...] = field(default=(), repr=False)
    _common_ascii_words: frozenset[str] = field(default_factory=frozenset, repr=False)
    _resource_key_index: dict[str, tuple[GlossaryEntry, ...]] = field(
        default_factory=dict,
        repr=False,
    )
    _resource_key_index_ready: bool = field(default=False, repr=False)
    _match_index_ready: bool = field(default=False, repr=False)
    _match_entries: dict[str, GlossaryEntry] = field(default_factory=dict, repr=False)
    _contextual_filtering: bool = field(default=True, repr=False)
    _official_term_count_override: int | None = field(default=None, repr=False)

    @property
    def mod_display_name_count(self) -> int:
        return sum(_is_mod_display_name(entry) for entry in self.entries.values())

    @property
    def official_term_count(self) -> int:
        if self._official_term_count_override is not None:
            return self._official_term_count_override
        return sum(
            not _is_mod_display_name(entry) and not _is_project_reference(entry)
            for entry in self.entries.values()
        )

    @property
    def coverage(self) -> GlossaryCoverage:
        # Catalogs constructed by older callers may only set scanned_archives.
        # Normalize their discovered count without changing the legacy fields.
        discovered = max(
            self.discovered_archives,
            self.scanned_archives + self.failed_archives,
        )
        skipped = max(0, discovered - self.scanned_archives - self.failed_archives)
        return GlossaryCoverage(
            discovered_archives=discovered,
            scanned_archives=self.scanned_archives,
            failed_archives=self.failed_archives,
            skipped_archives=skipped,
            archives_with_warnings=self.archives_with_warnings,
            partial_warning_count=self.partial_warning_count,
            warning_count=len(self.warnings),
            mod_display_names=self.mod_display_name_count,
            official_terms=self.official_term_count,
            minecraft_asset_warning_count=self.minecraft_asset_warning_count,
            external_sources_discovered=self.external_sources_discovered,
            external_sources_scanned=self.external_sources_scanned,
            external_sources_failed=self.external_sources_failed,
            external_sources_skipped=self.external_sources_skipped,
            external_sources_with_warnings=self.external_sources_with_warnings,
            external_asset_warning_count=self.external_asset_warning_count,
            kubejs_sources_scanned=self.kubejs_sources_scanned,
            resourcepack_sources_scanned=self.resourcepack_sources_scanned,
            resourcepacks_enabled=self.resourcepacks_enabled,
        )

    def coverage_summary(self) -> str:
        return self.coverage.summary

    def scoped_for_resources(
        self,
        source_text: str,
        resource_ids: tuple[str, ...],
    ) -> GlossaryCatalog:
        """Return a unit-only catalog backed by structured resource IDs.

        Resource aliases are never added to the global catalog. They are used
        only when an exact language key exists and the quest title contains
        exactly the same visible name content as that official name (word
        order and round parentheses may differ). Numbers, non-ASCII words,
        and meaningful punctuation must also agree.
        """

        if not resource_ids:
            return self
        visible = _visible_projection([source_text]).text.strip()
        signature = _resource_name_signature(visible)
        if not visible:
            return self

        self._ensure_resource_key_index()
        resource_entries: list[tuple[str, GlossaryEntry | None]] = []
        raw_resource_entries: list[GlossaryEntry] = []
        exact_resource_keys: set[str] = set()
        has_unresolved_resource_key = False
        namespaces: set[str] = set()
        for resource_id in resource_ids:
            parsed = _parse_resource_id(resource_id)
            if parsed is None:
                continue
            namespace, path = parsed
            namespaces.add(namespace)
            dotted_path = path.replace("/", ".")
            found_for_resource = False
            for prefix in _RESOURCE_LANGUAGE_PREFIXES:
                language_key = f"{prefix}.{namespace}.{dotted_path}"
                candidates = self._resource_key_index.get(language_key, ())
                if not candidates:
                    continue
                found_for_resource = True
                exact_resource_keys.add(language_key)
                raw_resource_entries.extend(candidates)
                resolved_candidates = _resolve_resource_language_entries(candidates)
                if not resolved_candidates:
                    has_unresolved_resource_key = True
                resource_entries.extend(
                    (resource_id, entry) for entry in resolved_candidates
                )
            if found_for_resource and not any(
                candidate_resource_id == resource_id
                for candidate_resource_id, _entry in resource_entries
            ):
                resource_entries.append((resource_id, None))

        if not resource_entries:
            return self

        aliases: list[tuple[str, GlossaryEntry]] = []
        visible_words = _resource_related_words(visible)
        related_resource_evidence = any(
            visible_words.intersection(_resource_related_words(entry.source))
            for entry in raw_resource_entries
        )
        for resource_id, entry in resource_entries:
            if entry is None:
                continue
            entry_signature = _resource_name_signature(entry.source)
            if signature is None or entry_signature != signature:
                continue
            aliases.append((resource_id, entry))
        if has_unresolved_resource_key:
            # One exact language-key identity is ambiguous. Do not borrow a
            # target from another registry prefix for the same resource ID.
            aliases.clear()

        scoped_entries = dict(self.entries)
        changed = False
        whole_title_entries: list[tuple[str, GlossaryEntry]] = []
        exact_global = scoped_entries.get(visible)
        if exact_global is not None:
            whole_title_entries.append((visible, exact_global))
        if visible.isascii():
            whole_title_entries.extend(
                (source, entry)
                for source, entry in scoped_entries.items()
                if source != visible
                and source.isascii()
                and source.casefold() == visible.casefold()
                and _is_mod_display_name(entry)
            )
        for global_source, global_entry in whole_title_entries:
            global_is_exact_resource = (
                global_entry.key.casefold() in exact_resource_keys
            )
            global_is_own_mod_name = (
                _is_mod_display_name(global_entry)
                and global_entry.mod_id.casefold() in namespaces
            )
            if (
                related_resource_evidence
                and not global_is_exact_resource
                and not global_is_own_mod_name
            ):
                # An exact task resource exists, but this whole-title match
                # came from another registry identity. Namespace equality
                # alone is not enough: two resources in one Mod may share a
                # short label. ASCII Mod displays follow normal case-insensitive
                # matching here as well.
                scoped_entries.pop(global_source, None)
                changed = True

        if aliases:
            translated_targets = {
                entry.target for _resource_id, entry in aliases if entry.translated
            }
            has_untranslated = any(not entry.translated for _resource_id, entry in aliases)
            if len(translated_targets) == 1 and not has_untranslated:
                target = next(iter(translated_targets))
                translated = True
            elif not translated_targets:
                # Keep the quest author's visible word order when the Mod has
                # no official target-locale value.
                target = visible
                translated = False
            else:
                target = ""
                translated = False
            if target:
                resource_id, representative = min(
                    aliases,
                    key=lambda item: (
                        item[0],
                        _glossary_evidence_sort_key(item[1]),
                    ),
                )
                scoped_entries[visible] = GlossaryEntry(
                    source=visible,
                    target=target,
                    key=representative.key,
                    mod_id=resource_id.split(":", 1)[0],
                    translated=translated,
                    provenance=(
                        f"resource-scoped {resource_id}: "
                        f"{representative.provenance}"
                    ),
                    target_state=("translated" if translated else "missing"),
                    source_tier=representative.source_tier,
                )
                changed = True

        if not changed:
            return self
        return GlossaryCatalog(
            entries=scoped_entries,
            conflicts=self.conflicts,
            evidence=self.evidence,
            scanned_archives=self.scanned_archives,
            warnings=self.warnings,
            discovered_archives=self.discovered_archives,
            failed_archives=self.failed_archives,
            archives_with_warnings=self.archives_with_warnings,
            partial_warning_count=self.partial_warning_count,
            minecraft_asset_warning_count=self.minecraft_asset_warning_count,
            external_sources_discovered=self.external_sources_discovered,
            external_sources_scanned=self.external_sources_scanned,
            external_sources_failed=self.external_sources_failed,
            external_sources_skipped=self.external_sources_skipped,
            external_sources_with_warnings=self.external_sources_with_warnings,
            external_asset_warning_count=self.external_asset_warning_count,
            kubejs_sources_scanned=self.kubejs_sources_scanned,
            resourcepack_sources_scanned=self.resourcepack_sources_scanned,
            resourcepacks_enabled=self.resourcepacks_enabled,
            input_snapshot=self.input_snapshot,
            _official_term_count_override=self.official_term_count,
            _contextual_filtering=self._contextual_filtering,
        )

    def with_source_preserved_terms(
        self,
        terms: Iterable[str],
    ) -> GlossaryCatalog:
        """Overlay project names whose unchecked source spelling must remain visible.

        These temporary entries are deliberately separate from Mod-language
        evidence and coverage counts.  They exist only for one translation
        run, and take precedence over an identically-spelled official term
        because an unchecked title continues to be displayed from the source
        catalog.
        """

        normalized = sorted(
            {
                term.strip()
                for term in terms
                if isinstance(term, str) and _project_reference_term_is_safe(term.strip())
            }
        )
        if not normalized:
            return self

        scoped_entries = dict(self.entries)
        changed = False
        for index, source in enumerate(normalized):
            existing = scoped_entries.get(source)
            if existing is not None and _is_project_reference(existing):
                continue
            backed_by_mod_display = bool(
                existing is not None
                and _is_mod_display_name(existing)
                and not existing.translated
                and existing.target == source
            )
            entry = GlossaryEntry(
                source=source,
                target=source,
                key=(
                    f"{_PROJECT_REFERENCE_MOD_DISPLAY_KEY_PREFIX}{index:08x}"
                    if backed_by_mod_display
                    else f"{_PROJECT_REFERENCE_KEY_PREFIX}{index:08x}"
                ),
                mod_id=(
                    existing.mod_id
                    if backed_by_mod_display and existing is not None
                    else f"mq_project_reference_{index:08x}"
                ),
                translated=False,
                provenance=(
                    "unchecked project title backed by Mod display: "
                    + existing.provenance
                    if backed_by_mod_display and existing is not None
                    else "unchecked project title"
                ),
                target_state="explicit_source",
                source_tier="project",
            )
            if scoped_entries.get(source) != entry:
                scoped_entries[source] = entry
                changed = True
        if not changed:
            return self
        return GlossaryCatalog(
            entries=scoped_entries,
            conflicts=self.conflicts,
            evidence=self.evidence,
            scanned_archives=self.scanned_archives,
            warnings=self.warnings,
            discovered_archives=self.discovered_archives,
            failed_archives=self.failed_archives,
            archives_with_warnings=self.archives_with_warnings,
            partial_warning_count=self.partial_warning_count,
            minecraft_asset_warning_count=self.minecraft_asset_warning_count,
            external_sources_discovered=self.external_sources_discovered,
            external_sources_scanned=self.external_sources_scanned,
            external_sources_failed=self.external_sources_failed,
            external_sources_skipped=self.external_sources_skipped,
            external_sources_with_warnings=self.external_sources_with_warnings,
            external_asset_warning_count=self.external_asset_warning_count,
            kubejs_sources_scanned=self.kubejs_sources_scanned,
            resourcepack_sources_scanned=self.resourcepack_sources_scanned,
            resourcepacks_enabled=self.resourcepacks_enabled,
            input_snapshot=self.input_snapshot,
            _contextual_filtering=self._contextual_filtering,
            _official_term_count_override=self.official_term_count,
        )

    def replacements_for(self, text: str) -> dict[str, str]:
        return self.replacements_for_parts([text])[0]

    def replacements_for_parts(self, parts: list[str]) -> list[dict[str, str]]:
        """Return per-part replacements, including Mod names split by styles/components."""

        replacements: list[dict[str, str]] = [{} for _part in parts]
        for match in self._matches(parts):
            expected = _expected_match_value(match)
            if expected != match.visible and match.contiguous:
                part_index, fragment = match.fragments[0]
                _merge_replacement(replacements[part_index], fragment, expected)
            else:
                # A Mod display name (or a styled/split official term) is kept
                # as its exact visible fragments so every fragment disappears
                # from the provider payload without moving formatting codes.
                for part_index, fragment in match.fragments:
                    _merge_replacement(replacements[part_index], fragment, fragment)
        return replacements

    def replacement_spans_for_parts(
        self,
        parts: list[str],
    ) -> list[list[TermReplacement]]:
        """Return occurrence-specific replacements without string-key collisions."""

        replacements: list[list[TermReplacement]] = [[] for _part in parts]
        for match in self._matches(parts):
            for fragment_index, (part_index, start, end) in enumerate(
                match.fragment_ranges
            ):
                replacement = (
                    _expected_match_value(match)
                    if match.contiguous
                    else match.fragments[fragment_index][1]
                )
                replacements[part_index].append(
                    TermReplacement(start, end, replacement)
                )
        for part_replacements in replacements:
            part_replacements.sort(key=lambda item: (item.start, item.end))
        return replacements

    def layout_replacement_spans_for_pair(
        self,
        source: str,
        candidate: str,
    ) -> tuple[list[TermReplacement], list[TermReplacement]]:
        """Return occurrence-aware canonical term spans for layout comparison.

        Canonical markers are based on the complete expected term, not a
        fragment's text.  Thus ``Sword`` inside a styled ``Rainbow Sword`` and
        an independent ``Sword -> 剣`` remain distinct even though a plain
        ``dict[str, str]`` cannot represent both occurrences.
        """

        matches = self._matches([source])
        expected_entries: dict[str, GlossaryEntry] = {}
        expected_by_match: list[str] = []
        for match in matches:
            expected = _expected_match_value(match)
            expected_by_match.append(expected)
            if expected in expected_entries:
                continue
            expected_entries[expected] = GlossaryEntry(
                source=expected,
                target=expected,
                key=f"mq_localizer.layout_expected.{len(expected_entries)}",
                mod_id="mq_localizer",
                translated=False,
                provenance="existing translation layout validation",
            )
        marker_by_expected = {
            expected: f"MQTERM{index:04X}"
            for index, expected in enumerate(sorted(expected_entries))
        }

        source_spans: list[TermReplacement] = []
        for match, expected in zip(matches, expected_by_match, strict=True):
            marker = marker_by_expected[expected]
            for fragment_index, (_part, start, end) in enumerate(
                match.fragment_ranges
            ):
                source_spans.append(
                    TermReplacement(
                        start,
                        end,
                        f"{marker}F{fragment_index:04X}",
                    )
                )

        candidate_spans: list[TermReplacement] = []
        expected_catalog = GlossaryCatalog(
            entries=expected_entries,
            _contextual_filtering=False,
        )
        for match in expected_catalog._matches([candidate]):
            marker = marker_by_expected[match.visible]
            for fragment_index, (_part, start, end) in enumerate(
                match.fragment_ranges
            ):
                candidate_spans.append(
                    TermReplacement(
                        start,
                        end,
                        f"{marker}F{fragment_index:04X}",
                    )
                )
        source_spans.sort(key=lambda item: (item.start, item.end))
        candidate_spans.sort(key=lambda item: (item.start, item.end))
        return source_spans, candidate_spans

    def candidate_preserves_term_layout(
        self,
        source_parts: list[str],
        candidate_parts: list[str],
    ) -> bool:
        """Verify term text and its formatting/component fragment boundaries.

        A raw JSON component's style belongs to its text leaf. Merely finding
        the complete Mod name after concatenating all leaves is insufficient:
        moving part of the name into a differently styled leaf changes what
        the player sees. Contiguous translated terms may move inside their
        original leaf, while split/styled terms retain every exact fragment.
        """

        if len(source_parts) != len(candidate_parts):
            return False
        source_matches = self._matches(source_parts)
        if not source_matches:
            return True

        expected_entries: dict[str, GlossaryEntry] = {}
        for match in source_matches:
            expected = _expected_match_value(match)
            if expected not in expected_entries:
                expected_entries[expected] = GlossaryEntry(
                    source=expected,
                    target=expected,
                    key=f"mq_localizer.layout_expected.{len(expected_entries)}",
                    mod_id="mq_localizer",
                    translated=False,
                    provenance="existing translation component validation",
                )

        expected_catalog = GlossaryCatalog(
            entries=expected_entries,
            _contextual_filtering=False,
        )
        candidate_matches = expected_catalog._matches(candidate_parts)

        def signature(match: _TermMatch, expected: str) -> tuple[object, ...]:
            if match.contiguous:
                return (expected, "contiguous", match.fragments[0][0])
            return (
                expected,
                "fragments",
                tuple(match.fragments),
            )

        source_signature = Counter(
            signature(match, _expected_match_value(match))
            for match in source_matches
        )
        candidate_signature = Counter(
            signature(match, match.visible)
            for match in candidate_matches
        )
        return source_signature == candidate_signature

    def candidate_preserves_terms(
        self,
        source_parts: list[str],
        candidate_parts: list[str],
    ) -> bool:
        """Check that a reusable translation still honors all discovered terminology."""

        expected = Counter(
            _expected_match_value(match)
            for match in self._matches(source_parts)
        )
        if not expected:
            return True
        candidate = _visible_projection(candidate_parts).text
        return all(_count_bounded(candidate, value) >= count for value, count in expected.items())

    def _matches(self, parts: list[str]) -> list[_TermMatch]:
        projection = _visible_projection(parts)
        if not projection.text or not self.entries:
            return []
        candidates = self._candidate_sources(projection.text)
        match_entries = self._match_entries
        protected_ranges = [protected_syntax_ranges(part) for part in parts]

        occupied: list[tuple[int, int]] = []
        matches: list[_TermMatch] = []
        # Prefer the longest exact official name. A short Mod display name is
        # not allowed to hide a longer item name such as ``Create Wrench`` or
        # ``Engineer's Blueprint`` from the provider.
        for source in sorted(candidates, key=self._match_rank.__getitem__):
            entry = match_entries[source]
            insensitive = _is_mod_display_name(entry) and source.isascii()
            for index in _term_occurrences(projection.text, source, insensitive):
                end = index + len(source)
                if (
                    end > len(projection.text)
                    or not _term_has_boundaries(
                        projection.text,
                        index,
                        end,
                        projection.text[index:end],
                    )
                    or (
                        self._contextual_filtering
                        and _looks_like_unbranded_mod_word(
                        projection.text,
                        index,
                        end,
                        entry,
                        self._common_ascii_words,
                        candidates,
                        match_entries,
                    )
                    )
                    or any(
                        index < used_end and end > used_start
                        for used_start, used_end in occupied
                    )
                ):
                    continue
                fragment_ranges = _projection_fragment_ranges(
                    projection,
                    index,
                    end,
                )
                # A Mod display name can also be a resource namespace
                # (``ElementalCraft`` inside
                # ``#elementalcraft:gems/fine_water``).  The complete
                # resource ID already has stronger exact-preservation
                # semantics, so claiming its namespace as a terminology
                # span would overlap TokenProtector's special span and
                # abort before the OpenAI request.  Formatting codes do
                # not cause false rejection because projected term
                # fragments lie strictly on either side of their ranges.
                syntax_overlap = any(
                    fragment_start < protected_end
                    and fragment_end > protected_start
                    for part_index, fragment_start, fragment_end in fragment_ranges
                    for protected_start, protected_end in protected_ranges[part_index]
                )
                if syntax_overlap:
                    if not _is_project_reference(entry):
                        continue
                    fragment_ranges = _subtract_protected_fragment_ranges(
                        fragment_ranges,
                        protected_ranges,
                    )
                    if not fragment_ranges:
                        continue
                if self._contextual_filtering:
                    if _looks_like_embedded_name_fragment(
                        projection.text,
                        index,
                        end,
                        entry,
                        parts,
                        fragment_ranges,
                        self._common_ascii_words,
                        candidates,
                        match_entries,
                    ):
                        continue
                    if _looks_like_ambiguous_official_word(
                        projection.text,
                        index,
                        end,
                        entry,
                        parts,
                        fragment_ranges,
                        self._common_ascii_words,
                        matches,
                        candidates,
                        match_entries,
                    ):
                        continue
                    if _looks_like_ambiguous_project_reference(
                        projection.text,
                        index,
                        end,
                        entry,
                        parts,
                        fragment_ranges,
                        self._common_ascii_words,
                    ):
                        continue
                fragments = tuple(
                    (
                        part_index,
                        parts[part_index][fragment_start:fragment_end],
                    )
                    for part_index, fragment_start, fragment_end in fragment_ranges
                )
                if fragments:
                    occupied.append((index, end))
                    matches.append(
                        _TermMatch(
                            entry=entry,
                            visible=projection.text[index:end],
                            fragments=fragments,
                            fragment_ranges=fragment_ranges,
                            contiguous=len(fragments) == 1
                            and fragments[0][1] == projection.text[index:end],
                        )
                    )
        return matches

    def _candidate_sources(self, visible_text: str) -> set[str]:
        """Return only terms whose short prefix actually occurs in visible text."""

        self._ensure_match_index()
        candidates: set[str] = set()
        for prefix_length in self._prefix_lengths:
            if prefix_length > len(visible_text):
                continue
            for start in range(len(visible_text) - prefix_length + 1):
                fragment = visible_text[start : start + prefix_length]
                candidates.update(self._prefix_index.get((prefix_length, fragment), ()))
                if fragment.isascii():
                    candidates.update(
                        self._ascii_mod_prefix_index.get((prefix_length, fragment.lower()), ())
                    )
        return candidates

    def _ensure_match_index(self) -> None:
        if self._match_index_ready:
            return
        self._match_entries = _entries_with_regular_plural_aliases(self.entries)
        word_mod_ids: dict[str, set[str]] = {}
        if self.evidence:
            evidence = [
                entry
                for source_evidence in self.evidence.values()
                for entry in source_evidence
            ]
        else:
            evidence = list(self.entries.values())
            evidence.extend(
                conflict_entry
                for conflict_entries in self.conflicts.values()
                for conflict_entry in conflict_entries
            )
        for entry in evidence:
            source = entry.source
            identity = entry.mod_id.casefold() or entry.provenance
            for word in _ASCII_WORD_PATTERN.findall(source):
                canonical = _canonical_ascii_word(word)
                word_mod_ids.setdefault(canonical, set()).add(identity)
        # The same plain word appearing in terminology from independent Mods
        # is evidence that it is ordinary vocabulary, not enough evidence by
        # itself to claim an occurrence in prose. This corpus-derived signal
        # avoids a language-specific denylist while retaining unique names
        # such as Ratlantis and Spiritfire.
        self._common_ascii_words = frozenset(
            word for word, mod_ids in word_mod_ids.items() if len(mod_ids) >= 2
        )
        ordered_sources = sorted(
            (source for source in self._match_entries if source),
            key=lambda source: (
                len(source),
                not _is_mod_display_name(self._match_entries[source]),
            ),
            reverse=True,
        )
        prefix_lengths: set[int] = set()
        for rank, source in enumerate(ordered_sources):
            self._match_rank[source] = rank
            prefix_length = min(_MATCH_PREFIX_LENGTH, len(source))
            prefix_lengths.add(prefix_length)
            key = (prefix_length, source[:prefix_length])
            if _is_mod_display_name(self._match_entries[source]) and source.isascii():
                key = (prefix_length, key[1].lower())
                self._ascii_mod_prefix_index.setdefault(key, []).append(source)
            else:
                self._prefix_index.setdefault(key, []).append(source)
        self._prefix_lengths = tuple(sorted(prefix_lengths))
        self._match_index_ready = True

    def _ensure_resource_key_index(self) -> None:
        if self._resource_key_index_ready:
            return
        indexed: dict[str, list[GlossaryEntry]] = {}
        evidence = (
            (
                entry
                for entries in self.evidence.values()
                for entry in entries
            )
            if self.evidence
            else iter(self.entries.values())
        )
        for entry in evidence:
            if _is_mod_display_name(entry):
                continue
            indexed.setdefault(entry.key.casefold(), []).append(entry)
        self._resource_key_index = {
            key: tuple(sorted(entries, key=_glossary_evidence_sort_key))
            for key, entries in indexed.items()
        }
        self._resource_key_index_ready = True


def _term_occurrences(text: str, term: str, ascii_case_insensitive: bool) -> Iterable[int]:
    start = 0
    while term:
        if ascii_case_insensitive:
            index = _find_ascii_case_insensitive(text, term, start)
        else:
            index = text.find(term, start)
        if index < 0:
            return
        yield index
        start = index + len(term)


def _looks_like_unbranded_mod_word(
    text: str,
    start: int,
    end: int,
    entry: GlossaryEntry,
    common_ascii_words: frozenset[str],
    candidate_sources: set[str],
    entries: dict[str, GlossaryEntry],
) -> bool:
    """Avoid hiding ordinary prose that happens to equal a Mod display name.

    ``Create a Forgotten Minion`` is an instruction, whereas both ``Build a
    Create machine`` and ``Use Create a lot`` refer to the Create Mod.  The
    metadata spelling also distinguishes branded ``Create`` from the lower-case
    verb in ``can create a machine``. These signals apply to every single-word
    ASCII Mod name; there is no product-name denylist. Formatting codes have
    already been removed from ``text``.
    """

    project_reference = _is_project_reference(entry)
    if not (
        (_is_mod_display_name(entry) or project_reference)
        and entry.source.isascii()
        and entry.source.isalpha()
    ):
        return False
    visible = text[start:end]
    common_word = _canonical_ascii_word(entry.source) in common_ascii_words
    if visible.islower() and not entry.source.islower():
        return True
    if not _is_sentence_or_list_start(text, start):
        return False
    if _ENGLISH_TECHNICAL_OBJECT.match(text, end) is not None:
        return True
    if _known_glossary_object_follows(
        text,
        end,
        entry,
        candidate_sources,
        entries,
    ):
        return True
    if project_reference:
        # A project may legitimately have a one-word name such as Ratlantis,
        # but ordinary quest titles such as Create, Good, or Storage must not
        # hide an instruction merely because that title was unchecked.
        if _ENGLISH_IMPERATIVE_OBJECT.match(text, end) is not None:
            return True
        return common_word and _ENGLISH_FOLLOWING_WORD.match(text, end) is not None
    return common_word and _ENGLISH_IMPERATIVE_OBJECT.match(text, end) is not None


def _known_glossary_object_follows(
    text: str,
    verb_end: int,
    verb_entry: GlossaryEntry,
    candidate_sources: set[str],
    entries: dict[str, GlossaryEntry],
) -> bool:
    article = _ENGLISH_ARTICLE_OBJECT.match(text, verb_end)
    if article is None:
        return False
    object_start = article.end()
    for source in candidate_sources:
        if source == verb_entry.source or not source:
            continue
        candidate_entry = entries[source]
        insensitive = _is_mod_display_name(candidate_entry) and source.isascii()
        if insensitive:
            occurs_here = (
                text[object_start : object_start + len(source)].isascii()
                and text[object_start : object_start + len(source)].lower() == source.lower()
            )
        else:
            occurs_here = text.startswith(source, object_start)
        object_end = object_start + len(source)
        if (
            occurs_here
            and object_end <= len(text)
            and _term_has_boundaries(
                text,
                object_start,
                object_end,
                text[object_start:object_end],
            )
        ):
            return True
    return False


def _looks_like_ambiguous_official_word(
    text: str,
    start: int,
    end: int,
    entry: GlossaryEntry,
    parts: list[str],
    fragment_ranges: tuple[tuple[int, int, int], ...],
    common_ascii_words: frozenset[str],
    accepted_matches: list[_TermMatch],
    candidate_sources: set[str],
    entries: dict[str, GlossaryEntry],
) -> bool:
    """Return whether a one-word label is too ambiguous to claim in prose.

    Mod language files contain short values such as ``Good``, ``Pressure``,
    and ``Storage``. They are authoritative only when the quest actually means
    that label; blindly replacing the same spelling in ordinary English makes
    hybrid output such as ``Good幸運`` or ``空気Pressure``. A word shared by
    independent Mods is treated as ambiguous unless the occurrence has an
    explicit signal: it is the whole value, is styled/quoted, or appears with
    a stronger term from the same Mod.
    """

    source = entry.source
    if (
        _is_mod_display_name(entry)
        or _is_project_reference(entry)
        or not _is_ordinary_shaped_ascii_word(source)
    ):
        return False
    if _term_has_attached_name_suffix(text, end):
        return True
    if _term_is_whole_visible_value(text, start, end):
        return False
    if _term_has_explicit_format_scope(parts, fragment_ranges):
        return False
    if _term_is_quoted(text, start, end):
        return False
    connected_start = _connected_name_extension_start(text, end)
    if connected_start is not None and _candidate_term_starts_at(
        text,
        connected_start,
        candidate_sources,
        entries,
    ):
        return False
    if _candidate_term_ends_before_separator(
        text,
        start,
        candidate_sources,
        entries,
    ):
        return False
    if any(
        match.entry.mod_id.casefold() == entry.mod_id.casefold()
        and (
            _is_mod_display_name(match.entry)
            or not _is_ordinary_shaped_ascii_word(match.entry.source)
        )
        for match in accepted_matches
    ):
        return False
    if _is_generic_group_entry(entry):
        return True
    if _canonical_ascii_word(source) in common_ascii_words:
        return True
    if not _entry_key_identifies_label(entry):
        return True
    # A sentence-initial ``Finding the ...`` is a gerund phrase, not enough
    # evidence that the text refers to a one-word painting/item named Finding.
    return (
        source.casefold().endswith("ing")
        and _is_sentence_or_list_start(text, start)
        and _ENGLISH_GERUND_OBJECT.match(text, end) is not None
    )


def _looks_like_ambiguous_project_reference(
    text: str,
    start: int,
    end: int,
    entry: GlossaryEntry,
    parts: list[str],
    fragment_ranges: tuple[tuple[int, int, int], ...],
    common_ascii_words: frozenset[str],
) -> bool:
    """Fail closed for ordinary one-word titles used as ordinary prose.

    Quest packs routinely use titles such as ``Blocks``, ``Plants``, and
    ``Storage``.  A whole, styled, quoted, or explicitly labelled occurrence
    is strong reference evidence.  Otherwise a corpus-common word and a word
    participating in ``Signs and Chants``-style coordination are left for the
    translator instead of being frozen as a name.
    """

    source = entry.source
    if not _is_project_reference(entry):
        return False
    action_shaped = _project_reference_looks_like_action_title(source)
    if _term_is_whole_visible_value(text, start, end) and not action_shaped:
        return False
    if _term_has_explicit_format_scope(parts, fragment_ranges):
        return False
    if _term_is_quoted(text, start, end):
        return False
    if _project_reference_has_label_context(text, start, end):
        return False
    if _is_project_reference_from_mod_display(entry):
        # Retain the Mod metadata evidence for ordinary branded references,
        # while the earlier imperative-object check still rejects a title such
        # as Create at the start of "Create a machine".
        return False
    if action_shaped:
        # An unchecked quest title can itself be an instruction (for example
        # "Kill The Warden"). Preserve it only when quoting, styling, or an
        # explicit book/chapter/quest label proved that the prose names the
        # title rather than repeating the objective.
        return True
    if len(_ASCII_WORD_PATTERN.findall(source)) > 1:
        # An exact quest title may also be copied verbatim as an instruction.
        # A lower-case action continuation is evidence for prose rather than a
        # reference, unless quoting/formatting/a label above proved otherwise.
        return _PROJECT_REFERENCE_ACTION_CONTINUATION.match(text[end:]) is not None
    if not _is_ordinary_shaped_ascii_word(source):
        return False
    if (
        _PROJECT_REFERENCE_COORDINATED_BEFORE.search(text[:start]) is not None
        or _PROJECT_REFERENCE_COORDINATED_AFTER.match(text[end:]) is not None
    ):
        return True
    return _canonical_ascii_word(source) in common_ascii_words


def _project_reference_looks_like_action_title(source: str) -> bool:
    words = _ASCII_WORD_PATTERN.findall(source)
    if len(words) < 2:
        return False
    first = words[0].casefold()
    return first in _ENGLISH_ACTION_TITLE_VERBS


def _project_reference_has_label_context(text: str, start: int, end: int) -> bool:
    if (
        _PROJECT_REFERENCE_LABEL_BEFORE.search(text[:start]) is not None
        or _PROJECT_REFERENCE_LABEL_AFTER.match(text[end:]) is not None
    ):
        return True
    return (
        _PROJECT_REFERENCE_QUOTED_LABEL_AFTER.match(text[end:]) is not None
        and _PROJECT_REFERENCE_QUOTED_ITEM_BEFORE.search(text[:start]) is not None
    )


def _is_ordinary_shaped_ascii_word(value: str) -> bool:
    """Recognize a potentially ambiguous word while retaining acronyms/CamelCase."""

    return (
        value.isascii()
        and value.isalpha()
        and not value.isupper()
        and (value.islower() or value.istitle())
    )


def _canonical_ascii_word(value: str) -> str:
    """Return a small corpus-only word stem without a vocabulary denylist."""

    folded = value.casefold()
    if len(folded) > 4 and folded.endswith("ies"):
        return folded[:-3] + "y"
    if (
        len(folded) > 3
        and folded.endswith("s")
        and not folded.endswith(("ss", "us", "is"))
    ):
        return folded[:-1]
    return folded


def _is_generic_group_entry(entry: GlossaryEntry) -> bool:
    folded_key = entry.key.casefold()
    return folded_key.startswith(("tag.", "key.categories."))


def _entry_key_identifies_label(entry: GlossaryEntry) -> bool:
    """Use a registry-like key as evidence that a unique word is a name."""

    if _is_mod_display_name(entry) or _is_generic_group_entry(entry):
        return False
    source_words = _ASCII_WORD_PATTERN.findall(entry.source)
    if len(source_words) != 1:
        return False
    wanted = _canonical_ascii_word(source_words[0])
    key_words = _TERM_KEY_TOKEN_SPLIT.split(entry.key.casefold())
    while key_words and key_words[-1] in {"label", "name", "title"}:
        key_words.pop()
    for width in range(1, min(4, len(key_words)) + 1):
        suffix = "".join(_canonical_ascii_word(word) for word in key_words[-width:])
        if suffix == wanted:
            return True
    return False


def _looks_like_embedded_name_fragment(
    text: str,
    start: int,
    end: int,
    entry: GlossaryEntry,
    parts: list[str],
    fragment_ranges: tuple[tuple[int, int, int], ...],
    common_ascii_words: frozenset[str],
    candidate_sources: set[str],
    entries: dict[str, GlossaryEntry],
) -> bool:
    """Reject weak terms that are only a fragment of a larger name phrase."""

    if not _term_is_contextually_weak(entry, common_ascii_words):
        return False
    if (
        _term_has_explicit_format_scope(parts, fragment_ranges)
        or _term_is_quoted(text, start, end)
    ):
        return False
    if _term_has_attached_name_suffix(text, end):
        return True
    connected_start = _connected_name_extension_start(text, end)
    if connected_start is not None:
        if _candidate_term_starts_at(
            text,
            connected_start,
            candidate_sources,
            entries,
        ):
            return False
        return True
    if _RIGHT_NAME_ATOM.match(text[end:]) is not None:
        if _is_mod_display_name(entry) and _same_mod_term_follows(
            text,
            end,
            entry,
            candidate_sources,
            entries,
        ):
            return False
        return True
    return _left_name_extension(text, start, common_ascii_words)


def _term_is_contextually_weak(
    entry: GlossaryEntry,
    common_ascii_words: frozenset[str],
) -> bool:
    words = _ASCII_WORD_PATTERN.findall(entry.source)
    if not words:
        return False
    canonical_words = tuple(_canonical_ascii_word(word) for word in words)
    if _is_mod_display_name(entry) or _is_project_reference(entry):
        return len(words) == 1 and _is_ordinary_shaped_ascii_word(entry.source)
    if _is_generic_group_entry(entry):
        return True
    if len(words) == 1:
        return (
            canonical_words[0] in common_ascii_words
            or not _entry_key_identifies_label(entry)
        )
    return all(word in common_ascii_words for word in canonical_words)


def _term_has_attached_name_suffix(text: str, end: int) -> bool:
    return end < len(text) and text[end] in {"+", "＋"}


def _connected_name_extension_start(text: str, end: int) -> int | None:
    match = _RIGHT_CONNECTED_NAME_ATOM.match(text[end:])
    return None if match is None else end + match.start("atom")


def _candidate_term_starts_at(
    text: str,
    start: int,
    candidate_sources: set[str],
    entries: dict[str, GlossaryEntry],
) -> bool:
    for source in candidate_sources:
        candidate = entries[source]
        visible = text[start : start + len(source)]
        matches = (
            visible.isascii()
            and visible.casefold() == source.casefold()
            if _is_mod_display_name(candidate) and source.isascii()
            else visible == source
        )
        end = start + len(source)
        if (
            matches
            and end <= len(text)
            and _term_has_boundaries(text, start, end, visible)
        ):
            return True
    return False


def _candidate_term_ends_before_separator(
    text: str,
    start: int,
    candidate_sources: set[str],
    entries: dict[str, GlossaryEntry],
) -> bool:
    separator = _LEFT_CONNECTED_SEPARATOR.search(text[:start])
    if separator is None:
        return False
    candidate_end = separator.start()
    while candidate_end > 0 and text[candidate_end - 1].isspace():
        candidate_end -= 1
    for source in candidate_sources:
        candidate = entries[source]
        candidate_start = candidate_end - len(source)
        if candidate_start < 0:
            continue
        visible = text[candidate_start:candidate_end]
        matches = (
            visible.isascii()
            and visible.casefold() == source.casefold()
            if _is_mod_display_name(candidate) and source.isascii()
            else visible == source
        )
        if matches and _term_has_boundaries(
            text,
            candidate_start,
            candidate_end,
            visible,
        ):
            return True
    return False


def _left_name_extension(
    text: str,
    start: int,
    common_ascii_words: frozenset[str],
) -> bool:
    match = _LEFT_NAME_ATOM.search(text[:start])
    if match is None:
        return False
    atom = match.group("atom")
    if not (atom[0].isupper() or atom.isupper()):
        return False
    atom_start = match.start("atom")
    folded = atom.casefold()
    if folded in {"a", "an", "the"}:
        return False
    if atom.endswith(("'", "’")) or folded.endswith(("'s", "’s")):
        return True
    if not _is_sentence_or_list_start(text, atom_start):
        return True
    return folded.rstrip("'’").endswith(
        ("al", "ary", "ed", "ful", "ic", "ing", "ive", "less", "ory", "ous")
    )


def _same_mod_term_follows(
    text: str,
    end: int,
    entry: GlossaryEntry,
    candidate_sources: set[str],
    entries: dict[str, GlossaryEntry],
) -> bool:
    next_start = end
    while next_start < len(text) and text[next_start].isspace():
        next_start += 1
    for source in candidate_sources:
        candidate = entries[source]
        if (
            source != entry.source
            and candidate.mod_id.casefold() == entry.mod_id.casefold()
            and text.startswith(source, next_start)
        ):
            return True
    return False


def _term_is_whole_visible_value(text: str, start: int, end: int) -> bool:
    return (
        _VISIBLE_VALUE_DECORATION.fullmatch(text[:start]) is not None
        and _VISIBLE_VALUE_DECORATION.fullmatch(text[end:]) is not None
    )


def _term_has_explicit_format_scope(
    parts: list[str],
    fragment_ranges: tuple[tuple[int, int, int], ...],
) -> bool:
    if len(fragment_ranges) > 1:
        return True
    for part_index, start, end in fragment_ranges:
        part = parts[part_index]
        has_start = any(
            match.end() == start
            for match in _FORMAT_CODE_PATTERN.finditer(part, 0, start)
        )
        has_end = _FORMAT_CODE_PATTERN.match(part, end) is not None
        if has_start and has_end:
            return True
    return False


def _term_is_quoted(text: str, start: int, end: int) -> bool:
    if start == 0 or end >= len(text):
        return False
    return (text[start - 1], text[end]) in {
        ('"', '"'),
        ("'", "'"),
        ("`", "`"),
        ("“", "”"),
        ("‘", "’"),
    }


def _is_sentence_or_list_start(text: str, start: int) -> bool:
    """Recognize a string, sentence, line, or explicit list-item start."""

    prefix = text[:start]
    boundary = max(
        (prefix.rfind(character) for character in _SENTENCE_BOUNDARIES),
        default=-1,
    )
    decoration = prefix[boundary + 1 :]
    return _SENTENCE_START_DECORATION.fullmatch(decoration) is not None


def _find_ascii_case_insensitive(text: str, term: str, start: int) -> int:
    """Find an ASCII term without case-folding the haystack or changing its indices."""

    folded_term = term.lower()
    limit = len(text) - len(term)
    first = folded_term[0]
    for index in range(start, limit + 1):
        character = text[index]
        if not character.isascii() or character.lower() != first:
            continue
        fragment = text[index : index + len(term)]
        if fragment.isascii() and fragment.lower() == folded_term:
            return index
    return -1


def _visible_projection(parts: list[str]) -> _VisibleProjection:
    characters: list[str] = []
    locations: list[tuple[int, int]] = []
    for part_index, part in enumerate(parts):
        cursor = 0
        for match in _FORMAT_CODE_PATTERN.finditer(part):
            for original_index in range(cursor, match.start()):
                characters.append(part[original_index])
                locations.append((part_index, original_index))
            cursor = match.end()
        for original_index in range(cursor, len(part)):
            characters.append(part[original_index])
            locations.append((part_index, original_index))
    return _VisibleProjection("".join(characters), tuple(locations))


def _subtract_protected_fragment_ranges(
    fragment_ranges: tuple[tuple[int, int, int], ...],
    protected_ranges: list[tuple[tuple[int, int], ...]],
) -> tuple[tuple[int, int, int], ...]:
    """Keep the literal parts of a project name around already-safe syntax."""

    result: list[tuple[int, int, int]] = []
    for part_index, start, end in fragment_ranges:
        remaining = [(start, end)]
        for protected_start, protected_end in protected_ranges[part_index]:
            next_remaining: list[tuple[int, int]] = []
            for segment_start, segment_end in remaining:
                if protected_end <= segment_start or protected_start >= segment_end:
                    next_remaining.append((segment_start, segment_end))
                    continue
                if segment_start < protected_start:
                    next_remaining.append((segment_start, protected_start))
                if protected_end < segment_end:
                    next_remaining.append((protected_end, segment_end))
            remaining = next_remaining
        result.extend(
            (part_index, segment_start, segment_end)
            for segment_start, segment_end in remaining
            if segment_start < segment_end
        )
    return tuple(result)


def visible_terminology_text(parts: Iterable[str]) -> str:
    """Return rendered literal text with Minecraft formatting codes removed."""

    return _visible_projection(list(parts)).text.strip()


def _project_reference_term_is_safe(value: str) -> bool:
    """Reject prose, control syntax, and inert values as project-name terms."""

    return (
        bool(value)
        and len(value) <= _MAX_PROJECT_REFERENCE_CHARS
        and any(character.isalnum() for character in value)
        and not any(character in "\r\n\t" or ord(character) < 0x20 for character in value)
        and all(
            kind == "special" and token == r"\&"
            for kind, token in protected_syntax_signature(value)
        )
    )


def _projection_fragment_ranges(
    projection: _VisibleProjection,
    start: int,
    end: int,
) -> tuple[tuple[int, int, int], ...]:
    locations = projection.locations[start:end]
    if not locations:
        return ()
    groups: list[tuple[int, int, int]] = []
    part_index, original_start = locations[0]
    previous = original_start
    for next_part, original_index in locations[1:]:
        if next_part == part_index and original_index == previous + 1:
            previous = original_index
            continue
        groups.append((part_index, original_start, previous + 1))
        part_index = next_part
        original_start = original_index
        previous = original_index
    groups.append((part_index, original_start, previous + 1))
    return tuple(groups)


def _term_has_boundaries(text: str, start: int, end: int, term: str) -> bool:
    def ascii_word(character: str) -> bool:
        return character.isascii() and (character.isalnum() or character == "_")

    before_ok = not term or not ascii_word(term[0]) or start == 0 or not ascii_word(text[start - 1])
    after_ok = not term or not ascii_word(term[-1]) or end == len(text) or not ascii_word(text[end])
    return before_ok and after_ok


def _count_bounded(text: str, term: str) -> int:
    count = 0
    start = 0
    while term:
        index = text.find(term, start)
        if index < 0:
            break
        end = index + len(term)
        if _term_has_boundaries(text, index, end, term):
            count += 1
        start = index + max(1, len(term))
    return count


def _merge_replacement(replacements: dict[str, str], source: str, target: str) -> None:
    if not source:
        return
    previous = replacements.get(source)
    replacements[source] = target if previous is None or previous == target else source


def _expected_match_value(match: _TermMatch) -> str:
    """Return the value a safe protection round-trip will actually restore."""

    if (
        match.entry.translated
        and match.contiguous
        and terminology_target_is_safe(match.visible, match.entry.target)
    ):
        return match.entry.target
    return match.visible


def _entries_with_regular_plural_aliases(
    entries: dict[str, GlossaryEntry],
) -> dict[str, GlossaryEntry]:
    """Add private, conservative aliases for safe registry names.

    The public catalog remains exact evidence.  Aliases exist only in the
    matcher, so coverage counts and resource-key resolution are unchanged. A
    missing or explicitly source-identical target derives a source-preserving
    plural, while a translated singular keeps the existing translated-plural
    behavior. An untranslated exact tag label is neutral (``Iron Ingots`` may
    borrow ``Iron Ingot -> 鉄インゴット``), while an explicit source spelling,
    rejected target, project title, Mod display name, or conflicting official
    translation blocks that borrowing.
    """

    result = dict(entries)
    derived_by_alias: dict[str, list[GlossaryEntry]] = {}
    for entry in entries.values():
        alias = _regular_plural_alias(entry)
        if alias is None or alias == entry.source:
            continue
        translated = entry.translated and entry.target_state == "translated"
        derived_by_alias.setdefault(alias, []).append(
            GlossaryEntry(
                source=alias,
                target=entry.target if translated else alias,
                key=entry.key,
                mod_id=entry.mod_id,
                translated=translated,
                provenance=f"{entry.provenance} [規則複数形: {entry.source}]",
                target_state="translated" if translated else entry.target_state,
                source_had_printf=entry.source_had_printf,
                source_tier=entry.source_tier,
            )
        )

    for alias, candidates in derived_by_alias.items():
        targets = {candidate.target for candidate in candidates}
        exact = entries.get(alias)
        if len(targets) != 1:
            if exact is None:
                result[alias] = GlossaryEntry(
                    source=alias,
                    target=alias,
                    key="mq_localizer.ambiguous_plural",
                    mod_id="",
                    translated=False,
                    provenance=f"{len(candidates)}件の規則複数形候補が競合",
                    target_state="rejected",
                )
            continue

        derived = min(candidates, key=_glossary_evidence_sort_key)
        if exact is None:
            result[alias] = derived
            continue
        if _is_project_reference(exact) or _is_mod_display_name(exact):
            continue
        if exact.target_state in {"explicit_source", "rejected"}:
            continue
        if exact.translated:
            translated_targets = {
                candidate.target for candidate in candidates if candidate.translated
            }
            if translated_targets and translated_targets != {exact.target}:
                result[alias] = GlossaryEntry(
                    source=alias,
                    target=alias,
                    key="mq_localizer.ambiguous_plural",
                    mod_id="",
                    translated=False,
                    provenance="完全一致の公式訳と規則複数形の公式訳が競合",
                    target_state="rejected",
                )
            continue
        # A missing target on an exact plural tag is not an instruction to
        # preserve English.  Use the unique translated singular registry name.
        if exact.target_state == "missing":
            result[alias] = derived
    return result


def _regular_plural_alias(entry: GlossaryEntry) -> str | None:
    translated = entry.translated and entry.target_state == "translated"
    source_preserved = (
        not entry.translated
        and entry.target_state in {"missing", "explicit_source"}
        and entry.target == entry.source
    )
    if (
        not (translated or source_preserved)
        or _is_mod_display_name(entry)
        or _is_project_reference(entry)
        or not entry.key.casefold().startswith(_REGISTRY_TERM_KEY_PREFIXES)
        or not entry.source.isascii()
        or protected_syntax_signature(entry.source)
    ):
        return None
    words = re.findall(r"[A-Za-z]+", entry.source)
    if len(words) < 2:
        # Single words are too likely to be verbs or ordinary prose.  Exact
        # whole-value handling can be added later with stronger context.
        return None
    match = re.search(r"(?P<word>[A-Za-z]+)\Z", entry.source)
    if match is None:
        return None
    word = match.group("word")
    lowered = word.casefold()
    if lowered.endswith("s"):
        # Avoid guessing whether an ``s``-ending word is singular (Glass) or
        # already plural (Drawers).
        return None
    if lowered.endswith("y") and len(word) > 1 and lowered[-2] not in "aeiou":
        plural_word = word[:-1] + ("IES" if word.isupper() else "ies")
    elif lowered.endswith(("ch", "sh", "x", "z")):
        plural_word = word + ("ES" if word.isupper() else "es")
    else:
        plural_word = word + ("S" if word.isupper() else "s")
    return entry.source[: match.start()] + plural_word


def _parse_resource_id(value: str) -> tuple[str, str] | None:
    match = _RESOURCE_ID.fullmatch(value)
    if match is None:
        return None
    namespace = match.group("namespace")
    path = match.group("path")
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        return None
    return namespace, path


def _resource_name_signature(
    value: str,
) -> tuple[tuple[str, int], ...] | None:
    """Return order-independent visible content for a resource-scoped alias.

    Reordered aliases are deliberately limited to ASCII letters, digits,
    whitespace, and balanced round parentheses. Digits remain part of each
    token, while non-ASCII text and all other punctuation fail closed. Exact
    names outside this grammar can still use the normal global exact match.
    """

    if _RESOURCE_ALIAS_SAFE_NAME.fullmatch(value) is None:
        return None
    depth = 0
    for character in value:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return None
    if depth:
        return None
    words = [word.casefold() for word in _RESOURCE_ALIAS_TOKEN.findall(value)]
    if not words:
        return None
    return tuple(sorted(Counter(words).items()))


def _resource_related_words(value: str) -> frozenset[str]:
    """Return conservative lexical overlap used only to disable a wrong match."""

    return frozenset(
        word.casefold()
        for word in re.findall(r"[^\W_]+", value, re.UNICODE)
    )


def _resolve_resource_language_entries(
    entries: Iterable[GlossaryEntry],
) -> tuple[GlossaryEntry, ...]:
    """Resolve exact language-key evidence while retaining source revisions."""

    by_source: dict[str, list[GlossaryEntry]] = {}
    for entry in entries:
        by_source.setdefault(entry.source, []).append(entry)
    resolved: list[GlossaryEntry] = []
    for source_entries in by_source.values():
        ordered = sorted(source_entries, key=_glossary_evidence_sort_key)
        targets = {
            entry.target
            for entry in ordered
            if entry.target_state == "translated" and entry.translated
        }
        blocked = any(
            entry.target_state in {"explicit_source", "rejected"}
            for entry in ordered
        )
        if len(targets) == 1 and not blocked:
            target = next(iter(targets))
            resolved.append(
                next(
                    entry
                    for entry in ordered
                    if entry.target_state == "translated" and entry.target == target
                )
            )
        elif not targets:
            resolved.append(ordered[0])
        # Multiple targets, or an explicit source mixed with a translation,
        # are deliberately omitted so the scoped alias fails closed.
    return tuple(resolved)


def _glossary_evidence_sort_key(entry: GlossaryEntry) -> tuple[str, ...]:
    return (
        f"{_SOURCE_TIER_PRIORITY.get(entry.source_tier, 99):02d}",
        entry.mod_id.casefold(),
        entry.key.casefold(),
        entry.target,
        entry.target_state,
        entry.provenance,
    )


def _prioritized_glossary_evidence(
    evidence_by_source: dict[str, list[GlossaryEntry]],
    cancel: Event | None = None,
) -> dict[str, list[GlossaryEntry]]:
    """Apply Minecraft > Mod > KubeJS > resource-pack identity authority.

    The highest tier owns the source label for a ``(namespace, key)`` identity.
    A lower tier can supply a target only for the exact same source spelling,
    and only while every higher tier reports ``missing``. An explicit source,
    rejected target, or translation at a tier stops fallback. All evidence at
    the selected tier is retained so same-tier disagreement remains fail-closed.
    """

    passthrough: list[GlossaryEntry] = []
    by_identity: dict[tuple[str, str], list[GlossaryEntry]] = {}
    for source_index, entries in enumerate(evidence_by_source.values()):
        if source_index % 1024 == 0:
            _raise_if_cancelled(cancel)
        for entry in entries:
            if _is_mod_display_name(entry) or _is_project_reference(entry):
                passthrough.append(entry)
                continue
            by_identity.setdefault(
                (entry.mod_id.casefold(), entry.key.casefold()),
                [],
            ).append(entry)

    retained: list[GlossaryEntry] = list(passthrough)
    for identity_index, identity_entries in enumerate(by_identity.values()):
        if identity_index % 1024 == 0:
            _raise_if_cancelled(cancel)
        best_priority = min(
            _SOURCE_TIER_PRIORITY.get(entry.source_tier, 99)
            for entry in identity_entries
        )
        authoritative_sources = {
            entry.source
            for entry in identity_entries
            if _SOURCE_TIER_PRIORITY.get(entry.source_tier, 99) == best_priority
        }
        for source in sorted(authoritative_sources):
            exact_source_entries = [
                entry for entry in identity_entries if entry.source == source
            ]
            priorities = sorted(
                {
                    _SOURCE_TIER_PRIORITY.get(entry.source_tier, 99)
                    for entry in exact_source_entries
                }
            )
            for priority in priorities:
                tier_entries = [
                    entry
                    for entry in exact_source_entries
                    if _SOURCE_TIER_PRIORITY.get(entry.source_tier, 99) == priority
                ]
                retained.extend(tier_entries)
                if any(entry.target_state != "missing" for entry in tier_entries):
                    break

    prioritized: dict[str, list[GlossaryEntry]] = {}
    for entry in sorted(retained, key=_glossary_evidence_sort_key):
        prioritized.setdefault(entry.source, []).append(entry)
    _raise_if_cancelled(cancel)
    return prioritized


def _resolve_glossary_evidence(
    catalog: GlossaryCatalog,
    evidence_by_source: dict[str, list[GlossaryEntry]],
    cancel: Event | None = None,
) -> None:
    """Resolve all language evidence deterministically after archive scanning.

    A missing target key is neutral only inside the same ``(mod_id, key)``
    identity. It must never borrow a translation from another Mod or another
    registry key merely because their English labels happen to be equal. If
    several identities share one source label, only identities whose source
    comes from the highest available tier participate in the global result.
    Lower-tier evidence remains in ``catalog.evidence`` for resource-scoped
    matching, but cannot cancel a higher-tier global translation.
    """

    evidence_by_source = _prioritized_glossary_evidence(
        evidence_by_source,
        cancel,
    )
    catalog.entries.clear()
    catalog.conflicts.clear()
    catalog.evidence.clear()
    for source_index, source in enumerate(sorted(evidence_by_source)):
        if source_index % 1024 == 0:
            _raise_if_cancelled(cancel)
        ordered = tuple(
            sorted(evidence_by_source[source], key=_glossary_evidence_sort_key)
        )
        catalog.evidence[source] = ordered

        display_names = [entry for entry in ordered if _is_mod_display_name(entry)]
        if display_names:
            # Product metadata is an exact name and therefore deliberately
            # wins over a localized item-group label with the same spelling.
            catalog.entries[source] = display_names[0]
            continue

        by_identity: dict[tuple[str, str], list[GlossaryEntry]] = {}
        for entry in ordered:
            by_identity.setdefault(
                (entry.mod_id.casefold(), entry.key.casefold()),
                [],
            ).append(entry)

        resolved_identities: list[
            tuple[
                str,
                GlossaryEntry,
                int,
                bool,
                tuple[GlossaryEntry, ...],
            ]
        ] = []
        for identity_entries in by_identity.values():
            identity_priority = min(
                _SOURCE_TIER_PRIORITY.get(entry.source_tier, 99)
                for entry in identity_entries
            )
            identity_is_ambiguous = False
            translated_targets = {
                entry.target
                for entry in identity_entries
                if entry.target_state == "translated" and entry.translated
            }
            blocks_borrowing = any(
                entry.target_state in {"explicit_source", "rejected"}
                for entry in identity_entries
            )
            if len(translated_targets) == 1 and not blocks_borrowing:
                value = next(iter(translated_targets))
                representative = next(
                    entry
                    for entry in identity_entries
                    if entry.target_state == "translated" and entry.target == value
                )
            elif not translated_targets:
                value = source
                state_priority = {
                    "explicit_source": 0,
                    "rejected": 1,
                    "missing": 2,
                    "translated": 3,
                    "auto": 4,
                }
                representative = min(
                    identity_entries,
                    key=lambda entry: (
                        state_priority[entry.target_state],
                        _glossary_evidence_sort_key(entry),
                    ),
                )
            else:
                value = source
                representative = identity_entries[0]
                identity_is_ambiguous = True
            resolved_identities.append(
                (
                    value,
                    representative,
                    identity_priority,
                    identity_is_ambiguous,
                    tuple(identity_entries),
                )
            )

        best_identity_priority = min(
            priority
            for _value, _entry, priority, _ambiguous, _entries
            in resolved_identities
        )
        selected_identities = [
            resolved
            for resolved in resolved_identities
            if resolved[2] == best_identity_priority
        ]
        resolved_values = {
            value
            for value, _entry, _priority, _ambiguous, _entries
            in selected_identities
        }
        selected_is_ambiguous = any(
            ambiguous
            for _value, _entry, _priority, ambiguous, _entries
            in selected_identities
        )
        if selected_is_ambiguous or len(resolved_values) != 1:
            conflicting_entries = sorted(
                (
                    entry
                    for _value, _representative, _priority, _ambiguous, entries
                    in selected_identities
                    for entry in entries
                ),
                key=_glossary_evidence_sort_key,
            )
            catalog.conflicts[source] = conflicting_entries
            catalog.entries[source] = GlossaryEntry(
                source=source,
                target=source,
                key="mq_localizer.ambiguous",
                mod_id="",
                translated=False,
                provenance=f"{len(conflicting_entries)}件の同順位公式言語候補が競合",
                target_state="rejected",
                source_tier=min(
                    conflicting_entries,
                    key=_glossary_evidence_sort_key,
                ).source_tier,
            )
            continue

        resolved_value = next(iter(resolved_values))
        representatives = [
            entry
            for value, entry, _priority, _ambiguous, _entries in selected_identities
            if value == resolved_value
        ]
        if resolved_value != source:
            representatives = [
                entry for entry in representatives if entry.translated
            ] or representatives
        catalog.entries[source] = min(
            representatives,
            key=_glossary_evidence_sort_key,
        )


def _minecraft_language_entries(
    bundle: MinecraftLanguageBundle,
    cancel: Event | None,
    warnings: list[str],
    target_locale: str,
    pending_warnings: list[_PendingTerminologyWarning] | None = None,
) -> list[GlossaryEntry]:
    """Convert version-matched vanilla assets into ordinary glossary evidence."""

    if not bundle.source_values or not bundle.source_provenance:
        return []
    source_values = bundle.source_values
    target_values = bundle.target_values
    source_keys = frozenset(key.casefold() for key in source_values)
    target_keys = frozenset(key.casefold() for key in target_values)
    independent_fixed_targets = _independent_fixed_target_labels(
        target_values.values(),
        cancel,
    )
    source_labels = {
        key.casefold(): _strip_display_formatting(value).strip()
        for key, value in source_values.items()
    }
    entries: list[GlossaryEntry] = []
    provenance_cache: dict[tuple[bool, str], str] = {}
    target_warning_location = _safe_warning_location(bundle.target_provenance)
    for index, (key, raw_source) in enumerate(source_values.items()):
        if index % 1024 == 0:
            _raise_if_cancelled(cancel)
        source_label = _fixed_terminology_source_label(
            key,
            raw_source,
            source_keys,
            source_labels,
            frozenset(),
        )
        if source_label is None:
            continue
        source, source_had_printf, _source_literal_fragments = source_label

        raw_target = target_values.get(key, "")
        target_known = key.casefold() in target_keys
        target_label, target_derived_from_possessive = _fixed_target_terminology_label(
            raw_target,
            target_locale,
            source_had_printf=source_had_printf,
            independent_fixed_targets=independent_fixed_targets,
        )
        target = "" if target_label is None else target_label[0]
        target_had_printf = bool(target_label and target_label[1])
        translated = bool(target and target != source)
        rejection = ""
        if target_known and raw_target and target_label is None:
            rejection = "固定名から安全に除去できない保護tokenを含みます"
        elif (
            target_had_printf
            and not source_had_printf
            and not target_derived_from_possessive
        ):
            rejection = "原文にないprintf引数へ依存します"
        elif target_had_printf and target_label is not None and target_label[2] != 1:
            rejection = "printf引数の前後に固定文字列が分かれています"
        elif (
            target_had_printf
            and not target_derived_from_possessive
            and _starts_with_dependent_japanese_particle(target)
        ):
            rejection = "printf引数に依存する日本語の助詞から始まります"
        elif translated and not terminology_target_is_safe(source, target):
            rejection = "原文と異なる保護token、制御文字、または不可視文字を含みます"
        if rejection:
            target = source
            translated = False

        if not target_known:
            target_state = "missing"
        elif not target or rejection:
            target_state = "rejected"
        elif translated:
            target_state = "translated"
        else:
            target_state = "explicit_source"
        provenance_key = (target_known, target_state)
        provenance = provenance_cache.get(provenance_key)
        if provenance is None:
            provenance = f"Minecraft公式: {bundle.source_provenance}"
            if target_known:
                provenance += f" -> {bundle.target_provenance} [{target_state}]"
            provenance_cache[provenance_key] = provenance
        entry = GlossaryEntry(
            source=source,
            target=target if translated else source,
            key=key,
            mod_id="minecraft",
            translated=translated,
            provenance=provenance,
            target_state=target_state,
            source_had_printf=source_had_printf,
            source_tier="minecraft",
        )
        entries.append(entry)
        if rejection:
            message = (
                f"{target_warning_location}: {_safe_warning_key(key)} "
                f"のMinecraft公式訳は{rejection}。"
                f"原語 {_short_value(source)!r} を保持します"
            )
            if pending_warnings is None:
                warnings.append(message)
            else:
                pending_warnings.append(
                    _PendingTerminologyWarning(
                        evidence=entry,
                        message=message,
                        container_label=bundle.target_provenance,
                    )
                )
    _raise_if_cancelled(cancel)
    return entries


class ModLanguageScanner:
    """Builds exact terminology pairs from metadata and language files in mod JARs."""

    def scan(
        self,
        location: Path | None,
        source_locale: str,
        target_locale: str,
        cancel: Event | None = None,
        progress: GlossaryProgressCallback | None = None,
        *,
        minecraft_version: str = "",
        instance_root: Path | None = None,
        game_root: Path | None = None,
        include_resourcepacks: bool = False,
        limits: GlossaryScanLimits | None = None,
    ) -> GlossaryCatalog:
        """Scan Mod archives and trusted instance language assets read-only.

        ``progress`` receives a :class:`GlossaryScanProgress` immediately
        before and after every attempted Mod archive or additional asset
        source. Exceptions raised by the callback are propagated and abort the
        scan; they are not converted to source warnings.

        ``instance_root`` remains the launcher root used to locate Minecraft's
        official assets. ``game_root`` is the selected instance game directory
        containing ``mods``, ``kubejs`` and ``resourcepacks``. KubeJS assets
        are always considered; resource packs require ``include_resourcepacks``.
        Identity authority is deterministic: Minecraft, Mod JAR, KubeJS, then
        resource pack. A lower tier supplies a translation only when every
        higher tier is missing that target and uses the exact same source name.
        """

        catalog = GlossaryCatalog(resourcepacks_enabled=include_resourcepacks)
        effective_limits = limits or GlossaryScanLimits()
        if limits is None:
            # Keep the historical module constants as the default source of
            # truth.  Besides preserving compatibility for callers that have
            # patched these safety constants, this makes the configured path
            # an explicit opt-in while retaining the exact prior defaults.
            source_member_limit = _MAX_ARCHIVE_MEMBERS
            language_file_limit = _MAX_LANGUAGE_MEMBER_BYTES
            source_language_limit = _MAX_ARCHIVE_LANGUAGE_BYTES
            total_language_limit = _MAX_SCAN_LANGUAGE_BYTES
            compressed_language_file_limit = _MAX_LANGUAGE_COMPRESSED_MEMBER_BYTES
        else:
            source_member_limit = limits.effective_max_source_members
            language_file_limit = limits.effective_max_language_file_bytes
            source_language_limit = limits.effective_max_source_language_bytes
            total_language_limit = limits.effective_max_total_language_bytes
            # A compressed language member is bounded by the same configured
            # per-file value as its expanded content.
            compressed_language_file_limit = limits.effective_max_language_file_bytes
        input_recorder = GlossaryInputRecorder(
            cancel,
            scan_limits=effective_limits,
        )
        _raise_if_cancelled(cancel)
        has_mod_location = location is not None and bool(str(location))
        jars = (
            list(
                _find_archives(
                    location,
                    source_member_limit,
                    cancel,
                )
            )
            if has_mod_location
            else []
        )
        input_recorder.capture_mod_inputs(
            location if has_mod_location else None,
            tuple(jars),
        )
        catalog.discovered_archives = len(jars)
        evidence_by_source: dict[str, list[GlossaryEntry]] = {}
        target_only_evidence: list[_TargetOnlyLanguageEvidence] = []
        pending_warnings: list[_PendingTerminologyWarning] = []
        mod_warned_archives: set[str] = set()
        external_warned_sources: set[str] = set()
        if has_mod_location and not jars:
            assert location is not None
            catalog.warnings.append(
                f"Mod JARが見つかりませんでした: {location.expanduser()}。"
                "この場所からはMod名・公式用語を保護できません。インスタンスルート直下の"
                "modsフォルダーを確認してください"
            )
            _raise_if_cancelled(cancel)

        effective_game_root = game_root
        if effective_game_root is None and has_mod_location:
            assert location is not None
            effective_game_root = _infer_game_root_from_mod_location(location)
        shared_budget = _SharedScanBudget(
            remaining_language_bytes=total_language_limit,
            language_byte_limit=total_language_limit,
            source_member_limit=source_member_limit,
            language_file_limit=language_file_limit,
            source_language_limit=source_language_limit,
            compressed_language_file_limit=compressed_language_file_limit,
            scan_limits=effective_limits,
        )
        input_recorder.capture_external_inputs(
            effective_game_root,
            include_resourcepacks,
        )
        external_discovery = _discover_external_asset_sources(
            effective_game_root,
            include_resourcepacks,
            cancel,
            maximum_source_members=source_member_limit,
        )
        catalog.external_sources_discovered = external_discovery.discovered_sources
        catalog.external_sources_failed = external_discovery.failed_sources
        catalog.external_asset_warning_count = len(external_discovery.warnings)
        catalog.warnings.extend(external_discovery.warnings)

        total_archives = len(jars)
        total_sources = total_archives + len(external_discovery.sources)
        scan_budget_exhausted = False
        for current_archive, jar_path in enumerate(jars, start=1):
            _raise_if_cancelled(cancel)
            _notify_scan_progress(
                progress,
                GlossaryScanProgress(
                    current=current_archive,
                    total=total_sources,
                    archive_name=jar_path.name,
                    phase="before",
                ),
            )
            _raise_if_cancelled(cancel)
            stop_after_archive = False
            try:
                archive_result = self._read_archive(
                    jar_path,
                    source_locale,
                    target_locale,
                    cancel,
                    shared_budget,
                )
                if len(archive_result) == 3:
                    # Backward-compatible scanner subclasses may still return
                    # the historical triple.
                    discovered, archive_warnings, _language_bytes = archive_result
                    archive_target_only: list[_TargetOnlyLanguageEvidence] = []
                    archive_pending_warnings: list[_PendingTerminologyWarning] = []
                elif len(archive_result) == 4:
                    (
                        discovered,
                        archive_warnings,
                        _language_bytes,
                        archive_target_only,
                    ) = archive_result
                    archive_pending_warnings = []
                elif len(archive_result) == 5:
                    (
                        discovered,
                        archive_warnings,
                        _language_bytes,
                        archive_target_only,
                        archive_pending_warnings,
                    ) = archive_result
                else:
                    raise ValueError("走査結果の項目数が不正です")
            except _ScanLanguageBudgetExceeded as exc:
                catalog.failed_archives += 1
                catalog.warnings.append(
                    f"{jar_path.name}: {exc}。安全上限に達したため、"
                    "このJARと以降の未走査JARの固有名詞は保護対象に追加していません"
                )
                _raise_if_cancelled(cancel)
                stop_after_archive = True
            except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
                catalog.failed_archives += 1
                catalog.warnings.append(
                    f"{jar_path.name}: JARを読めませんでした ({exc})。"
                    "このJARのMod名・公式用語だけ保護対象に追加していません"
                )
            else:
                _raise_if_cancelled(cancel)
                catalog.scanned_archives += 1
                if archive_warnings:
                    mod_warned_archives.add(jar_path.name)
                    catalog.partial_warning_count += len(archive_warnings)
                catalog.warnings.extend(archive_warnings)
                target_only_evidence.extend(archive_target_only)
                pending_warnings.extend(archive_pending_warnings)
                for entry_index, entry in enumerate(discovered):
                    if entry_index % 1024 == 0:
                        _raise_if_cancelled(cancel)
                    evidence_by_source.setdefault(entry.source, []).append(entry)
            _raise_if_cancelled(cancel)
            _notify_scan_progress(
                progress,
                GlossaryScanProgress(
                    current=current_archive,
                    total=total_sources,
                    archive_name=jar_path.name,
                    phase="after",
                ),
            )
            _raise_if_cancelled(cancel)
            if stop_after_archive:
                scan_budget_exhausted = True
                break
        _raise_if_cancelled(cancel)
        if scan_budget_exhausted:
            catalog.external_sources_skipped = len(external_discovery.sources)
        else:
            for external_index, source in enumerate(
                external_discovery.sources,
                start=1,
            ):
                _raise_if_cancelled(cancel)
                progress_index = total_archives + external_index
                _notify_scan_progress(
                    progress,
                    GlossaryScanProgress(
                        current=progress_index,
                        total=total_sources,
                        archive_name=source.label,
                        phase="before",
                        source_kind=source.source_kind,
                    ),
                )
                _raise_if_cancelled(cancel)
                stop_after_source = False
                try:
                    (
                        discovered,
                        source_warnings,
                        _language_bytes,
                        source_target_only,
                        source_pending_warnings,
                    ) = (
                        self._read_external_asset_source(
                            source,
                            source_locale,
                            target_locale,
                            cancel,
                            shared_budget,
                        )
                    )
                except _ScanLanguageBudgetExceeded as exc:
                    catalog.external_sources_failed += 1
                    catalog.external_asset_warning_count += 1
                    catalog.warnings.append(
                        f"{source.label}: {exc}。安全上限に達したため、この言語資産と"
                        "以降の追加言語資産の固有名詞は保護対象に追加していません"
                    )
                    stop_after_source = True
                except (
                    OSError,
                    UnicodeError,
                    zipfile.BadZipFile,
                    RuntimeError,
                    ValueError,
                ) as exc:
                    catalog.external_sources_failed += 1
                    catalog.external_asset_warning_count += 1
                    catalog.warnings.append(
                        f"{source.label}: 追加言語資産を安全に読めませんでした ({exc})。"
                        "この言語資産の公式用語だけ保護対象に追加していません"
                    )
                else:
                    _raise_if_cancelled(cancel)
                    catalog.external_sources_scanned += 1
                    if source.source_kind == "kubejs":
                        catalog.kubejs_sources_scanned += 1
                    else:
                        catalog.resourcepack_sources_scanned += 1
                    if source_warnings:
                        external_warned_sources.add(source.label)
                        catalog.external_asset_warning_count += len(source_warnings)
                    catalog.warnings.extend(source_warnings)
                    target_only_evidence.extend(source_target_only)
                    pending_warnings.extend(source_pending_warnings)
                    for entry_index, entry in enumerate(discovered):
                        if entry_index % 1024 == 0:
                            _raise_if_cancelled(cancel)
                        evidence_by_source.setdefault(entry.source, []).append(entry)
                _raise_if_cancelled(cancel)
                _notify_scan_progress(
                    progress,
                    GlossaryScanProgress(
                        current=progress_index,
                        total=total_sources,
                        archive_name=source.label,
                        phase="after",
                        source_kind=source.source_kind,
                    ),
                )
                _raise_if_cancelled(cancel)
                if stop_after_source:
                    catalog.external_sources_skipped += (
                        len(external_discovery.sources) - external_index
                    )
                    break
        _raise_if_cancelled(cancel)
        if minecraft_version and instance_root is not None:
            bundle = load_minecraft_language_bundle(
                instance_root,
                minecraft_version,
                source_locale,
                target_locale,
                asset_observer=input_recorder.observe_minecraft_path,
            )
            _raise_if_cancelled(cancel)
            if bundle is not None:
                catalog.minecraft_asset_warning_count += len(bundle.warnings)
                catalog.warnings.extend(bundle.warnings)
                for entry in _minecraft_language_entries(
                    bundle,
                    cancel,
                    catalog.warnings,
                    target_locale,
                    pending_warnings,
                ):
                    evidence_by_source.setdefault(entry.source, []).append(entry)
            _raise_if_cancelled(cancel)
        pending_warnings.extend(
            _merge_target_only_language_evidence(
                evidence_by_source,
                target_only_evidence,
                target_locale,
                cancel,
            )
        )

        # Rejected lower-tier candidates that cannot affect the resolved
        # glossary are discarded by the authority rules. Emit a warning only
        # for retained evidence, where the unsafe target is an actual reason
        # that the official term remains untranslated or conflicted.
        prioritized_evidence = _prioritized_glossary_evidence(
            evidence_by_source,
            cancel,
        )
        retained_evidence_ids = {
            id(entry)
            for entries in prioritized_evidence.values()
            for entry in entries
        }
        for pending in pending_warnings:
            _raise_if_cancelled(cancel)
            if id(pending.evidence) not in retained_evidence_ids:
                continue
            catalog.warnings.append(pending.message)
            if pending.evidence.source_tier == "minecraft":
                catalog.minecraft_asset_warning_count += 1
            elif pending.evidence.source_tier == "mod":
                catalog.partial_warning_count += 1
                mod_warned_archives.add(pending.container_label)
            elif pending.evidence.source_tier in {"kubejs", "resourcepack"}:
                catalog.external_asset_warning_count += 1
                external_warned_sources.add(pending.container_label)
        catalog.archives_with_warnings = len(mod_warned_archives)
        catalog.external_sources_with_warnings = len(external_warned_sources)
        _resolve_glossary_evidence(catalog, prioritized_evidence, cancel)
        _raise_if_cancelled(cancel)
        catalog.input_snapshot = input_recorder.freeze()
        return catalog

    def _read_external_asset_source(
        self,
        source: _ExternalAssetSource,
        source_locale: str,
        target_locale: str,
        cancel: Event | None = None,
        scan_language_budget: int | _SharedScanBudget | None = None,
    ) -> tuple[
        list[GlossaryEntry],
        list[str],
        int,
        list[_TargetOnlyLanguageEvidence],
        list[_PendingTerminologyWarning],
    ]:
        if source.storage == "directory":
            return _read_directory_asset_source(
                source,
                source_locale,
                target_locale,
                cancel,
                scan_language_budget,
            )
        return _read_zip_asset_source(
            source,
            source_locale,
            target_locale,
            cancel,
            scan_language_budget,
        )

    def _read_archive(
        self,
        jar_path: Path,
        source_locale: str,
        target_locale: str,
        cancel: Event | None = None,
        scan_language_budget: int | _SharedScanBudget | None = None,
    ) -> tuple[
        list[GlossaryEntry],
        list[str],
        int,
        list[_TargetOnlyLanguageEvidence],
        list[_PendingTerminologyWarning],
    ]:
        source_member_limit = (
            scan_language_budget.source_member_limit
            if isinstance(scan_language_budget, _SharedScanBudget)
            else _MAX_ARCHIVE_MEMBERS
        )
        language_file_limit = (
            scan_language_budget.language_file_limit
            if isinstance(scan_language_budget, _SharedScanBudget)
            else _MAX_LANGUAGE_MEMBER_BYTES
        )
        source_language_limit = (
            scan_language_budget.source_language_limit
            if isinstance(scan_language_budget, _SharedScanBudget)
            else _MAX_ARCHIVE_LANGUAGE_BYTES
        )
        total_language_limit = (
            scan_language_budget.language_byte_limit
            if isinstance(scan_language_budget, _SharedScanBudget)
            else _MAX_SCAN_LANGUAGE_BYTES
        )
        compressed_language_file_limit = (
            scan_language_budget.compressed_language_file_limit
            if isinstance(scan_language_budget, _SharedScanBudget)
            else _MAX_LANGUAGE_COMPRESSED_MEMBER_BYTES
        )
        with zipfile.ZipFile(jar_path) as archive:
            archive_infos = archive.infolist()
            if (
                source_member_limit is not None
                and len(archive_infos) > source_member_limit
            ):
                raise ValueError(
                    "JAR内のファイル・ディレクトリ項目数が上限を超えています "
                    f"({len(archive_infos)} > {source_member_limit})"
                )
            _raise_if_cancelled(cancel)
            names: dict[tuple[str, str, str], str] = {}
            archive_names: dict[str, str] = {}
            for info in archive_infos:
                _raise_if_cancelled(cancel)
                name = info.filename
                normalized = name.replace("\\", "/")
                archive_names.setdefault(normalized.casefold(), name)
                match = _LANG_PATH.match(normalized)
                if match:
                    mod_id, locale, extension = match.groups()
                    names[(mod_id, _normalize_locale(locale), extension.lower())] = name

            entries, warnings = _read_mod_display_names(
                archive,
                archive_names,
                jar_path.name,
                source_locale,
                cancel,
            )
            resource_backed_term_keys = _resource_backed_terminology_keys(archive_names)
            normalized_source_locale = _normalize_locale(source_locale)
            normalized_target_locale = _normalize_locale(target_locale)
            source_mod_ids = {
                mod_id
                for mod_id, locale, _extension in names
                if locale == normalized_source_locale
            }
            target_mod_ids = {
                mod_id
                for mod_id, locale, _extension in names
                if locale == normalized_target_locale
            }
            selected_language_names: set[str] = set()
            for mod_id in source_mod_ids | target_mod_ids:
                _raise_if_cancelled(cancel)
                source_name = _locale_name(names, mod_id, source_locale)
                target_name = _locale_name(names, mod_id, target_locale)
                if source_name:
                    selected_language_names.add(source_name)
                if target_name:
                    selected_language_names.add(target_name)
            readable_declared_bytes = 0
            for language_name in selected_language_names:
                _raise_if_cancelled(cancel)
                info = archive.getinfo(language_name)
                if (
                    info.file_size >= 0
                    and (
                        language_file_limit is None
                        or info.file_size <= language_file_limit
                    )
                ):
                    readable_declared_bytes += info.file_size
            if (
                source_language_limit is not None
                and readable_declared_bytes > source_language_limit
            ):
                warnings.append(
                    f"{jar_path.name}: 対象言語ファイルの合計がJAR単位の上限を超えるため"
                    f"言語用語をスキップしました ({readable_declared_bytes} > {source_language_limit} bytes)"
                )
                return entries, warnings, 0, [], []
            if isinstance(scan_language_budget, _SharedScanBudget):
                scan_language_budget.reserve_language_bytes(readable_declared_bytes)
                available_scan_budget = (
                    readable_declared_bytes
                    if scan_language_budget.language_byte_limit is not None
                    else None
                )
            else:
                available_scan_budget = (
                    total_language_limit
                    if scan_language_budget is None
                    else max(0, scan_language_budget)
                )
                if readable_declared_bytes > available_scan_budget:
                    raise _ScanLanguageBudgetExceeded(
                        f"対象言語ファイルの全走査上限 {total_language_limit} bytes に達しました"
                    )
            language_bytes_read = 0
            language_cache: dict[str, _ParsedLanguage] = {}
            language_errors: dict[str, Exception] = {}

            def read_language_values(name: str) -> _ParsedLanguage:
                nonlocal language_bytes_read
                cached = language_cache.get(name)
                if cached is not None:
                    return cached
                cached_error = language_errors.get(name)
                if cached_error is not None:
                    raise cached_error
                _raise_if_cancelled(cancel)
                remaining = _minimum_enabled_budget(
                    (
                        None
                        if source_language_limit is None
                        else source_language_limit - language_bytes_read
                    ),
                    (
                        None
                        if available_scan_budget is None
                        else available_scan_budget - language_bytes_read
                    ),
                )
                try:
                    if (
                        language_file_limit
                        == _DEFAULT_SCAN_LIMITS.max_language_file_bytes
                        and compressed_language_file_limit
                        == _DEFAULT_SCAN_LIMITS.max_language_file_bytes
                    ):
                        data = _read_language_archive_member(archive, name, remaining)
                    else:
                        data = _read_language_archive_member(
                            archive,
                            name,
                            remaining,
                            language_file_limit=language_file_limit,
                            compressed_language_file_limit=compressed_language_file_limit,
                        )
                    language_bytes_read += len(data)
                    _raise_if_cancelled(cancel)
                    parsed = _read_lang_bytes(data, Path(name).suffix)
                except (
                    OSError,
                    UnicodeError,
                    zipfile.BadZipFile,
                    RuntimeError,
                    ValueError,
                ) as exc:
                    language_errors[name] = exc
                    raise
                if parsed.duplicate_keys:
                    warnings.append(
                        _duplicate_language_key_warning(
                            jar_path.name,
                            name,
                            parsed.duplicate_keys,
                        )
                    )
                language_cache[name] = parsed
                return parsed

            pending_warnings: list[_PendingTerminologyWarning] = []
            entries.extend(
                _language_pair_entries(
                    names,
                    resource_backed_term_keys,
                    jar_path.name,
                    source_locale,
                    target_locale,
                    read_language_values,
                    warnings,
                    cancel,
                    pending_warnings=pending_warnings,
                )
            )
            target_only = _collect_target_only_language_evidence(
                names,
                source_mod_ids,
                target_mod_ids,
                source_locale,
                target_locale,
                read_language_values,
                warnings,
                jar_path.name,
                "mod",
                cancel,
            )
            _raise_if_cancelled(cancel)
            return (
                entries,
                warnings,
                language_bytes_read,
                target_only,
                pending_warnings,
            )


def _language_pair_entries(
    names: dict[tuple[str, str, str], str],
    resource_backed_term_keys: frozenset[str],
    container_label: str,
    source_locale: str,
    target_locale: str,
    read_language_values: Callable[[str], _ParsedLanguage],
    warnings: list[str],
    cancel: Event | None = None,
    *,
    source_tier: _SourceTier = "mod",
    pending_warnings: list[_PendingTerminologyWarning] | None = None,
) -> list[GlossaryEntry]:
    """Apply one terminology policy to Mod, KubeJS and resource-pack assets."""

    entries: list[GlossaryEntry] = []
    normalized_source_locale = _normalize_locale(source_locale)
    mod_ids = {
        mod_id
        for mod_id, locale, _extension in names
        if locale == normalized_source_locale
    }
    for mod_id in sorted(mod_ids):
        _raise_if_cancelled(cancel)
        source_name = _locale_name(names, mod_id, source_locale)
        if not source_name:
            continue
        target_name = _locale_name(names, mod_id, target_locale)
        provenance_cache: dict[tuple[bool, str], str] = {}
        target_warning_location = _safe_warning_location(
            f"{container_label}!/{target_name}"
        )
        try:
            source_language = read_language_values(source_name)
        except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
            warnings.append(
                f"{container_label}!/{source_name}: 原文言語ファイルを読めませんでした ({exc})。"
                "この言語ファイルの用語だけ保護対象に追加していません"
            )
            continue
        source_values = source_language.values
        source_keys = source_language.keys
        source_labels = {
            key.casefold(): _strip_display_formatting(value).strip()
            for key, value in source_values.items()
        }
        _raise_if_cancelled(cancel)
        target_language: _ParsedLanguage | None = None
        target_read_failed = False
        if target_name:
            try:
                target_language = read_language_values(target_name)
            except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
                target_read_failed = True
                warnings.append(
                    f"{container_label}!/{target_name}: 翻訳先言語ファイルを読めないため、"
                    f"このファイルの用語は公式訳へ置換せず原文を保持します ({exc})"
                )
        independent_fixed_targets = _independent_fixed_target_labels(
            () if target_language is None else target_language.values.values(),
            cancel,
        )
        _raise_if_cancelled(cancel)
        for entry_index, (key, raw_source) in enumerate(source_values.items()):
            if entry_index % 1024 == 0:
                _raise_if_cancelled(cancel)
            source_label = _fixed_terminology_source_label(
                key,
                raw_source,
                source_keys,
                source_labels,
                resource_backed_term_keys,
            )
            if source_label is None:
                continue
            source, source_had_printf, _source_literal_fragments = source_label
            target_values = {} if target_language is None else target_language.values
            raw_target = target_values.get(key, "")
            target_key_known = (
                target_language is not None
                and key.casefold() in target_language.keys
            )
            target_label, target_derived_from_possessive = (
                _fixed_target_terminology_label(
                    raw_target,
                    target_locale,
                    source_had_printf=source_had_printf,
                    independent_fixed_targets=independent_fixed_targets,
                )
            )
            target = "" if target_label is None else target_label[0]
            target_had_printf = bool(target_label and target_label[1])
            translated = bool(target and target != source)
            target_rejected_reason = ""
            if target_key_known and raw_target and target_label is None:
                target_rejected_reason = "固定名から安全に除去できない保護tokenを含みます"
            elif (
                target_had_printf
                and not source_had_printf
                and not target_derived_from_possessive
            ):
                target_rejected_reason = (
                    "原文にないprintf引数へ依存するため、固定した公式名として利用できません"
                )
            elif target_had_printf and target_label is not None and target_label[2] != 1:
                target_rejected_reason = (
                    "printf引数の前後に固定文字列が分かれ、単独の固定名として利用できません"
                )
            elif (
                target_had_printf
                and not target_derived_from_possessive
                and _starts_with_dependent_japanese_particle(target)
            ):
                target_rejected_reason = (
                    "printf引数に続く日本語の助詞から始まり、単独の固定名として利用できません"
                )
            elif translated and not terminology_target_is_safe(source, target):
                target_rejected_reason = (
                    "原文と異なる保護token、制御文字、または不可視文字を含みます"
                )
            if target_rejected_reason:
                target = source
                translated = False
            if target_read_failed:
                target_state = "rejected"
            elif not target_key_known:
                target_state = "missing"
            elif not target or target_rejected_reason:
                target_state = "rejected"
            elif translated:
                target_state = "translated"
            else:
                target_state = "explicit_source"
            provenance_key = (source_had_printf, target_state)
            provenance = provenance_cache.get(provenance_key)
            if provenance is None:
                provenance = f"{container_label}!/{source_name}"
                if source_had_printf:
                    provenance += " [printf表示引数から固定名を抽出]"
                if target_name and target_state != "missing":
                    provenance += (
                        f" -> {container_label}!/{target_name} [{target_state}]"
                    )
                provenance_cache[provenance_key] = provenance
            entry = GlossaryEntry(
                source=source,
                target=target if translated else source,
                key=key,
                mod_id=mod_id,
                translated=translated,
                provenance=provenance,
                target_state=target_state,
                source_had_printf=source_had_printf,
                source_tier=source_tier,
            )
            entries.append(entry)
            if target_rejected_reason:
                message = (
                    f"{target_warning_location}: {_safe_warning_key(key)} の公式訳は"
                    f"{target_rejected_reason}。原語 {_short_value(source)!r} を保持します "
                    f"(公式訳: {_short_value(raw_target)!r})"
                )
                if pending_warnings is None:
                    warnings.append(message)
                else:
                    pending_warnings.append(
                        _PendingTerminologyWarning(
                            evidence=entry,
                            message=message,
                            container_label=container_label,
                        )
                    )
    _raise_if_cancelled(cancel)
    return entries


def _merge_target_only_language_evidence(
    evidence_by_source: dict[str, list[GlossaryEntry]],
    target_only_evidence: Iterable[_TargetOnlyLanguageEvidence],
    target_locale: str,
    cancel: Event | None = None,
) -> list[_PendingTerminologyWarning]:
    """Join target-only overrides only to already validated source identities."""

    provenance_cache: dict[tuple[str, str, str], str] = {}
    warning_location_cache: dict[str, str] = {}
    sources_by_identity: dict[tuple[str, str], dict[str, list[GlossaryEntry]]] = {}
    for source_index, (source, entries) in enumerate(evidence_by_source.items()):
        if source_index % 1024 == 0:
            _raise_if_cancelled(cancel)
        for entry in entries:
            if _is_mod_display_name(entry) or _is_project_reference(entry):
                continue
            identity = (entry.mod_id.casefold(), entry.key.casefold())
            sources_by_identity.setdefault(identity, {}).setdefault(source, []).append(entry)

    pending_warnings: list[_PendingTerminologyWarning] = []
    ordered_target_evidence = sorted(
        target_only_evidence,
        key=lambda item: (
            item.mod_id.casefold(),
            item.key.casefold(),
            item.raw_target,
            item.provenance,
        ),
    )
    for target_index, target_evidence in enumerate(ordered_target_evidence):
        if target_index % 1024 == 0:
            _raise_if_cancelled(cancel)
        identity = (
            target_evidence.mod_id.casefold(),
            target_evidence.key.casefold(),
        )
        matching_sources = sources_by_identity.get(identity)
        if not matching_sources:
            # A target-locale string is not terminology evidence by itself.
            continue
        target_priority = _SOURCE_TIER_PRIORITY.get(
            target_evidence.source_tier,
            99,
        )
        allowed_by_source = {
            source: [
                entry
                for entry in source_entries
                if _SOURCE_TIER_PRIORITY.get(entry.source_tier, 99)
                <= target_priority
            ]
            for source, source_entries in matching_sources.items()
        }
        allowed_by_source = {
            source: entries
            for source, entries in allowed_by_source.items()
            if entries
        }
        if not allowed_by_source:
            # A target cannot promote a source spelling (or printf proof)
            # that exists only in a lower-authority tier.
            continue
        authoritative_priority = min(
            _SOURCE_TIER_PRIORITY.get(entry.source_tier, 99)
            for entries in allowed_by_source.values()
            for entry in entries
        )
        authoritative_sources = {
            source
            for source, entries in allowed_by_source.items()
            if any(
                _SOURCE_TIER_PRIORITY.get(entry.source_tier, 99)
                == authoritative_priority
                for entry in entries
            )
        }
        if len(authoritative_sources) != 1:
            # Translation-only assets provide no source spelling of their own.
            # If the best available source tier disagrees on that spelling (for
            # example, duplicate Mod revisions), attaching the same target to
            # every revision would manufacture evidence. Keep all originals.
            continue
        for source in sorted(authoritative_sources):
            allowed_source_entries = allowed_by_source[source]
            source_had_printf = any(
                entry.source_had_printf for entry in allowed_source_entries
            )
            target_label, target_derived_from_possessive = (
                _fixed_target_terminology_label(
                    target_evidence.raw_target,
                    target_locale,
                    source_had_printf=source_had_printf,
                    independent_fixed_targets=(
                        target_evidence.independent_fixed_targets
                    ),
                )
            )
            target = "" if target_label is None else target_label[0]
            target_had_printf = bool(target_label and target_label[1])
            translated = bool(target and target != source)
            rejection = ""
            if target_evidence.raw_target and target_label is None:
                rejection = "固定名から安全に除去できない保護tokenを含みます"
            elif (
                target_had_printf
                and not source_had_printf
                and not target_derived_from_possessive
            ):
                rejection = (
                    "原文にないprintf引数へ依存するため、固定した公式名として利用できません"
                )
            elif target_had_printf and target_label is not None and target_label[2] != 1:
                rejection = (
                    "printf引数の前後に固定文字列が分かれ、単独の固定名として利用できません"
                )
            elif (
                target_had_printf
                and not target_derived_from_possessive
                and _starts_with_dependent_japanese_particle(target)
            ):
                rejection = (
                    "printf引数に続く日本語の助詞から始まり、単独の固定名として利用できません"
                )
            elif translated and not terminology_target_is_safe(source, target):
                rejection = (
                    "原文と異なる保護token、制御文字、または不可視文字を含みます"
                )
            if rejection:
                target = source
                translated = False
            if not target or rejection:
                target_state = "rejected"
            elif translated:
                target_state = "translated"
            else:
                target_state = "explicit_source"
            representative = min(
                allowed_source_entries,
                key=_glossary_evidence_sort_key,
            )
            provenance_key = (
                representative.provenance,
                target_evidence.provenance,
                target_state,
            )
            provenance = provenance_cache.get(provenance_key)
            if provenance is None:
                provenance = (
                    f"{representative.provenance} -> "
                    f"{target_evidence.provenance} [target-only; {target_state}]"
                )
                provenance_cache[provenance_key] = provenance
            entry = GlossaryEntry(
                source=source,
                target=target if translated else source,
                key=representative.key,
                mod_id=representative.mod_id,
                translated=translated,
                provenance=provenance,
                target_state=target_state,
                source_had_printf=source_had_printf,
                source_tier=target_evidence.source_tier,
            )
            evidence_by_source[source].append(entry)
            if rejection:
                warning_location = warning_location_cache.get(
                    target_evidence.provenance
                )
                if warning_location is None:
                    warning_location = _safe_warning_location(
                        target_evidence.provenance
                    )
                    warning_location_cache[target_evidence.provenance] = (
                        warning_location
                    )
                pending_warnings.append(
                    _PendingTerminologyWarning(
                        evidence=entry,
                        message=(
                            f"{warning_location}: "
                            f"{_safe_warning_key(target_evidence.key)} の公式訳は"
                            f"{rejection}。原語 {_short_value(source)!r} を保持します "
                            f"(公式訳: {_short_value(target_evidence.raw_target)!r})"
                        ),
                        container_label=target_evidence.container_label,
                    )
                )
    _raise_if_cancelled(cancel)
    return pending_warnings


def _read_mod_display_names(
    archive: zipfile.ZipFile,
    archive_names: dict[str, str],
    jar_name: str,
    source_locale: str,
    cancel: Event | None = None,
) -> tuple[list[GlossaryEntry], list[str]]:
    entries: list[GlossaryEntry] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    manifest_declares_library = False

    # FMLModType belongs to the manifest main section and describes how Forge
    # must load the whole archive. In particular, LIBRARY/GAMELIBRARY and
    # LANGPROVIDER are not Mods even if the archive also happens to contain a
    # mods.toml entry. Inspect this declaration before primary metadata so a
    # library title can never become a protected Mod display name. Language
    # assets are scanned by the caller independently of this result.
    manifest_name = archive_names.get("meta-inf/manifest.mf")
    manifest_data: bytes | None = None
    manifest_error: Exception | None = None
    if manifest_name is not None:
        try:
            manifest_data = _read_small_archive_member(archive, manifest_name)
            manifest_declares_library = _manifest_declares_library(manifest_data)
        except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
            manifest_error = exc

    if manifest_declares_library:
        _raise_if_cancelled(cancel)
        return entries, warnings

    for expected_path, reader in _PRIMARY_METADATA_READERS:
        _raise_if_cancelled(cancel)
        archive_name = archive_names.get(expected_path.casefold())
        if archive_name is None:
            continue
        try:
            values = reader(_read_small_archive_member(archive, archive_name), source_locale)
        except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
            warnings.append(
                f"{jar_name}!/{archive_name}: Modメタデータを読めませんでした ({exc})。"
                "このmetadataからはMod表示名を保護対象に追加していません"
            )
            continue
        for mod_id, display_name in values:
            _raise_if_cancelled(cancel)
            normalized_name = _normalize_mod_display_name(display_name)
            if normalized_name is None:
                if isinstance(display_name, str) and display_name.strip():
                    warnings.append(
                        f"{jar_name}!/{archive_name}: 使用できないMod表示名をスキップしました "
                        f"({_short_value(display_name)!r})"
                    )
                continue
            identity = (mod_id.strip(), normalized_name)
            if identity in seen:
                continue
            seen.add(identity)
            entries.append(_mod_display_name_entry(normalized_name, mod_id, jar_name, archive_name))

    # Older and loader-agnostic JARs sometimes expose only a conventional title
    # in MANIFEST.MF. Treat it as a fallback to avoid overriding explicit loader data.
    if not entries:
        _raise_if_cancelled(cancel)
        if manifest_name is not None:
            if manifest_error is not None:
                warnings.append(
                    f"{jar_name}!/{manifest_name}: Modメタデータを読めませんでした "
                    f"({manifest_error})。"
                    "このmanifestからはMod表示名を保護対象に追加していません"
                )
            else:
                assert manifest_data is not None
                values = _read_manifest_metadata(manifest_data)
                for mod_id, display_name in values:
                    _raise_if_cancelled(cancel)
                    normalized_name = _normalize_mod_display_name(display_name)
                    if normalized_name is not None:
                        entries.append(_mod_display_name_entry(normalized_name, mod_id, jar_name, manifest_name))

    if not entries and not manifest_declares_library:
        warnings.append(
            f"{jar_name}: Modメタデータから使用可能な表示名を取得できませんでした。"
            "このJARの表示名は保護できませんが、読み取れた言語ファイルの公式用語は引き続き保護します"
        )
    _raise_if_cancelled(cancel)
    return entries, warnings


def _notify_scan_progress(
    callback: GlossaryProgressCallback | None,
    progress: GlossaryScanProgress,
) -> None:
    if callback is not None:
        callback(progress)


def _raise_if_cancelled(cancel: Event | None) -> None:
    if cancel and cancel.is_set():
        raise CancelledError("Mod JARの走査をキャンセルしました")


def _read_small_archive_member(archive: zipfile.ZipFile, name: str) -> bytes:
    return _read_limited_archive_member(archive, name, _MAX_METADATA_BYTES, "メタデータ")


def _read_language_archive_member(
    archive: zipfile.ZipFile,
    name: str,
    remaining_budget: int | None,
    *,
    language_file_limit: int | None = _MAX_LANGUAGE_MEMBER_BYTES,
    compressed_language_file_limit: int | None = _MAX_LANGUAGE_COMPRESSED_MEMBER_BYTES,
) -> bytes:
    info = archive.getinfo(name)
    if info.file_size < 0:
        raise ValueError("JAR内の言語ファイルの展開後サイズが不正です")
    if info.compress_size < 0:
        raise ValueError("JAR内の言語ファイルの圧縮サイズが不正です")
    if (
        compressed_language_file_limit is not None
        and info.compress_size > compressed_language_file_limit
    ):
        raise ValueError(
            "JAR内の言語ファイルの圧縮サイズが上限を超えています "
            f"({info.compress_size} > {compressed_language_file_limit} bytes)"
        )
    if language_file_limit is not None and info.file_size > language_file_limit:
        raise ValueError(
            "JAR内の言語ファイルが上限を超えています "
            f"({info.file_size} > {language_file_limit} bytes)"
        )
    if remaining_budget is not None and info.file_size > remaining_budget:
        raise ValueError("JAR内の対象言語ファイルが残りの展開後サイズ上限を超えています")
    maximum = _minimum_enabled_budget(language_file_limit, remaining_budget)
    if maximum is None:
        with archive.open(info, "r") as member:
            return member.read()
    return _read_limited_archive_member(
        archive,
        name,
        maximum,
        "JAR内の言語ファイル",
    )


def _read_limited_archive_member(
    archive: zipfile.ZipFile,
    name: str,
    max_bytes: int,
    label: str,
) -> bytes:
    info = archive.getinfo(name)
    if info.file_size < 0 or info.file_size > max_bytes:
        raise ValueError(f"{label}が大きすぎます ({info.file_size} > {max_bytes} bytes)")
    with archive.open(info, "r") as member:
        data = member.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{label}の実読込サイズが上限を超えています ({len(data)} > {max_bytes} bytes)")
    return data


def _read_forge_metadata(data: bytes) -> list[tuple[str, str]]:
    loaded = tomllib.loads(data.decode("utf-8-sig"))
    mods = loaded.get("mods", [])
    if not isinstance(mods, list):
        raise ValueError("mods が配列ではありません")
    result: list[tuple[str, str]] = []
    for mod in mods:
        if not isinstance(mod, dict):
            continue
        mod_id = mod.get("modId", "")
        display_name = mod.get("displayName", "")
        if isinstance(mod_id, str) and (
            not isinstance(display_name, str) or not display_name.strip() or "${" in display_name
        ):
            display_name = mod_id
        if isinstance(mod_id, str) and isinstance(display_name, str):
            result.append((mod_id, display_name))
    return result


def _read_fabric_metadata(data: bytes, source_locale: str) -> list[tuple[str, str]]:
    loaded = _read_metadata_json(data, "fabric.mod.json")
    mod_id = loaded.get("id", "")
    display_name = _localized_metadata_name(loaded.get("name"), source_locale) or (
        mod_id if isinstance(mod_id, str) else ""
    )
    return [(mod_id if isinstance(mod_id, str) else "", display_name)] if display_name else []


def _read_quilt_metadata(data: bytes, source_locale: str) -> list[tuple[str, str]]:
    loaded = _read_metadata_json(data, "quilt.mod.json")
    loader = loaded.get("quilt_loader")
    if not isinstance(loader, dict):
        raise ValueError("quilt_loader がオブジェクトではありません")
    metadata = loader.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("quilt_loader.metadata がオブジェクトではありません")
    mod_id = loader.get("id", "")
    display_name = _localized_metadata_name(metadata.get("name"), source_locale) or (
        mod_id if isinstance(mod_id, str) else ""
    )
    return [(mod_id if isinstance(mod_id, str) else "", display_name)] if display_name else []


def _read_legacy_forge_metadata(data: bytes, source_locale: str) -> list[tuple[str, str]]:
    loaded: object = json.loads(data.decode("utf-8-sig"))
    if isinstance(loaded, list):
        mods = loaded
    elif isinstance(loaded, dict) and isinstance(loaded.get("modList"), list):
        mods = loaded["modList"]
    elif isinstance(loaded, dict):
        mods = [loaded]
    else:
        raise ValueError("mcmod.info のルート形式が不正です")
    result: list[tuple[str, str]] = []
    for mod in mods:
        if not isinstance(mod, dict):
            continue
        mod_id = mod.get("modid", mod.get("modId", ""))
        display_name = _localized_metadata_name(mod.get("name"), source_locale) or (
            mod_id if isinstance(mod_id, str) else ""
        )
        if display_name:
            result.append((mod_id if isinstance(mod_id, str) else "", display_name))
    return result


def _read_manifest_main_attributes(data: bytes) -> dict[str, str]:
    unfolded: list[str] = []
    for line in data.decode("utf-8-sig").splitlines():
        if not line:
            break
        if line.startswith(" ") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    values: dict[str, str] = {}
    for line in unfolded:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().casefold()] = value.strip()
    return values


def _read_manifest_metadata(data: bytes) -> list[tuple[str, str]]:
    values = _read_manifest_main_attributes(data)
    display_name = values.get("implementation-title") or values.get("specification-title") or values.get("bundle-name")
    if not display_name:
        return []
    mod_id = values.get("automatic-module-name") or values.get("implementation-vendor-id") or "manifest"
    return [(mod_id, display_name)]


def _manifest_declares_library(data: bytes) -> bool:
    """Recognize loader-declared library JARs which have no Mod display name."""

    values = _read_manifest_main_attributes(data)
    library_kind = values.get("fmlmodtype", values.get("modtype", ""))
    return library_kind.strip().casefold() in {
        "library",
        "gamelibrary",
        "langprovider",
    }


def _read_metadata_json(data: bytes, label: str) -> dict[str, object]:
    loaded = json.loads(data.decode("utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} のルートがオブジェクトではありません")
    return loaded


def _localized_metadata_name(value: object, source_locale: str) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    normalized = _normalize_locale(source_locale)
    language = normalized.split("_", 1)[0]
    normalized_values = {
        _normalize_locale(str(key)): candidate for key, candidate in value.items()
    }
    for key in (normalized, language, "default"):
        candidate = normalized_values.get(key)
        if isinstance(candidate, str):
            return candidate
    return ""


def _normalize_mod_display_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    normalized = _strip_display_formatting(raw).strip()
    if (
        len(normalized) < 2
        or len(normalized) > 100
        or any(character in normalized for character in ("\r", "\n", "\x00"))
        or "${" in raw
        or not any(character.isalpha() for character in normalized)
    ):
        return None
    return normalized


def _strip_display_formatting(value: str) -> str:
    """Remove only Minecraft display styles; structural tokens stay visible to safety checks."""

    return _FORMAT_CODE_PATTERN.sub("", value)


def _terminology_language_label(value: str) -> tuple[str, bool, int] | None:
    """Return a fixed visible label and its printf-template evidence.

    Display formatting is never part of an official name.  Compact printf
    arguments may also be presentation-only, but only the language scanner's
    key correlation is allowed to promote the remaining text to terminology.
    Other protected syntax keeps the value dynamic and fails closed.
    """

    visible = _strip_display_formatting(value)
    skeleton = terminology_literal_skeleton(visible)
    if skeleton is None:
        return None
    literal, had_printf = skeleton
    if not had_printf:
        return literal.strip(), False, 1 if literal.strip() else 0

    meaningful_fragments: list[str] = []
    cursor = 0
    for start, end in protected_syntax_ranges(visible):
        fragment = visible[cursor:start]
        if any(character.isalnum() for character in fragment):
            meaningful_fragments.append(fragment)
        cursor = end
    fragment = visible[cursor:]
    if any(character.isalnum() for character in fragment):
        meaningful_fragments.append(fragment)
    # A single literal fragment may be wrapped by punctuation which only
    # separates it from presentation arguments, e.g. ``Name (%s, %s)``.
    # Keep punctuation inside the label but discard that outer shell.
    if len(meaningful_fragments) == 1:
        literal = meaningful_fragments[0].strip(" \t\r\n()[]{}<>,;:")
    # Removing an inline printf token must not concatenate independent words.
    # Language labels do not use significant runs of ASCII spaces.
    normalized = " ".join(literal.split())
    return normalized, True, len(meaningful_fragments)


def _independent_fixed_target_labels(
    values: Iterable[str],
    cancel: Event | None = None,
) -> frozenset[str]:
    """Return inert standalone target values usable as positive name evidence.

    This evidence does not itself add a glossary entry. It only proves that a
    Japanese suffix recovered from ``%sの...`` also exists independently of
    that printf template in the same official language file.
    """

    labels: set[str] = set()
    for index, raw_value in enumerate(values):
        if index % 1024 == 0:
            _raise_if_cancelled(cancel)
        label = _terminology_language_label(raw_value)
        if label is None:
            continue
        value, had_printf, _literal_fragments = label
        if (
            not had_printf
            and _usable_term(value)
            and _looks_like_short_term_label(value)
            and not _japanese_fixed_name_is_clause_or_status(value)
            and terminology_target_is_safe(value, value)
        ):
            labels.add(value)
    _raise_if_cancelled(cancel)
    return frozenset(labels)


def _japanese_fixed_name_is_clause_or_status(value: str) -> bool:
    """Recognize grammatical evidence incompatible with a standalone name."""

    return (
        _JAPANESE_CLAUSE_PARTICLE.search(value) is not None
        or _JAPANESE_STATUS_ENDING.search(value) is not None
        or _JAPANESE_PREDICATE_ENDING.search(value) is not None
        or _JAPANESE_SENTENCE_PUNCTUATION.search(value) is not None
    )


def _fixed_terminology_source_label(
    key: str,
    raw_source: str,
    available_keys: frozenset[str],
    source_labels: dict[str, str],
    resource_backed_keys: frozenset[str],
) -> tuple[str, bool, int] | None:
    """Classify one language value as a fixed terminology source.

    Dynamic status/messages are normal language data, not damaged terminology.
    Reject them during candidate detection rather than first admitting them and
    later reporting a partial-scan warning.  A printf-bearing display name is
    accepted only when its remaining literal tokens exactly identify the
    registry-language key.
    """

    if not _is_terminology_key(
        key,
        available_keys,
        source_labels,
        resource_backed_keys,
    ):
        return None
    label = _terminology_language_label(raw_source)
    if label is None:
        return None
    source, had_printf, literal_fragments = label
    if had_printf and not _printf_template_identifies_key(
        key,
        source,
        literal_fragments,
    ):
        return None
    return label if _usable_term(source) else None


def _fixed_target_terminology_label(
    raw_target: str,
    target_locale: str,
    *,
    source_had_printf: bool = False,
    independent_fixed_targets: frozenset[str] = frozenset(),
) -> tuple[tuple[str, bool, int] | None, bool]:
    """Return a fixed official target and whether a Japanese head noun was derived.

    Japanese translations commonly turn ``%s Blueprint`` into ``%sの設計図``.
    When there is exactly one leading printf argument and no other protected
    syntax, the suffix still needs positive name evidence: either the source
    printf label already matched its registry key exactly, or the target suffix
    occurs as an inert standalone value in the same official language file.
    A small grammatical guard then rejects clauses and status text. This keeps
    the decision extensible without treating every noun-shaped Japanese string
    as a global name. Other templates retain the same fail-closed path.
    """

    label = _terminology_language_label(raw_target)
    normalized_locale = _normalize_locale(target_locale)
    if (
        label is None
        or not label[1]
        or not (normalized_locale == "ja" or normalized_locale.startswith("ja_"))
    ):
        return label, False

    visible = _strip_display_formatting(raw_target)
    syntax_ranges = protected_syntax_ranges(visible)
    if len(syntax_ranges) != 1:
        return label, False
    start, end = syntax_ranges[0]
    if visible[:start].strip():
        return label, False
    printf_token = visible[start:end]
    if _STRING_PRINTF_TOKEN.fullmatch(printf_token) is None:
        return label, False
    suffix = visible[end:].strip()
    match = re.fullmatch(r"の\s*(.+)", suffix, re.DOTALL)
    if match is None:
        return label, False
    fixed_name = match.group(1).strip()
    if (
        not fixed_name
        or len(fixed_name) > 100
        or not any(character.isalpha() for character in fixed_name)
        or protected_syntax_signature(fixed_name)
        or not (source_had_printf or fixed_name in independent_fixed_targets)
        or _japanese_fixed_name_is_clause_or_status(fixed_name)
    ):
        return label, False
    return (fixed_name, True, 1), True


def _printf_template_identifies_key(
    key: str,
    label: str,
    literal_fragments: int,
) -> bool:
    """Prove that a printf-stripped English label is the key's fixed name."""

    if literal_fragments < 1:
        return False
    normalized_key = key.casefold()
    prefix = next(
        (
            candidate
            for candidate in sorted(_TERM_KEY_PREFIXES, key=len, reverse=True)
            if normalized_key.startswith(candidate)
        ),
        "",
    )
    if not prefix:
        return False
    remainder = normalized_key[len(prefix) :]
    _namespace, separator, resource_path = remainder.partition(".")
    if not separator or not resource_path:
        return False
    key_tokens = re.findall(r"[a-z0-9]+", resource_path)
    label_tokens = re.findall(r"[A-Za-z0-9]+", label)
    if not key_tokens or not label_tokens:
        return False
    roman_numbers = {
        "I": "1",
        "II": "2",
        "III": "3",
        "IV": "4",
        "V": "5",
        "VI": "6",
        "VII": "7",
        "VIII": "8",
        "IX": "9",
        "X": "10",
    }
    normalized_label_tokens = [
        roman_numbers.get(token, token.casefold()) for token in label_tokens
    ]
    return normalized_label_tokens == key_tokens


def _starts_with_dependent_japanese_particle(value: str) -> bool:
    """Reject a target fragment that only becomes a noun after a printf value."""

    return re.match(r"(?:から|まで|より|の|が|を|に|へ|と|で)", value) is not None


def _short_value(value: str, limit: int = 80) -> str:
    normalized = value.strip().replace("\r", "\\r").replace("\n", "\\n")
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"


def _mod_display_name_entry(
    display_name: str, mod_id: str, jar_name: str, metadata_name: str
) -> GlossaryEntry:
    normalized_id = mod_id.strip() or "unknown"
    return GlossaryEntry(
        source=display_name,
        target=display_name,
        key=f"{_MOD_NAME_KEY_PREFIX}{normalized_id}",
        mod_id=normalized_id,
        translated=False,
        provenance=f"{jar_name}!/{metadata_name}",
    )


def _is_mod_display_name(entry: GlossaryEntry) -> bool:
    return entry.key.startswith(
        (_MOD_NAME_KEY_PREFIX, _PROJECT_REFERENCE_MOD_DISPLAY_KEY_PREFIX)
    )


def _is_project_reference(entry: GlossaryEntry) -> bool:
    return entry.key.startswith(_PROJECT_REFERENCE_KEY_PREFIX)


def _is_project_reference_from_mod_display(entry: GlossaryEntry) -> bool:
    return entry.key.startswith(_PROJECT_REFERENCE_MOD_DISPLAY_KEY_PREFIX)


def _find_archives(
    location: Path,
    maximum_entries: int | None = _MAX_ARCHIVE_MEMBERS,
    cancel: Event | None = None,
) -> Iterable[Path]:
    location = location.expanduser()
    if location.is_file() and location.suffix.lower() in {".jar", ".zip"}:
        yield location
        return
    if not location.is_dir():
        return
    mods_dir = location / "mods" if (location / "mods").is_dir() else location
    children = _bounded_sorted_scandir(
        mods_dir,
        maximum_entries,
        "mods直下の項目数",
        cancel,
    )
    for child in children:
        if Path(child.name).suffix.casefold() in {".jar", ".zip"}:
            yield Path(child.path)


def _infer_game_root_from_mod_location(location: Path) -> Path | None:
    """Infer only the unambiguous legacy ``<game_root>/mods`` shape."""

    candidate = location.expanduser()
    if candidate.name.casefold() != "mods":
        return None
    game_root = candidate.parent
    try:
        candidate_stat = _checked_external_path_stat(
            candidate,
            game_root,
            "modsフォルダー",
        )
        root_stat = _checked_external_path_stat(
            game_root,
            game_root,
            "推定game_root",
        )
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISDIR(candidate_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return None
    return game_root


def _discover_external_asset_sources(
    game_root: Path | None,
    include_resourcepacks: bool,
    cancel: Event | None = None,
    *,
    maximum_source_members: int | None = _MAX_ARCHIVE_MEMBERS,
) -> _ExternalAssetDiscovery:
    if game_root is None or not str(game_root):
        return _ExternalAssetDiscovery()

    root = game_root.expanduser()
    warnings: list[str] = []
    sources: list[_ExternalAssetSource] = []
    discovered_sources = 0
    failed_sources = 0
    kubejs_sources = 0
    resourcepack_sources = 0
    _raise_if_cancelled(cancel)
    try:
        root_stat = _checked_external_path_stat(root, root, "game_root")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("ディレクトリではありません")
    except (OSError, RuntimeError, ValueError) as exc:
        warnings.append(
            f"追加言語資産のgame_rootを安全に確認できませんでした "
            f"({_short_value(str(root))!r}: {exc})。KubeJSとresource packは走査していません"
        )
        return _ExternalAssetDiscovery(warnings=tuple(warnings))

    kubejs_root = root / "kubejs"
    try:
        kubejs_stat = _optional_external_path_stat(
            kubejs_root,
            root,
            "KubeJSフォルダー",
        )
        if kubejs_stat is not None:
            if not stat.S_ISDIR(kubejs_stat.st_mode):
                raise ValueError("kubejs がディレクトリではありません")
            kubejs_assets = kubejs_root / "assets"
            assets_stat = _optional_external_path_stat(
                kubejs_assets,
                root,
                "KubeJS assets",
            )
            if assets_stat is not None:
                discovered_sources += 1
                kubejs_sources += 1
                if not stat.S_ISDIR(assets_stat.st_mode):
                    raise ValueError("kubejs/assets がディレクトリではありません")
                sources.append(
                    _ExternalAssetSource(
                        path=kubejs_root,
                        label="KubeJS (kubejs/assets)",
                        storage="directory",
                        source_kind="kubejs",
                        safety_anchor=root,
                    )
                )
    except (OSError, RuntimeError, ValueError) as exc:
        # A present but unsafe conventional KubeJS location is a discovered
        # source failure, not an invisible omission.
        if discovered_sources == 0:
            discovered_sources += 1
            kubejs_sources += 1
        failed_sources += 1
        warnings.append(
            f"KubeJS (kubejs/assets): symlink・junctionを含むか安全確認できないため"
            f"走査していません ({exc})"
        )

    _raise_if_cancelled(cancel)
    if not include_resourcepacks:
        return _ExternalAssetDiscovery(
            sources=tuple(sources),
            warnings=tuple(warnings),
            discovered_sources=discovered_sources,
            failed_sources=failed_sources,
            kubejs_sources=kubejs_sources,
            resourcepack_sources=resourcepack_sources,
        )

    resourcepacks_root = root / "resourcepacks"
    try:
        resourcepacks_stat = _optional_external_path_stat(
            resourcepacks_root,
            root,
            "resourcepacksフォルダー",
        )
        if resourcepacks_stat is None:
            return _ExternalAssetDiscovery(
                sources=tuple(sources),
                warnings=tuple(warnings),
                discovered_sources=discovered_sources,
                failed_sources=failed_sources,
                kubejs_sources=kubejs_sources,
                resourcepack_sources=resourcepack_sources,
            )
        if not stat.S_ISDIR(resourcepacks_stat.st_mode):
            raise ValueError("resourcepacks がディレクトリではありません")
        children = _bounded_sorted_scandir(
            resourcepacks_root,
            maximum_source_members,
            "resourcepacks直下の項目数",
            cancel,
        )
        for child in children:
            _raise_if_cancelled(cancel)
            child_path = Path(child.path)
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                discovered_sources += 1
                resourcepack_sources += 1
                failed_sources += 1
                warnings.append(
                    f"resource pack {_short_value(child.name)!r}: 安全確認できないため"
                    f"走査していません ({exc})"
                )
                continue
            if _external_stat_is_reparse(child_path, child_stat):
                discovered_sources += 1
                resourcepack_sources += 1
                failed_sources += 1
                warnings.append(
                    f"resource pack {_short_value(child.name)!r}: symlinkまたはjunctionのため"
                    "外部へ追跡せず走査していません"
                )
                continue
            if stat.S_ISREG(child_stat.st_mode):
                if child_path.suffix.casefold() != ".zip":
                    continue
                discovered_sources += 1
                resourcepack_sources += 1
                sources.append(
                    _ExternalAssetSource(
                        path=child_path,
                        label=f"resource pack ZIP {_short_value(child.name)!r}",
                        storage="zip",
                        source_kind="resourcepack",
                        safety_anchor=root,
                    )
                )
                continue
            if not stat.S_ISDIR(child_stat.st_mode):
                continue
            assets_path = child_path / "assets"
            try:
                assets_stat = _optional_external_path_stat(
                    assets_path,
                    root,
                    f"resource pack {child.name} のassets",
                )
            except (OSError, RuntimeError, ValueError) as exc:
                discovered_sources += 1
                resourcepack_sources += 1
                failed_sources += 1
                warnings.append(
                    f"resource pack folder {_short_value(child.name)!r}: assetsを安全確認"
                    f"できないため走査していません ({exc})"
                )
                continue
            if assets_stat is None:
                continue
            discovered_sources += 1
            resourcepack_sources += 1
            if not stat.S_ISDIR(assets_stat.st_mode):
                failed_sources += 1
                warnings.append(
                    f"resource pack folder {_short_value(child.name)!r}: assetsが"
                    "ディレクトリではないため走査していません"
                )
                continue
            sources.append(
                _ExternalAssetSource(
                    path=child_path,
                    label=f"resource pack folder {_short_value(child.name)!r}",
                    storage="directory",
                    source_kind="resourcepack",
                    safety_anchor=root,
                )
            )
    except (OSError, RuntimeError, ValueError) as exc:
        discovered_sources += 1
        resourcepack_sources += 1
        failed_sources += 1
        warnings.append(
            "resourcepacksフォルダー全体を安全に走査できないため、未確認の"
            f"resource packは走査していません ({exc})"
        )

    return _ExternalAssetDiscovery(
        sources=tuple(sources),
        warnings=tuple(warnings),
        discovered_sources=discovered_sources,
        failed_sources=failed_sources,
        kubejs_sources=kubejs_sources,
        resourcepack_sources=resourcepack_sources,
    )


def _bounded_sorted_scandir(
    path: Path,
    maximum_entries: int | None,
    description: str,
    cancel: Event | None,
    *,
    reverse: bool = False,
) -> list[os.DirEntry[str]]:
    """Collect entries, stopping at ``maximum_entries + 1`` when enabled."""

    children: list[os.DirEntry[str]] = []
    with os.scandir(path) as iterator:
        for child in iterator:
            _raise_if_cancelled(cancel)
            children.append(child)
            if maximum_entries is not None and len(children) > maximum_entries:
                raise ValueError(
                    f"{description}が上限を超えています "
                    f"({len(children)} > {maximum_entries})"
                )
    return sorted(
        children,
        key=lambda child: (child.name.casefold(), child.name),
        reverse=reverse,
    )


def _checked_external_path_stat(
    path: Path,
    anchor: Path,
    description: str,
) -> os.stat_result:
    raw_path = Path(path)
    raw_anchor = Path(anchor)
    if ".." in raw_path.parts or ".." in raw_anchor.parts:
        raise ValueError(f"{description}に親ディレクトリ参照 '..' は使用できません")
    lexical_path = Path(os.path.abspath(raw_path))
    lexical_anchor = Path(os.path.abspath(raw_anchor))
    if not lexical_path.is_relative_to(lexical_anchor):
        raise ValueError(f"{description}がgame_root外を参照しています")
    candidate = lexical_path
    while True:
        candidate_stat = candidate.lstat()
        if _external_stat_is_reparse(candidate, candidate_stat):
            raise ValueError(
                f"{description}またはその祖先がsymlinkまたはjunctionです: {candidate}"
            )
        if candidate == lexical_anchor:
            break
        parent = candidate.parent
        if parent == candidate:
            raise ValueError(f"{description}がgame_root外を参照しています")
        candidate = parent
    return lexical_path.lstat()


def _optional_external_path_stat(
    path: Path,
    anchor: Path,
    description: str,
) -> os.stat_result | None:
    try:
        return _checked_external_path_stat(path, anchor, description)
    except FileNotFoundError:
        return None


def _external_stat_is_reparse(path: Path, value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _read_directory_asset_source(
    source: _ExternalAssetSource,
    source_locale: str,
    target_locale: str,
    cancel: Event | None,
    scan_language_budget: int | _SharedScanBudget | None,
) -> tuple[
    list[GlossaryEntry],
    list[str],
    int,
    list[_TargetOnlyLanguageEvidence],
    list[_PendingTerminologyWarning],
]:
    language_file_limit = (
        scan_language_budget.language_file_limit
        if isinstance(scan_language_budget, _SharedScanBudget)
        else _MAX_LANGUAGE_MEMBER_BYTES
    )
    archive_names, language_paths = _directory_asset_inventory(
        source,
        cancel,
        scan_language_budget,
    )

    def declared_size(name: str) -> int:
        value = _checked_external_path_stat(
            language_paths[name],
            source.safety_anchor,
            f"{source.label} の言語ファイル",
        )
        if not stat.S_ISREG(value.st_mode):
            raise ValueError("言語ファイルが通常ファイルではありません")
        return value.st_size

    def read_bytes(name: str, remaining: int | None) -> bytes:
        if language_file_limit == _DEFAULT_SCAN_LIMITS.max_language_file_bytes:
            return _read_limited_external_file(
                language_paths[name],
                source.safety_anchor,
                remaining,
            )
        return _read_limited_external_file(
            language_paths[name],
            source.safety_anchor,
            remaining,
            language_file_limit=language_file_limit,
        )

    return _read_external_language_inventory(
        source.label,
        source.source_kind,
        archive_names,
        source_locale,
        target_locale,
        declared_size,
        read_bytes,
        cancel,
        scan_language_budget,
    )


def _directory_asset_inventory(
    source: _ExternalAssetSource,
    cancel: Event | None,
    scan_budget: int | _SharedScanBudget | None,
) -> tuple[dict[str, str], dict[str, Path]]:
    source_member_limit = (
        scan_budget.source_member_limit
        if isinstance(scan_budget, _SharedScanBudget)
        else _MAX_ARCHIVE_MEMBERS
    )
    root_stat = _checked_external_path_stat(
        source.path,
        source.safety_anchor,
        source.label,
    )
    assets_root = source.path / "assets"
    assets_stat = _checked_external_path_stat(
        assets_root,
        source.safety_anchor,
        f"{source.label} のassets",
    )
    if not stat.S_ISDIR(root_stat.st_mode) or not stat.S_ISDIR(assets_stat.st_mode):
        raise ValueError("言語資産のassetsがディレクトリではありません")

    archive_names: dict[str, str] = {}
    language_paths: dict[str, Path] = {}
    member_count = 0
    stack = [assets_root]
    while stack:
        _raise_if_cancelled(cancel)
        current = stack.pop()
        current_stat = _checked_external_path_stat(
            current,
            source.safety_anchor,
            source.label,
        )
        if not stat.S_ISDIR(current_stat.st_mode):
            raise ValueError(f"走査中のディレクトリが通常ディレクトリではありません: {current}")
        remaining_entries = (
            None
            if source_member_limit is None
            else source_member_limit - member_count
        )
        children = _bounded_sorted_scandir(
            current,
            remaining_entries,
            "言語資産内のファイル・ディレクトリ項目数",
            cancel,
            reverse=True,
        )
        for child in children:
            _raise_if_cancelled(cancel)
            member_count += 1
            child_path = Path(child.path)
            child_stat = child.stat(follow_symlinks=False)
            if _external_stat_is_reparse(child_path, child_stat):
                raise ValueError(
                    f"assets内にsymlinkまたはjunctionがあります: {child_path}"
                )
            if stat.S_ISDIR(child_stat.st_mode):
                stack.append(child_path)
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise ValueError(f"assets内に通常ファイル以外があります: {child_path}")
            logical_name = child_path.relative_to(source.path).as_posix()
            _validate_external_member_name(logical_name)
            normalized_name = logical_name.casefold()
            if normalized_name in archive_names:
                raise ValueError(
                    "大文字小文字を無視すると同じasset pathが重複しています: "
                    f"{_short_value(logical_name)!r}"
                )
            archive_names[normalized_name] = logical_name
            if _LANG_PATH.fullmatch(logical_name):
                language_paths[logical_name] = child_path
    return archive_names, language_paths


def _read_zip_asset_source(
    source: _ExternalAssetSource,
    source_locale: str,
    target_locale: str,
    cancel: Event | None,
    scan_language_budget: int | _SharedScanBudget | None,
) -> tuple[
    list[GlossaryEntry],
    list[str],
    int,
    list[_TargetOnlyLanguageEvidence],
    list[_PendingTerminologyWarning],
]:
    language_file_limit = (
        scan_language_budget.language_file_limit
        if isinstance(scan_language_budget, _SharedScanBudget)
        else _MAX_LANGUAGE_MEMBER_BYTES
    )
    compressed_language_file_limit = (
        scan_language_budget.compressed_language_file_limit
        if isinstance(scan_language_budget, _SharedScanBudget)
        else _MAX_LANGUAGE_COMPRESSED_MEMBER_BYTES
    )
    source_stat = _checked_external_path_stat(
        source.path,
        source.safety_anchor,
        source.label,
    )
    if not stat.S_ISREG(source_stat.st_mode):
        raise ValueError("resource pack ZIPが通常ファイルではありません")
    with source.path.open("rb") as zip_handle:
        opened_stat = os.fstat(zip_handle.fileno())
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or _external_file_identity(source_stat)
            != _external_file_identity(opened_stat)
        ):
            raise ValueError("resource pack ZIPが安全確認後に差し替えられました")
        expected_entries = _preflight_resourcepack_zip(
            zip_handle,
            opened_stat,
            scan_language_budget,
        )
        zip_handle.seek(0)
        with zipfile.ZipFile(zip_handle) as archive:
            archive_infos = archive.infolist()
            actual_entries = len(archive_infos)
            if len(archive_infos) != expected_entries:
                raise ValueError(
                    "ZIP事前検査の項目数と実際の項目数が一致しません "
                    f"({expected_entries} != {len(archive_infos)})"
                )
            archive_names: dict[str, str] = {}
            language_infos: dict[str, zipfile.ZipInfo] = {}
            for info in archive_infos:
                _raise_if_cancelled(cancel)
                logical_name = _validate_external_member_name(info.filename, info.is_dir())
                if logical_name is None:
                    continue
                if _zip_info_is_symlink(info):
                    raise ValueError(
                        f"ZIP内にsymlinkがあります: {_short_value(logical_name)!r}"
                    )
                if info.is_dir():
                    continue
                normalized_name = logical_name.casefold()
                if normalized_name in archive_names:
                    raise ValueError(
                        "大文字小文字を無視すると同じZIP内の項目パスが重複しています: "
                        f"{_short_value(logical_name)!r}"
                    )
                archive_names[normalized_name] = logical_name
                if _LANG_PATH.fullmatch(logical_name):
                    language_infos[logical_name] = info

            def declared_size(name: str) -> int:
                return language_infos[name].file_size

            def read_bytes(name: str, remaining: int | None) -> bytes:
                if (
                    language_file_limit
                    == _DEFAULT_SCAN_LIMITS.max_language_file_bytes
                    and compressed_language_file_limit
                    == _DEFAULT_SCAN_LIMITS.max_language_file_bytes
                ):
                    return _read_limited_zip_info(
                        archive,
                        language_infos[name],
                        remaining,
                    )
                return _read_limited_zip_info(
                    archive,
                    language_infos[name],
                    remaining,
                    language_file_limit=language_file_limit,
                    compressed_language_file_limit=compressed_language_file_limit,
                )

            result = _read_external_language_inventory(
                source.label,
                source.source_kind,
                archive_names,
                source_locale,
                target_locale,
                declared_size,
                read_bytes,
                cancel,
                scan_language_budget,
            )
        completed_stat = os.fstat(zip_handle.fileno())
        if _external_file_identity(opened_stat) != _external_file_identity(completed_stat):
            raise ValueError("resource pack ZIPが走査中に変更されました")
    final_stat = _checked_external_path_stat(
        source.path,
        source.safety_anchor,
        source.label,
    )
    if _external_file_identity(source_stat) != _external_file_identity(final_stat):
        raise ValueError("resource pack ZIPが走査中に変更されました")
    return result


def _preflight_resourcepack_zip(
    handle: object,
    file_stat: os.stat_result,
    scan_budget: int | _SharedScanBudget | None,
) -> int:
    """Validate ZIP metadata and apply the enabled bound before expansion."""

    source_member_limit = (
        scan_budget.source_member_limit
        if isinstance(scan_budget, _SharedScanBudget)
        else _MAX_ARCHIVE_MEMBERS
    )
    central_directory_limit = (
        None if source_member_limit is None else source_member_limit * 46
    )
    file_size = file_stat.st_size
    if file_size < 22:
        raise zipfile.BadZipFile("ZIP終端レコードがありません")
    tail_size = min(file_size, 22 + 65_535)
    handle.seek(file_size - tail_size)  # type: ignore[attr-defined]
    tail = handle.read(tail_size)  # type: ignore[attr-defined]
    search_end = len(tail)
    record: tuple[int, int, int, int, int, int, int] | None = None
    record_index = -1
    while search_end >= 4:
        index = tail.rfind(b"PK\x05\x06", 0, search_end)
        if index < 0:
            break
        if index + 22 <= len(tail):
            unpacked = struct.unpack_from("<4s4H2LH", tail, index)
            (
                _signature,
                disk_number,
                central_disk,
                entries_on_disk,
                total_entries,
                central_size,
                central_offset,
                comment_length,
            ) = unpacked
            if index + 22 + comment_length == len(tail):
                record = (
                    disk_number,
                    central_disk,
                    entries_on_disk,
                    total_entries,
                    central_size,
                    central_offset,
                    comment_length,
                )
                record_index = index
                break
        search_end = index
    if record is None:
        raise zipfile.BadZipFile("有効なZIP終端レコードがありません")
    (
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        _comment_length,
    ) = record
    if disk_number != 0 or central_disk != 0 or entries_on_disk != total_entries:
        raise ValueError("分割ZIPは走査できません")
    if (
        total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise ValueError("ZIP64 resource packは安全上限を事前確認できないため走査しません")
    if source_member_limit is not None and total_entries > source_member_limit:
        raise ValueError(
            "ZIP内項目数が上限を超えています "
            f"({total_entries} > {source_member_limit})"
        )
    if (
        central_directory_limit is not None
        and central_size > central_directory_limit
    ):
        raise ValueError(
            "ZIPの項目一覧サイズが上限を超えています "
            f"({central_size} > {central_directory_limit} bytes)"
        )
    eocd_offset = file_size - tail_size + record_index
    central_start = eocd_offset - central_size
    if central_start < 0 or central_offset > central_start:
        raise ValueError("ZIPの項目一覧位置が不正です")
    return _count_resourcepack_central_directory_entries(
        handle,
        central_start,
        central_size,
        total_entries,
        scan_budget,
    )


def _count_resourcepack_central_directory_entries(
    handle: object,
    central_start: int,
    central_size: int,
    declared_entries: int,
    scan_budget: int | _SharedScanBudget | None,
) -> int:
    """Count bounded central-directory headers before ``ZipFile`` allocates them.

    EOCD counts are untrusted, so the central directory is counted directly.
    The enabled per-source limit is checked during that count.  Regardless of
    the limit setting, the parsed count must match the declared count before
    ``ZipFile`` is allowed to materialize the entry list.
    """

    source_member_limit = (
        scan_budget.source_member_limit
        if isinstance(scan_budget, _SharedScanBudget)
        else _MAX_ARCHIVE_MEMBERS
    )
    central_end = central_start + central_size
    handle.seek(central_start)  # type: ignore[attr-defined]
    actual_entries = 0
    while handle.tell() < central_end:  # type: ignore[attr-defined]
        position = handle.tell()  # type: ignore[attr-defined]
        remaining = central_end - position
        if remaining < 46:
            raise zipfile.BadZipFile(
                "ZIPの項目一覧末尾に不完全なヘッダーがあります"
            )
        fixed_header = handle.read(46)  # type: ignore[attr-defined]
        if len(fixed_header) != 46 or fixed_header[:4] != b"PK\x01\x02":
            raise zipfile.BadZipFile(
                "ZIPの項目一覧に不正なヘッダーがあります"
            )
        name_length, extra_length, comment_length = struct.unpack_from(
            "<3H",
            fixed_header,
            28,
        )
        variable_length = name_length + extra_length + comment_length
        if 46 + variable_length > remaining:
            raise zipfile.BadZipFile(
                "ZIPの項目一覧ヘッダーの可変長領域が範囲外です"
            )
        actual_entries += 1
        if source_member_limit is not None and actual_entries > source_member_limit:
            raise ValueError(
                "ZIP内項目数が上限を超えています "
                f"({actual_entries} > {source_member_limit})"
            )
        handle.seek(variable_length, 1)  # type: ignore[attr-defined]

    if actual_entries != declared_entries:
        raise ValueError(
            "ZIP事前検査の項目数と実際の項目数が一致しません "
            f"({declared_entries} != {actual_entries})"
        )
    return actual_entries


def _validate_external_member_name(
    name: str,
    is_directory: bool = False,
) -> str | None:
    if not isinstance(name, str) or not name:
        raise ValueError("空の言語資産内の項目パスがあります")
    if "\\" in name or "\x00" in name or name.startswith("/"):
        raise ValueError(f"安全でない言語資産内の項目パスです: {_short_value(name)!r}")
    logical_name = name[:-1] if is_directory and name.endswith("/") else name
    if not logical_name:
        return None
    parts = logical_name.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError(f"安全でない言語資産内の項目パスです: {_short_value(name)!r}")
    if ":" in parts[0]:
        raise ValueError(f"drive指定を含む言語資産内の項目パスです: {_short_value(name)!r}")
    return logical_name


def _zip_info_is_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(unix_mode)


def _external_file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000)),
    )


def _read_limited_external_file(
    path: Path,
    safety_anchor: Path,
    remaining_budget: int | None,
    *,
    language_file_limit: int | None = _MAX_LANGUAGE_MEMBER_BYTES,
) -> bytes:
    before = _checked_external_path_stat(path, safety_anchor, "追加言語資産の言語ファイル")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("言語ファイルが通常ファイルではありません")
    if before.st_size < 0:
        raise ValueError("言語ファイルのサイズが不正です")
    maximum = _minimum_enabled_budget(
        language_file_limit,
        None if remaining_budget is None else max(0, remaining_budget),
    )
    if maximum is not None and before.st_size > maximum:
        raise ValueError(
            f"言語ファイルが上限を超えています ({before.st_size} > {maximum} bytes)"
        )
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("openした言語ファイルが通常ファイルではありません")
        if _external_file_identity(before) != _external_file_identity(opened):
            raise ValueError("言語ファイルが安全確認後に差し替えられました")
        data = handle.read() if maximum is None else handle.read(maximum + 1)
        read_complete = os.fstat(handle.fileno())
    if maximum is not None and len(data) > maximum:
        raise ValueError(
            f"言語ファイルの実読込サイズが上限を超えています ({len(data)} > {maximum} bytes)"
        )
    after = _checked_external_path_stat(path, safety_anchor, "追加言語資産の言語ファイル")
    if (
        _external_file_identity(opened) != _external_file_identity(read_complete)
        or _external_file_identity(before) != _external_file_identity(after)
        or len(data) != before.st_size
    ):
        raise ValueError("言語ファイルが走査中に変更されました")
    return data


def _read_limited_zip_info(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    remaining_budget: int | None,
    *,
    language_file_limit: int | None = _MAX_LANGUAGE_MEMBER_BYTES,
    compressed_language_file_limit: int | None = _MAX_LANGUAGE_COMPRESSED_MEMBER_BYTES,
) -> bytes:
    if info.file_size < 0:
        raise ValueError("ZIP内の言語ファイルの展開後サイズが不正です")
    if info.compress_size < 0:
        raise ValueError("ZIP内の言語ファイルの圧縮サイズが不正です")
    if (
        compressed_language_file_limit is not None
        and info.compress_size > compressed_language_file_limit
    ):
        raise ValueError(
            "ZIP内の言語ファイルの圧縮サイズが上限を超えています "
            f"({info.compress_size} > {compressed_language_file_limit} bytes)"
        )
    maximum = _minimum_enabled_budget(
        language_file_limit,
        None if remaining_budget is None else max(0, remaining_budget),
    )
    if maximum is not None and info.file_size > maximum:
        raise ValueError(
            f"ZIP内の言語ファイルが上限を超えています ({info.file_size} > {maximum} bytes)"
        )
    with archive.open(info, "r") as member:
        data = member.read() if maximum is None else member.read(maximum + 1)
    if maximum is not None and len(data) > maximum:
        raise ValueError(
            "ZIP内の言語ファイルの実読込サイズが上限を超えています "
            f"({len(data)} > {maximum} bytes)"
        )
    return data


def _read_external_language_inventory(
    container_label: str,
    source_tier: Literal["kubejs", "resourcepack"],
    archive_names: dict[str, str],
    source_locale: str,
    target_locale: str,
    declared_size: Callable[[str], int],
    read_bytes: Callable[[str, int | None], bytes],
    cancel: Event | None,
    scan_language_budget: int | _SharedScanBudget | None,
) -> tuple[
    list[GlossaryEntry],
    list[str],
    int,
    list[_TargetOnlyLanguageEvidence],
    list[_PendingTerminologyWarning],
]:
    language_file_limit = (
        scan_language_budget.language_file_limit
        if isinstance(scan_language_budget, _SharedScanBudget)
        else _MAX_LANGUAGE_MEMBER_BYTES
    )
    source_language_limit = (
        scan_language_budget.source_language_limit
        if isinstance(scan_language_budget, _SharedScanBudget)
        else _MAX_ARCHIVE_LANGUAGE_BYTES
    )
    total_language_limit = (
        scan_language_budget.language_byte_limit
        if isinstance(scan_language_budget, _SharedScanBudget)
        else _MAX_SCAN_LANGUAGE_BYTES
    )
    warnings: list[str] = []
    names: dict[tuple[str, str, str], str] = {}
    for logical_name in archive_names.values():
        _raise_if_cancelled(cancel)
        match = _LANG_PATH.fullmatch(logical_name)
        if match is None:
            continue
        mod_id, locale, extension = match.groups()
        identity = (
            mod_id.casefold(),
            _normalize_locale(locale),
            extension.casefold(),
        )
        if identity in names:
            raise ValueError(
                "同じnamespace・locale・形式の言語ファイルが重複しています: "
                f"{_short_value(logical_name)!r}"
            )
        names[identity] = logical_name

    normalized_source_locale = _normalize_locale(source_locale)
    normalized_target_locale = _normalize_locale(target_locale)
    source_mod_ids = {
        mod_id
        for mod_id, locale, _extension in names
        if locale == normalized_source_locale
    }
    target_mod_ids = {
        mod_id
        for mod_id, locale, _extension in names
        if locale == normalized_target_locale
    }
    selected_language_names: set[str] = set()
    for mod_id in source_mod_ids | target_mod_ids:
        _raise_if_cancelled(cancel)
        source_name = _locale_name(names, mod_id, source_locale)
        target_name = _locale_name(names, mod_id, target_locale)
        if source_name:
            selected_language_names.add(source_name)
        if target_name:
            selected_language_names.add(target_name)

    readable_declared_bytes = 0
    for language_name in selected_language_names:
        _raise_if_cancelled(cancel)
        member_size = declared_size(language_name)
        if (
            member_size >= 0
            and (
                language_file_limit is None
                or member_size <= language_file_limit
            )
        ):
            readable_declared_bytes += member_size
    if (
        source_language_limit is not None
        and readable_declared_bytes > source_language_limit
    ):
        warnings.append(
            f"{container_label}: 対象言語ファイルの合計が言語資産ごとの上限を超えるため"
            f"言語用語をスキップしました ({readable_declared_bytes} > "
            f"{source_language_limit} bytes)"
        )
        return [], warnings, 0, [], []
    if isinstance(scan_language_budget, _SharedScanBudget):
        scan_language_budget.reserve_language_bytes(readable_declared_bytes)
        available_scan_budget = (
            readable_declared_bytes
            if scan_language_budget.language_byte_limit is not None
            else None
        )
    else:
        available_scan_budget = (
            total_language_limit
            if scan_language_budget is None
            else max(0, scan_language_budget)
        )
        if readable_declared_bytes > available_scan_budget:
            raise _ScanLanguageBudgetExceeded(
                f"対象言語ファイルの全走査上限 {total_language_limit} bytes に達しました"
            )

    language_bytes_read = 0
    language_cache: dict[str, _ParsedLanguage] = {}
    language_errors: dict[str, Exception] = {}

    def read_language_values(name: str) -> _ParsedLanguage:
        nonlocal language_bytes_read
        cached = language_cache.get(name)
        if cached is not None:
            return cached
        cached_error = language_errors.get(name)
        if cached_error is not None:
            raise cached_error
        _raise_if_cancelled(cancel)
        remaining = _minimum_enabled_budget(
            (
                None
                if source_language_limit is None
                else source_language_limit - language_bytes_read
            ),
            (
                None
                if available_scan_budget is None
                else available_scan_budget - language_bytes_read
            ),
        )
        try:
            data = read_bytes(name, remaining)
            language_bytes_read += len(data)
            _raise_if_cancelled(cancel)
            parsed = _read_lang_bytes(data, Path(name).suffix)
        except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
            language_errors[name] = exc
            raise
        if parsed.duplicate_keys:
            warnings.append(
                _duplicate_language_key_warning(
                    container_label,
                    name,
                    parsed.duplicate_keys,
                )
            )
        language_cache[name] = parsed
        return parsed

    pending_warnings: list[_PendingTerminologyWarning] = []
    entries = _language_pair_entries(
        names,
        _resource_backed_terminology_keys(archive_names),
        container_label,
        source_locale,
        target_locale,
        read_language_values,
        warnings,
        cancel,
        source_tier=source_tier,
        pending_warnings=pending_warnings,
    )
    target_only = _collect_target_only_language_evidence(
        names,
        source_mod_ids,
        target_mod_ids,
        source_locale,
        target_locale,
        read_language_values,
        warnings,
        container_label,
        source_tier,
        cancel,
    )
    _raise_if_cancelled(cancel)
    return entries, warnings, language_bytes_read, target_only, pending_warnings


def _collect_target_only_language_evidence(
    names: dict[tuple[str, str, str], str],
    source_mod_ids: set[str],
    target_mod_ids: set[str],
    source_locale: str,
    target_locale: str,
    read_language_values: Callable[[str], _ParsedLanguage],
    warnings: list[str],
    container_label: str,
    source_tier: _SourceTier,
    cancel: Event | None = None,
) -> list[_TargetOnlyLanguageEvidence]:
    target_only: list[_TargetOnlyLanguageEvidence] = []
    for mod_id in sorted(target_mod_ids):
        _raise_if_cancelled(cancel)
        target_name = _locale_name(names, mod_id, target_locale)
        if target_name is None:
            continue
        target_provenance = f"{container_label}!/{target_name}"
        source_keys = frozenset()
        source_name = _locale_name(names, mod_id, source_locale)
        if source_name is not None:
            try:
                source_keys = read_language_values(source_name).keys
            except (
                OSError,
                UnicodeError,
                zipfile.BadZipFile,
                RuntimeError,
                ValueError,
            ):
                # A present but unreadable source file cannot prove that a
                # target key is actually an override. The normal pair pass has
                # already emitted the actionable read warning; do not turn all
                # target keys into target-only candidates.
                continue
        try:
            target_language = read_language_values(target_name)
        except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
            if mod_id not in source_mod_ids:
                warnings.append(
                    f"{container_label}!/{target_name}: 翻訳先言語だけのファイルを"
                    f"読めないため、他の原文言語資産との用語照合に利用していません ({exc})"
                )
            continue
        independent_fixed_targets = _independent_fixed_target_labels(
            target_language.values.values(),
            cancel,
        )
        for entry_index, (key, raw_target) in enumerate(target_language.values.items()):
            if entry_index % 1024 == 0:
                _raise_if_cancelled(cancel)
            if (
                key.casefold() in source_keys
                or not key.casefold().startswith(_TERM_KEY_PREFIXES)
            ):
                continue
            target_only.append(
                _TargetOnlyLanguageEvidence(
                    mod_id=mod_id,
                    key=key,
                    raw_target=raw_target,
                    provenance=target_provenance,
                    container_label=container_label,
                    independent_fixed_targets=independent_fixed_targets,
                    source_tier=source_tier,
                )
            )
    _raise_if_cancelled(cancel)
    return target_only


def _locale_name(names: dict[tuple[str, str, str], str], mod_id: str, locale: str) -> str | None:
    locale = _normalize_locale(locale)
    return names.get((mod_id, locale, "json")) or names.get((mod_id, locale, "lang"))


def _normalize_locale(locale: str) -> str:
    return locale.casefold().replace("-", "_")


def _skip_json_whitespace(text: str, start: int) -> int:
    index = start
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _reject_nonstandard_json_constant(constant: str) -> object:
    raise ValueError(
        f"言語JSONにJSON標準外の定数 {constant!r} があるため読み取れません"
    )


def _json_container_stack_at(text: str, end: int) -> tuple[str, ...] | None:
    """Return open JSON containers before ``end`` without accepting JSON syntax.

    This lexer is only a structural guard for the single-comma recovery below;
    the repaired document must still pass Python's strict JSON decoder in full.
    """

    if not (0 <= end <= len(text)):
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(end):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            elif ord(character) < 0x20:
                return None
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            stack.append("{")
        elif character == "[":
            stack.append("[")
        elif character == "}":
            if not stack or stack.pop() != "{":
                return None
        elif character == "]":
            if not stack or stack.pop() != "[":
                return None
        elif ord(character) < 0x20 and character not in " \t\r\n":
            return None
    if in_string or escaped:
        return None
    return tuple(stack)


def _recover_single_missing_root_string_comma(
    text: str,
    error: json.JSONDecodeError,
) -> _JsonObjectPairs | None:
    """Recover one missing comma between root-level string language entries.

    The archive member is never changed.  Recovery is allowed only when the
    decoder points at the next string key, the previous root value visibly
    ends as a string, the next value is also a string, and inserting exactly
    one comma makes the entire document valid strict JSON.
    """

    if error.msg != "Expecting ',' delimiter" or not (0 <= error.pos <= len(text)):
        return None
    next_key_start = error.pos
    if next_key_start >= len(text) or text[next_key_start] != '"':
        return None
    previous_end = next_key_start - 1
    while previous_end >= 0 and text[previous_end] in " \t\r\n":
        previous_end -= 1
    if previous_end < 0 or text[previous_end] != '"':
        return None
    if _json_container_stack_at(text, next_key_start) != ("{",):
        return None

    decoder = json.JSONDecoder(parse_constant=_reject_nonstandard_json_constant)
    try:
        next_key, key_end = decoder.raw_decode(text, next_key_start)
        if not isinstance(next_key, str):
            return None
        colon = _skip_json_whitespace(text, key_end)
        if colon >= len(text) or text[colon] != ":":
            return None
        value_start = _skip_json_whitespace(text, colon + 1)
        next_value, _value_end = decoder.raw_decode(text, value_start)
        if not isinstance(next_value, str):
            return None
    except json.JSONDecodeError:
        return None

    repaired = text[:next_key_start] + "," + text[next_key_start:]
    try:
        loaded = json.loads(
            repaired,
            object_pairs_hook=_JsonObjectPairs,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, _JsonObjectPairs) else None


def _read_lang_bytes(data: bytes, extension: str) -> _ParsedLanguage:
    text = data.decode("utf-8-sig")
    if extension.lower() == ".json":
        decode_error: json.JSONDecodeError | None = None
        try:
            loaded = json.loads(
                text,
                object_pairs_hook=_JsonObjectPairs,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except json.JSONDecodeError as exc:
            loaded = _recover_single_missing_root_string_comma(text, exc)
            if loaded is None:
                decode_error = exc
        if decode_error is not None:
            explanations = {
                "Expecting ',' delimiter": (
                    "項目同士を区切るカンマ「,」またはJSONを閉じる記号がありません"
                ),
                "Expecting ':' delimiter": "キーと値を区切るコロン「:」がありません",
                "Expecting property name enclosed in double quotes": (
                    "キーがダブルクォートで囲まれていないか、末尾に余分なカンマがあります"
                ),
                "Expecting value": "値がないか、値の書式が正しくありません",
                "Extra data": "JSONオブジェクトの後ろに余分なデータがあります",
                "Unterminated string starting at": "文字列を閉じるダブルクォートがありません",
                "Invalid control character at": "文字列内に未エスケープの制御文字があります",
            }
            explanation = explanations.get(
                decode_error.msg,
                f"JSONとして解釈できない記述があります（詳細: {decode_error.msg}）",
            )
            raise ValueError(
                "言語JSONの構文エラー"
                f"（{decode_error.lineno}行{decode_error.colno}列）: {explanation}"
            ) from decode_error
        if not isinstance(loaded, _JsonObjectPairs):
            raise ValueError("言語 JSON のルートがオブジェクトではありません")
        if len(loaded) > _MAX_LANGUAGE_ENTRIES_PER_MEMBER:
            raise ValueError(
                f"言語entry数が上限を超えています ({len(loaded)} > {_MAX_LANGUAGE_ENTRIES_PER_MEMBER})"
            )
        key_counts = Counter(key for key, _value in loaded)
        duplicate_keys = tuple(sorted(key for key, count in key_counts.items() if count > 1))
        duplicate_key_set = set(duplicate_keys)
        values = {
            key: value
            for key, value in loaded
            if key not in duplicate_key_set and isinstance(value, str)
        }
        return _ParsedLanguage(
            values=values,
            keys=frozenset(key.casefold() for key in key_counts),
            duplicate_keys=duplicate_keys,
        )
    result: dict[str, str] = {}
    all_keys: set[str] = set()
    duplicate_keys: set[str] = set()
    parsed_entries = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        parsed_entries += 1
        if parsed_entries > _MAX_LANGUAGE_ENTRIES_PER_MEMBER:
            raise ValueError(
                f"言語entry数が上限を超えています ({parsed_entries} > {_MAX_LANGUAGE_ENTRIES_PER_MEMBER})"
            )
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in all_keys:
            duplicate_keys.add(normalized_key)
            result.pop(normalized_key, None)
            continue
        all_keys.add(normalized_key)
        result[normalized_key] = value.replace("\\n", "\n")
    return _ParsedLanguage(
        values=result,
        keys=frozenset(key.casefold() for key in all_keys),
        duplicate_keys=tuple(sorted(duplicate_keys)),
    )


def _duplicate_language_key_warning(
    jar_name: str,
    language_name: str,
    duplicate_keys: tuple[str, ...],
) -> str:
    preview_limit = 20
    preview = ", ".join(_safe_warning_key(key) for key in duplicate_keys[:preview_limit])
    omitted = len(duplicate_keys) - preview_limit
    if omitted > 0:
        preview += f" ほか{omitted}件"
    return (
        f"{jar_name}!/{language_name}: 言語keyが重複しているため、重複した"
        f"{len(duplicate_keys)}件のkeyだけを除外しました: {preview}。"
        "同じ言語ファイル内の他の用語は保護に利用します"
    )


def _safe_warning_key(key: str) -> str:
    """Render an untrusted language key without control characters or huge logs."""

    rendered = json.dumps(key, ensure_ascii=True)
    maximum_length = 120
    if len(rendered) <= maximum_length:
        return rendered
    return rendered[: maximum_length - 4] + '..."'


def _safe_warning_location(location: str) -> str:
    """Render an untrusted file location without log injection or path amplification."""

    rendered = json.dumps(location, ensure_ascii=False)
    maximum_length = 240
    if len(rendered) <= maximum_length:
        return rendered
    return rendered[: maximum_length - 4] + '..."'


def _resource_backed_terminology_keys(archive_names: dict[str, str]) -> frozenset[str]:
    """Return item/block language keys proven by resource files in the JAR."""

    result: set[str] = set()
    for normalized_path in archive_names:
        item_match = _ITEM_MODEL_PATH.fullmatch(normalized_path)
        if item_match:
            namespace, resource_path = item_match.groups()
            result.add(f"item.{namespace}.{resource_path.replace('/', '.')}")
            continue
        block_match = _BLOCKSTATE_PATH.fullmatch(normalized_path)
        if block_match:
            namespace, resource_path = block_match.groups()
            result.add(f"block.{namespace}.{resource_path.replace('/', '.')}")
    return frozenset(result)


def _is_terminology_key(
    key: str,
    available_keys: frozenset[str],
    source_labels: dict[str, str],
    resource_backed_keys: frozenset[str],
) -> bool:
    """Accept registry labels while rejecting descriptions and child UI labels."""

    normalized = key.casefold()
    if not normalized.startswith(_TERM_KEY_PREFIXES):
        return False

    # A matching item model or blockstate is stronger evidence than a suffix:
    # dots are legal in real registry paths, including names such as ``manual.desc``.
    if normalized in resource_backed_keys:
        return True

    # Description/tooltip/lore keys are prose even when a companion base key
    # is absent. Split underscores and numeric suffixes as used by many Mods.
    key_segments = _TERM_KEY_TOKEN_SPLIT.split(normalized)
    dotted_segments = normalized.split(".")
    if any(_is_descriptive_key_segment(segment) for segment in key_segments[2:]) or any(
        segment in _DOTTED_DESCRIPTIVE_TERM_KEY_SEGMENTS for segment in dotted_segments[2:]
    ):
        return False

    if not normalized.startswith(_REGISTRY_TERM_KEY_PREFIXES):
        return not _looks_like_prose_value(source_labels.get(normalized, ""))

    parent: str | None = None
    dot_index = normalized.rfind(".")
    while dot_index > 0:
        candidate = normalized[:dot_index]
        if candidate in available_keys and candidate.startswith(_REGISTRY_TERM_KEY_PREFIXES):
            parent = candidate
            break
        dot_index = normalized.rfind(".", 0, dot_index)
    if parent is None:
        return not _looks_like_prose_value(source_labels.get(normalized, ""))

    child_label = source_labels.get(normalized, "")
    parent_label = source_labels.get(parent, "")
    if _label_contains_term(child_label, parent_label) and _looks_like_short_term_label(
        child_label
    ):
        # Examples: Lunarian -> Lunarian Armorer, Shield -> Red Shield.
        return True

    suffix_tokens = tuple(
        token for token in _TERM_KEY_TOKEN_SPLIT.split(normalized[len(parent) + 1 :]) if token
    )
    if (
        len(suffix_tokens) > 1
        and suffix_tokens[0] in _DERIVED_TERM_KEY_SEGMENTS
        and _looks_like_short_term_label(child_label)
    ):
        # Some subtype names do not repeat their base label, for example
        # ``terrapin.variant_koopa = Koopa``.
        return True

    # A child with neither resource evidence nor a semantic relationship to
    # its base is normally a book section, mode label, or other UI fragment.
    return False


def _is_descriptive_key_segment(segment: str) -> bool:
    without_numeric_suffix = segment.rstrip("0123456789")
    return without_numeric_suffix in _DESCRIPTIVE_TERM_KEY_SEGMENTS


def _label_contains_term(label: str, term: str) -> bool:
    label = " ".join(label.split())
    term = " ".join(term.split())
    if not label or not term:
        return False
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
            label,
            re.IGNORECASE,
        )
    )


def _looks_like_short_term_label(value: str) -> bool:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 60 or len(normalized.split()) > 8:
        return False
    if normalized.endswith((".", "!", "?", ":", ";")):
        return False
    return not protected_syntax_signature(normalized)


def _looks_like_prose_value(value: str) -> bool:
    """Reject sentence-like language values that are not resource-backed names."""

    if "\n" in value or "\r" in value:
        return True
    normalized = " ".join(value.split())
    word_count = len(normalized.split())
    if word_count >= 9:
        return True
    # A directly attached sentence terminator is prose evidence. A separated
    # glyph such as ``Light Blue Rune .`` can be an intentional symbol name.
    return word_count >= 4 and bool(re.search(r"\S[.!?…]\Z", normalized))


def _usable_term(value: object) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    if len(value) > 100:
        return False
    if len(value) == 2:
        # Preserve compact technical abbreviations such as XP, RF, AE, and
        # 3D without turning arbitrary two-letter prose into glossary terms.
        return bool(re.fullmatch(r"[A-Z0-9]{2}", value)) and any(
            character.isalpha() for character in value
        )
    if len(value) < 3:
        return False
    return any(character.isalpha() for character in value)
