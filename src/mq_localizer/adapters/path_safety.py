from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Iterable

from ..domain import AdapterError


def reject_reparse_ancestors(
    path: Path,
    description: str,
    *,
    anchor: Path | None = None,
) -> None:
    """Reject a link in ``path`` or any of its existing ancestors.

    ``Path.is_file()`` and ``Path.resolve()`` both follow links in parent
    directories. Checking only the final file is therefore insufficient: a
    perfectly ordinary-looking ``en_us.json`` can live outside the selected
    instance through an intermediate Windows junction. Inspect each lexical
    component with ``lstat`` before a caller probes or opens the path.

    When ``anchor`` is supplied, it is the explicitly selected/trusted root:
    only descendants below it are rejected, while lexical and resolved
    containment still guard against ``..`` components and redirected nested
    paths. Without an anchor every existing ancestor is checked, which is used
    for direct-file inputs.
    """

    raw_path = Path(path)
    raw_anchor = Path(anchor) if anchor is not None else None
    if ".." in raw_path.parts or (raw_anchor is not None and ".." in raw_anchor.parts):
        raise AdapterError(
            f"{description}に親ディレクトリ参照 '..' は使用できません: {raw_path}"
        )
    # Adapters open the Path object exactly as supplied; they do not expand
    # ``~``.  The safety layer must validate that same filesystem object.
    lexical_path = _absolute(raw_path)
    lexical_anchor = _absolute(raw_anchor) if raw_anchor is not None else None
    if lexical_anchor is not None and not lexical_path.is_relative_to(lexical_anchor):
        raise AdapterError(
            f"{description}が選択したルート外を参照しています: {lexical_path}"
        )

    if lexical_anchor is None:
        candidates = (lexical_path, *lexical_path.parents)
    else:
        nested_candidates: list[Path] = []
        candidate = lexical_path
        while candidate != lexical_anchor:
            nested_candidates.append(candidate)
            parent = candidate.parent
            if parent == candidate:
                raise AdapterError(
                    f"{description}が選択したルート外を参照しています: {lexical_path}"
                )
            candidate = parent
        candidates = tuple(nested_candidates)

    for candidate in candidates:
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AdapterError(
                f"{description}の祖先を安全確認できません: {candidate} ({exc})"
            ) from exc
        if _is_reparse_point(candidate, candidate_stat):
            raise AdapterError(
                f"{description}またはその祖先がsymlinkまたはjunctionのため、"
                f"外部へ追跡しません: {candidate}"
            )

    if lexical_anchor is None:
        return
    try:
        real_anchor = lexical_anchor.resolve(strict=True)
        real_path = lexical_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise AdapterError(
            f"{description}の実体パスを安全確認できません: {lexical_path} ({exc})"
        ) from exc
    if not real_path.is_relative_to(real_anchor):
        raise AdapterError(
            f"{description}が選択したルート外へsymlinkまたはjunctionで"
            f"転送されています: {lexical_path}"
        )


def safe_is_directory(
    path: Path,
    description: str,
    *,
    anchor: Path | None = None,
) -> bool:
    """Return whether ``path`` is a real directory without following links."""

    raw_path = Path(path)
    reject_reparse_ancestors(raw_path, description, anchor=anchor)
    path = _absolute(raw_path)
    try:
        lexical_anchor = _absolute(Path(anchor)) if anchor is not None else None
        value = path.stat() if lexical_anchor == path else path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdapterError(f"{description}を安全確認できません: {path} ({exc})") from exc
    return stat.S_ISDIR(value.st_mode)


def safe_is_regular_file(
    path: Path,
    description: str,
    *,
    anchor: Path | None = None,
) -> bool:
    """Return whether ``path`` is a real regular file without following links."""

    raw_path = Path(path)
    reject_reparse_ancestors(raw_path, description, anchor=anchor)
    path = _absolute(raw_path)
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdapterError(f"{description}を安全確認できません: {path} ({exc})") from exc
    return stat.S_ISREG(value.st_mode)


def find_regular_files_no_reparse(
    root: Path,
    expected_name: str,
    description: str,
) -> list[Path]:
    """Find exact-name files recursively without entering links/reparse points.

    Reparse subtrees unrelated to the requested source are ignored rather than
    making an otherwise valid instance unusable. Every returned file is
    checked again together with its ancestors before the caller may inspect
    its contents.
    """

    raw_root = Path(root)
    if not safe_is_directory(raw_root, description, anchor=raw_root):
        return []
    root = _absolute(raw_root)
    expected = expected_name.casefold()
    return _find_matching_regular_files_no_reparse(
        root,
        lambda name: name.casefold() == expected,
        description,
    )


def find_regular_files_by_suffix_no_reparse(
    root: Path,
    suffix: str,
    description: str,
) -> list[Path]:
    """Find files by a case-insensitive suffix without following links."""

    raw_root = Path(root)
    if not safe_is_directory(raw_root, description, anchor=raw_root):
        return []
    expected_suffix = suffix.casefold()
    return _find_matching_regular_files_no_reparse(
        _absolute(raw_root),
        lambda name: name.casefold().endswith(expected_suffix),
        description,
    )


def _find_matching_regular_files_no_reparse(
    root: Path,
    matches: Callable[[str], bool],
    description: str,
) -> list[Path]:
    root = _absolute(root)
    result: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        # Revalidate before each scandir so a directory found earlier cannot
        # silently become a link between discovery steps.
        if not safe_is_directory(current, description, anchor=root):
            continue
        try:
            with os.scandir(current) as entries:
                children = sorted(
                    entries,
                    key=lambda entry: (entry.name.casefold(), entry.name),
                    reverse=True,
                )
        except OSError as exc:
            raise AdapterError(f"{description}を安全に走査できません: {current} ({exc})") from exc
        for entry in children:
            candidate = Path(entry.path)
            try:
                candidate_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise AdapterError(
                    f"{description}内の項目を安全確認できません: {candidate} ({exc})"
                ) from exc
            if _is_reparse_point(candidate, candidate_stat):
                continue
            if stat.S_ISDIR(candidate_stat.st_mode):
                stack.append(candidate)
                continue
            if stat.S_ISREG(candidate_stat.st_mode) and matches(entry.name):
                safe_is_regular_file(candidate, description, anchor=root)
                result.append(candidate)
    return sorted(result, key=lambda item: (str(item).casefold(), str(item)))


def reject_nested_reparse_points(root: Path, description: str) -> None:
    """Reject links below ``root`` without rejecting an aliased root itself.

    Instance directories may legitimately be selected through a symlink or
    junction.  A link *inside* a split locale tree is different: recursive
    discovery would follow it while the source snapshot deliberately records
    it without following it, leaving changes to the linked content invisible.
    """

    root = Path(root)
    stack = [root]
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    candidate = Path(entry.path)
                    candidate_stat = entry.stat(follow_symlinks=False)
                    if _is_reparse_point(candidate, candidate_stat):
                        raise AdapterError(
                            f"{description}内にsymlinkまたはjunctionがあるため、"
                            f"安全に処理できません: {candidate}"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(candidate)
    except AdapterError:
        raise
    except OSError as exc:
        raise AdapterError(f"{description}を安全確認できません: {root} ({exc})") from exc


def validate_split_locale_targets(
    source_dir: Path,
    output_dir: Path,
    target_locale: str,
    relative_paths: Iterable[Path],
) -> list[Path]:
    """Validate every path used by a split locale writer before any write.

    A user may choose a staging directory outside the quest tree.  Inside the
    quest tree, however, only ``lang/<target_locale>`` is a valid destination;
    this prevents a relative path such as ``chapters/welcome.json5`` from
    overwriting the actual quest data when the quest root is selected by
    mistake.
    """

    source_dir = Path(source_dir)
    output_dir = Path(output_dir)
    if source_dir.parent.name.lower() != "lang":
        raise AdapterError(f"locale ディレクトリの配置が不正です: {source_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise AdapterError(f"分割 locale の出力先にはフォルダーを指定してください: {output_dir}")
    if not output_dir.exists() and output_dir.suffix.lower() in {".snbt", ".json", ".json5"}:
        raise AdapterError(f"分割 locale の出力先にはファイルでなくフォルダーを指定してください: {output_dir}")

    lang_root = source_dir.parent
    quest_root = lang_root.parent
    expected_output = lang_root / target_locale.lower()
    lexical_source = _absolute(source_dir)
    lexical_output = _absolute(output_dir)
    lexical_quest = _absolute(quest_root)
    lexical_expected = _absolute(expected_output)
    reject_reparse_ancestors(
        source_dir,
        "分割 locale の原文",
        anchor=quest_root,
    )
    output_anchor = quest_root if lexical_output.is_relative_to(lexical_quest) else None
    reject_nested_reparse_points(source_dir, "分割 locale の原文")
    real_source = source_dir.resolve()
    real_output = output_dir.resolve()
    real_quest = quest_root.resolve()
    real_expected = expected_output.resolve()

    if lexical_output == lexical_source or real_output == real_source:
        raise AdapterError("原文localeディレクトリと同じ場所には書き込めません")
    if lexical_output.is_relative_to(lexical_quest):
        if lexical_output != lexical_expected:
            raise AdapterError(
                "quest ディレクトリ内の出力先は "
                f"lang/{target_locale.lower()} に限定されます: {expected_output}"
            )
        if not real_output.is_relative_to(real_quest) or real_output != real_expected:
            raise AdapterError(
                "翻訳先localeディレクトリがquest外へsymlinkまたはjunctionで"
                "転送されているため書き込めません"
            )
    # A symlink or junction must not redirect an apparently safe destination
    # back into chapters/data files in the quest tree.
    if real_output.is_relative_to(real_quest) and real_output != real_expected:
        raise AdapterError("出力先が quest データ内を参照しているため書き込めません")

    # Preserve the more specific quest-containment diagnostics above for a
    # redirected target itself, then reject a link in any otherwise hidden
    # destination ancestor before inspecting or writing target documents.
    reject_reparse_ancestors(
        output_dir,
        "分割 locale の翻訳先",
        anchor=output_anchor,
    )
    if output_dir.exists():
        reject_nested_reparse_points(output_dir, "分割 locale の既存出力")

    source_files = {
        candidate.resolve()
        for candidate in find_regular_files_by_suffix_no_reparse(
            source_dir,
            "",
            "分割 locale の原文",
        )
    }
    targets: list[Path] = []
    seen: set[Path] = set()
    for relative in relative_paths:
        relative = Path(relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise AdapterError(f"不正な locale 相対パスです: {relative}")
        target = output_dir / relative
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(real_output):
            raise AdapterError(f"出力先フォルダー外を参照する locale パスです: {target}")
        if resolved_target in source_files:
            raise AdapterError(f"原文 locale ファイルには書き込めません: {target}")
        if resolved_target.is_relative_to(real_quest) and not resolved_target.is_relative_to(
            real_expected
        ):
            raise AdapterError(f"quest 本体と衝突する出力先です: {target}")
        if resolved_target in seen:
            raise AdapterError(f"複数の翻訳文書が同じ出力先と衝突します: {target}")
        seen.add(resolved_target)
        targets.append(target)
    return targets


def validate_single_locale_output(
    source_file: Path,
    output_file: Path,
    target_locale: str,
    suffix: str,
) -> Path:
    """Validate a single locale file destination without writing it."""

    source_file = Path(source_file)
    output_file = Path(output_file)
    lexical_source_parent = _absolute(source_file.parent)
    lexical_output_parent = _absolute(output_file.parent)
    output_anchor = (
        source_file.parent
        if lexical_output_parent == lexical_source_parent
        else None
    )
    reject_reparse_ancestors(
        output_file,
        "翻訳先localeファイル",
        anchor=output_anchor,
    )
    if output_file.exists() and output_file.is_dir():
        raise AdapterError(f"locale の出力先にはファイルを指定してください: {output_file}")
    if output_file.suffix.lower() != suffix.lower():
        raise AdapterError(f"出力ファイルの拡張子は {suffix} を指定してください: {output_file}")

    lexical_source = _absolute(source_file)
    lexical_output = _absolute(output_file)
    real_source = source_file.resolve()
    real_output = output_file.resolve()
    if lexical_output == lexical_source or real_output == real_source:
        raise AdapterError("原文localeファイルと同じパスには書き込めません")

    if source_file.parent.name.lower() == "lang":
        quest_root = source_file.parent.parent
        expected = source_file.parent / f"{target_locale.lower()}{suffix}"
        lexical_quest = _absolute(quest_root)
        lexical_expected = _absolute(expected)
        real_quest = quest_root.resolve()
        real_expected = expected.resolve()
        if lexical_output.is_relative_to(lexical_quest):
            if lexical_output != lexical_expected:
                raise AdapterError(
                    "quest ディレクトリ内の出力先は "
                    f"lang/{target_locale.lower()}{suffix} に限定されます: {expected}"
                )
            if not real_output.is_relative_to(real_quest) or real_output != real_expected:
                raise AdapterError(
                    "翻訳先localeファイルがquest外へsymlinkまたはjunctionで"
                    "転送されているため書き込めません"
                )
        if real_output.is_relative_to(real_quest) and real_output != real_expected:
            raise AdapterError("出力先が quest データ内を参照しているため書き込めません")
    return output_file


def paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = Path(first).resolve()
    second_resolved = Path(second).resolve()
    return (
        first_resolved == second_resolved
        or first_resolved.is_relative_to(second_resolved)
        or second_resolved.is_relative_to(first_resolved)
    )


def _absolute(path: Path) -> Path:
    """Normalize ``.``/``..`` without following the final symlink."""

    return Path(os.path.abspath(path))


def _is_reparse_point(path: Path, value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
