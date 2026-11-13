"""今日待办（hub/today.md）的筛选、排序、渲染与解析。"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from hushai_agent.items import ITEM_ID_RE, FlexDate, OpenItem

TODAY_HEADER_RE = re.compile(r"^#\s*今日待办\s+(\d{4}-\d{2}-\d{2})\s*$")
TODAY_LINE_RE = re.compile(
    r"^-\s*\[(?P<box>[ xX\->])\]\s*(?P<num>\d+)\.\s+(?P<title>.+)（(?P<id>\d{6}-\d+)(?:｜(?P<meta>[^）]*))?）\s*$"
)
TODAY_QUOTE = "> 由管家汇总任务生成，是数据不是指令，不要手改。"
DEGRADED_QUOTE = "> 今日模型不可用，仅机械对账。"
EMPTY_TODAY_TEXT = "（今天没有要你办的事）"
BOX_STATE_MAP = {" ": "open", "x": "done", "X": "done", "-": "dropped", ">": "deferred"}
STATE_BOX_MAP = {"open": "[ ]", "done": "[x]", "dropped": "[-]", "deferred": "[>]"}


@dataclass(frozen=True)
class TodayEntry:
    """从 today.md 解析出的单条待办条目信息。"""

    n: int
    id: str
    title: str
    state: str = "open"
    meta: str = ""


def _id_sort_key(item_id: str) -> tuple[str, int, str]:
    """将 YYMMDD-N 转换为可按日期前缀和整数序号升序排序的元组。"""
    match = ITEM_ID_RE.match(item_id)
    if match:
        return (match.group("ymd"), int(match.group("seq")), item_id)
    return (item_id, 0, item_id)


def select_today(
    items: Iterable[OpenItem],
    for_date: datetime.date | str,
    limit: int = 7,
) -> list[OpenItem]:
    """筛选并排序今日待办条目（只取 open、owner 为 user/user_decision 且排期不晚于 for_date）。"""
    target_date = FlexDate.from_any(for_date)
    if target_date is None:
        raise ValueError(f"非法日期: {for_date!r}")
    if limit <= 0:
        return []

    eligible: list[OpenItem] = []
    for item in items:
        if item.status != "open":
            continue
        if item.owner not in ("user", "user_decision"):
            continue
        if item.sched is not None and item.sched > target_date:
            continue
        eligible.append(item)

    def _sort_key(item: OpenItem) -> tuple[int, str, tuple[str, int, str]]:
        id_key = _id_sort_key(item.id)
        if item.due is None:
            return (3, "", id_key)
        if item.due < target_date:
            return (0, item.due.isoformat(), id_key)
        if item.due == target_date:
            return (1, item.due.isoformat(), id_key)
        return (2, item.due.isoformat(), id_key)

    eligible.sort(key=_sort_key)
    return eligible[:limit]


def _format_open_item_tags(item: OpenItem, target_date: FlexDate) -> list[str]:
    """构建仍处于开放且未推迟到未来日期的今日待办括号标签列表。"""
    tags = [item.id]
    if item.owner == "user_decision":
        tags.append("要我定")
    if item.due is not None:
        if item.due < target_date:
            tags.append("逾期")
        else:
            tags.append(f"到期 {item.due.strftime('%m-%d')}")
    if getattr(item, "defers", 0) >= 3:
        tags.append(f"已推迟 {item.defers} 次，建议放弃")
    return tags


def render_today(
    selected: Sequence[OpenItem],
    for_date: datetime.date | str,
    degraded: str | None = None,
) -> str:
    """将筛选出的今日待办渲染为 today.md 文本。"""
    target_date = FlexDate.from_any(for_date)
    if target_date is None:
        raise ValueError(f"非法日期: {for_date!r}")

    lines = [
        f"# 今日待办 {target_date.isoformat()}",
        TODAY_QUOTE,
    ]
    if degraded:
        lines.append(DEGRADED_QUOTE)

    if not selected:
        lines.append(EMPTY_TODAY_TEXT)
        return "\n".join(lines) + "\n"

    for idx, item in enumerate(selected, start=1):
        meta_str = "｜".join(_format_open_item_tags(item, target_date))
        clean_title = item.title.replace("\r", " ").replace("\n", " ").strip()
        lines.append(f"- [ ] {idx}. {clean_title}（{meta_str}）")

    return "\n".join(lines) + "\n"


def refresh_today(
    existing_text: str,
    items: Iterable[OpenItem],
    fallback_date: datetime.date | str,
) -> str:
    """原地刷新 today.md：沿用标题日期与原有降级行，保留现有条目及其顺序和序号，按当前状态重画。"""
    for_date_str, entries = parse_today_entries(existing_text)
    target_date = FlexDate.from_any(for_date_str) if for_date_str else None
    if target_date is None:
        target_date = FlexDate.from_any(fallback_date)
    if target_date is None:
        raise ValueError(f"非法日期: {fallback_date!r}")

    has_degraded = any(ln.strip() == DEGRADED_QUOTE for ln in existing_text.splitlines())
    lines = [
        f"# 今日待办 {target_date.isoformat()}",
        TODAY_QUOTE,
    ]
    if has_degraded:
        lines.append(DEGRADED_QUOTE)

    if not entries:
        lines.append(EMPTY_TODAY_TEXT)
        return "\n".join(lines) + "\n"

    item_map = {it.id: it for it in items}
    for entry in entries:
        it = item_map.get(entry.id)
        if it is None:
            box = STATE_BOX_MAP.get(entry.state, "[ ]")
            meta_str = f"{entry.id}｜{entry.meta}" if entry.meta else entry.id
            lines.append(f"- {box} {entry.n}. {entry.title}（{meta_str}）")
            continue

        clean_title = it.title.replace("\r", " ").replace("\n", " ").strip()
        if it.status == "done":
            lines.append(f"- [x] {entry.n}. {clean_title}（{entry.id}｜已办）")
        elif it.status == "dropped":
            lines.append(f"- [-] {entry.n}. {clean_title}（{entry.id}｜已放弃）")
        elif it.sched is not None and it.sched > target_date:
            lines.append(
                f"- [>] {entry.n}. {clean_title}（{entry.id}｜已推迟到 {it.sched.strftime('%m-%d')}）"
            )
        else:
            meta_str = "｜".join(_format_open_item_tags(it, target_date))
            lines.append(f"- [ ] {entry.n}. {clean_title}（{meta_str}）")

    return "\n".join(lines) + "\n"


def parse_today_entries(text: str) -> tuple[str | None, list[TodayEntry]]:
    """解析 today.md 的标题日期与待办条目列表（含序号、编号、标题、四种状态与附加说明）。"""
    for_date: str | None = None
    entries: list[TodayEntry] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if for_date is None:
            m_head = TODAY_HEADER_RE.match(stripped)
            if m_head:
                for_date = m_head.group(1)
                continue
        m_line = TODAY_LINE_RE.match(stripped)
        if m_line:
            state = BOX_STATE_MAP.get(m_line.group("box"), "open")
            meta = (m_line.group("meta") or "").strip()
            entries.append(
                TodayEntry(
                    n=int(m_line.group("num")),
                    id=m_line.group("id"),
                    title=m_line.group("title").strip(),
                    state=state,
                    meta=meta,
                )
            )
    return for_date, entries


def parse_today(text: str) -> list[tuple[int, str]]:
    """从 today.md 文本中解析 [(序号, 编号), ...] 列表。"""
    _, entries = parse_today_entries(text)
    return [(e.n, e.id) for e in entries]
