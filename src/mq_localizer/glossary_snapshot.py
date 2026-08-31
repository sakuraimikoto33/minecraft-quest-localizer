from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Literal

from .domain import CancelledError, TranslationError
from .scan_limits import GlossaryScanLimits


_MAX_MINECRAFT_PATHS = 4_096
_CHANGED_MESSAGE = (
    "固有名詞保護に使用したファイルが解析後に変更されました。"
    "翻訳を開始する前に再解析してください"
)


@dataclass(frozen=True, slots=True)
class GlossaryFollowedState:
    """State of the object reached through one symlink or junction."""

    status: Literal["present", "missing", "error"]
    kind: Literal["file", "directory", "other", "missing", "error"]
    mode: int = 0
    size: int = 0
    mtime_ns: int = 0
    ctime_ns: int = 0
    device: int = 0
    inode: int = 0
    file_attributes: int = 0
    error_identity: str = ""


@dataclass(frozen=True, slots=True)
class GlossaryFileState:
    """Metadata-only identity for one input path.

    File contents are deliberately not read here.  The scanner has already
    validated and parsed them; size and the filesystem change/identity fields
    make the normal translation-start check inexpensive even for large Mod
    archives.
    """

    status: Literal["present", "missing", "error"]
    kind: Literal["file", "directory", "symlink", "other", "missing", "error"]
    mode: int = 0
    size: int = 0
    mtime_ns: int = 0
    ctime_ns: int = 0
    device: int = 0
    inode: int = 0
    file_attributes: int = 0
    reparse: bool = False
    resolved_path: str = ""
    error_identity: str = ""
    followed: GlossaryFollowedState | None = None


@dataclass(frozen=True, slots=True)
class GlossaryInventoryEntry:
    relative_path: str
    state: GlossaryFileState


@dataclass(frozen=True, slots=True)
class GlossaryInventoryState:
    root_state: GlossaryFileState
    entries: tuple[GlossaryInventoryEntry, ...] = ()
    digest: str = ""
    entry_count: int = 0
    error_identity: str = ""


@dataclass(frozen=True, slots=True)
class GlossaryPathWatch:
    label: str
    path: str
    state: GlossaryFileState
    comparison: Literal["exact", "safe_directory_guard"] = "exact"
    follow_reparse_target: bool = False


@dataclass(frozen=True, slots=True)
class GlossaryInventoryWatch:
    label: str
    path: str
    mode: Literal["all", "archives", "resourcepacks"]
    state: GlossaryInventoryState
    allow_reparse_root: bool = False
    guard_path: str = ""


@dataclass(frozen=True, slots=True)
class GlossaryInputSnapshot:
    """Immutable inventory of every filesystem input used by one scan."""

    paths: tuple[GlossaryPathWatch, ...] = ()
    inventories: tuple[GlossaryInventoryWatch, ...] = ()
    resourcepacks_included: bool = False
    scan_limits: GlossaryScanLimits = field(default_factory=GlossaryScanLimits)


class GlossaryInputRecorder:
    """Capture scanner inputs without retaining mutable scanner state."""

    def __init__(
        self,
        cancel: Event | None = None,
        *,
        scan_limits: GlossaryScanLimits | None = None,
    ) -> None:
        self._cancel = cancel
        self._paths: dict[str, GlossaryPathWatch] = {}
        self._inventories: list[GlossaryInventoryWatch] = []
        self._resourcepacks_included = False
        self._scan_limits = scan_limits or GlossaryScanLimits()
        self._source_member_limit = (
            self._scan_limits.effective_max_source_members
        )

    def capture_mod_inputs(
        self,
        location: Path | None,
        archives: tuple[Path, ...],
    ) -> None:
        _raise_if_cancelled(self._cancel)
        if location is None or not str(location):
            return
        candidate = Path(location).expanduser()
        if candidate.is_file() and candidate.suffix.casefold() in {".jar", ".zip"}:
            self._add_path(
                "Mod archive",
                candidate,
                follow_reparse_target=True,
            )
            return

        nested_mods = candidate / "mods"
        selected_root = nested_mods if nested_mods.is_dir() else candidate
        # The presence of <location>/mods changes _find_archives' selected
        # directory, so retain its missing state as well.
        self._add_path("Mod archive discovery path", nested_mods)
        inventory = self._add_inventory(
            "Mod archives",
            selected_root,
            "archives",
            allow_reparse_root=True,
        )
        discovered = {_path_key(path) for path in archives}
        captured = {
            _path_key(selected_root / entry.relative_path)
            for entry in inventory.state.entries
        }
        if discovered != captured:
            raise TranslationError(
                "Mod archiveの一覧が解析開始中に変更されました。再解析してください"
            )

    def capture_external_inputs(
        self,
        game_root: Path | None,
        include_resourcepacks: bool,
    ) -> None:
        _raise_if_cancelled(self._cancel)
        self._resourcepacks_included = include_resourcepacks
        if game_root is None or not str(game_root):
            return
        root = Path(game_root).expanduser()
        self._add_path("Glossary asset game root", root)
        root_state = _path_state(root)
        root_is_safe = (
            root_state.status == "present"
            and root_state.kind == "directory"
            and not root_state.reparse
        )
        kubejs_root = root / "kubejs"
        if root_is_safe:
            self._add_path(
                "KubeJS root",
                kubejs_root,
                comparison="safe_directory_guard",
            )
        self._add_inventory(
            "KubeJS assets",
            kubejs_root / "assets",
            "all",
            guard_path=kubejs_root if root_is_safe else root,
        )
        if include_resourcepacks:
            self._add_inventory(
                "resourcepacks",
                root / "resourcepacks",
                "resourcepacks",
                guard_path=None if root_is_safe else root,
            )

    def observe_minecraft_path(self, path: Path, label: str) -> None:
        """Observer passed to the Minecraft asset loader.

        Missing candidates are intentionally retained: creation of a new
        higher-priority metadata/client candidate must invalidate the cached
        official terminology just like modification of a file that was used.
        """

        _raise_if_cancelled(self._cancel)
        if len(self._paths) >= _MAX_MINECRAFT_PATHS:
            key = _path_key(path)
            if key not in self._paths:
                raise TranslationError(
                    "Minecraft公式言語資産の確認対象が安全上限を超えました。"
                    "ランチャー構成を確認して再解析してください"
                )
        self._add_path(label, path, follow_reparse_target=True)

    def freeze(self) -> GlossaryInputSnapshot:
        _raise_if_cancelled(self._cancel)
        return GlossaryInputSnapshot(
            paths=tuple(
                sorted(
                    self._paths.values(),
                    key=lambda watch: (watch.path.casefold(), watch.path, watch.label),
                )
            ),
            inventories=tuple(self._inventories),
            resourcepacks_included=self._resourcepacks_included,
            scan_limits=self._scan_limits,
        )

    def _add_path(
        self,
        label: str,
        path: Path,
        *,
        comparison: Literal["exact", "safe_directory_guard"] = "exact",
        follow_reparse_target: bool = False,
    ) -> None:
        _raise_if_cancelled(self._cancel)
        absolute = _absolute_path(path)
        key = _path_key(absolute)
        if key in self._paths:
            return
        self._paths[key] = GlossaryPathWatch(
            label=label,
            path=str(absolute),
            state=_path_state(
                absolute,
                follow_reparse_target=follow_reparse_target,
            ),
            comparison=comparison,
            follow_reparse_target=follow_reparse_target,
        )

    def _add_inventory(
        self,
        label: str,
        path: Path,
        mode: Literal["all", "archives", "resourcepacks"],
        *,
        allow_reparse_root: bool = False,
        guard_path: Path | None = None,
    ) -> GlossaryInventoryWatch:
        _raise_if_cancelled(self._cancel)
        absolute = _absolute_path(path)
        try:
            captured = _capture_inventory(
                absolute,
                mode,
                self._cancel,
                allow_reparse_root=allow_reparse_root,
                guard_path=guard_path,
                maximum_entries=self._source_member_limit,
            )
        except _InventoryLimitExceeded as exc:
            target = f"\n対象: {exc.source}" if exc.source else ""
            limit_text = _entry_limit_text(self._source_member_limit)
            raise TranslationError(
                "固有名詞保護用の1資産内の入力一覧が上限 "
                f"{limit_text}を超えたため、"
                "変更確認用の解析結果を作成できません。"
                f"対象フォルダーを確認して再解析してください{target}"
            ) from exc
        watch = GlossaryInventoryWatch(
            label=label,
            path=str(absolute),
            mode=mode,
            state=captured,
            allow_reparse_root=allow_reparse_root,
            guard_path=str(_absolute_path(guard_path)) if guard_path is not None else "",
        )
        self._inventories.append(watch)
        return watch


def assert_glossary_inputs_unchanged(
    snapshot: GlossaryInputSnapshot | None,
    cancel: Event | None = None,
) -> None:
    """Verify cached glossary inputs using only stat calls and inventories.

    Mod/resource-pack archives and Minecraft JSON/object contents are not
    reopened on the unchanged path.  Any observed identity or membership
    change fails closed and asks the user to run analysis again.
    """

    if snapshot is None:
        # Hand-built/legacy catalogs have no scanner cache to validate.
        return
    _raise_if_cancelled(cancel)
    source_member_limit = snapshot.scan_limits.effective_max_source_members
    for watch in snapshot.paths:
        _raise_if_cancelled(cancel)
        current = _path_state(
            Path(watch.path),
            follow_reparse_target=watch.follow_reparse_target,
        )
        if not _path_states_match(watch, current):
            _raise_changed(watch.label, watch.path)
    for watch in snapshot.inventories:
        _raise_if_cancelled(cancel)
        try:
            current = _capture_inventory(
                Path(watch.path),
                watch.mode,
                cancel,
                allow_reparse_root=watch.allow_reparse_root,
                guard_path=Path(watch.guard_path) if watch.guard_path else None,
                maximum_entries=source_member_limit,
            )
        except _InventoryLimitExceeded as exc:
            target = f"\n超過対象: {exc.source}" if exc.source else ""
            limit_text = _entry_limit_text(source_member_limit)
            raise TranslationError(
                f"{_CHANGED_MESSAGE}\n対象: {watch.label}\n"
                "理由: 現在の1資産内の入力一覧が上限 "
                f"{limit_text}を超えています{target}"
            ) from exc
        if current != watch.state:
            _raise_changed(watch.label, watch.path)
    _raise_if_cancelled(cancel)


class _InventoryLimitExceeded(RuntimeError):
    def __init__(self, source: object = "") -> None:
        super().__init__(str(source))
        self.source = str(source)


def _entry_limit_text(limit: int | None) -> str:
    """Format an active entry cap without applying numeric formatting to None."""

    return f"{limit:,}件" if limit is not None else "無制限"


def _capture_inventory(
    root: Path,
    mode: Literal["all", "archives", "resourcepacks"],
    cancel: Event | None,
    *,
    allow_reparse_root: bool,
    guard_path: Path | None = None,
    maximum_entries: int | None,
) -> GlossaryInventoryState:
    if guard_path is not None:
        guard_state = _path_state(guard_path)
        if guard_state.status == "missing":
            # A missing kubejs ancestor and an empty, ordinary kubejs folder
            # both leave kubejs/assets missing and produce the same scanner
            # result.  Let the descendant's canonical missing state represent
            # both cases.
            pass
        elif (
            guard_state.status != "present"
            or guard_state.kind != "directory"
            or guard_state.reparse
        ):
            # Do not even enumerate the guarded descendant.  In particular,
            # kubejs may be an unsafe junction while kubejs/assets itself looks
            # like an ordinary directory after ancestor traversal.
            return GlossaryInventoryState(
                root_state=GlossaryFileState(
                    status="error",
                    kind="error",
                    resolved_path=_lexical_path(root),
                    error_identity="guarded-ancestor-is-not-a-safe-directory",
                )
            )
    if mode == "resourcepacks":
        return _capture_resourcepacks_inventory(
            root,
            cancel,
            maximum_entries,
        )
    return _capture_directory_inventory(
        root,
        cancel,
        recursive=(mode == "all"),
        archives_only=(mode == "archives"),
        allow_reparse_root=allow_reparse_root,
        maximum_entries=maximum_entries,
    )


def _capture_directory_inventory(
    root: Path,
    cancel: Event | None,
    *,
    recursive: bool,
    archives_only: bool,
    allow_reparse_root: bool,
    maximum_entries: int | None,
) -> GlossaryInventoryState:
    _raise_if_cancelled(cancel)
    root_state = _path_state(
        root,
        follow_reparse_target=allow_reparse_root,
    )
    root_is_directory = root_state.kind == "directory" or (
        allow_reparse_root
        and root_state.reparse
        and root_state.followed is not None
        and root_state.followed.status == "present"
        and root_state.followed.kind == "directory"
    )
    if (
        root_state.status != "present"
        or not root_is_directory
        or (root_state.reparse and not allow_reparse_root)
    ):
        return GlossaryInventoryState(root_state=root_state)

    entries: list[GlossaryInventoryEntry] = []
    digest = hashlib.blake2b(digest_size=32)
    visited = 0
    stack = [root]
    try:
        while stack:
            _raise_if_cancelled(cancel)
            current = stack.pop()
            remaining = (
                None
                if maximum_entries is None
                else maximum_entries - visited
            )
            children = _bounded_scandir(
                current,
                remaining,
                cancel,
                reverse=True,
                limit_source=root,
            )
            visited += len(children)
            for child in children:
                _raise_if_cancelled(cancel)
                child_path = Path(child.path)
                archive_candidate = archives_only and _is_discovered_mod_archive_name(
                    child_path.name
                )
                child_state = _direntry_state(
                    child,
                    child_path,
                    follow_reparse_target=archive_candidate,
                )
                relative = child_path.relative_to(root).as_posix()
                include = not archives_only or archive_candidate
                if include:
                    if archives_only:
                        entries.append(GlossaryInventoryEntry(relative, child_state))
                    else:
                        _update_inventory_digest(digest, relative, child_state)
                if (
                    recursive
                    and child_state.status == "present"
                    and child_state.kind == "directory"
                    and not child_state.reparse
                ):
                    stack.append(child_path)
    except _InventoryLimitExceeded:
        raise
    except OSError as exc:
        return GlossaryInventoryState(
            root_state=root_state,
            entries=tuple(_sorted_entries(entries)),
            digest="" if archives_only else digest.hexdigest(),
            entry_count=len(entries) if archives_only else visited,
            error_identity=_error_identity(exc),
        )
    return GlossaryInventoryState(
        root_state=root_state,
        entries=tuple(_sorted_entries(entries)),
        digest="" if archives_only else digest.hexdigest(),
        entry_count=len(entries) if archives_only else visited,
    )


def _capture_resourcepacks_inventory(
    root: Path,
    cancel: Event | None,
    maximum_entries: int | None,
) -> GlossaryInventoryState:
    """Digest direct candidates and each folder pack with an independent cap."""

    _raise_if_cancelled(cancel)
    root_state = _path_state(root)
    if (
        root_state.status != "present"
        or root_state.kind != "directory"
        or root_state.reparse
    ):
        return GlossaryInventoryState(root_state=root_state)

    digest = hashlib.blake2b(digest_size=32)
    relevant_entries = 0
    try:
        children = _bounded_scandir(
            root,
            maximum_entries,
            cancel,
            limit_source=root,
        )
        for child in children:
            _raise_if_cancelled(cancel)
            child_path = Path(child.path)
            child_state = _direntry_state(child, child_path)
            is_zip = (
                child_state.kind == "file"
                and child_path.suffix.casefold() == ".zip"
            )
            is_folder = child_state.kind == "directory" and not child_state.reparse
            if child_state.status != "present" or child_state.reparse or is_zip:
                _update_inventory_digest(
                    digest,
                    child_path.relative_to(root).as_posix(),
                    child_state,
                )
                relevant_entries += 1
            if not is_folder:
                continue

            assets_root = child_path / "assets"
            assets_state = _path_state(assets_root)
            # A plain folder without assets is not a resource-pack candidate.
            # It remains ignored until assets appears; the next capture then
            # includes both paths and differs from the baseline.
            if assets_state.status == "missing":
                continue
            _update_inventory_digest(
                digest,
                child_path.relative_to(root).as_posix(),
                child_state,
            )
            _update_inventory_digest(
                digest,
                assets_root.relative_to(root).as_posix(),
                assets_state,
            )
            relevant_entries += 2
            if (
                assets_state.status != "present"
                or assets_state.kind != "directory"
                or assets_state.reparse
            ):
                continue
            stack = [assets_root]
            pack_entries = 0
            while stack:
                _raise_if_cancelled(cancel)
                current = stack.pop()
                remaining = (
                    None
                    if maximum_entries is None
                    else maximum_entries - pack_entries
                )
                asset_children = _bounded_scandir(
                    current,
                    remaining,
                    cancel,
                    reverse=True,
                    limit_source=child_path,
                )
                pack_entries += len(asset_children)
                for asset_child in asset_children:
                    _raise_if_cancelled(cancel)
                    asset_path = Path(asset_child.path)
                    asset_state = _direntry_state(asset_child, asset_path)
                    _update_inventory_digest(
                        digest,
                        asset_path.relative_to(root).as_posix(),
                        asset_state,
                    )
                    if (
                        asset_state.status == "present"
                        and asset_state.kind == "directory"
                        and not asset_state.reparse
                    ):
                        stack.append(asset_path)
            relevant_entries += pack_entries
    except _InventoryLimitExceeded:
        raise
    except OSError as exc:
        return GlossaryInventoryState(
            root_state=root_state,
            digest=digest.hexdigest(),
            entry_count=relevant_entries,
            error_identity=_error_identity(exc),
        )
    return GlossaryInventoryState(
        root_state=root_state,
        digest=digest.hexdigest(),
        entry_count=relevant_entries,
    )


def _bounded_scandir(
    path: Path,
    remaining: int | None,
    cancel: Event | None,
    *,
    reverse: bool = False,
    limit_source: object = "",
) -> list[os.DirEntry[str]]:
    children: list[os.DirEntry[str]] = []
    with os.scandir(path) as iterator:
        for child in iterator:
            _raise_if_cancelled(cancel)
            children.append(child)
            if remaining is not None and len(children) > remaining:
                raise _InventoryLimitExceeded(limit_source or path)
    return sorted(
        children,
        key=lambda child: (child.name.casefold(), child.name),
        reverse=reverse,
    )


def _update_inventory_digest(
    digest: Any,
    relative_path: str,
    state: GlossaryFileState,
) -> None:
    """Hash one canonical path/state record without retaining every member."""

    payload = repr((relative_path, _file_state_digest_tuple(state))).encode(
        "utf-8",
        "surrogatepass",
    )
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _file_state_digest_tuple(state: GlossaryFileState) -> tuple[object, ...]:
    followed = state.followed
    followed_state: tuple[object, ...] | None = None
    if followed is not None:
        followed_state = (
            followed.status,
            followed.kind,
            followed.mode,
            followed.size,
            followed.mtime_ns,
            followed.ctime_ns,
            followed.device,
            followed.inode,
            followed.file_attributes,
            followed.error_identity,
        )
    return (
        state.status,
        state.kind,
        state.mode,
        state.size,
        state.mtime_ns,
        state.ctime_ns,
        state.device,
        state.inode,
        state.file_attributes,
        state.reparse,
        state.resolved_path,
        state.error_identity,
        followed_state,
    )


def _direntry_state(
    entry: os.DirEntry[str],
    path: Path,
    *,
    follow_reparse_target: bool = False,
) -> GlossaryFileState:
    try:
        value = entry.stat(follow_symlinks=False)
    except FileNotFoundError:
        return _missing_state(path)
    except OSError as exc:
        return _error_state(path, exc)
    return _state_from_stat(
        path,
        value,
        follow_reparse_target=follow_reparse_target,
    )


def _path_state(
    path: Path,
    *,
    follow_reparse_target: bool = False,
) -> GlossaryFileState:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return _missing_state(path)
    except OSError as exc:
        return _error_state(path, exc)
    return _state_from_stat(
        path,
        value,
        follow_reparse_target=follow_reparse_target,
    )


def _state_from_stat(
    path: Path,
    value: os.stat_result,
    *,
    follow_reparse_target: bool = False,
) -> GlossaryFileState:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse = stat.S_ISLNK(value.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )
    if stat.S_ISLNK(value.st_mode):
        kind: Literal["file", "directory", "symlink", "other"] = "symlink"
    elif stat.S_ISREG(value.st_mode):
        kind = "file"
    elif stat.S_ISDIR(value.st_mode):
        kind = "directory"
    else:
        kind = "other"
    is_directory = kind == "directory"
    return GlossaryFileState(
        status="present",
        kind=kind,
        mode=int(value.st_mode),
        # Directory membership is recorded explicitly by inventories.  Some
        # filesystems update directory size/timestamps lazily during scandir,
        # and unrelated ignored children must not invalidate an archive-only
        # inventory.
        size=0 if is_directory else int(value.st_size),
        mtime_ns=(
            0
            if is_directory
            else int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000)))
        ),
        ctime_ns=(
            0
            if is_directory
            else int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000)))
        ),
        device=int(value.st_dev),
        inode=int(value.st_ino),
        file_attributes=attributes,
        reparse=reparse,
        resolved_path=(
            _resolved_path(path)
            if not reparse or follow_reparse_target
            else _unfollowed_reparse_identity(path)
        ),
        followed=(
            _followed_state(path)
            if reparse and follow_reparse_target
            else None
        ),
    )


def _followed_state(path: Path) -> GlossaryFollowedState:
    """Capture the target read by normal file operations on a reparse path."""

    try:
        value = path.stat()
    except FileNotFoundError:
        return GlossaryFollowedState(status="missing", kind="missing")
    except OSError as exc:
        return GlossaryFollowedState(
            status="error",
            kind="error",
            error_identity=_error_identity(exc),
        )

    if stat.S_ISREG(value.st_mode):
        kind: Literal["file", "directory", "other"] = "file"
    elif stat.S_ISDIR(value.st_mode):
        kind = "directory"
    else:
        kind = "other"
    is_directory = kind == "directory"
    return GlossaryFollowedState(
        status="present",
        kind=kind,
        mode=int(value.st_mode),
        size=0 if is_directory else int(value.st_size),
        mtime_ns=(
            0
            if is_directory
            else int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000)))
        ),
        ctime_ns=(
            0
            if is_directory
            else int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000)))
        ),
        device=int(value.st_dev),
        inode=int(value.st_ino),
        file_attributes=int(getattr(value, "st_file_attributes", 0)),
    )


def _missing_state(path: Path) -> GlossaryFileState:
    return GlossaryFileState(
        status="missing",
        kind="missing",
        resolved_path=_resolved_path(path),
    )


def _error_state(path: Path, exc: OSError) -> GlossaryFileState:
    return GlossaryFileState(
        status="error",
        kind="error",
        resolved_path=_resolved_path(path),
        error_identity=_error_identity(exc),
    )


def _error_identity(exc: OSError) -> str:
    return (
        f"{type(exc).__name__}:errno={getattr(exc, 'errno', None)}:"
        f"winerror={getattr(exc, 'winerror', None)}"
    )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _resolved_path(path: Path) -> str:
    try:
        value = os.path.realpath(_absolute_path(path))
    except OSError:
        value = str(_absolute_path(path))
    return os.path.normcase(os.path.normpath(value))


def _unfollowed_reparse_identity(path: Path) -> str:
    """Record a link/junction reference without opening its destination."""

    try:
        target = os.readlink(_absolute_path(path))
    except OSError:
        target = ""
    normalized_target = os.path.normcase(os.path.normpath(target)) if target else ""
    return f"{_lexical_path(path)}->{normalized_target}"


def _lexical_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(_absolute_path(path))))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(_absolute_path(path))))


def _is_discovered_mod_archive_name(name: str) -> bool:
    # Keep the snapshot inventory identical to glossary._find_archives on
    # every platform, including uppercase archive suffixes on POSIX.
    return Path(name).suffix.casefold() in {".jar", ".zip"}


def _path_states_match(
    watch: GlossaryPathWatch,
    current: GlossaryFileState,
) -> bool:
    if watch.comparison == "exact":
        return watch.state == current

    def safe_or_missing(value: GlossaryFileState) -> bool:
        return value.status == "missing" or (
            value.status == "present"
            and value.kind == "directory"
            and not value.reparse
        )

    if safe_or_missing(watch.state) and safe_or_missing(current):
        return True
    return watch.state == current


def _sorted_entries(
    entries: list[GlossaryInventoryEntry],
) -> list[GlossaryInventoryEntry]:
    return sorted(
        entries,
        key=lambda item: (item.relative_path.casefold(), item.relative_path),
    )


def _raise_changed(label: str, path: str) -> None:
    raise TranslationError(f"{_CHANGED_MESSAGE}\n対象: {label}\nパス: {path}")


def _raise_if_cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("固有名詞保護の解析結果確認をキャンセルしました")
