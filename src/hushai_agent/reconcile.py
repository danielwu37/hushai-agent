"""当日日报累积状态（daily/<日期>.json）管理与即时机械对账（run_mechanical_only）。"""

from __future__ import annotations

from typing import Any

from hushai_agent import (
    candidates,
    checkpoint,
    clock,
    collect,
    cortex,
    items,
    ledger,
    lock,
    mechanical,
    report,
    todaysel,
)
from hushai_agent.checkpoint_verify import sha256_text
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.config_ext import load_ext
from hushai_agent.daily_state import (  # noqa: F401
    load_daily_accumulated,
    merge_daily_facts,
    update_daily_state,
)
from hushai_agent.digest_apply import (
    build_append_targets_manifest,
    build_candidate_and_correction_lines,
    build_closing_plan,
)
from hushai_agent.fsio import read_for_prompt
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir


def drain_pending_mechanical(cfg: InstanceConfig) -> int:
    """在释放 runner.lock 后排空锁冲突期间积压的即时对账事件。"""
    return lock.drain_after_unlock(
        cfg,
        "mechanical",
        "runner.lock",
        lambda: _run_mechanical_locked(cfg),
    )


def run_mechanical_only(cfg: InstanceConfig | str) -> dict[str, Any]:
    """执行即时机械对账：不调模型、不记 call 账本行、不占每日预算，原地刷新 today.md。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    lock_ctx = lock.acquire_or_mark_dirty(inst_cfg, "mechanical", "runner.lock")
    if lock_ctx is None:
        return {"skipped": "lock-busy", "dirty_marked": True}

    try:
        return _run_mechanical_locked(inst_cfg)
    finally:
        lock_ctx.__exit__(None, None, None)
        drain_pending_mechanical(inst_cfg)


def _run_mechanical_locked(inst_cfg: InstanceConfig) -> dict[str, Any]:
    checkpoint.recover_pending(inst_cfg)
    now_dt = clock.now(inst_cfg)
    as_of_date = FlexDate(now_dt.year, now_dt.month, now_dt.day)
    as_of_str = as_of_date.isoformat()

    all_items = items.load(inst_cfg)
    raw_records = collect.collect(inst_cfg, as_of_date, include_local_domains=True)
    mech = mechanical.apply_actions(all_items, raw_records)
    records = collect.exclude_local_only_domains(inst_cfg, raw_records)
    ai_rest = collect.exclude_local_only_domains(inst_cfg, mech.rest)

    rest_id_set = {r.line_id for r in mech.rest}
    processed = [r for r in raw_records if r.line_id not in rest_id_set]
    if not processed:
        return {
            "date": as_of_str,
            "processed_lines": 0,
            "mechanical_applied": 0,
            "unmatched": 0,
            "candidates_added": 0,
            "corrections_added": 0,
            "git_committed": False,
        }

    vault = inst_cfg.vault
    ext = load_ext(inst_cfg)
    cand_manifest = candidates.apply_mech(inst_cfg, mech, ext.approval_doors)
    cortex_res = cortex.apply_mech(inst_cfg, mech, write=True)

    cand_lines, corr_lines = build_candidate_and_correction_lines(
        as_of_date=as_of_date,
        mech_candidates=mech.candidates,
        mech_corrections=mech.corrections,
        model_data=None,
    )
    cand_added, _, expected_cand_pairs = candidates.planned_pairs(
        inst_cfg, cand_lines, len(cand_lines), as_of_date, ext.candidates_pending
    )

    today_path = vault / "hub" / "today.md"
    existing_today = (
        read_for_prompt(today_path, vault, inst_cfg.local_only)
        if today_path.is_file()
        else ""
    )
    refreshed_today = todaysel.refresh_today(existing_today, all_items, as_of_date)

    by_door: dict[str, int] = {}
    for rec in records:
        if rec.line_id not in rest_id_set:
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
    run_id = checkpoint.new_run_id("mechanical", as_of_str)
    prev_acc = load_daily_accumulated(state_dir, as_of_str)
    preview_acc = merge_daily_facts(prev_acc, cur_facts, is_mechanical=True)
    daily_text = report.render_daily(preview_acc)

    keep_items, archive_items = items.split_archive(all_items, as_of_date)
    open_items_text = items.render_open_items(keep_items, as_of_date)
    plan: dict[str, Any] = {
        "version": 1,
        "kind": "mechanical",
        "run_id": run_id,
        "as_of": as_of_str,
        "for_date": for_date_hdr or as_of_str,
        "open_items": {
            "content": open_items_text,
            "sha256": sha256_text(open_items_text),
            "archived_lines": [
                {"id": it.id, "line": items.format_item_line(it)} for it in archive_items
            ],
        },
        "atomic_targets": {
            "hub/today.md": {"content": refreshed_today, "sha256": sha256_text(refreshed_today)},
        },
        "daily_state": {
            "run_id": run_id,
            "as_of": as_of_str,
            "cur_facts": cur_facts,
            "is_mechanical": True,
            "daily_report_rel": f"hub/reports/daily-{as_of_str}.md",
            "daily_report_text": daily_text,
            "daily_report_sha256": sha256_text(daily_text),
        },
        "candidates_add": {
            "lines": cand_lines,
            "n_user": len(cand_lines),
            "limit": ext.candidates_pending,
            "expected_pairs": expected_cand_pairs,
        },
        "corrections": corr_lines,
        "append_targets": build_append_targets_manifest(cand_manifest, cortex_res),
        "status_archive": [],
        "advance_ids": [r.line_id for r in processed],
        "closing": build_closing_plan(
            inst_cfg,
            as_of_date,
            ai_rest,
            commit_msg=f"mechanical {as_of_str} {now_dt.strftime('%H:%M')}",
            ledger_job="mechanical",
        ),
    }
    closing_res = checkpoint.execute_commit_plan(inst_cfg, plan)
    git_committed = bool(closing_res.get("git_committed"))

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
        "git_committed": git_committed,
    }
