"""按规范校验任意 vault 目录（无配置校验入口）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hushai_agent.items import ItemFormatError, parse_item_line
from hushai_agent.validate_basic import (  # noqa: F401
    PAREN_COPY_RE,
    REQUIRED_DIRS,
    REQUIRED_FILES,
    SPACE_NUM_RE,
    SYNC_CONFLICT_RE,
    CheckItem,
    check_inbox_lines,
    check_inbox_names,
    check_sync_conflicts,
    check_vault_structure,
    is_sync_conflict_filename,
)


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


def check_brain(vault_path: Path, index_max_lines: int = 200) -> CheckItem:
    """检查 second-brain 目录与 hub/index.md 行数上限。"""
    from hushai_agent.brain_model import BRAIN_SUBDIRS, extract_md_links, resolve_hub_link

    hub_dir = vault_path / "hub"
    idx_file = hub_dir / "index.md"
    if not (idx_file.is_file() or any((hub_dir / d).is_dir() for d in BRAIN_SUBDIRS)):
        return CheckItem(id="second-brain", level="OK", message="未启用 second-brain 目录（跳过）")

    missing = [f"hub/{d}/" for d in BRAIN_SUBDIRS if not (hub_dir / d).is_dir()]
    if not idx_file.is_file():
        missing.append("hub/index.md")
    if missing:
        return CheckItem(id="second-brain", level="FAIL", message=f"second-brain 缺失项: {', '.join(missing)}")

    idx_lines = len(idx_file.read_text(encoding="utf-8").splitlines())
    if idx_lines > index_max_lines:
        return CheckItem(id="second-brain", level="FAIL", message=f"hub/index.md 行数 {idx_lines} 超过上限 {index_max_lines}")

    broken: list[str] = []
    for sub in ("notes", "sources", "people", "projects"):
        sub_dir = hub_dir / sub
        for p in sorted(sub_dir.glob("*.md")):
            txt = p.read_text(encoding="utf-8")
            for _, target in extract_md_links(txt):
                res = resolve_hub_link(hub_dir, p, target)
                if res is None or not res.exists():
                    broken.append(f"{sub}/{p.name}->{target}")
    if broken:
        return CheckItem(id="second-brain", level="WARN", message=f"second-brain 存在断链: {', '.join(broken[:5])}")
    return CheckItem(id="second-brain", level="OK", message="second-brain 目录、索引与链接正常")


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
        check_brain(vpath),
        check_sync_conflicts(vpath),
    ]
    return {"checks": [item.to_dict() for item in checks], "ok": all(item.level != "FAIL" for item in checks)}
