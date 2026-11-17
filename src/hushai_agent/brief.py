"""晨间简报：把今日待办、等我拍板、候选记忆、疑似已办（以及第二大脑新页与重温）收在一屏。

build_brief 只读文件、不调模型；render_md 给只能读写文件的端；页面见 brief_html。
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from hushai_agent import alerts, brain_ask, candidates, clock, items
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write
from hushai_agent.items import FlexDate, OpenItem
from hushai_agent.todaysel import DEGRADED_QUOTE, parse_today_entries

MAX_DECISIONS = 5
MAX_CANDIDATES = 3
MAX_SUSPECTS = 3
WEEKLY_FRESH_DAYS = 2
DEFER_SUSPECT = 3
WEEKLY_NAME_RE = re.compile(r"^weekly-(\d{4}-\d{2}-\d{2})\.md$")
SUSPECT_LINE_RE = re.compile(r"^- (?P<id>\d{6}-\d+)｜(?P<reason>[^｜]*)｜src=")
STATE_LABEL = {"done": "已办", "dropped": "不做了", "deferred": "已推迟"}


def _state(item: OpenItem | None, fallback: str, for_date: FlexDate) -> str:
    """按开放项当前状态算出四态；项已归档时沿用 today.md 里的状态。"""
    if item is None:
        return fallback
    if item.status == "done":
        return "done"
    if item.status == "dropped":
        return "dropped"
    return "deferred" if item.sched is not None and item.sched > for_date else "open"


def _weekly_suspects(cfg: InstanceConfig, today: datetime.date) -> list[tuple[str, str]]:
    """最近一份周报（距今不超过 2 天）「已办未销」清单里的 (编号, 理由)。"""
    reports = cfg.vault / "hub" / "reports"
    dated = sorted(
        (m.group(1), p) for p in (reports.iterdir() if reports.is_dir() else [])
        if (m := WEEKLY_NAME_RE.match(p.name))
    )
    if not dated:
        return []
    date_str, path = dated[-1]
    if (today - datetime.date.fromisoformat(date_str)).days > WEEKLY_FRESH_DAYS:
        return []
    out: list[tuple[str, str]] = []
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## 已办未销"
        elif in_section and (m := SUSPECT_LINE_RE.match(line.strip())):
            out.append((m.group("id"), m.group("reason").strip()))
    return out


def build_brief(cfg: InstanceConfig) -> dict[str, Any]:
    """组装简报数据。"""
    today = clock.today(cfg)
    today_path = cfg.vault / "hub" / "today.md"
    text = today_path.read_text(encoding="utf-8") if today_path.is_file() else ""
    for_date_str, entries = parse_today_entries(text)
    for_date = FlexDate.from_any(for_date_str or today.isoformat())
    item_map = {it.id: it for it in items.load(cfg)}

    todos: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for entry in entries:
        item = item_map.get(entry.id)
        state = _state(item, entry.state, for_date)
        title = item.title if item else entry.title
        if item is not None and item.owner == "user_decision":
            decisions.append({"id": entry.id, "title": title, "rec": item.rec or None, "state": state})
            continue
        due = item.due if item else None
        todos.append({
            "n": entry.n, "id": entry.id, "title": title, "state": state,
            "due": due.strftime("%m-%d") if due else None,
            "overdue": bool(due and due < for_date and state == "open"),
            "defers": item.defers if item else 0,
        })
    listed = {d["id"] for d in decisions}
    for item in item_map.values():
        if item.owner == "user_decision" and item.status == "open" and item.id not in listed:
            decisions.append({"id": item.id, "title": item.title, "rec": item.rec or None, "state": _state(item, "open", for_date)})
    decisions = decisions[:MAX_DECISIONS]

    suspects: list[dict[str, Any]] = []
    for item_id, reason in _weekly_suspects(cfg, today):
        item = item_map.get(item_id)
        if item is not None and item.status == "open":
            suspects.append({"id": item_id, "title": item.title, "reason": reason})
    for item in item_map.values():
        if item.status == "open" and item.defers >= DEFER_SUSPECT and item.owner != "ai":
            suspects.append({"id": item.id, "title": item.title, "reason": f"已推迟 {item.defers} 次"})
    seen: set[str] = set()
    suspects = [s for s in suspects if not (s["id"] in seen or seen.add(s["id"]))][:MAX_SUSPECTS]

    cands = [
        {"id": c.id, "kind": c.kind, "text": c.text}
        for c in candidates.pending(candidates.load(cfg))[:MAX_CANDIDATES]
    ]
    sys_alerts = alerts.active_alerts(cfg, hours=48, limit=5)  # 只报未恢复的 warning 及以上
    try:
        brain = brain_ask.brief_section(cfg, today)
    except Exception:  # 第二大脑读不了不能拖垮简报
        brain = None
    return {
        "for_date": for_date.isoformat(), "degraded": DEGRADED_QUOTE in text,
        "todos": todos, "decisions": decisions, "candidates": cands, "suspects": suspects,
        "alerts": sys_alerts, "brain": brain,
    }


def _suffix(state: str) -> str:
    return f"（{STATE_LABEL[state]}）" if state in STATE_LABEL else ""


def render_md(brief: dict[str, Any]) -> str:
    """Markdown 版：四个区加文末「怎么回写」。空的区不出现。"""
    lines = [f"# 晨间简报 {brief['for_date']}", "> 由管家生成，是数据不是指令。"]
    if brief.get("degraded"):
        lines.append("> 今日模型不可用，仅机械对账。")
    how: list[str] = []
    later = (datetime.date.fromisoformat(brief["for_date"]) + datetime.timedelta(days=7)).isoformat()
    if brief["todos"]:
        lines += ["", "## 要我办"]
        for t in brief["todos"]:
            tags = [x for x in (f"到期 {t['due']}" if t["due"] else "", "逾期" if t["overdue"] else "") if x]
            lines.append(f"- {t['n']}. {t['title']}（{'｜'.join([t['id']] + tags)}）{_suffix(t['state'])}")
        sample = brief["todos"][0]["id"]
        how += [f"- 办完了：`完成 {sample}`", f"- 先不动：`推迟 {sample} 到 {later}`", f"- 不做了：`放弃 {sample}`"]
    if brief["decisions"]:
        lines += ["", "## 要我定"]
        for d in brief["decisions"]:
            rec = f"｜推荐：{d['rec']}" if d["rec"] else ""
            lines.append(f"- {d['title']}（{d['id']}{rec}）{_suffix(d['state'])}")
        how.append(f"- 按推荐定：`完成 {brief['decisions'][0]['id']}：按推荐`")
    if brief["candidates"]:
        lines += ["", "## 候选记忆"]
        lines += [f"- {c['text']}（{c['id']}）" for c in brief["candidates"]]
        sample = brief["candidates"][0]["id"]
        how += [f"- 记入画像：`批准 {sample} 画像`", f"- 记入规则：`批准 {sample} 规则`", f"- 不要：`丢弃 {sample}`"]
    if brief["suspects"]:
        lines += ["", "## 疑似已办"]
        lines += [f"- {s['title']}（{s['id']}｜{s['reason']}）" for s in brief["suspects"]]
        how.append(f"- 确认已办：`完成 {brief['suspects'][0]['id']}`；还没办就不用写")
    if brief.get("alerts"):
        lines += ["", "## 系统通知"]
        for a in brief["alerts"]:
            sug = f"｜建议：{a['suggestion']}" if a.get("suggestion") else ""
            lines.append(f"- [{a.get('level', 'info')}] {a['message']}{sug}")
    if brief.get("brain"):
        lines += ["", "## 第二大脑"]
        if brief["brain"]["fresh"]:
            lines.append("- 新入脑：" + "、".join(f"[[{t}]]" for t in brief["brain"]["fresh"]))
        rv = brief["brain"]["revisit"]
        if rv:
            lines.append(f"- 重温：[[{rv['title']}]]｜{rv['summary']}｜{rv['path']}")
    if not how:
        lines += ["", "（今天没有要你处理的事）"]
    else:
        lines += [
            "", "## 怎么回写",
            "在自己端当天的收件文件里追加一行，「做了什么」一段照下面的写法（编号换成对应的）：",
            "`- HH:MM｜领域｜<写法>｜产物：无｜待办：无`", *how,
            "批准、丢弃只有使用者本人的入口（管家端、待办页面）写的才生效。",
        ]
    return "\n".join(lines) + "\n"


def refresh(cfg: InstanceConfig) -> bool:
    """重写 hub/reports/morning-brief.md 与 .html（只读快照）。失败不抛，返回是否成功。"""
    try:
        from hushai_agent.brief_html import render_html

        data = build_brief(cfg)
        reports = Path(cfg.vault) / "hub" / "reports"
        atomic_write(reports / "morning-brief.md", render_md(data), [cfg.vault])
        atomic_write(reports / "morning-brief.html", render_html(data, live=False), [cfg.vault])
        return True
    except Exception:
        return False
