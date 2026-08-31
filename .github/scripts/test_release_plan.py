from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from release_plan import PlanError, create_release_plan


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _release(tag: str, *, draft: bool = False, prerelease: bool = False) -> dict[str, object]:
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease}


class RepositoryFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        _git(root, "init", "--quiet")
        _git(root, "config", "user.name", "Release Plan Tests")
        _git(root, "config", "user.email", "release-plan@example.invalid")

    def commit(
        self,
        version: str,
        *,
        init_version: str | None = None,
        source_marker: str = "initial",
        message: str = "application",
    ) -> str:
        package = self.root / "src" / "mq_localizer"
        package.mkdir(parents=True, exist_ok=True)
        (self.root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "minecraft-quest-localizer"\n'
            f'version = "{version}"\n',
            encoding="utf-8",
        )
        (self.root / "launch.pyw").write_text(
            "from mq_localizer.app import main\nmain()\n",
            encoding="utf-8",
        )
        (package / "__init__.py").write_text(
            f'__version__ = "{init_version or version}"\n',
            encoding="utf-8",
        )
        (package / "app.py").write_text(
            f'MARKER = "{source_marker}"\n',
            encoding="utf-8",
        )
        _git(self.root, "add", "pyproject.toml", "launch.pyw", "src")
        _git(self.root, "commit", "--quiet", "-m", message)
        return _git(self.root, "rev-parse", "HEAD")

    def docs_commit(self, marker: str = "docs") -> str:
        (self.root / "README.md").write_text(marker + "\n", encoding="utf-8")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "--quiet", "-m", "docs")
        return _git(self.root, "rev-parse", "HEAD")

    def tag(self, name: str, revision: str = "HEAD") -> None:
        _git(self.root, "tag", name, revision)


class ReleasePlanTests(unittest.TestCase):
    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], RepositoryFixture]:
        temporary = tempfile.TemporaryDirectory()
        return temporary, RepositoryFixture(Path(temporary.name))

    def test_first_release_builds_and_releases(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            repository.commit("1.0.0")
            plan = create_release_plan(repository.root, [])

        self.assertTrue(plan.build_required)
        self.assertTrue(plan.version_updated)
        self.assertTrue(plan.version_is_newer)
        self.assertTrue(plan.release_required)
        self.assertEqual(plan.tag, "v1.0.0")
        self.assertEqual(
            plan.asset_name,
            "MinecraftQuestLocalizer-v1.0.0-windows-x64.exe",
        )

    def test_docs_only_change_after_release_does_not_build(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.0.0")
            repository.tag("v1.0.0", released)
            repository.docs_commit()
            plan = create_release_plan(repository.root, [_release("v1.0.0")])

        self.assertFalse(plan.build_required)
        self.assertFalse(plan.release_required)
        self.assertEqual(plan.changed_paths, ())

    def test_application_change_without_version_change_builds_but_does_not_release(
        self,
    ) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.0.0")
            repository.tag("v1.0.0", released)
            repository.commit("1.0.0", source_marker="changed")
            plan = create_release_plan(repository.root, [_release("v1.0.0")])

        self.assertTrue(plan.build_required)
        self.assertFalse(plan.version_updated)
        self.assertFalse(plan.release_required)
        self.assertIn("src/mq_localizer/app.py", plan.changed_paths)

    def test_pep440_comparison_treats_1_10_as_newer_than_1_9(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.9.0")
            repository.tag("v1.9.0", released)
            repository.commit("1.10.0", source_marker="new")
            plan = create_release_plan(repository.root, [_release("v1.9.0")])

        self.assertTrue(plan.version_is_newer)
        self.assertTrue(plan.release_required)

    def test_older_project_version_builds_but_never_releases(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("2.0.0")
            repository.tag("v2.0.0", released)
            repository.commit("1.5.0", source_marker="older")
            plan = create_release_plan(repository.root, [_release("v2.0.0")])

        self.assertTrue(plan.build_required)
        self.assertTrue(plan.version_updated)
        self.assertFalse(plan.version_is_newer)
        self.assertFalse(plan.release_required)

    def test_package_and_pyproject_versions_must_match(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            repository.commit("1.0.0", init_version="0.9.0")
            with self.assertRaisesRegex(PlanError, "Version mismatch"):
                create_release_plan(repository.root, [])

    def test_stable_version_is_newer_than_its_release_candidate(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("2.0.0rc1")
            repository.tag("v2.0.0rc1", released)
            repository.commit("2.0.0", source_marker="stable")
            plan = create_release_plan(
                repository.root,
                [_release("v2.0.0rc1", prerelease=True)],
            )

        self.assertFalse(plan.prerelease)
        self.assertTrue(plan.release_required)

    def test_prerelease_output_is_marked(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.0.0")
            repository.tag("v1.0.0", released)
            repository.commit("1.1.0rc1", source_marker="candidate")
            plan = create_release_plan(repository.root, [_release("v1.0.0")])

        self.assertTrue(plan.prerelease)
        self.assertTrue(plan.release_required)

    def test_matching_draft_release_blocks_automatic_publication(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.0.0")
            repository.tag("v1.0.0", released)
            repository.commit("1.1.0", source_marker="new")
            plan = create_release_plan(
                repository.root,
                [_release("v1.0.0"), _release("v1.1.0", draft=True)],
            )

        self.assertTrue(plan.build_required)
        self.assertFalse(plan.release_required)
        self.assertIn("Draft Release", plan.reason)

    def test_equivalent_draft_version_blocks_automatic_publication(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.0.0")
            repository.tag("v1.0.0", released)
            repository.commit("1.1.0", source_marker="new")
            plan = create_release_plan(
                repository.root,
                [_release("v1.0.0"), _release("1.1", draft=True)],
            )

        self.assertTrue(plan.build_required)
        self.assertFalse(plan.release_required)
        self.assertIn("1.1", plan.reason)

    def test_existing_tag_on_another_commit_is_not_moved(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.0.0")
            repository.tag("v1.0.0", released)
            wrong_target = repository.commit("1.1.0", source_marker="candidate")
            repository.tag("v1.2.0", wrong_target)
            repository.commit("1.2.0", source_marker="final")
            plan = create_release_plan(repository.root, [_release("v1.0.0")])

        self.assertTrue(plan.build_required)
        self.assertFalse(plan.release_required)
        self.assertTrue(plan.tag_exists)
        self.assertIn("different commit", plan.reason)

    def test_existing_tag_on_head_can_be_used_without_moving_it(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.0.0")
            repository.tag("v1.0.0", released)
            head = repository.commit("1.1.0", source_marker="new")
            repository.tag("v1.1.0", head)
            plan = create_release_plan(repository.root, [_release("v1.0.0")])

        self.assertTrue(plan.release_required)
        self.assertTrue(plan.tag_exists)

    def test_non_version_release_tag_is_ignored(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            repository.commit("1.0.0")
            plan = create_release_plan(repository.root, [_release("nightly")])

        self.assertTrue(plan.release_required)
        self.assertEqual(plan.ignored_release_tags, ("nightly",))

    def test_duplicate_latest_versioned_releases_fail_closed(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.0.0")
            repository.tag("v1.0.0", released)
            repository.tag("1.0.0", released)
            repository.commit("1.1.0", source_marker="new")
            with self.assertRaisesRegex(PlanError, "Multiple published Releases"):
                create_release_plan(
                    repository.root,
                    [_release("v1.0.0"), _release("1.0.0")],
                )

    def test_missing_published_release_tag_fails_closed(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            repository.commit("1.1.0")
            with self.assertRaisesRegex(PlanError, "not available"):
                create_release_plan(repository.root, [_release("v1.0.0")])

    def test_previous_release_tag_must_match_its_pyproject_version(self) -> None:
        temporary, repository = self.fixture()
        with temporary:
            released = repository.commit("1.0.0")
            repository.tag("v2.0.0", released)
            repository.commit("2.1.0", source_marker="new")
            with self.assertRaisesRegex(PlanError, "represents 2.0.0"):
                create_release_plan(repository.root, [_release("v2.0.0")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
