from __future__ import annotations

import json
import os
import re
import stat
import tomllib
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .domain import LocalizerError


_MINECRAFT_VERSION = re.compile(
    r"^(?:"
    r"1\.(?:0|[1-9]\d?)(?:\.(?:0|[1-9]\d*))?"
    r"|(?:2[6-9]|[3-9]\d)\.(?:0|[1-9]\d?)(?:\.(?:0|[1-9]\d*))?"
    r")$"
)
_FTB_QUESTS_JAR = re.compile(r"(?:^|[-_.])ftb[-_]?quests(?:[-_.]|$)", re.IGNORECASE)
_FTB_VERSION_CODE = re.compile(
    r"ftb[-_]?quests(?:[-_.](?:forge|neoforge|fabric))?[-_.](?P<code>\d{4})(?:[-_.]|$)",
    re.IGNORECASE,
)
_FTB_CALENDAR_VERSION = re.compile(
    r"ftb[-_]?quests(?:[-_.](?:forge|neoforge|fabric))?[-_.]"
    r"(?P<year>2[6-9]|[3-9]\d)\."
    r"(?P<minor>0|[1-9]\d?)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:\.(?:0|[1-9]\d*))?(?!\.\d)(?:[-_.]|$)",
    re.IGNORECASE,
)
_MAX_METADATA_BYTES = 2 * 1024 * 1024
_MAX_CFG_BYTES = 256 * 1024
_MAX_JAR_BYTES = 256 * 1024 * 1024
_MAX_JAR_METADATA_BYTES = 1024 * 1024
_MAX_JAR_MEMBERS = 100_000
_MAX_MOD_ENTRIES = 20_000


class InstanceInspectionError(LocalizerError):
    """The selected path cannot be treated as a safe modpack instance."""


@dataclass(frozen=True, slots=True)
class VersionEvidence:
    detector: str
    source: Path
    field: str
    value: str
    priority: int


@dataclass(frozen=True, slots=True)
class InstanceInfo:
    selected_root: Path
    instance_root: Path
    game_root: Path
    mods_path: Path
    minecraft_version: str
    detected_by: VersionEvidence | None
    evidence: tuple[VersionEvidence, ...]
    warnings: tuple[str, ...]


def inspect_instance_root(path: Path) -> InstanceInfo:
    """Validate a selected instance root and detect its Minecraft version.

    Detection is intentionally conservative: only an exact legacy
    ``1.x[.y]`` or calendar-style ``26.x[.y]`` value from a Minecraft-specific
    field is accepted. Generic top-level ``version`` values are pack or
    launcher versions and are never treated as Minecraft.
    """

    selected = _absolute(Path(path).expanduser())
    _require_safe_directory(selected, "選択したModpack instance")
    if selected.name.lower() == "mods":
        raise InstanceInspectionError(
            "modsフォルダーではなく、その親のModpack instanceを選択してください"
        )

    instance_root, game_root = _locate_game_root(selected)
    mods_path = game_root / "mods"
    warnings: list[str] = []
    if _path_lexically_exists(mods_path):
        _require_safe_directory(mods_path, "modsフォルダー")
    else:
        warnings.append(f"modsフォルダーが見つかりません: {mods_path}")

    evidence: list[VersionEvidence] = []
    seen_metadata: set[Path] = set()

    def inspect_json(
        metadata_path: Path,
        detector: str,
        priority: int,
        extractor: Callable[[Any], list[tuple[str, object]]],
    ) -> None:
        normalized = _absolute(metadata_path)
        if normalized in seen_metadata:
            return
        seen_metadata.add(normalized)
        loaded = _read_json_metadata(normalized, warnings)
        if loaded is None:
            return
        for field, raw_value in extractor(loaded):
            version = _exact_minecraft_version(raw_value)
            if version:
                evidence.append(
                    VersionEvidence(detector, normalized, field, version, priority)
                )

    inspect_json(
        instance_root / "mmc-pack.json",
        "Prism/MultiMC mmc-pack.json",
        10,
        _extract_mmc_pack,
    )
    evidence.extend(_read_instance_cfg(instance_root / "instance.cfg", warnings))
    for metadata_root in _unique_paths((instance_root, game_root)):
        inspect_json(
            metadata_root / "minecraftinstance.json",
            "CurseForge minecraftinstance.json",
            30,
            _extract_minecraft_instance,
        )
        inspect_json(
            metadata_root / "manifest.json",
            "CurseForge manifest.json",
            40,
            _extract_curse_manifest,
        )

    generic_names = ("profile.json", "instance.json", "modpack.json")
    for root in _unique_paths((instance_root, game_root)):
        for name in generic_names:
            inspect_json(
                root / name,
                f"profile/instance metadata ({name})",
                50,
                _extract_generic_profile,
            )

    if not evidence and mods_path.is_dir():
        evidence.extend(_inspect_ftb_quests_jars(mods_path, warnings))

    chosen = _choose_evidence(evidence, warnings)
    if chosen is None:
        warnings.append("Minecraftバージョンを安全に自動検出できませんでした")

    return InstanceInfo(
        selected_root=selected,
        instance_root=instance_root,
        game_root=game_root,
        mods_path=mods_path,
        minecraft_version=chosen.value if chosen else "",
        detected_by=chosen,
        evidence=tuple(sorted(evidence, key=_evidence_sort_key)),
        warnings=tuple(warnings),
    )


def _locate_game_root(selected: Path) -> tuple[Path, Path]:
    if selected.name.lower() in {"minecraft", ".minecraft"}:
        parent = selected.parent
        if _has_launcher_metadata(parent):
            return parent, selected
        return selected, selected

    child_candidates: list[Path] = []
    for name in ("minecraft", ".minecraft"):
        child = selected / name
        if not _path_lexically_exists(child):
            continue
        _require_safe_directory(child, f"game root候補 {name}")
        child_candidates.append(child)
    if len(child_candidates) > 1:
        raise InstanceInspectionError(
            "instance直下に minecraft と .minecraft の両方があり、game rootを一意に判定できません"
        )
    return selected, child_candidates[0] if child_candidates else selected


def _has_launcher_metadata(path: Path) -> bool:
    return any(
        _is_safe_regular_file(path / name)
        for name in (
            "mmc-pack.json",
            "instance.cfg",
            "minecraftinstance.json",
            "manifest.json",
        )
    )


def _read_json_metadata(path: Path, warnings: list[str]) -> Any | None:
    data = _read_safe_file(path, _MAX_METADATA_BYTES, warnings)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, ValueError) as exc:
        warnings.append(f"metadata JSONを解析できません: {path} ({exc})")
        return None


def _read_instance_cfg(path: Path, warnings: list[str]) -> list[VersionEvidence]:
    data = _read_safe_file(path, _MAX_CFG_BYTES, warnings)
    if data is None:
        return []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        warnings.append(f"instance.cfgを解析できません: {path} ({exc})")
        return []
    accepted = {
        "intendedversion": "IntendedVersion",
        "minecraftversion": "MinecraftVersion",
        "minecraft_version": "minecraft_version",
        "gameversion": "GameVersion",
    }
    result: list[VersionEvidence] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        canonical = accepted.get(key.strip().lower())
        version = _exact_minecraft_version(value.strip())
        if canonical and version:
            result.append(
                VersionEvidence(
                    "Prism/MultiMC instance.cfg",
                    path,
                    canonical,
                    version,
                    20,
                )
            )
    return result


def _extract_mmc_pack(value: Any) -> list[tuple[str, object]]:
    if not isinstance(value, dict) or not isinstance(value.get("components"), list):
        return []
    result: list[tuple[str, object]] = []
    for index, component in enumerate(value["components"]):
        if isinstance(component, dict) and component.get("uid") == "net.minecraft":
            result.append((f"components[{index}].version", component.get("version")))
    return result


def _extract_minecraft_instance(value: Any) -> list[tuple[str, object]]:
    if not isinstance(value, dict):
        return []
    result = _explicit_top_level_versions(value)
    loader = value.get("baseModLoader")
    if isinstance(loader, dict):
        result.append(("baseModLoader.minecraftVersion", loader.get("minecraftVersion")))
    minecraft = value.get("minecraft")
    if isinstance(minecraft, dict):
        result.append(("minecraft.version", minecraft.get("version")))
    return result


def _extract_curse_manifest(value: Any) -> list[tuple[str, object]]:
    if not isinstance(value, dict):
        return []
    minecraft = value.get("minecraft")
    if not isinstance(minecraft, dict):
        return []
    # Deliberately ignore manifest.version: it is the modpack release version.
    return [("minecraft.version", minecraft.get("version"))]


def _extract_generic_profile(value: Any) -> list[tuple[str, object]]:
    if not isinstance(value, dict):
        return []
    result = _explicit_top_level_versions(value)
    minecraft = value.get("minecraft")
    if isinstance(minecraft, dict):
        result.extend(
            (
                ("minecraft.version", minecraft.get("version")),
                ("minecraft.minecraftVersion", minecraft.get("minecraftVersion")),
            )
        )
    result.append(("lastVersionId", value.get("lastVersionId")))
    # A bare "version" field is intentionally never considered.
    return result


def _explicit_top_level_versions(value: dict[str, Any]) -> list[tuple[str, object]]:
    return [
        (field, value.get(field))
        for field in (
            "minecraftVersion",
            "minecraft_version",
            "gameVersion",
            "game_version",
        )
    ]


def _inspect_ftb_quests_jars(
    mods_path: Path,
    warnings: list[str],
) -> list[VersionEvidence]:
    candidates: list[Path] = []
    try:
        with os.scandir(mods_path) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_MOD_ENTRIES:
                    warnings.append(
                        f"modsフォルダーの項目数が上限 {_MAX_MOD_ENTRIES} を超えたため走査を中止しました"
                    )
                    break
                if not entry.name.lower().endswith(".jar") or not _FTB_QUESTS_JAR.search(entry.name):
                    continue
                path = Path(entry.path)
                value = entry.stat(follow_symlinks=False)
                if _is_reparse_point(path, value):
                    warnings.append(f"FTB Quests JARのlinkを追跡せずスキップしました: {path}")
                    continue
                if stat.S_ISREG(value.st_mode):
                    candidates.append(path)
    except OSError as exc:
        warnings.append(f"modsフォルダーを走査できません: {mods_path} ({exc})")
        return []

    result: list[VersionEvidence] = []
    for jar in sorted(candidates, key=lambda item: (item.name.casefold(), item.name)):
        metadata_versions, is_ftb_quests = _read_ftb_jar_metadata(jar, warnings)
        if not is_ftb_quests:
            warnings.append(
                f"FTB Questsとしてmetadata確認できないためfilename fallbackを使用しません: {jar}"
            )
            continue
        result.extend(
            VersionEvidence("FTB Quests JAR metadata", jar, field, version, 80)
            for field, version in metadata_versions
        )
        filename_version = _version_from_ftb_filename(jar.name)
        if filename_version:
            result.append(
                VersionEvidence(
                    "FTB Quests JAR filename fallback",
                    jar,
                    "FTB version code",
                    filename_version,
                    90,
                )
            )
    return result


def _read_ftb_jar_metadata(
    path: Path,
    warnings: list[str],
) -> tuple[list[tuple[str, str]], bool]:
    try:
        size = path.stat().st_size
        if size > _MAX_JAR_BYTES:
            warnings.append(f"FTB Quests JARが大きすぎるためmetadataを読みません: {path}")
            return [], False
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_JAR_MEMBERS:
                warnings.append(f"FTB Quests JARの項目数が多すぎます: {path}")
                return [], False
            by_name: dict[str, zipfile.ZipInfo] = {}
            for member in members:
                normalized_name = member.filename.casefold()
                if normalized_name in by_name:
                    raise ValueError(f"重複archive member: {member.filename}")
                by_name[normalized_name] = member
            result: list[tuple[str, str]] = []
            is_ftb_quests = False
            fabric = by_name.get("fabric.mod.json")
            if fabric is not None:
                data = _read_zip_member(archive, fabric)
                loaded = json.loads(
                    data.decode("utf-8-sig"),
                    object_pairs_hook=_unique_json_object,
                )
                if isinstance(loaded, dict) and _is_ftb_quests_mod_id(loaded.get("id")):
                    is_ftb_quests = True
                if (
                    is_ftb_quests
                    and isinstance(loaded, dict)
                    and isinstance(loaded.get("depends"), dict)
                ):
                    raw = loaded["depends"].get("minecraft")
                    values = raw if isinstance(raw, list) else [raw]
                    for index, item in enumerate(values):
                        version = _exact_minecraft_version(item)
                        if version:
                            result.append((f"fabric.depends.minecraft[{index}]", version))
            for metadata_name in ("meta-inf/neoforge.mods.toml", "meta-inf/mods.toml"):
                member = by_name.get(metadata_name)
                if member is None:
                    continue
                loaded_toml = tomllib.loads(_read_zip_member(archive, member).decode("utf-8-sig"))
                if _toml_declares_ftb_quests(loaded_toml):
                    is_ftb_quests = True
                    result.extend(_extract_toml_minecraft_versions(loaded_toml, metadata_name))
            for metadata_name in ("mcmod.info", "meta-inf/mcmod.info"):
                member = by_name.get(metadata_name)
                if member is None:
                    continue
                legacy_versions, declares_ftb_quests = _extract_legacy_forge_metadata(
                    _read_zip_member(archive, member),
                    metadata_name,
                )
                if declares_ftb_quests:
                    is_ftb_quests = True
                    result.extend(legacy_versions)
            return result, is_ftb_quests
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        warnings.append(f"FTB Quests JAR metadataを解析できません: {path} ({exc})")
        return [], False


def _extract_toml_minecraft_versions(
    value: dict[str, Any],
    metadata_name: str,
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for field in ("minecraftVersion", "minecraft_version", "mcVersion", "mcversion"):
        version = _exact_minecraft_version(value.get(field))
        if version:
            result.append((f"{metadata_name}:{field}", version))
    dependencies = value.get("dependencies")
    if isinstance(dependencies, dict):
        for dependency_list in dependencies.values():
            if not isinstance(dependency_list, list):
                continue
            for index, dependency in enumerate(dependency_list):
                if not isinstance(dependency, dict) or dependency.get("modId") != "minecraft":
                    continue
                version = _exact_minecraft_version(dependency.get("versionRange"))
                if version:
                    result.append(
                        (f"{metadata_name}:dependencies.minecraft[{index}].versionRange", version)
                    )
    return result


def _toml_declares_ftb_quests(value: dict[str, Any]) -> bool:
    mods = value.get("mods")
    return isinstance(mods, list) and any(
        isinstance(item, dict) and _is_ftb_quests_mod_id(item.get("modId"))
        for item in mods
    )


def _extract_legacy_forge_metadata(
    data: bytes,
    metadata_name: str,
) -> tuple[list[tuple[str, str]], bool]:
    loaded = json.loads(
        data.decode("utf-8-sig"),
        object_pairs_hook=_unique_json_object,
    )
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(loaded, list):
        raw_entries = loaded
        field_prefix = ""
    elif isinstance(loaded, dict) and "modList" in loaded:
        raw_entries = loaded["modList"]
        if not isinstance(raw_entries, list):
            raise ValueError(f"{metadata_name}: modListが配列ではありません")
        field_prefix = "modList"
    elif isinstance(loaded, dict):
        raw_entries = [loaded]
        field_prefix = "single"
    else:
        raise ValueError(f"{metadata_name}: JSON rootがobjectまたはarrayではありません")

    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{metadata_name}: mod entry[{index}]がobjectではありません")
        if field_prefix == "single":
            field = "mcversion"
        elif field_prefix:
            field = f"{field_prefix}[{index}].mcversion"
        else:
            field = f"[{index}].mcversion"
        entries.append((field, raw_entry))

    result: list[tuple[str, str]] = []
    declares_ftb_quests = False
    for field, entry in entries:
        # Legacy Forge calls this field "modid".  Do not infer identity from
        # the display name, filename, or similarly-spelled identifiers.
        if entry.get("modid") != "ftbquests":
            continue
        declares_ftb_quests = True
        version = _exact_minecraft_version(entry.get("mcversion"))
        if version:
            result.append((f"{metadata_name}:{field}", version))
    return result, declares_ftb_quests


def _is_ftb_quests_mod_id(value: object) -> bool:
    return isinstance(value, str) and value.lower().replace("_", "") == "ftbquests"


def _read_zip_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    if member.flag_bits & 0x1:
        raise ValueError(f"暗号化されたmetadataです: {member.filename}")
    if member.file_size > _MAX_JAR_METADATA_BYTES:
        raise ValueError(f"metadataが大きすぎます: {member.filename}")
    with archive.open(member) as handle:
        data = handle.read(_MAX_JAR_METADATA_BYTES + 1)
    if len(data) > _MAX_JAR_METADATA_BYTES:
        raise ValueError(f"metadataが大きすぎます: {member.filename}")
    return data


def _version_from_ftb_filename(filename: str) -> str | None:
    calendar_match = _FTB_CALENDAR_VERSION.search(filename)
    if calendar_match:
        return ".".join(
            calendar_match.group(part) for part in ("year", "minor", "patch")
        )

    match = _FTB_VERSION_CODE.search(filename)
    if not match:
        return None
    code = match.group("code")
    minor = int(code[:2])
    patch = int(code[2:])
    # FTB's historical code is MMpp (for example 2001 -> Minecraft 1.20.1).
    # Keep the accepted minor range deliberately narrow so arbitrary four
    # digit pack releases cannot masquerade as a future Minecraft version.
    if not 7 <= minor <= 30:
        return None
    return f"1.{minor}.{patch}"


def _choose_evidence(
    evidence: list[VersionEvidence],
    warnings: list[str],
) -> VersionEvidence | None:
    if not evidence:
        return None
    ordered = sorted(evidence, key=_evidence_sort_key)
    best_priority = ordered[0].priority
    best = [item for item in ordered if item.priority == best_priority]
    best_versions = {item.value for item in best}
    if len(best_versions) != 1:
        details = ", ".join(sorted(best_versions))
        warnings.append(f"同順位の検出根拠が競合しています: {details}")
        return None
    chosen = best[0]
    conflicts = sorted({item.value for item in ordered if item.value != chosen.value})
    if conflicts:
        warnings.append(
            f"優先度の低い検出根拠とMinecraftバージョンが競合しています: "
            f"採用={chosen.value}, 他={', '.join(conflicts)}"
        )
    return chosen


def _read_safe_file(
    path: Path,
    limit: int,
    warnings: list[str],
) -> bytes | None:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        warnings.append(f"metadataを確認できません: {path} ({exc})")
        return None
    if _is_reparse_point(path, value):
        warnings.append(f"metadataのsymlink/junctionを追跡せずスキップしました: {path}")
        return None
    if not stat.S_ISREG(value.st_mode):
        warnings.append(f"metadataが通常ファイルではないためスキップしました: {path}")
        return None
    if value.st_size > limit:
        warnings.append(f"metadataが大きすぎるためスキップしました: {path}")
        return None
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        warnings.append(f"metadataを読み込めません: {path} ({exc})")
        return None
    if len(data) > limit:
        warnings.append(f"metadataが大きすぎるためスキップしました: {path}")
        return None
    return data


def _require_safe_directory(path: Path, description: str) -> None:
    try:
        value = path.lstat()
    except FileNotFoundError as exc:
        raise InstanceInspectionError(f"{description}が存在しません: {path}") from exc
    except OSError as exc:
        raise InstanceInspectionError(f"{description}を確認できません: {path} ({exc})") from exc
    if _is_reparse_point(path, value):
        raise InstanceInspectionError(
            f"{description}がsymlinkまたはjunctionのため、外部へ追跡しません: {path}"
        )
    if not stat.S_ISDIR(value.st_mode):
        raise InstanceInspectionError(f"{description}にはフォルダーを指定してください: {path}")


def _is_safe_regular_file(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(value.st_mode) and not _is_reparse_point(path, value)


def _path_lexically_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _is_reparse_point(path: Path, value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _exact_minecraft_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _MINECRAFT_VERSION.fullmatch(candidate) else None


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _unique_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in paths:
        if path not in result:
            result.append(path)
    return tuple(result)


def _evidence_sort_key(item: VersionEvidence) -> tuple[int, str, str]:
    return item.priority, str(item.source).casefold(), item.field


def _unique_json_object(pairs: list[tuple[str, object]]) -> OrderedDict[str, object]:
    result: OrderedDict[str, object] = OrderedDict()
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重複JSONキー: {key}")
        result[key] = value
    return result


__all__ = [
    "InstanceInfo",
    "InstanceInspectionError",
    "VersionEvidence",
    "inspect_instance_root",
]
