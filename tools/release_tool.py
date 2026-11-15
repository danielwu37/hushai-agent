"""版本同步校验、CHANGELOG 提取与自动化发布辅助工具（纯标准库）。

用法示例：
  python3 tools/release_tool.py bump 0.2.1
  python3 tools/release_tool.py verify 0.2.1
  python3 tools/release_tool.py notes 0.2.1 --out /tmp/release-notes.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SEMVER_RE = re.compile(r"^[vV]?(?P<ver>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")
PYPROJECT_VER_RE = re.compile(r'(?m)^version\s*=\s*"([^"]+)"\s*$')
INIT_VER_RE = re.compile(r'(?m)^__version__\s*=\s*"([^"]+)"\s*$')
README_VER_RE = re.compile(r"(当前版本的诚实边界（[vV]?)([^）\s]+)(）)")
CHANGELOG_HDR_RE = re.compile(r"(?m)^## \[([^\]]+)\](?:\s*-\s*([^\s]+))?\s*$")


def normalize_version(raw: str) -> str:
    """规范化版本号（去掉可选前缀 v/V 并校验 X.Y.Z 格式）。"""
    match = SEMVER_RE.match(raw.strip())
    if not match:
        raise ValueError(f"非法版本号格式: {raw!r}（期望如 0.2.1 或 v0.2.1）")
    return match.group("ver")


def _extract_pyproject_version(text: str) -> str:
    m = PYPROJECT_VER_RE.search(text)
    if not m:
        raise ValueError("pyproject.toml 中未找到 version 字段")
    return m.group(1)


def _extract_init_version(text: str) -> str:
    m = INIT_VER_RE.search(text)
    if not m:
        raise ValueError("src/hushai_agent/__init__.py 中未找到 __version__ 字段")
    return m.group(1)


def extract_changelog_section(changelog_text: str, version: str) -> str:
    """从 CHANGELOG.md 提取指定版本（或 Unreleased）的正文内容。"""
    headers = list(CHANGELOG_HDR_RE.finditer(changelog_text))
    for idx, hdr in enumerate(headers):
        if hdr.group(1) == version:
            start = hdr.end()
            end = headers[idx + 1].start() if idx + 1 < len(headers) else len(changelog_text)
            body = changelog_text[start:end].strip()
            if not body:
                raise ValueError(f"CHANGELOG.md 中 [{version}] 章节内容为空")
            return body
    raise ValueError(f"CHANGELOG.md 中未找到 ## [{version}] 章节")


def read_project_versions(root: Path) -> dict[str, str | None]:
    """读取仓库内各处声明的版本号。"""
    pyproject_path = root / "pyproject.toml"
    init_path = root / "src" / "hushai_agent" / "__init__.py"
    readme_path = root / "README.md"

    py_ver = _extract_pyproject_version(pyproject_path.read_text(encoding="utf-8"))
    init_ver = _extract_init_version(init_path.read_text(encoding="utf-8"))
    readme_ver: str | None = None
    if readme_path.is_file():
        m = README_VER_RE.search(readme_path.read_text(encoding="utf-8"))
        if m:
            readme_ver = normalize_version(m.group(2))
    return {
        "pyproject": py_ver,
        "init": init_ver,
        "readme": readme_ver,
    }


def verify_release(root: Path, raw_version: str) -> dict[str, Any]:
    """校验 pyproject.toml、__init__.py、README.md 与 CHANGELOG.md 版本一致且内容非空。"""
    version = normalize_version(raw_version)
    versions = read_project_versions(root)
    errors: list[str] = []
    if versions["pyproject"] != version:
        errors.append(f"pyproject.toml 版本为 {versions['pyproject']}，期望 {version}")
    if versions["init"] != version:
        errors.append(f"src/hushai_agent/__init__.py 版本为 {versions['init']}，期望 {version}")
    if versions["readme"] is not None and versions["readme"] != version:
        errors.append(f"README.md 版本标题为 {versions['readme']}，期望 {version}")

    changelog_path = root / "CHANGELOG.md"
    if not changelog_path.is_file():
        errors.append("缺少 CHANGELOG.md")
    else:
        try:
            extract_changelog_section(changelog_path.read_text(encoding="utf-8"), version)
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        raise ValueError("发布校验失败:\n- " + "\n- ".join(errors))
    return {
        "ok": True,
        "version": version,
        "versions": versions,
    }


def _run_git(root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def previous_version_tag(root: Path, current_tag: str) -> str | None:
    """获取早于 current_tag 的最近一个 v*.*.* 发布标签。"""
    raw_tags = _run_git(root, ["tag", "-l", "v*", "--sort=-v:refname"])
    if not raw_tags:
        return None
    tags = [t.strip() for t in raw_tags.splitlines() if t.strip()]
    if current_tag in tags:
        idx = tags.index(current_tag)
        return tags[idx + 1] if idx + 1 < len(tags) else None
    for tag in tags:
        if tag != current_tag:
            return tag
    return None


def collect_commit_lines(root: Path, version: str) -> tuple[str | None, str, list[str]]:
    """收集从上一个发布标签到当前目标标签（或 HEAD）的非合并提交清单。"""
    tag = f"v{version}"
    has_tag = bool(_run_git(root, ["rev-parse", "--verify", f"refs/tags/{tag}"]))
    head_ref = tag if has_tag else "HEAD"
    prev_tag = previous_version_tag(root, tag)
    rev_range = f"{prev_tag}..{head_ref}" if prev_tag else head_ref
    out = _run_git(root, ["log", "--no-merges", "--pretty=format:- `%h` %s", rev_range])
    lines = [line for line in out.splitlines() if line.strip()]
    return prev_tag, head_ref, lines


def build_release_notes(root: Path, raw_version: str, *, include_commits: bool = True) -> str:
    """生成可直接用于 GitHub Releases 的中文更新说明。"""
    version = normalize_version(raw_version)
    changelog_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    section = extract_changelog_section(changelog_text, version)
    parts = [section]
    if include_commits:
        prev_tag, head_ref, commits = collect_commit_lines(root, version)
        if commits:
            span = f"{prev_tag} → {head_ref}" if prev_tag else head_ref
            parts.append("\n---\n")
            parts.append(f"### 包含提交清单（`{span}`）\n")
            parts.append("\n".join(commits))
    return "\n".join(parts).rstrip() + "\n"


def bump_release(root: Path, raw_version: str, *, release_date: str | None = None) -> dict[str, Any]:
    """同步版本号并将 CHANGELOG.md 的 [Unreleased] 升格为指定发布版本。"""
    version = normalize_version(raw_version)
    rdate = release_date or dt.date.today().isoformat()

    changelog_path = root / "CHANGELOG.md"
    cl_text = changelog_path.read_text(encoding="utf-8")
    existing_headers = [m.group(1) for m in CHANGELOG_HDR_RE.finditer(cl_text)]
    if version in existing_headers:
        raise ValueError(f"CHANGELOG.md 中已存在 ## [{version}] 章节，拒绝重复升级")

    unreleased_body = extract_changelog_section(cl_text, "Unreleased")
    hdrs = list(CHANGELOG_HDR_RE.finditer(cl_text))
    unrel_idx = next(i for i, h in enumerate(hdrs) if h.group(1) == "Unreleased")
    unrel_hdr = hdrs[unrel_idx]
    next_start = hdrs[unrel_idx + 1].start() if unrel_idx + 1 < len(hdrs) else len(cl_text)
    new_block = (
        "## [Unreleased]\n\n"
        "<!-- 后续新增改动先写在此处，下个版本发布时自动归档至版本章节 -->\n\n"
        f"## [{version}] - {rdate}\n\n"
        f"{unreleased_body}\n\n"
    )
    updated_cl = cl_text[: unrel_hdr.start()] + new_block + cl_text[next_start:].lstrip("\n")

    pyproject_path = root / "pyproject.toml"
    py_text = pyproject_path.read_text(encoding="utf-8")
    updated_py, py_cnt = PYPROJECT_VER_RE.subn(f'version = "{version}"', py_text, count=1)
    if py_cnt != 1:
        raise ValueError("更新 pyproject.toml version 失败")

    init_path = root / "src" / "hushai_agent" / "__init__.py"
    init_text = init_path.read_text(encoding="utf-8")
    updated_init, init_cnt = INIT_VER_RE.subn(f'__version__ = "{version}"', init_text, count=1)
    if init_cnt != 1:
        raise ValueError("更新 src/hushai_agent/__init__.py __version__ 失败")

    readme_path = root / "README.md"
    updated_readme: str | None = None
    if readme_path.is_file():
        rd_text = readme_path.read_text(encoding="utf-8")
        if README_VER_RE.search(rd_text):
            updated_readme = README_VER_RE.sub(rf"\g<1>{version}\g<3>", rd_text, count=1)

    pyproject_path.write_text(updated_py, encoding="utf-8")
    init_path.write_text(updated_init, encoding="utf-8")
    changelog_path.write_text(updated_cl, encoding="utf-8")
    if updated_readme is not None:
        readme_path.write_text(updated_readme, encoding="utf-8")

    return verify_release(root, version)


def _run_cmd_checked(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ValueError(f"命令失败 ({' '.join(cmd)}): {detail[-600:]}")
    return proc.stdout.strip()


def publish_release(
    root: Path,
    raw_version: str,
    *,
    title: str | None = None,
    dry_run: bool = False,
    run_tests: bool = True,
) -> dict[str, Any]:
    """一键本地自动化发布：校验版本、跑通单测、推送标签并自动创建/更新 GitHub Release。"""
    version = normalize_version(raw_version)
    tag = f"v{version}"
    verify_release(root, version)

    if run_tests:
        test_env = os.environ.copy()
        test_env["PYTHONPATH"] = str(root / "src")
        _run_cmd_checked(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            cwd=root,
            env=test_env,
        )

    notes = build_release_notes(root, version, include_commits=True)
    has_tag = bool(_run_git(root, ["rev-parse", "--verify", f"refs/tags/{tag}"]))
    tag_subject = _run_git(root, ["for-each-ref", "--format=%(contents:subject)", f"refs/tags/{tag}"])
    resolved_title = (title or "").strip() or (tag_subject.splitlines()[0].strip() if tag_subject else "") or tag

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "version": version,
            "tag": tag,
            "title": resolved_title,
            "tag_exists": has_tag,
            "notes": notes,
        }

    dirty = _run_git(root, ["status", "--porcelain"])
    if dirty:
        raise ValueError("工作区尚有未提交修改，请先提交后再执行 publish。")

    if not has_tag:
        _run_cmd_checked(["git", "-C", str(root), "tag", "-a", tag, "-m", resolved_title], cwd=root)

    _run_cmd_checked(["git", "-C", str(root), "push", "origin", "HEAD", tag], cwd=root)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as tf:
        tf.write(notes)
        notes_file = Path(tf.name)
    try:
        exists_proc = subprocess.run(
            ["gh", "release", "view", tag],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if exists_proc.returncode == 0:
            _run_cmd_checked(
                ["gh", "release", "edit", tag, "--title", resolved_title, "--notes-file", str(notes_file)],
                cwd=root,
            )
            action = "updated"
        else:
            _run_cmd_checked(
                ["gh", "release", "create", tag, "--verify-tag", "--title", resolved_title, "--notes-file", str(notes_file)],
                cwd=root,
            )
            action = "created"
    finally:
        notes_file.unlink(missing_ok=True)

    return {
        "ok": True,
        "dry_run": False,
        "action": action,
        "version": version,
        "tag": tag,
        "title": resolved_title,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="hushAi-agent 版本与发布工作流辅助工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("verify", "notes", "bump", "publish"):
        p = sub.add_parser(name)
        p.add_argument("version", help="目标版本号（如 0.2.1 或 v0.2.1）")
        p.add_argument("--root", default=".", help="仓库根目录（默认当前目录）")
        if name == "notes":
            p.add_argument("--no-commits", action="store_true", help="不附加提交清单")
            p.add_argument("--out", default=None, help="输出文件路径（默认打印到标准输出）")
        elif name == "bump":
            p.add_argument("--date", default=None, help="发布日期 YYYY-MM-DD（默认今天）")
        elif name == "publish":
            p.add_argument("--title", default=None, help="GitHub Release 标题（默认读取带附注标签主题）")
            p.add_argument("--dry-run", action="store_true", help="仅校验并跑单测，不推送与发版")
            p.add_argument("--skip-tests", action="store_true", help="跳过单元测试（若刚完整跑过测试）")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        if args.cmd == "verify":
            res = verify_release(root, args.version)
            print(f"OK: 版本 {res['version']} 校验一致（pyproject / __init__ / README / CHANGELOG）。")
            return 0
        if args.cmd == "notes":
            notes = build_release_notes(root, args.version, include_commits=not args.no_commits)
            if args.out:
                Path(args.out).write_text(notes, encoding="utf-8")
            else:
                sys.stdout.write(notes)
            return 0
        if args.cmd == "bump":
            res = bump_release(root, args.version, release_date=args.date)
            print(f"OK: 已升级至版本 {res['version']}。")
            return 0
        if args.cmd == "publish":
            res = publish_release(
                root,
                args.version,
                title=args.title,
                dry_run=args.dry_run,
                run_tests=not args.skip_tests,
            )
            if res["dry_run"]:
                print(f"DRY-RUN OK: {res['tag']} ({res['title']})")
            else:
                print(f"OK: GitHub Release {res['tag']} ({res['action']}): {res['title']}")
            return 0
    except (ValueError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
