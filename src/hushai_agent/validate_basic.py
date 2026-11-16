"""Vault 基础目录结构、同步冲突副本名与收件文件名/行格式校验。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from hushai_agent.inboxfmt import (
    InboxFormatError,
    is_ignorable_inbox_line,
    parse_inbox_filename,
    parse_inbox_line,
)

REQUIRED_DIRS = ("hub", "hub/inbox", "hub/reports", "hub/exchange/in", "hub/exchange/out", "domains")
REQUIRED_FILES = (
    "hub/AGENTS.md", "hub/AGENTS.local.md", "hub/profile.md", "hub/status.md",
    "hub/status-index.md", "hub/open-items.md", "hub/today.md", "hub/insights.md",
    "hub/decisions.md", "hub/lessons.md",
)
PAREN_COPY_RE = re.compile(r"^(?P<stem>.+) \(\d+\)(?P<ext>\.[^.]+)?$")
SPACE_NUM_RE = re.compile(r"^(?P<stem>.+) \d+(?P<ext>\.[^.]+)?$")
SYNC_CONFLICT_RE = re.compile(
    r"(\.sync-conflict-|syncthing conflict|conflicted copy|case conflict|"
    r"[\(（][^\)）]*(?:冲突|conflict)[^\)）]*[\)）]|"
    r"[_.\-\s]冲突(?:版|副本)?(?:\.[^.]+)?$)",
    re.I,
)


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
    """判断文件名是否符合同步工具冲突副本特征（避免误判正常含'冲突'字样的笔记标题）。"""
    path_obj = Path(name)
    fname = path_obj.name
    if SYNC_CONFLICT_RE.search(fname):
        return True

    candidates: list[str] = []
    for regex in (PAREN_COPY_RE, SPACE_NUM_RE):
        if match := regex.match(fname):
            candidates.append(f"{match.group('stem')}{match.group('ext') or ''}")
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
    missing = [f"{d}/" for d in REQUIRED_DIRS if not (vault_path / d).is_dir()]
    missing.extend(f for f in REQUIRED_FILES if not (vault_path / f).is_file())
    domains_dir = vault_path / "domains"
    if domains_dir.is_dir():
        for child in sorted(domains_dir.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and not (child / "AGENTS.md").is_file():
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


def check_inbox_lines(vault_path: Path, allowed_domains: Iterable[str] | None = None) -> CheckItem:
    """检查 hub/inbox/ 下所有收件文件的行格式是否合法。"""
    inbox_dir = vault_path / "hub" / "inbox"
    if not inbox_dir.is_dir():
        return CheckItem(id="inbox-lines", level="FAIL", message="hub/inbox 目录不存在")
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
        return CheckItem(id="inbox-lines", level="FAIL", message=f"收件行格式错误: {'; '.join(errors)}")
    return CheckItem(id="inbox-lines", level="OK", message="所有收件行格式合法")


def check_sync_conflicts(vault_path: Path) -> CheckItem:
    """扫描 vault 内的同步冲突副本文件。"""
    if not vault_path.is_dir():
        return CheckItem(id="sync-conflicts", level="WARN", message="vault 目录不存在，无法扫描冲突文件")
    conflicts: list[str] = []
    for root, dirs, files in os.walk(os.fspath(vault_path)):
        dirs[:] = [d for d in sorted(dirs) if d != ".git"]
        root_path, file_set = Path(root), set(files)
        for fname in sorted(files):
            if is_sync_conflict_filename(fname, existing_names=file_set):
                conflicts.append((root_path / fname).relative_to(vault_path).as_posix())
    if conflicts:
        return CheckItem(id="sync-conflicts", level="WARN", message=f"发现同步冲突副本: {', '.join(conflicts)}")
    return CheckItem(id="sync-conflicts", level="OK", message="未发现同步冲突副本")
