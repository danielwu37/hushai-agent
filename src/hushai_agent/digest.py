"""汇总主流程（digest）：收集收件、机械对账、模型判断、更新派生视图、推进游标与提交 Git。"""

from __future__ import annotations

import datetime
from typing import Any

from hushai_agent import (
    candidates, checkpoint, collect, cortex, cursor, items, judge, ledger, lock, mechanical, report, runner, statusfile, todaysel,
)
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.config_ext import load_ext
from hushai_agent.daily_state import load_daily_accumulated, merge_daily_facts
from hushai_agent.digest_apply import (
    apply_model_output,
    build_candidate_and_correction_lines,
)
from hushai_agent.digest_plan import execute_digest_commit_plan, should_call_model_in_run
from hushai_agent.fsio import atomic_write
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.reconcile import drain_pending_mechanical, run_mechanical_only

__all__ = ["run_digest", "run_mechanical_only"]


def _resolve_cfg(cfg: InstanceConfig | str) -> InstanceConfig:
    return cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)


def run_digest(
    cfg: InstanceConfig | str,
    as_of: datetime.date | str,
    dry_run: bool = False,
    for_date: datetime.date | str | None = None,
    with_model: bool = False,
) -> dict[str, Any]:
    """执行指定基准日期的每日汇总主流程，返回汇总摘要字典。"""
    inst_cfg = _resolve_cfg(cfg)
    as_of_date = FlexDate.from_any(as_of)
    if as_of_date is None:
        raise ValueError(f"非法基准日期 as_of: {as_of!r}")
    as_of_str = as_of_date.isoformat()

    if for_date is None or (isinstance(for_date, str) and not for_date.strip()):
        target_for_date = FlexDate(as_of_date.year, as_of_date.month, as_of_date.day) + datetime.timedelta(days=1)
    else:
        parsed_for = FlexDate.from_any(for_date)
        if parsed_for is None:
            raise ValueError(f"非法目标日期 for_date: {for_date!r}")
        target_for_date = parsed_for

    try:
        lock_ctx = lock.runner_lock(inst_cfg, wait_seconds=lock.job_wait_seconds())
        lock_ctx.__enter__()
    except lock.LockBusy:
        ledger.record(
            inst_cfg, "job", job="digest", date=as_of_str, dry_run=dry_run, result="failed", note="lock-busy"
        )
        raise

    try:
        if not dry_run:
            checkpoint.recover_pending(inst_cfg)
        all_items = items.load(inst_cfg)
        pre_open_ids = {it.id for it in all_items if it.status == "open"}
        archived_ids = items.load_archived_ids(inst_cfg)
        status_rows = statusfile.load(inst_cfg)
        records = collect.collect(inst_cfg, as_of_date, include_local_domains=True)
        mech = mechanical.apply_actions(all_items, records)
        ai_records = collect.exclude_local_only_domains(inst_cfg, records)
        ai_rest = collect.exclude_local_only_domains(inst_cfg, mech.rest)
        ext = load_ext(inst_cfg)
        cand_manifest = candidates.apply_mech(inst_cfg, mech, ext.approval_doors, write=not dry_run)
        cortex_res = cortex.apply_mech(inst_cfg, mech, write=not dry_run)

        by_door: dict[str, int] = {}
        for rec in ai_records:
            by_door[rec.door] = by_door.get(rec.door, 0) + 1

        item_map = {it.id: it for it in all_items}
        mech_closed = []
        mech_deferred = []
        for app in mech.applied:
            it_obj = item_map.get(app.id)
            if it_obj is None:
                continue
            if app.action in ("done", "drop"):
                mech_closed.append(it_obj)
            elif app.action == "defer":
                mech_deferred.append(it_obj)

        model_data: dict[str, Any] | None = None
        degraded: str | None = None
        dropped_entries: list[dict[str, Any]] = []

        if ai_rest:
            if not should_call_model_in_run(inst_cfg, "digest", dry_run=dry_run, with_model=with_model):
                degraded = "dry-run-no-model"
            else:
                prompt = judge.build_prompt(inst_cfg, ai_rest, all_items, as_of_date)
                known_line_ids = {r.line_id for r in ai_records} | cursor.load_ids(inst_cfg)
                existing_item_ids = {it.id for it in all_items} | set(archived_ids)
                validator = judge.make_validator(
                    known_line_ids=known_line_ids,
                    item_ids=existing_item_ids,
                    domains=inst_cfg.domains,
                    n_lines=len(ai_rest),
                )
                meta = {
                    "job": "digest",
                    "date": as_of_str,
                    "inbox_dir": inst_cfg.vault / "hub" / "inbox",
                    "empty": {"items": [], "status_rows": [], "memory_candidates": []},
                }
                model_data, run_info = runner.run_model(
                    inst_cfg,
                    "digest",
                    prompt,
                    judge.SCHEMA,
                    meta,
                    validator,
                )
                if model_data is None:
                    degraded = str(run_info.get("degraded") or "all-failed")
                else:
                    dropped_entries = list(model_data.get("_dropped") or [])

        applied_info = apply_model_output(
            all_items=all_items,
            archived_ids=archived_ids,
            status_rows=status_rows,
            model_data=model_data,
            as_of_date=as_of_date,
            pre_open_ids=pre_open_ids,
        )
        cand_lines, corr_lines = build_candidate_and_correction_lines(
            as_of_date=as_of_date,
            mech_candidates=mech.candidates,
            mech_corrections=mech.corrections,
            model_data=model_data,
        )
        cand_added, cand_skipped, expected_cand_pairs = candidates.planned_pairs(
            inst_cfg, cand_lines, len(mech.candidates), as_of_date, ext.candidates_pending
        )

        today_selected = todaysel.select_today(all_items, target_for_date, inst_cfg.today_items)
        today_text = todaysel.render_today(today_selected, target_for_date, degraded=degraded)
        status_rows, archived_status = statusfile.trim_for_index_limit(
            status_rows, inst_cfg.status_index_lines
        )
        status_text = statusfile.render_status(status_rows, as_of_date, inst_cfg.stale_days)
        index_text = statusfile.render_index(status_rows)

        all_closed = mech_closed + applied_info["closed"]
        all_deferred = mech_deferred + applied_info["deferred"]

        summary: dict[str, Any] = {
            "date": as_of_str,
            "for_date": target_for_date.isoformat(),
            "dry_run": dry_run,
            "new_lines": len(records),
            "by_door": by_door,
            "mechanical_applied": len(mech.applied),
            "applied": [
                {
                    "action": e.action,
                    "id": e.id,
                    "line_id": e.line_id,
                    "date": e.date.isoformat() if e.date else None,
                }
                for e in mech.applied
            ],
            "unmatched": [
                {
                    "action": e.action,
                    "id": e.id,
                    "line_id": e.line_id,
                    "did": e.record.did if e.record else "",
                    "raw": e.record.raw if e.record else "",
                    "reason": e.reason,
                }
                for e in mech.unmatched
            ],
            "created": [
                {"id": it.id, "title": it.title, "owner": it.owner, "domain": it.domain}
                for it in applied_info["created"]
            ],
            "closed": [
                {"id": it.id, "title": it.title, "status": it.status} for it in all_closed
            ],
            "deferred": [
                {
                    "id": it.id,
                    "title": it.title,
                    "sched": it.sched.isoformat() if it.sched else "-",
                }
                for it in all_deferred
            ],
            "touched_closed": applied_info["touched_closed"],
            "ignored": applied_info["ignored"],
            "dropped": len(dropped_entries),
            "dropped_details": dropped_entries,
            "candidates_added": cand_added,
            "candidates_skipped": cand_skipped,
            "candidates_pending": ext.candidates_pending,
            "corrections_added": len(corr_lines),
            "today_count": len(today_selected),
            "today_items": [{"id": it.id, "title": it.title} for it in today_selected],
            "degraded": degraded,
            "git_committed": False,
        }
        state_dir = get_instance_state_dir(inst_cfg.name)
        run_id = checkpoint.new_run_id("digest", as_of_str)
        prev_acc = load_daily_accumulated(state_dir, as_of_str)
        accumulated = merge_daily_facts(prev_acc, summary, is_mechanical=False)
        daily_text = report.render_daily(accumulated)

        if dry_run:
            dry_hub = state_dir / "dry-run" / as_of_str / "hub"
            keep_items, _ = items.split_archive(all_items, as_of_date)
            atomic_write(
                dry_hub / "open-items.md",
                items.render_open_items(keep_items, as_of_date),
                [state_dir],
            )
            atomic_write(dry_hub / "today.md", today_text, [state_dir])
            atomic_write(dry_hub / "status.md", status_text, [state_dir])
            atomic_write(dry_hub / "status-index.md", index_text, [state_dir])
            if archived_status:
                statusfile.append_status_archive(
                    dry_hub / "reports" / "status-archive.md",
                    archived_status,
                    [state_dir],
                )
            atomic_write(dry_hub / "reports" / f"daily-{as_of_str}.md", daily_text, [state_dir])
        else:
            closing_res = execute_digest_commit_plan(
                inst_cfg,
                run_id=run_id,
                as_of_date=as_of_date,
                target_for_date=target_for_date,
                all_items=all_items,
                records=records,
                ai_rest=ai_rest,
                degraded=degraded,
                today_text=today_text,
                status_text=status_text,
                index_text=index_text,
                summary=summary,
                daily_text=daily_text,
                cand_lines=cand_lines,
                mech_cand_count=len(mech.candidates),
                ext=ext,
                expected_cand_pairs=expected_cand_pairs,
                corr_lines=corr_lines,
                cand_manifest=cand_manifest,
                cortex_res=cortex_res,
                archived_status=archived_status,
            )
            for k in ("brain_daily", "brain", "brain_resurface"):
                if k in closing_res:
                    summary[k] = closing_res[k]
            summary["git_committed"] = bool(closing_res.get("git_committed"))

        note_parts = []
        if degraded:
            note_parts.append(f"degraded={degraded}")
        if dropped_entries:
            note_parts.append(f"dropped={len(dropped_entries)}")
        if applied_info["ignored"]:
            note_parts.append(f"ignored={len(applied_info['ignored'])}")

        ledger.record(
            inst_cfg,
            "job",
            job="digest",
            date=as_of_str,
            dry_run=dry_run,
            result="degraded" if degraded else "ok",
            note="; ".join(note_parts),
        )
        return summary
    except Exception as exc:
        ledger.record(
            inst_cfg,
            "job",
            job="digest",
            date=as_of_str,
            dry_run=dry_run,
            result="failed",
            note=str(exc),
        )
        raise
    finally:
        lock_ctx.__exit__(None, None, None)
        if not dry_run:
            drain_pending_mechanical(inst_cfg)
