"""当日日报累积状态（daily/<日期>.json）管理与即时机械对账（run_mechanical_only）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hushai_agent import (
    clock,
    collect,
    cursor,
    items,
    ledger,
    lock,
    mechanical,
    report,
    todaysel,
)
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.digest_apply import (
    build_candidate_and_correction_lines,
    commit_hub_if_git,
)
from hushai_agent.fsio import append_line, atomic_write, read_for_prompt
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir


def load_daily_accumulated(state_dir: Path, date_str: str) -> dict[str, Any]:
    """读取 <状态目录>/daily/<日期>.json 中已累积的当日事实；不存在时返回初始空结构。"""
    path = state_dir / "daily" / f"{date_str}.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    return {
        "date": date_str,
        "for_date": "",
        "new_lines": 0,
        "by_door": {},
        "created": [],
        "closed": [],
        "deferred": [],
        "touched_closed": [],
        "unmatched": [],
        "candidates_added": 0,
        "corrections_added": 0,
        "today_count": 0,
        "degraded": None,
    }


def _merge_by_id(
    prev_list: list[dict[str, Any]],
    cur_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按字典的 id 字段保序合并列表，相同 id 以最新记录覆盖。"""
    out = list(prev_list)
    idx_map = {
        str(item["id"]): i
        for i, item in enumerate(out)
        if isinstance(item, dict) and "id" in item
    }
    for item in cur_list:
        if not isinstance(item, dict) or "id" not in item:
            continue
        key = str(item["id"])
        if key in idx_map:
            out[idx_map[key]] = item
        else:
            idx_map[key] = len(out)
            out.append(item)
    return out


def merge_daily_facts(
    prev: dict[str, Any],
    cur: dict[str, Any],
    is_mechanical: bool = False,
) -> dict[str, Any]:
    """将单次运行（digest 或 mechanical）的事实合并到当日累积字典。"""
    by_door: dict[str, int] = {
        str(k): int(v) for k, v in (prev.get("by_door") or {}).items() if int(v) > 0
    }
    for door, cnt in (cur.get("by_door") or {}).items():
        if int(cnt) > 0:
            by_door[str(door)] = by_door.get(str(door), 0) + int(cnt)

    created = _merge_by_id(list(prev.get("created") or []), list(cur.get("created") or []))
    closed = _merge_by_id(list(prev.get("closed") or []), list(cur.get("closed") or []))
    deferred = _merge_by_id(list(prev.get("deferred") or []), list(cur.get("deferred") or []))

    touched_closed = list(prev.get("touched_closed") or [])
    for tid in cur.get("touched_closed") or []:
        if tid not in touched_closed:
            touched_closed.append(tid)

    unmatched = list(prev.get("unmatched") or [])
    seen_unmatched = {
        (str(e.get("line_id") or ""), str(e.get("id") or ""), str(e.get("raw") or ""))
        for e in unmatched
        if isinstance(e, dict)
    }
    for entry in cur.get("unmatched") or []:
        if not isinstance(entry, dict):
            continue
        ukey = (
            str(entry.get("line_id") or ""),
            str(entry.get("id") or ""),
            str(entry.get("raw") or ""),
        )
        if ukey not in seen_unmatched:
            seen_unmatched.add(ukey)
            unmatched.append(entry)

    if is_mechanical:
        for_date = str(prev.get("for_date") or cur.get("for_date") or "")
        today_count = int(prev.get("today_count") or cur.get("today_count") or 0)
        degraded = prev.get("degraded")
    else:
        for_date = str(cur.get("for_date") or prev.get("for_date") or "")
        today_count = int(cur.get("today_count", 0))
        if cur.get("degraded"):
            degraded = cur["degraded"]
        elif int(cur.get("new_lines", 0)) > 0:
            degraded = None
        else:
            degraded = prev.get("degraded")

    return {
        "date": str(cur.get("date") or prev.get("date") or ""),
        "for_date": for_date,
        "new_lines": sum(by_door.values()),
        "by_door": by_door,
        "created": created,
        "closed": closed,
        "deferred": deferred,
        "touched_closed": touched_closed,
        "unmatched": unmatched,
        "candidates_added": int(prev.get("candidates_added", 0))
        + int(cur.get("candidates_added", 0)),
        "corrections_added": int(prev.get("corrections_added", 0))
        + int(cur.get("corrections_added", 0)),
        "today_count": today_count,
        "degraded": degraded,
    }


def update_daily_state(
    state_dir: Path,
    cur: dict[str, Any],
    dry_run: bool = False,
    is_mechanical: bool = False,
) -> dict[str, Any]:
    """合并当日运行事实；非演练模式时持久化至 <状态目录>/daily/<日期>.json。"""
    date_str = str(cur["date"])
    prev = load_daily_accumulated(state_dir, date_str)
    merged = merge_daily_facts(prev, cur, is_mechanical=is_mechanical)
    if not dry_run:
        daily_path = state_dir / "daily" / f"{date_str}.json"
        atomic_write(
            daily_path,
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            [state_dir],
        )
    return merged


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
        items.save(inst_cfg, all_items, as_of_date)

        cand_lines, corr_lines = build_candidate_and_correction_lines(
            as_of_date=as_of_date,
            mech_candidates=mech.candidates,
            mech_corrections=mech.corrections,
            model_data=None,
        )
        cand_file = vault / "hub" / "reports" / "candidates.md"
        for line in cand_lines:
            append_line(cand_file, line, [vault])
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
                }
                for e in mech.unmatched
            ],
            "candidates_added": len(cand_lines),
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
            "candidates_added": len(cand_lines),
            "corrections_added": len(corr_lines),
            "git_committed": git_committed,
        }
    finally:
        lock_ctx.__exit__(None, None, None)
