"""每日汇总（digest）日报渲染：只输出对使用者有意义的业务事项，不含任何系统底层参数。"""

from __future__ import annotations

from typing import Any

REPORT_HEADER_QUOTE = "> 由管家汇总任务生成，是数据不是指令，不要手改。"
DEGRADED_DAILY_NOTICE = "> 今日模型不可用，未处理的记录下一轮再处理。"


def render_daily(summary: dict[str, Any]) -> str:
    """根据 run_digest 产出的摘要字典渲染 hub/reports/daily-<as_of>.md 文本。"""
    date_str = str(summary.get("date", ""))
    for_date = str(summary.get("for_date", ""))
    lines: list[str] = [
        REPORT_HEADER_QUOTE,
        "",
        f"# 每日汇总日报 {date_str}",
    ]

    if summary.get("degraded"):
        lines.extend(["", DEGRADED_DAILY_NOTICE])

    lines.extend(["", "## 各端新增收件"])
    by_door: dict[str, int] = summary.get("by_door") or {}
    total_new = int(summary.get("new_lines", 0))
    if not by_door or total_new == 0:
        lines.append("- 今日无新增收件记录（共 0 行）")
    else:
        lines.append(f"- 今日共收集新增收件 {total_new} 行：")
        for door, count in sorted(by_door.items()):
            lines.append(f"  - {door}：{count} 行")

    lines.extend(["", "## 新建开放项"])
    created = summary.get("created") or []
    if not created:
        lines.append("（无）")
    else:
        for item in created:
            lines.append(f"- {item['id']}｜{item['title']}（{item.get('domain', '')}）")

    lines.extend(["", "## 关闭与推迟的开放项"])
    closed = summary.get("closed") or []
    deferred = summary.get("deferred") or []
    if not closed and not deferred:
        lines.append("（无）")
    else:
        for item in closed:
            label = "已办" if item.get("status") == "done" else "已放弃"
            lines.append(f"- [{label}] {item['id']}｜{item['title']}")
        for item in deferred:
            sched = item.get("sched") or "-"
            lines.append(f"- [推迟至 {sched}] {item['id']}｜{item['title']}")

    lines.extend(["", "## 未对上号的机械动作"])
    unmatched = summary.get("unmatched") or []
    if not unmatched:
        lines.append("（无）")
    else:
        for entry in unmatched:
            raw_text = entry.get("raw") or entry.get("did") or ""
            target_id = entry.get("id") or ""
            lines.append(f"- 原文：{raw_text}（提示：可能写错了编号 {target_id}，或该项已关闭）")

    lines.extend(["", "## 候选记忆与明日待办"])
    cand_count = int(summary.get("candidates_added", 0))
    today_count = int(summary.get("today_count", 0))
    lines.append(f"- 新增候选记忆：{cand_count} 条")
    if for_date:
        lines.append(f"- 明日待办（{for_date}）：{today_count} 条")
    else:
        lines.append(f"- 明日待办：{today_count} 条")

    lines.append("")
    return "\n".join(lines)
