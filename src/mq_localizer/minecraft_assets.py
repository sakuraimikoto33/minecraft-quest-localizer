from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


_MAX_METADATA_BYTES = 8 * 1024 * 1024
_MAX_ASSET_INDEX_BYTES = 32 * 1024 * 1024
_MAX_MINECRAFT_LANGUAGE_BYTES = 16 * 1024 * 1024
_MAX_CLIENT_JAR_BYTES = 256 * 1024 * 1024
_MAX_LANGUAGE_ENTRIES = 250_000
_CLIENT_LANGUAGE_MEMBER = "assets/minecraft/lang/{locale}.json"


MinecraftAssetObserver = Callable[[Path, str], None]


@dataclass(frozen=True, slots=True)
class MinecraftLanguageBundle:
    source_values: dict[str, str]
    target_values: dict[str, str]
    source_provenance: str
    target_provenance: str
    warnings: tuple[str, ...] = ()
    debug_messages: tuple[str, ...] = ()


class _UniqueObject(dict[str, Any]):
    pass


class _DuplicateJSONKeysError(ValueError):
    def __init__(self, keys: tuple[str, ...]) -> None:
        self.keys = keys
        super().__init__(f"JSON keyが{len(keys)}件重複しています")


def load_minecraft_language_bundle(
    instance_root: Path,
    minecraft_version: str,
    source_locale: str,
    target_locale: str,
    *,
    asset_observer: MinecraftAssetObserver | None = None,
) -> MinecraftLanguageBundle | None:
    """Load version-matched local Minecraft language assets without network I/O.

    PrismLauncher/MultiMC and the official launcher store the English language
    in the client JAR and downloaded locales in a content-addressed asset
    object.  Only a launcher root containing metadata for the detected version
    is considered.  A corrupt candidate is reported, while a machine with no
    discoverable local launcher assets simply continues with Mod terminology.
    A verified source locale remains useful for preserving official names even
    when the requested target locale has not been downloaded.  User-configured
    Mod/KubeJS/resource-pack budgets do not apply here; Minecraft assets use
    their own fixed integrity and safety checks.
    """

    version = minecraft_version.strip()
    if not version:
        return None
    warnings: list[str] = []
    debug_messages: list[str] = []
    saw_version_candidate = False
    source_only_fallback: tuple[dict[str, str], str] | None = None
    for root in _launcher_roots(instance_root):
        metadata_path = _version_metadata_path(
            root,
            version,
            asset_observer,
        )
        if metadata_path is None:
            continue
        saw_version_candidate = True
        try:
            metadata = _read_json_object(metadata_path, _MAX_METADATA_BYTES)
            client_jar = _client_jar_path(root, version, asset_observer)
            if client_jar is None:
                raise ValueError("Minecraft client JARが見つかりません")
            _validate_client_jar(client_jar, metadata)
            source_from_client = _read_client_locale(client_jar, source_locale)
        except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
            _record_duplicate_json_keys(
                debug_messages,
                exc,
                f"Minecraft {version} 公式言語資産: {root}",
            )
            warnings.append(
                f"Minecraft {version} の公式言語資産を読めませんでした: "
                f"{root} ({exc})"
            )
            continue
        try:
            index_id = _asset_index_id(metadata)
            if not index_id:
                raise ValueError("version metadataにasset index IDがありません")
            index_path = root / "assets" / "indexes" / f"{index_id}.json"
            _observe_asset_path(asset_observer, index_path, "Minecraft asset index")
            index_data = _read_limited_file(index_path, _MAX_ASSET_INDEX_BYTES)
            _validate_asset_index(index_data, metadata)
            asset_index = _parse_json_object(index_data, index_path)
        except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
            _record_duplicate_json_keys(
                debug_messages,
                exc,
                f"Minecraft {version} asset index: {root}",
            )
            if source_from_client is not None and source_only_fallback is None:
                source_only_fallback = source_from_client
            preservation = (
                " 検証済みの原文公式名は原語保護に利用します。"
                if source_from_client is not None
                else ""
            )
            warnings.append(
                f"Minecraft {version} の公式言語資産を読めませんでした: "
                f"{root} ({exc}){preservation}"
            )
            continue
        try:
            if source_from_client is None:
                source_values, source_provenance = _read_locale(
                    root,
                    client_jar,
                    asset_index,
                    source_locale,
                    asset_observer,
                )
            else:
                source_values, source_provenance = source_from_client
        except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
            _record_duplicate_json_keys(
                debug_messages,
                exc,
                f"Minecraft {version} 原文公式言語 {source_locale}: {root}",
            )
            warnings.append(
                f"Minecraft {version} の原文公式言語 {source_locale} を読めませんでした: "
                f"{root} ({exc})"
            )
            continue
        try:
            target_values, target_provenance = _read_locale(
                root,
                client_jar,
                asset_index,
                target_locale,
                asset_observer,
            )
        except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
            _record_duplicate_json_keys(
                debug_messages,
                exc,
                f"Minecraft {version} 翻訳先公式言語 {target_locale}: {root}",
            )
            warnings.append(
                f"Minecraft {version} の翻訳先公式言語 {target_locale} を読めませんでした。"
                f"検証済みの原文公式名は原語保護に利用します: {root} ({exc})"
            )
            if source_only_fallback is None:
                source_only_fallback = (source_values, source_provenance)
            continue
        return MinecraftLanguageBundle(
            source_values=source_values,
            target_values=target_values,
            source_provenance=source_provenance,
            target_provenance=target_provenance,
            warnings=(),
        )
    if source_only_fallback is not None:
        source_values, source_provenance = source_only_fallback
        return MinecraftLanguageBundle(
            source_values=source_values,
            target_values={},
            source_provenance=source_provenance,
            target_provenance="",
            warnings=tuple(warnings),
            debug_messages=tuple(debug_messages),
        )
    if saw_version_candidate and warnings:
        return MinecraftLanguageBundle(
            {},
            {},
            "",
            "",
            tuple(warnings),
            tuple(debug_messages),
        )
    return None


def _launcher_roots(instance_root: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    current = Path(instance_root).expanduser()
    candidates.extend((current, *tuple(current.parents)[:5]))

    appdata = os.environ.get("APPDATA", "").strip()
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if appdata:
        base = Path(appdata)
        candidates.extend(
            (
                base / "PrismLauncher",
                base / "MultiMC",
                base / ".minecraft",
            )
        )
    if local_appdata:
        base = Path(local_appdata)
        candidates.extend((base / "PrismLauncher", base / "MultiMC"))

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        absolute = candidate.absolute()
        key = os.path.normcase(os.path.normpath(str(absolute)))
        if key in seen:
            continue
        seen.add(key)
        result.append(absolute)
    return tuple(result)


def _version_metadata_path(
    root: Path,
    version: str,
    observer: MinecraftAssetObserver | None = None,
) -> Path | None:
    candidates = (
        root / "meta" / "net.minecraft" / f"{version}.json",
        root / "versions" / version / f"{version}.json",
    )
    for candidate in candidates:
        _observe_asset_path(observer, candidate, "Minecraft version metadata candidate")
    return next((path for path in candidates if path.is_file()), None)


def _client_jar_path(
    root: Path,
    version: str,
    observer: MinecraftAssetObserver | None = None,
) -> Path | None:
    candidates = (
        root
        / "libraries"
        / "com"
        / "mojang"
        / "minecraft"
        / version
        / f"minecraft-{version}-client.jar",
        root / "versions" / version / f"{version}.jar",
    )
    for candidate in candidates:
        _observe_asset_path(observer, candidate, "Minecraft client JAR candidate")
    return next((path for path in candidates if path.is_file()), None)


def _asset_index_id(metadata: dict[str, Any]) -> str:
    asset_index = metadata.get("assetIndex")
    if isinstance(asset_index, dict) and isinstance(asset_index.get("id"), str):
        return asset_index["id"].strip()
    assets = metadata.get("assets")
    return assets.strip() if isinstance(assets, str) else ""


def _validate_client_jar(path: Path, metadata: dict[str, Any]) -> None:
    descriptor: object = None
    main_jar = metadata.get("mainJar")
    if isinstance(main_jar, dict):
        downloads = main_jar.get("downloads")
        if isinstance(downloads, dict):
            descriptor = downloads.get("artifact")
    if descriptor is None:
        downloads = metadata.get("downloads")
        if isinstance(downloads, dict):
            descriptor = downloads.get("client")
    if not isinstance(descriptor, dict):
        return
    expected_hash = descriptor.get("sha1")
    expected_size = descriptor.get("size")
    if not isinstance(expected_hash, str) or len(expected_hash) != 40:
        raise ValueError("client JAR metadataのSHA-1が不正です")
    if type(expected_size) is not int or not (0 <= expected_size <= _MAX_CLIENT_JAR_BYTES):
        raise ValueError("client JAR metadataのsizeが不正です")
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise ValueError("Minecraft client JARのsizeがmetadataと一致しません")
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != expected_hash.casefold():
        raise ValueError("Minecraft client JARのSHA-1がmetadataと一致しません")


def _validate_asset_index(data: bytes, metadata: dict[str, Any]) -> None:
    descriptor = metadata.get("assetIndex")
    if not isinstance(descriptor, dict):
        return
    expected_hash = descriptor.get("sha1")
    expected_size = descriptor.get("size")
    # Some launcher metadata variants expose only the index ID.  Validate
    # whenever integrity metadata is present, and reject a partial/malformed
    # descriptor instead of trusting an unverified content-addressed mapping.
    if expected_hash is None and expected_size is None:
        return
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 40
        or any(character not in "0123456789abcdef" for character in expected_hash.casefold())
    ):
        raise ValueError("asset index metadataのSHA-1が不正です")
    if type(expected_size) is not int or not (0 <= expected_size <= _MAX_ASSET_INDEX_BYTES):
        raise ValueError("asset index metadataのsizeが不正です")
    if len(data) != expected_size:
        raise ValueError("asset indexのsizeがmetadataと一致しません")
    if hashlib.sha1(data).hexdigest() != expected_hash.casefold():
        raise ValueError("asset indexのSHA-1がmetadataと一致しません")


def _read_locale(
    root: Path,
    client_jar: Path,
    asset_index: dict[str, Any],
    locale: str,
    observer: MinecraftAssetObserver | None = None,
) -> tuple[dict[str, str], str]:
    normalized = locale.strip().casefold().replace("-", "_")
    client_locale = _read_client_locale(client_jar, normalized)
    if client_locale is not None:
        return client_locale

    objects = asset_index.get("objects")
    if not isinstance(objects, dict):
        raise ValueError("asset indexのobjectsがobjectではありません")
    descriptor = objects.get(f"minecraft/lang/{normalized}.json")
    if not isinstance(descriptor, dict):
        raise ValueError(f"公式言語 {normalized} がasset indexにありません")
    digest = descriptor.get("hash")
    size = descriptor.get("size")
    if not isinstance(digest, str) or len(digest) != 40 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"公式言語 {normalized} のSHA-1が不正です")
    if type(size) is not int or not (0 <= size <= _MAX_MINECRAFT_LANGUAGE_BYTES):
        raise ValueError(f"公式言語 {normalized} のsizeが不正です")
    object_path = root / "assets" / "objects" / digest[:2] / digest
    _observe_asset_path(
        observer,
        object_path,
        f"Minecraft language object {normalized}",
    )
    data = _read_limited_file(object_path, _MAX_MINECRAFT_LANGUAGE_BYTES)
    if len(data) != size:
        raise ValueError(
            f"公式言語 {normalized} のsizeがasset indexと一致しません"
        )
    if hashlib.sha1(data).hexdigest() != digest:
        raise ValueError(
            f"公式言語 {normalized} のSHA-1がasset indexと一致しません"
        )
    return _parse_language(data, normalized), str(object_path)


def _read_client_locale(
    client_jar: Path,
    locale: str,
) -> tuple[dict[str, str], str] | None:
    normalized = locale.strip().casefold().replace("-", "_")
    member_name = _CLIENT_LANGUAGE_MEMBER.format(locale=normalized)
    try:
        with zipfile.ZipFile(client_jar) as archive:
            info = archive.getinfo(member_name)
            if (
                info.file_size < 0
                or info.file_size > _MAX_MINECRAFT_LANGUAGE_BYTES
            ):
                raise ValueError(
                    f"{member_name} がサイズ上限を超えています ({info.file_size})"
                )
            if (
                info.compress_size < 0
                or info.compress_size > _MAX_MINECRAFT_LANGUAGE_BYTES
            ):
                raise ValueError(
                    f"{member_name} が圧縮サイズ上限を超えています "
                    f"({info.compress_size})"
                )
            with archive.open(info) as member:
                data = member.read(_MAX_MINECRAFT_LANGUAGE_BYTES + 1)
            if len(data) > _MAX_MINECRAFT_LANGUAGE_BYTES:
                raise ValueError(f"{member_name} がサイズ上限を超えています")
        return _parse_language(data, member_name), f"{client_jar}!/{member_name}"
    except KeyError:
        return None


def _observe_asset_path(
    observer: MinecraftAssetObserver | None,
    path: Path,
    label: str,
) -> None:
    if observer is not None:
        observer(path, label)


def _read_json_object(path: Path, maximum: int) -> dict[str, Any]:
    return _parse_json_object(_read_limited_file(path, maximum), path)


def _parse_json_object(data: bytes, label: object) -> dict[str, Any]:
    loaded = json.loads(
        data.decode("utf-8-sig"),
        object_pairs_hook=_unique_object,
    )
    if not isinstance(loaded, _UniqueObject):
        raise ValueError(f"JSON rootがobjectではありません: {label}")
    return dict(loaded)


def _parse_language(data: bytes, label: str) -> dict[str, str]:
    loaded = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    if not isinstance(loaded, _UniqueObject):
        raise ValueError(f"公式言語JSON rootがobjectではありません: {label}")
    if len(loaded) > _MAX_LANGUAGE_ENTRIES:
        raise ValueError(
            f"公式言語entry数が上限を超えています ({len(loaded)})"
        )
    return {key: value for key, value in loaded.items() if isinstance(value, str)}


def _unique_object(pairs: list[tuple[str, Any]]) -> _UniqueObject:
    result = _UniqueObject()
    duplicate_keys: set[str] = set()
    for key, value in pairs:
        if key in result:
            duplicate_keys.add(key)
            result.pop(key, None)
            continue
        if key in duplicate_keys:
            continue
        result[key] = value
    if duplicate_keys:
        raise _DuplicateJSONKeysError(tuple(sorted(duplicate_keys)))
    return result


def _record_duplicate_json_keys(
    messages: list[str],
    error: BaseException,
    location: str,
) -> None:
    if not isinstance(error, _DuplicateJSONKeysError):
        return
    keys = "\n".join(
        f"- {json.dumps(key, ensure_ascii=False)}" for key in error.keys
    )
    messages.append(
        "重複JSONキー詳細\n"
        f"資産: {location}\n"
        f"件数: {len(error.keys)}\n"
        f"キー:\n{keys}"
    )


def _read_limited_file(path: Path, maximum: int) -> bytes:
    size = path.stat().st_size
    if size < 0 or size > maximum:
        raise ValueError(f"fileがサイズ上限を超えています: {path} ({size})")
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError(f"fileがサイズ上限を超えています: {path}")
    return data
