"""实例全量体检（doctor）逻辑。"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent import alerts, brain_cfg, clock, cursor, ledger
from hushai_agent.config import ConfigError, InstanceConfig, load_instance_config
from hushai_agent.inboxscan import iter_inbox_lines
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.validate import (
    CheckItem,
    check_brain,
    check_decisions,
    check_inbox_lines,
    check_inbox_names,
    check_lessons,
    check_open_items,
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
                if line.startswith(("- [ ]", "- [x]", "- [X]", "- [-]", "- [>]"))
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
    """检查 [privacy] local_only 路径黑名单与 local_only_domains 领域隔离配置。"""
    domains_note = ",".join(cfg.local_only_domains) if cfg.local_only_domains else "未设置"
    if not cfg.local_only:
        return CheckItem(
            id="local-only",
            level="OK",
            message=f"未配置额外 local_only 路径；本地隔离领域={domains_note}",
        )

    invalid: list[str] = []
    missing: list[str] = []
    for rel in cfg.local_only:
        rel_path = Path(rel)
        parts = rel.replace("\\", "/").split("/")
        if rel_path.is_absolute() or os.path.isabs(rel) or ".." in rel_path.parts or ".." in parts:
            invalid.append(rel)
            continue
        target = cfg.vault / rel
        if not target.exists():
            missing.append(rel)

    if invalid:
        return CheckItem(
            id="local-only",
            level="FAIL",
            message=f"local_only 路径非法（不能为绝对路径或包含 ..）: {', '.join(invalid)}",
        )
    if missing:
        return CheckItem(
            id="local-only",
            level="OK",
            message=f"local_only 黑名单规则正常（尚未创建: {', '.join(missing)}）；本地隔离领域={domains_note}",
        )
    return CheckItem(
        id="local-only",
        level="OK",
        message=f"local_only 路径均已存在；本地隔离领域={domains_note}",
    )


def _parse_ledger_ts(ts_raw: Any, tz: ZoneInfo) -> datetime.datetime | None:
    """将账本行中的 ISO 时间戳解析为实例时区的 datetime 对象。"""
    if not isinstance(ts_raw, str) or not ts_raw.strip():
        return None
    text = ts_raw.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _check_ledger_recent(cfg: InstanceConfig) -> CheckItem:
    """检查账本首行满 3 天后，最近 3 天内是否存在成功的 digest 记录。"""
    rows = ledger.read(cfg)
    if not rows:
        return CheckItem(id="ledger-recent", level="OK", message="账本为空，跳过最近运行检查")

    now_dt = clock.now(cfg)
    tz = ZoneInfo(cfg.timezone)
    parsed_rows = [(row, _parse_ledger_ts(row.get("ts"), tz)) for row in rows]
    first_dt = next((dt for _, dt in parsed_rows if dt is not None), None)
    if first_dt is None:
        return CheckItem(id="ledger-recent", level="OK", message="账本无有效时间戳，跳过检查")

    three_days = datetime.timedelta(days=3)
    if (now_dt - first_dt) < three_days:
        return CheckItem(id="ledger-recent", level="OK", message="账本首行距今未满 3 天")

    cutoff = now_dt - three_days
    has_recent_digest = any(
        row.get("job") == "digest"
        and row.get("result") in ("ok", "degraded")
        and dt is not None
        and dt >= cutoff
        for row, dt in parsed_rows
    )
    if has_recent_digest:
        return CheckItem(id="ledger-recent", level="OK", message="最近 3 天存在成功的 digest 运行记录")
    return CheckItem(
        id="ledger-recent",
        level="WARN",
        message="账本首行距今已满 3 天，且最近 3 天没有成功的 digest 记录，定时任务可能未生效",
    )


def _check_inbox_backlog(cfg: InstanceConfig) -> CheckItem:
    """检查是否存在文件日期早于今天减 2 天且尚未进入游标的合法收件行。"""
    today_date = clock.today(cfg)
    cutoff_date = today_date - datetime.timedelta(days=2)
    digested_ids = cursor.load_ids(cfg)
    inbox_dir = cfg.vault / "hub" / "inbox"

    backlog = 0
    for rec in iter_inbox_lines(inbox_dir):
        rec_date = FlexDate.from_any(rec.date)
        if rec_date is not None and rec_date < cutoff_date and rec.line_id not in digested_ids:
            backlog += 1

    if backlog > 0:
        return CheckItem(
            id="inbox-backlog",
            level="WARN",
            message=f"存在 {backlog} 条早于 {cutoff_date.isoformat()} 且未进入游标的积压收件行",
        )
    return CheckItem(id="inbox-backlog", level="OK", message="无超过 2 天的未处理收件积压")


def _check_expired_inbox_lines(cfg: InstanceConfig) -> CheckItem:
    """检查是否存在早于 window_days 窗口（默认 14 天）且未进游标的晚到/遗漏收件行。"""
    today_date = clock.today(cfg)
    window_cutoff = today_date - datetime.timedelta(days=cfg.window_days)
    digested_ids = cursor.load_ids(cfg)
    inbox_dir = cfg.vault / "hub" / "inbox"

    expired = 0
    for rec in iter_inbox_lines(inbox_dir):
        rec_date = FlexDate.from_any(rec.date)
        if rec_date is not None and rec_date < window_cutoff and rec.line_id not in digested_ids:
            expired += 1

    if expired > 0:
        return CheckItem(
            id="inbox-expired",
            level="WARN",
            message=f"存在 {expired} 条超过 {cfg.window_days} 天窗口（早于 {window_cutoff.isoformat()}）且未进入游标的晚到收件行，日常 digest 不会再收集",
        )
    return CheckItem(id="inbox-expired", level="OK", message=f"无超出 {cfg.window_days} 天窗口的遗漏收件行")


def _check_recent_alerts(cfg: InstanceConfig) -> CheckItem:
    """检查 <state>/alerts.jsonl 最近 24 小时内是否有未处理的严重系统告警。"""
    recent = alerts.load_recent_alerts(cfg, hours=24, limit=20)
    severe = [a for a in recent if a.get("level") in ("error", "critical", "needs_human")]
    if severe:
        return CheckItem(
            id="system-alerts",
            level="WARN",
            message=f"最近 24 小时存在 {len(severe)} 条需关注系统告警（见 alerts.jsonl / 晨间简报）：{severe[0]['message']}",
        )
    return CheckItem(id="system-alerts", level="OK", message="最近 24 小时无严重系统告警")


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
    checks.append(check_open_items(cfg.vault))
    checks.append(check_decisions(cfg.vault))
    checks.append(check_lessons(cfg.vault))
    bcfg = brain_cfg.load_brain_config(cfg)
    checks.append(check_brain(cfg.vault, index_max_lines=bcfg.index_max_lines))
    checks.append(check_sync_conflicts(cfg.vault))
    checks.append(_check_state_dir(instance))
    checks.append(_check_vault_git(cfg.vault))
    checks.append(_check_local_only(cfg))
    checks.append(_check_ledger_recent(cfg))
    checks.append(_check_inbox_backlog(cfg))
    checks.append(_check_expired_inbox_lines(cfg))
    checks.append(_check_recent_alerts(cfg))

    ok = all(item.level != "FAIL" for item in checks)
    return {
        "checks": [item.to_dict() for item in checks],
        "ok": ok,
    }
