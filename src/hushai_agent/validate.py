"""按规范校验任意 vault 目录（无需配置）。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from hushai_agent.inboxfmt import (
    InboxFormatError,
    is_ignorable_inbox_line,
    parse_inbox_filename,
    parse_inbox_line,
)
from hushai_agent.items import ItemFormatError, parse_item_line

REQUIRED_DIRS = (
    "hub",
    "hub/inbox",
    "hub/reports",
    "hub/exchange/in",
    "hub/exchange/out",
    "domains",
)

REQUIRED_FILES = (
    "hub/AGENTS.md",
    "hub/AGENTS.local.md",
    "hub/profile.md",
    "hub/status.md",
    "hub/status-index.md",
    "hub/open-items.md",
    "hub/today.md",
    "hub/insights.md",
    "hub/decisions.md",
    "hub/lessons.md",
)

PAREN_COPY_RE = re.compile(r"^(?P<stem>.+) \(\d+\)(?P<ext>\.[^.]+)?$")
SPACE_NUM_RE = re.compile(r"^(?P<stem>.+) \d+(?P<ext>\.[^.]+)?$")


@dataclass(frozen=True)
class CheckItem:
    """单项检查结果。"""

    id: str
    level: str  # "OK" | "WARN" | "FAIL"
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "level": self.level, "message": self.message}


def is_sync_conflict_filename(
    name: str | Path,
    existing_names: Iterable[str] | None = None,
) -> bool:
    """判断文件名是否符合同步工具冲突副本特征。

    - 含 conflict（忽略大小写）、含 冲突、含 .sync-conflict- 直接命中。
    - 形如 '名字 数字.扩展名' 或 '名字 (1).扩展名' 的文件，要求同目录存在原文件才算冲突。
    """
    path_obj = Path(name)
    fname = path_obj.name
    lower = fname.lower()
    if "conflict" in lower or ".sync-conflict-" in lower:
        return True
    if "冲突" in fname:
        return True

    candidates: list[str] = []
    for regex in (PAREN_COPY_RE, SPACE_NUM_RE):
        match = regex.match(fname)
        if match:
            ext = match.group("ext") or ""
            candidates.append(f"{match.group('stem')}{ext}")

    if not candidates:
        return False

    if existing_names is not None:
        name_set = set(existing_names)
        return any(orig in name_set for orig in candidates)

    if path_obj.parent != Path(""):
        return any((path_obj.parent / orig).exists() for orig in candidates)

    return False


def check_vault_structure(vault_path: Path) -> CheckItem:
    """检查 vault 目录结构与必需文件是否齐全。"""
    if not vault_path.exists() or not vault_path.is_dir():
        return CheckItem(id="vault-structure", level="FAIL", message=f"vault 目录不存在: {vault_path}")

    missing: list[str] = []
    for rel_dir in REQUIRED_DIRS:
        if not (vault_path / rel_dir).is_dir():
            missing.append(f"{rel_dir}/")

    for rel_file in REQUIRED_FILES:
        if not (vault_path / rel_file).is_file():
            missing.append(rel_file)

    domains_dir = vault_path / "domains"
    if domains_dir.is_dir():
        for child in sorted(domains_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if not (child / "AGENTS.md").is_file():
                    missing.append(f"domains/{child.name}/AGENTS.md")

    if missing:
        return CheckItem(id="vault-structure", level="FAIL", message=f"缺失必需目录或文件: {', '.join(missing)}")
    return CheckItem(id="vault-structure", level="OK", message="目录与必需文件齐全")


def check_inbox_names(vault_path: Path) -> CheckItem:
    """检查 hub/inbox/ 下所有收件文件名是否合法。"""
    inbox_dir = vault_path / "hub" / "inbox"
    if not inbox_dir.is_dir():
        return CheckItem(id="inbox-names", level="FAIL", message="hub/inbox 目录不存在")

    invalid: list[str] = []
    for entry in sorted(inbox_dir.iterdir()):
        if entry.name == ".gitkeep":
            continue
        if entry.is_dir():
            invalid.append(f"{entry.name}/ (不允许子目录)")
            continue
        try:
            parse_inbox_filename(entry.name)
        except InboxFormatError as exc:
            invalid.append(f"{entry.name} ({exc})")

    if invalid:
        return CheckItem(id="inbox-names", level="FAIL", message=f"非法收件文件名: {'; '.join(invalid)}")
    return CheckItem(id="inbox-names", level="OK", message="所有收件文件名合法")


def check_inbox_lines(
    vault_path: Path,
    allowed_domains: Iterable[str] | None = None,
) -> CheckItem:
    """检查 hub/inbox/ 下所有收件文件的行格式是否合法。"""
    inbox_dir = vault_path / "hub" / "inbox"
    if not inbox_dir.is_dir():
        return CheckItem(
            id="inbox-lines",
            level="FAIL",
            message="hub/inbox 目录不存在",
        )

    errors: list[str] = []
    for entry in sorted(inbox_dir.iterdir()):
        if entry.name == ".gitkeep" or entry.is_dir():
            continue
        try:
            lines = entry.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{entry.name}: 无法读取 ({exc})")
            continue

        for lineno, line in enumerate(lines, start=1):
            if is_ignorable_inbox_line(line):
                continue
            try:
                parse_inbox_line(line, allowed_domains=allowed_domains)
            except InboxFormatError as exc:
                errors.append(f"{entry.name}:{lineno}: {exc}")

    if errors:
        return CheckItem(
            id="inbox-lines",
            level="FAIL",
            message=f"收件行格式错误: {'; '.join(errors)}",
        )
    return CheckItem(id="inbox-lines", level="OK", message="所有收件行格式合法")


def check_sync_conflicts(vault_path: Path) -> CheckItem:
    """扫描 vault 内的同步冲突副本文件。"""
    if not vault_path.is_dir():
        return CheckItem(
            id="sync-conflicts",
            level="WARN",
            message="vault 目录不存在，无法扫描冲突文件",
        )

    conflicts: list[str] = []
    for root, dirs, files in os.walk(os.fspath(vault_path)):
        # 跳过 .git 目录
        dirs[:] = [d for d in sorted(dirs) if d != ".git"]
        root_path = Path(root)
        file_set = set(files)
        for fname in sorted(files):
            if is_sync_conflict_filename(fname, existing_names=file_set):
                rel = (root_path / fname).relative_to(vault_path).as_posix()
                conflicts.append(rel)

    if conflicts:
        return CheckItem(
            id="sync-conflicts",
            level="WARN",
            message=f"发现同步冲突副本: {', '.join(conflicts)}",
        )
    return CheckItem(id="sync-conflicts", level="OK", message="未发现同步冲突副本")


def check_open_items(vault_path: Path) -> CheckItem:
    """检查 hub/open-items.md 中以 '- [' 开头的行是否均符合开放项格式。"""
    open_items_file = vault_path / "hub" / "open-items.md"
    if not open_items_file.is_file():
        return CheckItem(id="open-items", level="FAIL", message="hub/open-items.md 不存在")
    try:
        lines = open_items_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return CheckItem(id="open-items", level="FAIL", message=f"open-items.md 无法读取 ({exc})")

    errors: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        try:
            parse_item_line(stripped)
        except ItemFormatError as exc:
            errors.append(f"open-items.md:{lineno}: {exc}")

    if errors:
        return CheckItem(id="open-items", level="FAIL", message=f"open-items.md 格式错误: {'; '.join(errors)}")
    return CheckItem(id="open-items", level="OK", message="open-items.md 格式合法")


def check_candidates(vault_path: Path) -> CheckItem:
    """检查 hub/reports/candidates.md 中以 '- [' 开头的行是否都能解析（文件可以不存在）。"""
    from hushai_agent.candidates import format_errors

    bad = format_errors(vault_path)
    if bad:
        return CheckItem(id="candidates", level="FAIL", message=f"candidates.md 格式错误: {'; '.join(bad[:3])}")
    return CheckItem(id="candidates", level="OK", message="candidates.md 格式合法")


def check_decisions(vault_path: Path) -> CheckItem:
    """检查 hub/decisions.md 的行格式是否合法。"""
    from hushai_agent.cortex import check_decisions_errors

    bad = check_decisions_errors(vault_path)
    if bad:
        return CheckItem(id="decisions", level="FAIL", message=f"decisions.md 格式错误: {'; '.join(bad[:3])}")
    return CheckItem(id="decisions", level="OK", message="decisions.md 格式合法")


def check_lessons(vault_path: Path) -> CheckItem:
    """检查 hub/lessons.md 的行格式是否合法。"""
    from hushai_agent.cortex import check_lessons_errors

    bad = check_lessons_errors(vault_path)
    if bad:
        return CheckItem(id="lessons", level="FAIL", message=f"lessons.md 格式错误: {'; '.join(bad[:3])}")
    return CheckItem(id="lessons", level="OK", message="lessons.md 格式合法")


def validate_vault(vault_path: str | Path) -> dict[str, Any]:
    """对指定 vault 路径执行无配置规范校验。"""
    vpath = Path(vault_path).expanduser().resolve()
    checks = [
        check_vault_structure(vpath),
        check_inbox_names(vpath),
        check_inbox_lines(vpath, allowed_domains=None),
        check_open_items(vpath),
        check_candidates(vpath),
        check_decisions(vpath),
        check_lessons(vpath),
        check_sync_conflicts(vpath),
    ]
    ok = all(item.level != "FAIL" for item in checks)
    return {
        "checks": [item.to_dict() for item in checks],
        "ok": ok,
    }
