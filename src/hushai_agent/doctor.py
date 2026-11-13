"""实例全量体检（doctor）逻辑。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hushai_agent.config import ConfigError, InstanceConfig, load_instance_config
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.validate import (
    CheckItem,
    check_inbox_lines,
    check_inbox_names,
    check_sync_conflicts,
    check_vault_structure,
)


def _check_limits(cfg: InstanceConfig) -> CheckItem:
    """检查核心约定与索引文件行数及 today.md 条目数是否超出上限。"""
    line_targets = (
        ("hub/AGENTS.md", cfg.agents_lines),
        ("hub/profile.md", cfg.profile_lines),
        ("hub/status-index.md", cfg.status_index_lines),
    )
    violations: list[str] = []

    for rel_path, max_lines in line_targets:
        fpath = cfg.vault / rel_path
        if not fpath.is_file():
            violations.append(f"{rel_path} 不存在")
            continue
        try:
            count = len(fpath.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError) as exc:
            violations.append(f"{rel_path} 读取失败 ({exc})")
            continue
        if count > max_lines:
            violations.append(f"{rel_path} 行数 {count} 超过上限 {max_lines}")

    today_file = cfg.vault / "hub" / "today.md"
    if not today_file.is_file():
        violations.append("hub/today.md 不存在")
    else:
        try:
            lines = today_file.read_text(encoding="utf-8").splitlines()
            todo_count = sum(
                1
                for line in lines
                if line.startswith(("- [ ]", "- [x]", "- [X]"))
            )
            if todo_count > cfg.today_items:
                violations.append(
                    f"hub/today.md 待办条目数 {todo_count} 超过上限 {cfg.today_items}"
                )
        except (OSError, UnicodeDecodeError) as exc:
            violations.append(f"hub/today.md 读取失败 ({exc})")

    if violations:
        return CheckItem(
            id="limits",
            level="FAIL",
            message="; ".join(violations),
        )
    return CheckItem(id="limits", level="OK", message="文件行数与今日条目数均在上限内")


def _check_state_dir(instance: str) -> CheckItem:
    """检查实例状态目录是否存在且可写。"""
    state_dir = get_instance_state_dir(instance)
    if not state_dir.exists() or not state_dir.is_dir():
        return CheckItem(
            id="state-dir",
            level="FAIL",
            message=f"状态目录不存在: {state_dir}",
        )
    if not os.access(os.fspath(state_dir), os.W_OK):
        return CheckItem(
            id="state-dir",
            level="FAIL",
            message=f"状态目录不可写: {state_dir}",
        )
    return CheckItem(id="state-dir", level="OK", message=f"状态目录正常可写: {state_dir}")


def _check_vault_git(vault_path: Path) -> CheckItem:
    """检查 vault 是否为 Git 仓库。"""
    git_marker = vault_path / ".git"
    if git_marker.exists():
        return CheckItem(id="vault-git", level="OK", message="vault 已初始化为 Git 仓库")
    return CheckItem(
        id="vault-git",
        level="WARN",
        message="vault 尚未初始化为 Git 仓库",
    )


def _check_local_only(cfg: InstanceConfig) -> CheckItem:
    """检查 [privacy] local_only 配置的路径是否存在。"""
    if not cfg.local_only:
        return CheckItem(id="local-only", level="OK", message="未配置或无需额外 local_only 路径")

    missing: list[str] = []
    for rel in cfg.local_only:
        target = cfg.vault / rel
        if not target.exists():
            missing.append(rel)

    if missing:
        return CheckItem(
            id="local-only",
            level="WARN",
            message=f"local_only 路径不存在: {', '.join(missing)}",
        )
    return CheckItem(id="local-only", level="OK", message="local_only 路径均存在")


def run_doctor(instance: str) -> dict[str, Any]:
    """对指定实例执行全量体检，返回 {"checks": [...], "ok": bool}。"""
    checks: list[CheckItem] = []

    try:
        cfg = load_instance_config(instance)
        checks.append(CheckItem(id="config", level="OK", message="配置文件合法且字段齐全"))
    except ConfigError as exc:
        checks.append(CheckItem(id="config", level="FAIL", message=str(exc)))
        return {
            "checks": [item.to_dict() for item in checks],
            "ok": False,
        }

    checks.append(check_vault_structure(cfg.vault))
    checks.append(_check_limits(cfg))
    checks.append(check_inbox_names(cfg.vault))
    checks.append(check_inbox_lines(cfg.vault, allowed_domains=cfg.allowed_domains))
    checks.append(check_sync_conflicts(cfg.vault))
    checks.append(_check_state_dir(instance))
    checks.append(_check_vault_git(cfg.vault))
    checks.append(_check_local_only(cfg))

    ok = all(item.level != "FAIL" for item in checks)
    return {
        "checks": [item.to_dict() for item in checks],
        "ok": ok,
    }
