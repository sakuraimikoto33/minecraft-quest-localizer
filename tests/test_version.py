from __future__ import annotations

from pathlib import Path
import sys
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mq_localizer import __version__  # noqa: E402


class VersionMetadataTests(unittest.TestCase):
    def test_package_version_matches_pyproject(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project_version = tomllib.load(handle)["project"]["version"]

        self.assertEqual(__version__, project_version)


if __name__ == "__main__":
    unittest.main()
