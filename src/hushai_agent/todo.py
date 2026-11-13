"""今日待办叠加视图（today）与待办回写命令（todo done|defer|drop）实现。"""

from __future__ import annotations

import datetime
from pathlib import Path
import re
from typing import Any, Mapping

from hushai_agent import clock, cursor, items, mechanical
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.inboxscan import iter_inbox_lines
from hushai_agent.items import FlexDate
from hushai_agent.todaysel import EMPTY_TODAY_TEXT, STATE_BOX_MAP, parse_today, parse_today_entries
from hushai_agent.vault import add_inbox_entry

SEQ_NUM_RE = re.compile(r"^\d+$")
ITEM_ID_RE = re.compile(r"^\d{6}-\d+$")
REL_DAYS_RE = re.compile(r"^\+(?P<days>\d+)d$", re.IGNORECASE)
TERMINAL_STATES = frozenset({"done", "dropped"})


def _compute_overlaid_states(
    cfg: InstanceConfig,
    initial_states: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, tuple[str, str]]:
    """基于初始状态并按收件顺序叠加未对账机械动作；一旦进入已办或已放弃终态则忽略后续动作。"""
    digested_ids = cursor.load_ids(cfg)
    inbox_dir = cfg.vault / "hub" / "inbox"
    overlaid: dict[str, tuple[str, str]] = dict(initial_states or {})

    for rec in iter_inbox_lines(inbox_dir):
        if rec.line_id in digested_ids:
            continue
        act = mechanical.parse_action(rec.did)
        if act is None or act.kind not in ("done", "defer", "drop"):
            continue
        prev = overlaid.get(act.target)
        if prev is not None and prev[0] in TERMINAL_STATES:
            continue
        if act.kind == "done":
            overlaid[act.target] = ("done", "已办")
        elif act.kind == "drop":
            overlaid[act.target] = ("dropped", "已放弃")
        elif act.kind == "defer":
            date_s = act.date.isoformat() if act.date else ""
            overlaid[act.target] = ("deferred", f"已推迟到 {date_s}")
    return overlaid


def get_today_status(instance: str) -> tuple[dict[str, Any], list[str]]:
    """读取 today.md 四种状态并叠加尚未进入游标的机械动作行，返回 (json_dict, 文本行列表)。"""
    cfg = load_instance_config(instance)
    today_path = cfg.vault / "hub" / "today.md"
    text = today_path.read_text(encoding="utf-8") if today_path.is_file() else ""
    for_date, entries = parse_today_entries(text)
    if not for_date:
        for_date = clock.today(cfg).isoformat()

    initial = {e.id: (e.state, e.meta) for e in entries}
    overlaid = _compute_overlaid_states(cfg, initial)
    json_items: list[dict[str, Any]] = []
    display_lines: list[str] = [f"# 今日待办 {for_date}"]

    if not entries:
        display_lines.append(EMPTY_TODAY_TEXT)
    else:
        for entry in entries:
            state, label = overlaid.get(entry.id, (entry.state, entry.meta))
            json_items.append(
                {
                    "n": entry.n,
                    "id": entry.id,
                    "title": entry.title,
                    "state": state,
                }
            )
            suffix = f"（{entry.id}｜{label}）" if label else f"（{entry.id}）"
            box = STATE_BOX_MAP.get(state, "[ ]")
            display_lines.append(f"- {box} {entry.n}. {entry.title}{suffix}")

    return {"for_date": for_date, "items": json_items}, display_lines


def _resolve_defer_date(cfg: InstanceConfig, to_date: str | None) -> str:
    """解析推迟目标日期：不给时默认今天加 7 天，支持 +Nd 与 YYYY-MM-DD。"""
    base_today = clock.today(cfg)
    if to_date is None or not to_date.strip():
        return (base_today + datetime.timedelta(days=7)).isoformat()
    raw = to_date.strip()
    m_rel = REL_DAYS_RE.match(raw)
    if m_rel:
        days = int(m_rel.group("days"))
        if days <= 0:
            raise ValueError(f"推迟天数必须大于 0: {to_date}")
        return (base_today + datetime.timedelta(days=days)).isoformat()
    parsed = FlexDate.from_any(raw)
    if parsed is None:
        raise ValueError(f"非法推迟日期: {to_date}")
    return parsed.isoformat()


def run_todo_action(
    instance: str,
    action: str,
    target: str,
    to_date: str | None = None,
    door: str | None = None,
) -> Path:
    """将待办回写动作（done / defer / drop）追加为对应端当天的标准收件行并触发即时机械对账。"""
    if action not in ("done", "defer", "drop"):
        raise ValueError(f"不支持的待办动作: {action}")

    cfg = load_instance_config(instance)
    clean_to = _resolve_defer_date(cfg, to_date) if action == "defer" else ""
    clean_target = target.strip()

    today_path = cfg.vault / "hub" / "today.md"
    today_text = today_path.read_text(encoding="utf-8") if today_path.is_file() else ""
    _, today_entries = parse_today_entries(today_text)

    if SEQ_NUM_RE.match(clean_target):
        seq_num = int(clean_target)
        seq_map = dict(parse_today(today_text))
        if seq_num not in seq_map:
            raise ValueError(f"today.md 中找不到序号: {seq_num}")
        item_id = seq_map[seq_num]
    elif ITEM_ID_RE.match(clean_target):
        item_id = clean_target
    else:
        raise ValueError(f"无效的今日序号或开放项编号: {target}")

    all_items = items.load(cfg)
    matched = next((it for it in all_items if it.id == item_id), None)
    if matched is None:
        raise ValueError(f"open-items.md 中找不到编号为 {item_id} 的开放项")
    if matched.status != "open":
        raise ValueError(f"开放项 {item_id} 已关闭（当前状态: {matched.status}）")

    initial_states = {e.id: (e.state, e.meta) for e in today_entries}
    overlaid = _compute_overlaid_states(cfg, initial_states)
    overlaid_state = overlaid.get(item_id, ("open", ""))[0]
    if overlaid_state in TERMINAL_STATES:
        raise ValueError(f"开放项 {item_id} 在叠加视图中已处于终态（{overlaid_state}）")

    if cfg.lang == "en":
        if action == "done":
            did = f"done {matched.id}: {matched.title}"
        elif action == "defer":
            did = f"defer {matched.id} to {clean_to}: {matched.title}"
        else:
            did = f"drop {matched.id}: {matched.title}"
    else:
        if action == "done":
            did = f"完成 {matched.id}：{matched.title}"
        elif action == "defer":
            did = f"推迟 {matched.id} 到 {clean_to}：{matched.title}"
        else:
            did = f"放弃 {matched.id}：{matched.title}"

    actual_door = door.strip() if door and door.strip() else cfg.steward_door
    return add_inbox_entry(
        instance=instance,
        door=actual_door,
        domain=matched.domain,
        did=did,
    )
