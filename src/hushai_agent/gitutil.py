"""记忆库 hub/ 目录 Git 状态检测与自动提交辅助。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def is_hub_git_dirty(vault_path: Path) -> bool:
    """检查 Git 仓库内 hub/ 是否有未提交变更。"""
    if not (vault_path / ".git").exists() or shutil.which("git") is None:
        return False
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain", "--", "hub"],
            cwd=os.fspath(vault_path),
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0 and bool(res.stdout.strip())
    except OSError:
        return False


def commit_hub_if_git(vault_path: Path, message: str) -> bool:
    """当 vault 是 Git 仓库且 hub/ 下有变更时，执行 git add -A hub 并提交一次。"""
    if not (vault_path / ".git").exists() or shutil.which("git") is None:
        return False

    cwd = os.fspath(vault_path)
    try:
        res_add = subprocess.run(
            ["git", "add", "-A", "hub"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if res_add.returncode != 0:
            return False

        res_diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if res_diff.returncode == 0:
            return False

        res_commit = subprocess.run(
            [
                "git",
                "-c",
                "user.name=hushai",
                "-c",
                "user.email=hushai@localhost",
                "commit",
                "-m",
                message,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return res_commit.returncode == 0
    except OSError:
        return False
