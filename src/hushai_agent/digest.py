"""汇总主流程（digest）：收集收件、机械对账、模型判断、更新派生视图、推进游标与提交 Git。"""

from __future__ import annotations

import datetime
from typing import Any

from hushai_agent import (
    brief, candidates, collect, cortex, cursor, items, judge, ledger, lock, mechanical, report, runner, statusfile, todaysel,
)
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.config_ext import load_ext
from hushai_agent.digest_apply import (
    apply_model_output, build_candidate_and_correction_lines, commit_hub_if_git,
)
from hushai_agent.fsio import append_line, atomic_write
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.reconcile import run_mechanical_only, update_daily_state

__all__ = ["run_digest", "run_mechanical_only"]


def _resolve_cfg(cfg: InstanceConfig | str) -> InstanceConfig:
    return cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)


def run_digest(
    cfg: InstanceConfig | str,
    as_of: datetime.date | str,
    dry_run: bool = False,
    for_date: datetime.date | str | None = None,
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
        lock_ctx = lock.runner_lock(inst_cfg)
        lock_ctx.__enter__()
    except lock.LockBusy:
        ledger.record(
            inst_cfg, "job", job="digest", date=as_of_str, dry_run=dry_run, result="failed", note="lock-busy"
        )
        raise

    try:
        all_items = items.load(inst_cfg)
        pre_open_ids = {it.id for it in all_items if it.status == "open"}
        archived_ids = items.load_archived_ids(inst_cfg)
        status_rows = statusfile.load(inst_cfg)
        records = collect.collect(inst_cfg, as_of_date)
        mech = mechanical.apply_actions(all_items, records)
        ext = load_ext(inst_cfg)
        candidates.apply_mech(inst_cfg, mech, ext.approval_doors, write=not dry_run)
        cortex.apply_mech(inst_cfg, mech, write=not dry_run)

        by_door: dict[str, int] = {}
        for rec in records:
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

        if mech.rest:
            prompt = judge.build_prompt(inst_cfg, mech.rest, all_items, as_of_date)
            known_line_ids = {r.line_id for r in records} | cursor.load_ids(inst_cfg)
            existing_item_ids = {it.id for it in all_items} | set(archived_ids)
            validator = judge.make_validator(
                known_line_ids=known_line_ids,
                item_ids=existing_item_ids,
                domains=inst_cfg.domains,
                n_lines=len(mech.rest),
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
        cand_added, cand_skipped = candidates.add(
            inst_cfg, cand_lines, len(mech.candidates), as_of_date, ext.candidates_pending, write=not dry_run
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
        accumulated = update_daily_state(
            state_dir, summary, dry_run=dry_run, is_mechanical=False
        )
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
            vault = inst_cfg.vault
            items.save(inst_cfg, all_items, as_of_date)
            atomic_write(vault / "hub" / "today.md", today_text, [vault])
            atomic_write(vault / "hub" / "status.md", status_text, [vault])
            atomic_write(vault / "hub" / "status-index.md", index_text, [vault])
            if archived_status:
                statusfile.append_status_archive(
                    vault / "hub" / "reports" / "status-archive.md",
                    archived_status,
                    [vault],
                )

            corr_file = vault / "hub" / "reports" / "corrections.md"
            for line in corr_lines:
                append_line(corr_file, line, [vault])

            daily_file = vault / "hub" / "reports" / f"daily-{as_of_str}.md"
            atomic_write(daily_file, daily_text, [vault])

            rest_id_set = {r.line_id for r in mech.rest}
            if degraded:
                advance_ids = [r.line_id for r in records if r.line_id not in rest_id_set]
            else:
                advance_ids = [r.line_id for r in records]
            if advance_ids:
                cursor.append(inst_cfg, advance_ids, as_of_date)

            brief.refresh(inst_cfg)
            summary["git_committed"] = commit_hub_if_git(vault, f"digest {as_of_str}")

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
