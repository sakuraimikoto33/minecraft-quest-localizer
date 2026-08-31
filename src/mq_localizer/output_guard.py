from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .domain import AdapterError, CancelledError


@dataclass(frozen=True, slots=True)
class PathSnapshotEntry:
    """One filesystem object below a snapshotted output path."""

    relative_path: str
    kind: str
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class PathSnapshot:
    """Content snapshot used to reject lost updates after user confirmation."""

    path: Path
    resolved_path: Path
    root_kind: str
    root_link_target: str
    entries: tuple[PathSnapshotEntry, ...]

    @property
    def exists(self) -> bool:
        return self.root_kind != "missing"


def snapshot_path(path: Path, cancel: Event | None = None) -> PathSnapshot:
    """Capture path existence, type, links, and recursive file contents.

    The lexical path and its resolved destination are both retained.  This
    detects parent-directory symlink/junction retargeting as well as ordinary
    file or directory changes.  Directory symlinks inside the tree are recorded
    as links rather than followed, preventing cycles and unbounded traversal.
    """

    _raise_if_cancelled(cancel)
    lexical_path = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    try:
        root_stat = lexical_path.lstat()
    except FileNotFoundError:
        try:
            _raise_if_cancelled(cancel)
            resolved = lexical_path.resolve(strict=False)
            _raise_if_cancelled(cancel)
        except (OSError, RuntimeError) as exc:
            raise _snapshot_error(lexical_path, exc) from exc
        return PathSnapshot(
            path=lexical_path,
            resolved_path=resolved,
            root_kind="missing",
            root_link_target="",
            entries=(),
        )
    except OSError as exc:
        raise _snapshot_error(lexical_path, exc) from exc

    try:
        _raise_if_cancelled(cancel)
        resolved = lexical_path.resolve(strict=False)
        root_kind = _kind(lexical_path, root_stat)
        link_target = (
            _link_fingerprint(lexical_path, root_stat)
            if root_kind in {"symlink", "junction"}
            else ""
        )
        entries = tuple(_snapshot_entries(resolved, cancel))
        _raise_if_cancelled(cancel)
    except (OSError, RuntimeError) as exc:
        raise _snapshot_error(lexical_path, exc) from exc
    return PathSnapshot(
        path=lexical_path,
        resolved_path=resolved,
        root_kind=root_kind,
        root_link_target=link_target,
        entries=entries,
    )


def assert_path_unchanged(expected: PathSnapshot, cancel: Event | None = None) -> None:
    """Raise before writing if an output changed after user confirmation."""

    _assert_snapshot_unchanged(
        expected,
        cancel,
        "出力先が確認後に変更されたため、安全のため書き込みを中止しました。"
        "再度解析し、出力先を確認してから翻訳してください。",
    )


def assert_source_unchanged(expected: PathSnapshot, cancel: Event | None = None) -> None:
    """Raise before writing if analyzed source data changed during translation."""

    _assert_snapshot_unchanged(
        expected,
        cancel,
        "翻訳元が解析後に変更されたため、安全のため書き込みを中止しました。"
        "再度解析してから翻訳してください。",
    )


def assert_relocated_path_unchanged(
    expected: PathSnapshot,
    relocated_path: Path,
    cancel: Event | None = None,
) -> None:
    """Verify snapshotted content after its root was atomically renamed.

    This is used by directory transactions which first move the live output
    out of the destination namespace.  Comparing only content and object kinds
    (rather than the lexical/resolved root path) closes the check-to-rename
    window without treating the transaction's own rename as an external edit.
    """

    current = snapshot_path(relocated_path, cancel)
    if (
        current.root_kind != expected.root_kind
        or current.root_link_target != expected.root_link_target
        or current.entries != expected.entries
    ):
        raise AdapterError(
            "出力先が確認後に変更されたため、安全のため書き込みを中止しました。"
            "再度解析し、出力先を確認してから翻訳してください。"
        )


def _assert_snapshot_unchanged(
    expected: PathSnapshot,
    cancel: Event | None,
    message: str,
) -> None:
    current = snapshot_path(expected.path, cancel)
    if current != expected:
        raise AdapterError(message)


def _snapshot_entries(root: Path, cancel: Event | None) -> list[PathSnapshotEntry]:
    _raise_if_cancelled(cancel)
    try:
        root_stat = root.stat()
    except FileNotFoundError:
        return [PathSnapshotEntry(".", "missing")]

    root_kind = _kind(root, root_stat)
    if root_kind == "file":
        return [PathSnapshotEntry(".", "file", _hash_file(root, cancel))]
    if root_kind != "directory":
        return [PathSnapshotEntry(".", root_kind, _other_fingerprint(root_stat))]

    result = [PathSnapshotEntry(".", "directory")]

    def visit(directory: Path, relative_directory: Path) -> None:
        _raise_if_cancelled(cancel)
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
        _raise_if_cancelled(cancel)
        for child in children:
            _raise_if_cancelled(cancel)
            relative = (relative_directory / child.name).as_posix()
            child_stat = child.stat(follow_symlinks=False)
            _raise_if_cancelled(cancel)
            child_path = Path(child.path)
            child_kind = _kind(child_path, child_stat)
            if child_kind == "directory":
                result.append(PathSnapshotEntry(relative, "directory"))
                visit(child_path, relative_directory / child.name)
            elif child_kind == "file":
                result.append(PathSnapshotEntry(relative, "file", _hash_file(child_path, cancel)))
            elif child_kind == "symlink":
                result.append(PathSnapshotEntry(relative, "symlink", os.readlink(child_path)))
            elif child_kind == "junction":
                # Windows junctions report a directory mode even with
                # follow_symlinks=False.  Record the reparse target but never
                # recurse into it, avoiding cycles and traversal outside the
                # confirmed output tree.
                result.append(
                    PathSnapshotEntry(
                        relative,
                        "junction",
                        _link_fingerprint(child_path, child_stat),
                    )
                )
            else:
                result.append(
                    PathSnapshotEntry(relative, child_kind, _other_fingerprint(child_stat))
                )

    visit(root, Path())
    return result


def _hash_file(path: Path, cancel: Event | None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            _raise_if_cancelled(cancel)
            block = handle.read(1024 * 1024)
            _raise_if_cancelled(cancel)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _raise_if_cancelled(cancel: Event | None) -> None:
    if cancel and cancel.is_set():
        raise CancelledError("処理をキャンセルしました")


def _kind(path: Path, value: os.stat_result) -> str:
    mode = value.st_mode
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return "junction"
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(value, "st_file_attributes", 0)
    if reparse_flag and file_attributes & reparse_flag:
        return "junction"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def _other_fingerprint(value: os.stat_result) -> str:
    return f"{value.st_mode}:{value.st_size}:{value.st_mtime_ns}"


def _link_fingerprint(path: Path, value: os.stat_result) -> str:
    try:
        target = os.readlink(path)
    except OSError:
        target = str(path.resolve(strict=False))
    return f"{target}|{_other_fingerprint(value)}"


def _snapshot_error(path: Path, error: BaseException) -> AdapterError:
    return AdapterError(f"出力先の現在状態を読み取れません: {path} ({error})")
