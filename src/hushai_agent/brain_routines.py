"""Second-brain 周期任务调度入口（聚合 brain_daily 与 brain_maint，保持公开接口不变）。"""

from __future__ import annotations

import datetime
from typing import Any

from hushai_agent.brain_cfg import load_brain_config, load_brain_state
from hushai_agent.brain_daily import (  # noqa: F401
    _brain_routine_lock,
    _find_unseen_connection,
    _open_promises,
    _resolve_date,
    _run_daily_brain_inner,
    _run_weekly_brain_inner,
    write_daily_brain_note,
)
from hushai_agent.brain_maint import (  # noqa: F401
    _note_date,
    _run_monthly_inner,
    _run_resurface_inner,
)
from hushai_agent.brain_model import (  # noqa: F401
    BRAIN_SUBDIRS,
    check_brain_links,
    find_duplicate_notes,
)
from hushai_agent.config import InstanceConfig


def run_daily_brain(
    cfg: InstanceConfig,
    as_of: datetime.date | str | None = None,
    force: bool = False,
    dry_run: bool = False,
    _hold_lock: bool = True,
) -> dict[str, Any]:
    """每日 routine（21:00 口径）：加工收件、写 daily/<date>.md，并输出 3 行摘要；无新内容则静默。"""
    bcfg = load_brain_config(cfg)
    as_of_date = _resolve_date(cfg, as_of)
    as_of_str = as_of_date.isoformat()
    if not force and not bcfg.daily_enabled:
        return {"skipped": "disabled", "routine": "daily", "date": as_of_str, "silent": True}
    with _brain_routine_lock(cfg, "brain-daily", as_of_str, dry_run, _hold_lock):
        return _run_daily_brain_inner(cfg, as_of_date, as_of_str, dry_run=dry_run)


def run_weekly_brain(
    cfg: InstanceConfig,
    as_of: datetime.date | str | None = None,
    force: bool = False,
    dry_run: bool = False,
    _hold_lock: bool = True,
) -> dict[str, Any]:
    """每周 routine（周日 18:00 口径）：写 weekly.md。"""
    bcfg = load_brain_config(cfg)
    as_of_date = _resolve_date(cfg, as_of)
    as_of_str = as_of_date.isoformat()
    if not force and not bcfg.weekly_enabled:
        return {"skipped": "disabled", "routine": "weekly", "date": as_of_str}
    with _brain_routine_lock(cfg, "brain-weekly", as_of_str, dry_run, _hold_lock):
        return _run_weekly_brain_inner(cfg, as_of_date, as_of_str, dry_run=dry_run)


def run_resurface(
    cfg: InstanceConfig,
    as_of: datetime.date | str | None = None,
    force: bool = False,
    dry_run: bool = False,
    _hold_lock: bool = True,
) -> dict[str, Any]:
    """每日一次唤醒一条与当前工作相关的旧笔记（不修改旧笔记，不另起守护进程）。"""
    bcfg = load_brain_config(cfg)
    as_of_date = _resolve_date(cfg, as_of)
    as_of_str = as_of_date.isoformat()
    if not force and not bcfg.resurface_enabled:
        return {"skipped": "disabled", "routine": "resurface", "date": as_of_str}
    with _brain_routine_lock(cfg, "brain-resurface", as_of_str, dry_run, _hold_lock):
        return _run_resurface_inner(cfg, bcfg, as_of_date, as_of_str, force=force, dry_run=dry_run)


def run_monthly_maintenance(
    cfg: InstanceConfig,
    as_of: datetime.date | str | None = None,
    confirm_merge: bool = False,
    force: bool = False,
    dry_run: bool = False,
    _hold_lock: bool = True,
) -> dict[str, Any]:
    """每月维护：合并重复（需同意）、修断链、标记 6 个月孤立笔记、清理 index.md 至 <=200 行。"""
    bcfg = load_brain_config(cfg)
    as_of_date = _resolve_date(cfg, as_of)
    as_of_str = as_of_date.isoformat()
    ym_str = as_of_str[:7]
    if not force and not bcfg.monthly_enabled:
        return {"skipped": "disabled", "routine": "monthly", "date": as_of_str}
    with _brain_routine_lock(cfg, "brain-monthly", as_of_str, dry_run, _hold_lock):
        return _run_monthly_inner(cfg, bcfg, as_of_date, as_of_str, ym_str, confirm_merge=confirm_merge, dry_run=dry_run)


def get_brain_status(cfg: InstanceConfig) -> dict[str, Any]:
    """汇总当前实例 second-brain 的引导状态、routine 开关、笔记数量与链接健康。"""
    bcfg = load_brain_config(cfg)
    state = load_brain_state(cfg)
    link_info = check_brain_links(cfg, min_links=bcfg.min_links)
    hub_dir = cfg.vault / "hub"
    counts = {
        d: len([p for p in (hub_dir / d).glob("*.md") if p.is_file()]) if (hub_dir / d).is_dir() else 0
        for d in BRAIN_SUBDIRS
    }
    return {
        "instance": cfg.name,
        "onboarded": bool(state.get("onboarded")),
        "min_links": bcfg.min_links,
        "routines": {
            "enabled": bcfg.enabled,
            "daily": bcfg.daily_enabled,
            "weekly": bcfg.weekly_enabled,
            "resurface": bcfg.resurface_enabled,
            "monthly": bcfg.monthly_enabled,
        },
        "counts": counts,
        "broken_links": len(link_info["broken_links"]),
        "underlinked_notes": len(link_info["underlinked_notes"]),
    }
