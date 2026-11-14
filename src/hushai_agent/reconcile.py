"""当日日报累积状态（daily/<日期>.json）管理与即时机械对账（run_mechanical_only）。"""

from __future__ import annotations

from typing import Any

from hushai_agent import (
    brain_cfg,
    brief,
    candidates,
    clock,
    collect,
    cortex,
    cursor,
    items,
    ledger,
    lock,
    mechanical,
    report,
    todaysel,
)
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.config_ext import load_ext
from hushai_agent.daily_state import (  # noqa: F401
    load_daily_accumulated,
    merge_daily_facts,
    update_daily_state,
)
from hushai_agent.digest_apply import (
    apply_brain_step,
    build_candidate_and_correction_lines,
    commit_hub_if_git,
)
from hushai_agent.fsio import append_line, atomic_write, read_for_prompt
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir


def run_mechanical_only(cfg: InstanceConfig | str) -> dict[str, Any]:
    """执行即时机械对账：不调模型、不记 call 账本行、不占每日预算，原地刷新 today.md。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    try:
        lock_ctx = lock.runner_lock(inst_cfg)
        lock_ctx.__enter__()
    except lock.LockBusy:
        return {"skipped": "lock-busy"}

    try:
        now_dt = clock.now(inst_cfg)
        as_of_date = FlexDate(now_dt.year, now_dt.month, now_dt.day)
        as_of_str = as_of_date.isoformat()

        all_items = items.load(inst_cfg)
        records = collect.collect(inst_cfg, as_of_date)
        mech = mechanical.apply_actions(all_items, records)

        rest_id_set = {r.line_id for r in mech.rest}
        processed = [r for r in records if r.line_id not in rest_id_set]
        if not processed:
            bcfg = brain_cfg.load_brain_config(inst_cfg)
            brain_info = (
                apply_brain_step(inst_cfg, as_of_date, records=mech.rest, dry_run=False, process_when_enabled=True)
                if (bcfg.enabled and bcfg.daily_enabled)
                else {"filed_count": 0, "auto_linked": [], "broken_links": [], "underlinked_notes": [], "duplicate_candidates": []}
            )
            git_committed = (
                commit_hub_if_git(inst_cfg.vault, f"brain-reconcile {as_of_str} {now_dt.strftime('%H:%M')}")
                if brain_info.get("filed_count", 0) > 0
                else False
            )
            return {
                "date": as_of_str,
                "processed_lines": 0,
                "mechanical_applied": 0,
                "unmatched": 0,
                "candidates_added": 0,
                "corrections_added": 0,
                "brain": brain_info,
                "git_committed": git_committed,
            }

        vault = inst_cfg.vault
        ext = load_ext(inst_cfg)
        candidates.apply_mech(inst_cfg, mech, ext.approval_doors)
        cortex.apply_mech(inst_cfg, mech, write=True)
        items.save(inst_cfg, all_items, as_of_date)

        cand_lines, corr_lines = build_candidate_and_correction_lines(
            as_of_date=as_of_date,
            mech_candidates=mech.candidates,
            mech_corrections=mech.corrections,
            model_data=None,
        )
        cand_added, _ = candidates.add(
            inst_cfg, cand_lines, len(cand_lines), as_of_date, ext.candidates_pending
        )
        corr_file = vault / "hub" / "reports" / "corrections.md"
        for line in corr_lines:
            append_line(corr_file, line, [vault])

        today_path = vault / "hub" / "today.md"
        existing_today = (
            read_for_prompt(today_path, vault, inst_cfg.local_only)
            if today_path.is_file()
            else ""
        )
        refreshed_today = todaysel.refresh_today(existing_today, all_items, as_of_date)
        atomic_write(today_path, refreshed_today, [vault])

        advance_ids = [r.line_id for r in processed]
        cursor.append(inst_cfg, advance_ids, as_of_date)

        by_door: dict[str, int] = {}
        for rec in processed:
            by_door[rec.door] = by_door.get(rec.door, 0) + 1
        item_map = {it.id: it for it in all_items}
        mech_closed = [
            item_map[a.id]
            for a in mech.applied
            if a.action in ("done", "drop") and a.id in item_map
        ]
        mech_deferred = [
            item_map[a.id] for a in mech.applied if a.action == "defer" and a.id in item_map
        ]
        for_date_hdr, t_entries = todaysel.parse_today_entries(refreshed_today)
        open_today_cnt = sum(1 for e in t_entries if e.state == "open")

        cur_facts: dict[str, Any] = {
            "date": as_of_str,
            "for_date": for_date_hdr or "",
            "new_lines": len(processed),
            "by_door": by_door,
            "created": [],
            "closed": [
                {"id": it.id, "title": it.title, "status": it.status} for it in mech_closed
            ],
            "deferred": [
                {
                    "id": it.id,
                    "title": it.title,
                    "sched": it.sched.isoformat() if it.sched else "-",
                }
                for it in mech_deferred
            ],
            "touched_closed": [],
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
            "candidates_added": cand_added,
            "corrections_added": len(corr_lines),
            "today_count": open_today_cnt,
            "degraded": None,
        }
        state_dir = get_instance_state_dir(inst_cfg.name)
        accumulated = update_daily_state(
            state_dir, cur_facts, dry_run=False, is_mechanical=True
        )
        daily_file = vault / "hub" / "reports" / f"daily-{as_of_str}.md"
        atomic_write(daily_file, report.render_daily(accumulated), [vault])

        brief.refresh(inst_cfg)
        brain_info = apply_brain_step(
            inst_cfg, as_of_date, records=None, dry_run=False, process_when_enabled=True
        )
        git_committed = commit_hub_if_git(
            vault, f"mechanical {as_of_str} {now_dt.strftime('%H:%M')}"
        )
        ledger.record(
            inst_cfg,
            "job",
            job="mechanical",
            date=as_of_str,
            dry_run=False,
            result="ok",
            note=f"applied={len(mech.applied)}",
        )
        return {
            "date": as_of_str,
            "processed_lines": len(processed),
            "mechanical_applied": len(mech.applied),
            "unmatched": len(mech.unmatched),
            "candidates_added": cand_added,
            "corrections_added": len(corr_lines),
            "brain": brain_info,
            "git_committed": git_committed,
        }
    finally:
        lock_ctx.__exit__(None, None, None)
