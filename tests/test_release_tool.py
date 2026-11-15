"""发布工作流与版本同步校验工具单元测试。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools import release_tool


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr or proc.stdout}")


class TestReleaseTool(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "src" / "hushai_agent").mkdir(parents=True)
        (self.repo / "pyproject.toml").write_text(
            '[project]\nname = "hushai-agent"\nversion = "0.2.0"\n',
            encoding="utf-8",
        )
        (self.repo / "src" / "hushai_agent" / "__init__.py").write_text(
            '"""pkg."""\n\n__version__ = "0.2.0"\n',
            encoding="utf-8",
        )
        (self.repo / "README.md").write_text(
            "## 当前版本的诚实边界（v0.2.0）\n\n- 仅作示例\n",
            encoding="utf-8",
        )
        (self.repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "### Added\n"
            "- 新增自动发布工作流与版本校验。\n\n"
            "## [0.2.0] - 2026-11-14\n\n"
            "### Added\n"
            "- 旧版本功能。\n",
            encoding="utf-8",
        )
        _git(self.repo, "init")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "release(v0.2.0): initial")
        _git(self.repo, "tag", "-a", "v0.2.0", "-m", "v0.2.0")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_01_bump_verify_and_notes_with_commits(self) -> None:
        (self.repo / "extra.txt").write_text("hi\n", encoding="utf-8")
        _git(self.repo, "add", "extra.txt")
        _git(self.repo, "commit", "-m", "fix: keep vault safe")

        res = release_tool.bump_release(self.repo, "v0.2.1", release_date="2026-11-15")
        self.assertTrue(res["ok"])
        self.assertEqual(res["version"], "0.2.1")

        self.assertIn('version = "0.2.1"', (self.repo / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn('__version__ = "0.2.1"', (self.repo / "src" / "hushai_agent" / "__init__.py").read_text(encoding="utf-8"))
        self.assertIn("当前版本的诚实边界（v0.2.1）", (self.repo / "README.md").read_text(encoding="utf-8"))
        cl = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## [Unreleased]", cl)
        self.assertIn("## [0.2.1] - 2026-11-15", cl)
        self.assertIn("新增自动发布工作流与版本校验", cl)

        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "release(v0.2.1): bump version")
        _git(self.repo, "tag", "-a", "v0.2.1", "-m", "v0.2.1: 测试发布")

        notes = release_tool.build_release_notes(self.repo, "0.2.1", include_commits=True)
        self.assertIn("新增自动发布工作流与版本校验", notes)
        self.assertIn("v0.2.0 → v0.2.1", notes)
        self.assertIn("fix: keep vault safe", notes)
        self.assertNotIn("旧版本功能", notes)

    def test_02_verify_rejects_version_mismatch_and_empty_changelog(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            release_tool.verify_release(self.repo, "0.2.1")
        msg = str(ctx.exception)
        self.assertIn("pyproject.toml", msg)
        self.assertIn("__init__.py", msg)
        self.assertIn("CHANGELOG.md", msg)

    def test_03_publish_dry_run_skips_remote_and_formats_release(self) -> None:
        release_tool.bump_release(self.repo, "0.2.1", release_date="2026-11-15")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "tests" / "test_smoke.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n",
            encoding="utf-8",
        )
        res = release_tool.publish_release(
            self.repo,
            "0.2.1",
            title="v0.2.1: 测试发布",
            dry_run=True,
            run_tests=True,
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["tag"], "v0.2.1")
        self.assertEqual(res["title"], "v0.2.1: 测试发布")
        self.assertIn("新增自动发布工作流与版本校验", res["notes"])


if __name__ == "__main__":
    unittest.main()
