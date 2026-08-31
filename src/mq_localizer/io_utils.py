from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .domain import AdapterError
from .output_guard import PathSnapshot, assert_path_unchanged


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int
    links: int
    mode: int


def read_text_detect(path: Path) -> tuple[str, str, str]:
    """Return text, encoding, and the dominant newline style."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AdapterError(f"ファイルを読み込めません: {path} ({exc})") from exc
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    try:
        text = data.decode(encoding)
    except UnicodeError as exc:
        raise AdapterError(f"テキストの文字コードを解析できません: {path} ({exc})") from exc
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    newline = "\r\n" if crlf >= max(lf, cr) and crlf else ("\r" if cr > lf else "\n")
    return text, encoding, newline


def atomic_write_text(
    path: Path,
    text: str,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> _PathIdentity:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _text_with_newline(text, newline)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    identity: _PathIdentity
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            identity = _identity_from_stat(os.fstat(handle.fileno()))
        os.replace(temporary, path)
        return identity
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def atomic_write_bytes(path: Path, data: bytes) -> _PathIdentity:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    identity: _PathIdentity
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            identity = _identity_from_stat(os.fstat(handle.fileno()))
        os.replace(temporary, path)
        return identity
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def atomic_write_many_text(
    writes: Iterable[tuple[Path, str, str | None]],
    *,
    encoding: str = "utf-8",
    expected_path_snapshot: PathSnapshot | None = None,
    expected_originals: Mapping[Path, bytes | None] | None = None,
) -> None:
    """Write several text files and restore every original if one write fails.

    Each individual replacement is atomic.  Filesystems do not provide one
    atomic operation spanning unrelated files, so this helper snapshots all
    destinations before the first mutation and rolls the whole set back on a
    later failure.  New files and any now-empty directories created for them
    are removed during rollback.
    """

    pending = [(Path(path), text, newline) for path, text, newline in writes]
    if not pending:
        return
    if expected_path_snapshot is not None:
        assert_path_unchanged(expected_path_snapshot)

    normalized: set[Path] = set()
    originals: list[tuple[Path, bytes | None]] = []
    missing_directories: set[Path] = set()
    for path, _text, _newline in pending:
        resolved = path.resolve()
        if resolved in normalized:
            raise ValueError(f"同じ出力先が複数回指定されています: {path}")
        normalized.add(resolved)
        current = _read_optional_bytes(path)
        if expected_originals is not None:
            if path not in expected_originals:
                raise ValueError(f"出力計画にない書き込み先です: {path}")
            if current != expected_originals[path]:
                raise AdapterError(
                    "出力先が書き込み計画の作成後に変更されたため、"
                    "安全のため書き込みを中止しました"
                )
        originals.append((path, current))
        if current is None:
            parent = path.parent
            while not parent.exists():
                missing_directories.add(parent)
                if parent.parent == parent:
                    break
                parent = parent.parent

    committed: list[tuple[Path, bytes | None, bytes, _PathIdentity]] = []
    try:
        for (path, text, newline), original in zip(pending, originals):
            if _read_optional_bytes(path) != original[1]:
                raise AdapterError(
                    "出力先が複数ファイルの書き込み開始後に変更されたため、"
                    "安全のため書き込みを中止しました"
                )
            written = _text_with_newline(text, newline).encode(encoding)
            written_identity = atomic_write_text(
                path,
                text,
                encoding=encoding,
                newline=newline,
            )
            committed.append((original[0], original[1], written, written_identity))
    except BaseException as write_error:
        rollback_errors: list[str] = []
        # atomic_write_text performs no fallible work after os.replace, so an
        # exception means the current destination was not replaced.  Restore
        # only writes that returned successfully; this avoids touching later
        # read-only files that never changed.
        for path, original, written, written_identity in reversed(committed):
            try:
                try:
                    current_identity = _path_identity(path)
                    current = path.read_bytes()
                except FileNotFoundError:
                    if original is None:
                        continue
                    rollback_errors.append(
                        f"{path}: 書き込み後に外部で削除されたため復元していません"
                    )
                    continue
                if current == original:
                    continue
                if current != written:
                    rollback_errors.append(
                        f"{path}: 書き込み後の外部変更を検出したため、その内容を保持しました"
                    )
                    continue
                if current_identity != written_identity:
                    rollback_errors.append(
                        f"{path}: 書き込み後の外部置換を検出したため、その内容を保持しました"
                    )
                    continue
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(path, original)
            except BaseException as rollback_error:
                rollback_errors.append(f"{path}: {rollback_error}")
        for directory in sorted(
            missing_directories,
            key=lambda candidate: len(candidate.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                # A concurrent creator or a leftover file makes the directory
                # non-empty; never delete content that this call did not make.
                pass
        if rollback_errors:
            details = "\n".join(f"- {error}" for error in rollback_errors)
            raise OSError(
                "複数ファイルの書き込みに失敗し、次の出力先を完全には復元できませんでした:\n"
                f"{details}"
            ) from write_error
        raise


def _text_with_newline(text: str, newline: str | None) -> str:
    if newline and newline != "\n":
        return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    return text


def _path_identity(path: Path) -> _PathIdentity:
    return _identity_from_stat(path.lstat())


def _read_optional_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _identity_from_stat(value: os.stat_result) -> _PathIdentity:
    return _PathIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        links=value.st_nlink,
        mode=stat.S_IFMT(value.st_mode),
    )
