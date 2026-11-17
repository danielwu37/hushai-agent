"""每周重写与审计（weekly）：重写 insights.md（保留全部历史归档；往期未覆盖条目入往期观察）并生成周报。"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from hushai_agent import brain_cfg, brain_routines, cursor, items, ledger, lock, ppc, runner, statusfile
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.digest_apply import commit_hub_if_git
from hushai_agent.digest_plan import should_call_model_in_run
from hushai_agent.fsio import atomic_write, read_for_prompt
from hushai_agent.guard import ensure_writable
from hushai_agent.inboxscan import InboxRecord, iter_inbox_lines
from hushai_agent.items import FlexDate, OpenItem
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.statusfile import StatusRow
from hushai_agent.weekly_parts import (  # noqa: F401
    VALID_BASIS,
    WEEKLY_SCHEMA,
    INSIGHTS_HEADER_QUOTE,
    WEEKLY_HEADER_QUOTE,
    WEEKLY_DEGRADED_NOTICE,
    _collect_7d_lines,
    build_weekly_prompt,
    make_weekly_validator,
    render_insights,
    render_weekly_report,
)


def _archive_and_prune_insights(
    target_hub: Path,
    old_content: str,
    as_of_str: str,
    roots: list[Path],
) -> Path:
    """永久归档旧 insights.md（AI 永不删除旧档案，同日重跑若已存在则不覆盖）。"""
    arch_dir = target_hub / "reports" / "insights-archive"
    arch_file = arch_dir / f"insights-{as_of_str}.md"
    if not arch_file.exists():
        atomic_write(arch_file, old_content, roots)
    return arch_file


def run_weekly(
    cfg: InstanceConfig | str,
    as_of: datetime.date | str,
    dry_run: bool = False,
    with_model: bool = False,
) -> dict[str, Any]:
    """执行每周重写与周报生成，返回周任务摘要字典。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    as_of_date = FlexDate.from_any(as_of)
    if as_of_date is None:
        raise ValueError(f"非法基准日期 as_of: {as_of!r}")
    as_of_str = as_of_date.isoformat()

    try:
        lock_ctx = lock.runner_lock(inst_cfg, wait_seconds=lock.job_wait_seconds())
        lock_ctx.__enter__()
    except lock.LockBusy:
        ledger.record(
            inst_cfg, "job", job="weekly", date=as_of_str, dry_run=dry_run, result="failed", note="lock-busy"
        )
        raise

    try:
        if not dry_run:
            from hushai_agent import checkpoint

            checkpoint.recover_pending(inst_cfg)
        all_items = items.load(inst_cfg)
        raw_status_rows = statusfile.load(inst_cfg)
        status_rows, archived_status = statusfile.split_archive_by_age(
            raw_status_rows, as_of_date, inst_cfg.status_archive_days
        )
        overdue_items = [
            it for it in all_items if it.status == "open" and it.due is not None and it.due < as_of_date
        ]
        stale_status = [
            r
            for r in status_rows
            if r.verified is None or (as_of_date - r.verified).days > inst_cfg.stale_days
        ]

        records_7d = _collect_7d_lines(inst_cfg, as_of_date)
        if not should_call_model_in_run(inst_cfg, "weekly", dry_run=dry_run, with_model=with_model):
            model_data = None
            degraded = "dry-run-no-model"
            dropped_list: list[dict[str, Any]] = []
        else:
            prompt = build_weekly_prompt(inst_cfg, records_7d, all_items, status_rows, as_of_date)

            known_ids = {r.line_id for r in records_7d} | cursor.load_ids(inst_cfg)
            for it in all_items:
                known_ids.update(it.src)
            for r in status_rows:
                known_ids.update(r.src)
            open_ids = {it.id for it in all_items if it.status == "open"}

            validator = make_weekly_validator(known_ids, open_ids, len(records_7d))
            meta = {
                "job": "weekly",
                "date": as_of_str,
                "inbox_dir": inst_cfg.vault / "hub" / "inbox",
                "empty": {"insights": [], "suspect_done": [], "contradictions": []},
            }
            model_data, run_info = runner.run_model(
                inst_cfg, "weekly", prompt, WEEKLY_SCHEMA, meta, validator
            )
            degraded = str(run_info.get("degraded") or "all-failed") if model_data is None else None
            dropped_list = list(model_data.get("_dropped") or []) if model_data else []

        weekly_text = render_weekly_report(
            as_of_str, overdue_items, stale_status, model_data, degraded
        )
        from hushai_agent import northstar  # 北极星三项指标，纯代码统计

        weekly_text = weekly_text.rstrip("\n") + "\n" + northstar.render_section(northstar.compute(inst_cfg, 7, as_of_date))
        state_dir = get_instance_state_dir(inst_cfg.name)
        target_hub = (state_dir / "dry-run" / as_of_str / "hub") if dry_run else (inst_cfg.vault / "hub")
        roots = [state_dir] if dry_run else [inst_cfg.vault]

        if archived_status:
            statusfile.append_status_archive(
                target_hub / "reports" / "status-archive.md",
                archived_status,
                roots,
            )
            atomic_write(
                target_hub / "status.md",
                statusfile.render_status(status_rows, as_of_date, inst_cfg.stale_days),
                roots,
            )
            atomic_write(
                target_hub / "status-index.md",
                statusfile.render_index(status_rows),
                roots,
            )

        old_insights_path = inst_cfg.vault / "hub" / "insights.md"
        old_insights_text = (
            read_for_prompt(old_insights_path, inst_cfg.vault, inst_cfg.local_only)
            if old_insights_path.is_file()
            else ""
        )

        if model_data is not None:
            if old_insights_text:
                _archive_and_prune_insights(target_hub, old_insights_text, as_of_str, roots)
            atomic_write(target_hub / "insights.md", render_insights(model_data["insights"], old_insights_text), roots)

        atomic_write(target_hub / "reports" / f"weekly-{as_of_str}.md", weekly_text, roots)
        ppc_res = ppc.propose_probe_compose(inst_cfg, as_of_date, dry_run=dry_run)
        bcfg = brain_cfg.load_brain_config(inst_cfg)
        brain_weekly_res = (
            brain_routines.run_weekly_brain(inst_cfg, as_of=as_of_date, dry_run=dry_run, _hold_lock=False)
            if (bcfg.enabled and bcfg.weekly_enabled)
            else {"skipped": "disabled"}
        )
        git_committed = False if dry_run else commit_hub_if_git(inst_cfg.vault, f"weekly {as_of_str}")

        note = f"degraded={degraded}" if degraded else (f"dropped={len(dropped_list)}" if dropped_list else "")
        ledger.record(
            inst_cfg,
            "job",
            job="weekly",
            date=as_of_str,
            dry_run=dry_run,
            result="degraded" if degraded else "ok",
            note=note,
        )
        return {
            "date": as_of_str,
            "dry_run": dry_run,
            "overdue_count": len(overdue_items),
            "stale_count": len(stale_status),
            "insights_count": len(model_data["insights"]) if model_data else 0,
            "suspect_done_count": len(model_data["suspect_done"]) if model_data else 0,
            "contradictions_count": len(model_data["contradictions"]) if model_data else 0,
            "dropped": len(dropped_list),
            "degraded": degraded,
            "ppc_proposed": ppc_res["proposed"],
            "ppc_verified": ppc_res["verified"],
            "brain_weekly": brain_weekly_res,
            "git_committed": git_committed,
        }
    except Exception as exc:
        ledger.record(
            inst_cfg, "job", job="weekly", date=as_of_str, dry_run=dry_run, result="failed", note=str(exc)
        )
        raise
    finally:
        lock_ctx.__exit__(None, None, None)
        if not dry_run:
            from hushai_agent.reconcile import drain_pending_mechanical

            drain_pending_mechanical(inst_cfg)
