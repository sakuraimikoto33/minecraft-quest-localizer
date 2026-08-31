from __future__ import annotations

from dataclasses import dataclass
import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any, Iterable

from packaging.version import InvalidVersion, Version


APPLICATION_FILES = frozenset({"launch.pyw", "pyproject.toml"})
APPLICATION_PREFIXES = ("src/",)
PYPROJECT_PATH = "pyproject.toml"
PACKAGE_INIT_PATH = "src/mq_localizer/__init__.py"


class PlanError(RuntimeError):
    """The workflow cannot make a safe build or release decision."""


@dataclass(frozen=True, slots=True)
class VersionedRelease:
    tag: str
    version: Version
    draft: bool
    prerelease: bool


@dataclass(frozen=True, slots=True)
class ReleasePlan:
    build_required: bool
    release_required: bool
    version_updated: bool
    version_is_newer: bool
    version: str
    tag: str
    asset_name: str
    prerelease: bool
    tag_exists: bool
    previous_version: str
    previous_tag: str
    changed_paths: tuple[str, ...]
    ignored_release_tags: tuple[str, ...]
    reason: str

    def github_outputs(self) -> dict[str, str]:
        return {
            "build_required": _bool_output(self.build_required),
            "release_required": _bool_output(self.release_required),
            "version_updated": _bool_output(self.version_updated),
            "version_is_newer": _bool_output(self.version_is_newer),
            "version": self.version,
            "tag": self.tag,
            "asset_name": self.asset_name,
            "prerelease": _bool_output(self.prerelease),
            "tag_exists": _bool_output(self.tag_exists),
            "previous_version": self.previous_version,
            "previous_tag": self.previous_tag,
            "reason": self.reason,
        }


def _bool_output(value: bool) -> str:
    return "true" if value else "false"


def _run_git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PlanError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def _resolve_commit(repository: Path, revision: str) -> str:
    result = _run_git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    return result.stdout.decode("ascii").strip()


def _resolve_tag(repository: Path, tag: str) -> str | None:
    result = _run_git(
        repository,
        "rev-parse",
        "--verify",
        f"refs/tags/{tag}^{{commit}}",
        check=False,
    )
    if result.returncode:
        return None
    return result.stdout.decode("ascii").strip()


def _read_commit_file(repository: Path, commit: str, relative_path: str) -> bytes:
    result = _run_git(repository, "show", f"{commit}:{relative_path}", check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PlanError(
            f"Cannot read {relative_path} from commit {commit[:12]}: {detail}"
        )
    return result.stdout


def _project_version(raw_toml: bytes, source: str) -> tuple[str, Version]:
    try:
        parsed = tomllib.loads(raw_toml.decode("utf-8"))
        raw_version = parsed["project"]["version"]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise PlanError(f"Cannot read [project].version from {source}: {exc}") from exc
    if not isinstance(raw_version, str) or not raw_version:
        raise PlanError(f"[project].version in {source} must be a non-empty string")
    try:
        version = Version(raw_version)
    except InvalidVersion as exc:
        raise PlanError(
            f"[project].version in {source} is not a valid PEP 440 version: "
            f"{raw_version!r}"
        ) from exc
    canonical = str(version)
    if raw_version != canonical:
        raise PlanError(
            f"[project].version in {source} must use canonical PEP 440 spelling: "
            f"{raw_version!r} should be {canonical!r}"
        )
    return raw_version, version


def _package_init_version(raw_python: bytes, source: str) -> str:
    try:
        module = ast.parse(raw_python.decode("utf-8"), filename=source)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise PlanError(f"Cannot parse {source}: {exc}") from exc

    values: list[str] = []
    for node in module.body:
        value_node: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value_node = node.value
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            value_node = node.value
            targets = [node.target]
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            if not isinstance(value_node, ast.Constant) or not isinstance(
                value_node.value, str
            ):
                raise PlanError(f"{source} must assign a string literal to __version__")
            values.append(value_node.value)

    if len(values) != 1:
        raise PlanError(f"{source} must assign __version__ exactly once")
    return values[0]


def _version_from_tag(tag: str) -> Version | None:
    candidate = tag[1:] if tag[:1].lower() == "v" else tag
    if not candidate:
        return None
    try:
        return Version(candidate)
    except InvalidVersion:
        return None


def _flatten_release_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise PlanError("GitHub Releases response must be a JSON array")
    if payload and all(isinstance(page, list) for page in payload):
        entries = [entry for page in payload for entry in page]
    else:
        entries = payload
    if not all(isinstance(entry, dict) for entry in entries):
        raise PlanError("GitHub Releases response contains a non-object entry")
    return entries


def load_release_payload(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return _flatten_release_payload(json.load(handle))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"Cannot read GitHub Releases JSON from {path}: {exc}") from exc


def _release_records(
    entries: Iterable[dict[str, Any]],
) -> tuple[list[VersionedRelease], tuple[str, ...], dict[Version, tuple[str, ...]]]:
    versioned: list[VersionedRelease] = []
    ignored: set[str] = set()
    draft_tags_by_version: dict[Version, set[str]] = {}
    for entry in entries:
        tag = entry.get("tag_name")
        if not isinstance(tag, str) or not tag:
            raise PlanError("A GitHub Release is missing its tag_name")
        draft = bool(entry.get("draft", False))
        prerelease = bool(entry.get("prerelease", False))
        if draft:
            draft_version = _version_from_tag(tag)
            if draft_version is not None:
                draft_tags_by_version.setdefault(draft_version, set()).add(tag)
            continue
        version = _version_from_tag(tag)
        if version is None:
            ignored.add(tag)
            continue
        versioned.append(
            VersionedRelease(
                tag=tag,
                version=version,
                draft=False,
                prerelease=prerelease,
            )
        )
    return (
        versioned,
        tuple(sorted(ignored)),
        {
            version: tuple(sorted(tags))
            for version, tags in draft_tags_by_version.items()
        },
    )


def _latest_versioned_release(
    releases: Iterable[VersionedRelease],
) -> VersionedRelease | None:
    records = list(releases)
    if not records:
        return None
    latest_version = max(record.version for record in records)
    matches = [record for record in records if record.version == latest_version]
    if len(matches) != 1:
        tags = ", ".join(sorted(record.tag for record in matches))
        raise PlanError(
            f"Multiple published Releases represent version {latest_version}: {tags}"
        )
    return matches[0]


def _changed_paths(
    repository: Path,
    head_commit: str,
    previous_commit: str | None,
) -> tuple[str, ...]:
    if previous_commit is None:
        result = _run_git(
            repository,
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            head_commit,
        )
    else:
        result = _run_git(
            repository,
            "diff",
            "--name-only",
            "-z",
            previous_commit,
            head_commit,
        )
    paths = {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    }
    return tuple(sorted(path for path in paths if _is_application_path(path)))


def _is_application_path(path: str) -> bool:
    return path in APPLICATION_FILES or any(
        path.startswith(prefix) for prefix in APPLICATION_PREFIXES
    )


def create_release_plan(
    repository: Path,
    release_entries: Iterable[dict[str, Any]],
    *,
    head: str = "HEAD",
) -> ReleasePlan:
    repository = repository.resolve()
    head_commit = _resolve_commit(repository, head)
    raw_version, current_version = _project_version(
        _read_commit_file(repository, head_commit, PYPROJECT_PATH),
        f"{PYPROJECT_PATH} at {head_commit[:12]}",
    )
    init_version = _package_init_version(
        _read_commit_file(repository, head_commit, PACKAGE_INIT_PATH),
        f"{PACKAGE_INIT_PATH} at {head_commit[:12]}",
    )
    if init_version != raw_version:
        raise PlanError(
            f"Version mismatch: {PYPROJECT_PATH} declares {raw_version!r}, but "
            f"{PACKAGE_INIT_PATH} declares {init_version!r}"
        )

    releases, ignored_tags, draft_tags_by_version = _release_records(release_entries)
    previous = _latest_versioned_release(releases)
    previous_commit: str | None = None
    previous_version = ""
    previous_tag = ""
    if previous is not None:
        previous_commit = _resolve_tag(repository, previous.tag)
        if previous_commit is None:
            raise PlanError(
                f"Published Release tag {previous.tag!r} is not available in the Git checkout"
            )
        previous_raw, previous_project_version = _project_version(
            _read_commit_file(repository, previous_commit, PYPROJECT_PATH),
            f"{PYPROJECT_PATH} at Release {previous.tag}",
        )
        if previous_project_version != previous.version:
            raise PlanError(
                f"Release {previous.tag!r} represents {previous.version}, but its "
                f"{PYPROJECT_PATH} declares {previous_raw!r}"
            )
        previous_version = str(previous.version)
        previous_tag = previous.tag

    changed_paths = _changed_paths(repository, head_commit, previous_commit)
    build_required = bool(changed_paths)
    version_updated = previous is None or current_version != previous.version
    version_is_newer = previous is None or current_version > previous.version
    canonical_version = str(current_version)
    tag = f"v{canonical_version}"
    asset_name = f"MinecraftQuestLocalizer-v{canonical_version}-windows-x64.exe"
    existing_tag_commit = _resolve_tag(repository, tag)
    tag_exists = existing_tag_commit is not None
    equivalent_draft_tags = draft_tags_by_version.get(current_version, ())

    release_required = False
    if not build_required:
        reason = "No application changes exist since the latest versioned Release."
    elif not version_updated:
        reason = "The project version has not changed since the latest Release."
    elif not version_is_newer:
        reason = "The project version is not newer than the latest Release."
    elif equivalent_draft_tags:
        draft_list = ", ".join(repr(item) for item in equivalent_draft_tags)
        reason = (
            f"Draft Release tag(s) {draft_list} already represent version "
            f"{canonical_version}; they will not be modified automatically."
        )
    elif existing_tag_commit is not None and existing_tag_commit != head_commit:
        reason = f"Git tag {tag} already points to a different commit."
    else:
        release_required = True
        reason = "A newer synchronized project version is ready for Release."

    return ReleasePlan(
        build_required=build_required,
        release_required=release_required,
        version_updated=version_updated,
        version_is_newer=version_is_newer,
        version=canonical_version,
        tag=tag,
        asset_name=asset_name,
        prerelease=current_version.is_prerelease or current_version.is_devrelease,
        tag_exists=tag_exists,
        previous_version=previous_version,
        previous_tag=previous_tag,
        changed_paths=changed_paths,
        ignored_release_tags=ignored_tags,
        reason=reason,
    )


def _write_github_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise PlanError(f"GitHub output {name!r} contains a newline")
            handle.write(f"{name}={value}\n")


def _append_step_summary(path: Path, plan: ReleasePlan) -> None:
    changed = ", ".join(f"`{item}`" for item in plan.changed_paths) or "none"
    previous = f"`{plan.previous_tag}`" if plan.previous_tag else "none"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("## Package / Release plan\n\n")
        handle.write(f"- Current version: `{plan.version}`\n")
        handle.write(f"- Previous versioned Release: {previous}\n")
        handle.write(f"- Application changes: {changed}\n")
        handle.write(f"- Build required: `{_bool_output(plan.build_required)}`\n")
        handle.write(f"- Release required: `{_bool_output(plan.release_required)}`\n")
        handle.write(f"- Decision: {plan.reason}\n")
        if plan.ignored_release_tags:
            handle.write(
                "- Ignored non-version Release tags: "
                + ", ".join(f"`{tag}`" for tag in plan.ignored_release_tags)
                + "\n"
            )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan a Windows build and a version-gated GitHub Release."
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--releases-json", type=Path, required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--step-summary", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    try:
        plan = create_release_plan(
            arguments.repository,
            load_release_payload(arguments.releases_json),
            head=arguments.head,
        )
        if arguments.github_output is not None:
            _write_github_outputs(arguments.github_output, plan.github_outputs())
        if arguments.step_summary is not None:
            _append_step_summary(arguments.step_summary, plan)
    except PlanError as exc:
        print(f"Release planning failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                **plan.github_outputs(),
                "changed_paths": list(plan.changed_paths),
                "ignored_release_tags": list(plan.ignored_release_tags),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
