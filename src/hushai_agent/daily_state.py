"""当日日报累积状态（<状态目录>/daily/<日期>.json）读取与合并持久化。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hushai_agent.fsio import atomic_write


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
