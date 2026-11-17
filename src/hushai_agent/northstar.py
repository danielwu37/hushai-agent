"""北极星指标统计（任务书 §1.4）：需人介入次数、待办有用率、系统自嗨率。纯代码，不调模型。

- 需人介入：账本里 failed / needs_human 的任务数 + alerts.jsonl 里非自愈来源（如周边任务）的 needs_human 告警数。
- 待办有用率：窗口内出现在 today.md（取 vault git 历史各版）的事项里，被收件「完成 / 推迟 / 放弃」过的比例。
- 系统自嗨率：窗口内收件行里讲系统自身的比例（按关键词粗估，[stats] system_keywords 可改）。
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
from typing import Any

from hushai_agent import alerts, clock, ledger
from hushai_agent.config import InstanceConfig
from hushai_agent.inboxscan import iter_inbox_lines
from hushai_agent.mechanical import DEFER_RE, DONE_RE, DROP_RE

DEFAULT_SYSTEM_KEYWORDS = (
    "hushai", "管家", "闸门", "单测", "迁移", "整改", "审核", "同步", "定时器", "systemd",
    "doorkit", "第二大脑", "切换说明", "接入提示词",
)
TODAY_ID_RE = re.compile(r"（(\d{6}-\d+)[｜）]")


def _keywords(cfg: InstanceConfig) -> tuple[str, ...]:
    import configparser

    from hushai_agent.paths import get_instance_config_dir

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(get_instance_config_dir(cfg.name) / "config.ini", encoding="utf-8")
        raw = parser.get("stats", "system_keywords", fallback="")
    except (configparser.Error, OSError):
        raw = ""
    kws = tuple(k.strip() for k in raw.split(",") if k.strip())
    return kws or DEFAULT_SYSTEM_KEYWORDS


def _today_ids_in_window(cfg: InstanceConfig, since: datetime.date) -> set[str]:
    """取窗口内 hub/today.md 各版本里出现过的事项编号；vault 不是 git 时只看当前版。"""
    texts: list[str] = []
    try:
        shas = subprocess.run(
            ["git", "-C", str(cfg.vault), "log", f"--since={since.isoformat()}", "--format=%H", "--", "hub/today.md"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.split()
        for sha in shas[:200]:
            out = subprocess.run(["git", "-C", str(cfg.vault), "show", f"{sha}:hub/today.md"],
                                 capture_output=True, text=True, timeout=30, check=False)
            texts.append(out.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    cur = cfg.vault / "hub" / "today.md"
    if cur.is_file():
        texts.append(cur.read_text(encoding="utf-8", errors="replace"))
    ids: set[str] = set()
    for t in texts:
        ids.update(TODAY_ID_RE.findall(t))
    return ids


def compute(cfg: InstanceConfig, days: int = 7, as_of: datetime.date | None = None) -> dict[str, Any]:
    end = as_of or clock.today(cfg)
    since = end - datetime.timedelta(days=max(1, days) - 1)
    in_win = lambda d: since.isoformat() <= str(d)[:10] <= end.isoformat()  # noqa: E731

    # 自愈的需人工告警与账本里那次 heal 任务是同一件事，不重复计
    severe = [a for a in alerts._read_rows(cfg) if a.get("level") == "needs_human"
              and a.get("kind") != "heal_needs_human" and in_win(a.get("ts", ""))]
    failed_jobs = [r for r in ledger.read(cfg) if r.get("kind") == "job" and in_win(r.get("ts", ""))
                   and r.get("result") in ("failed", "needs_human")]

    shown = _today_ids_in_window(cfg, since)
    acted: set[str] = set()
    kws = tuple(k.casefold() for k in _keywords(cfg))
    total_lines = system_lines = 0
    for rec in iter_inbox_lines(cfg.vault / "hub" / "inbox"):
        if not in_win(rec.date):
            continue
        total_lines += 1
        if any(k in rec.did.casefold() for k in kws):
            system_lines += 1
        for rx in (DONE_RE, DEFER_RE, DROP_RE):
            m = rx.match(rec.did.strip())
            if m:
                acted.add(m.group("id"))
    useful = len(shown & acted)
    return {
        "since": since.isoformat(), "until": end.isoformat(), "days": days,
        "needs_human_alerts": len(severe), "failed_jobs": len(failed_jobs),
        "needs_human_total": len(severe) + len(failed_jobs),
        "today_shown": len(shown), "today_acted": useful,
        "today_useful_rate": round(useful / len(shown), 3) if shown else None,
        "inbox_lines": total_lines, "system_lines": system_lines,
        "self_talk_rate": round(system_lines / total_lines, 3) if total_lines else None,
    }


def _pct(v: float | None) -> str:
    return "无数据" if v is None else f"{v * 100:.0f}%"


def render_section(stats: dict[str, Any]) -> str:
    """周报里的「北极星」一节（Markdown）。"""
    return "\n".join([
        "", f"## 北极星（{stats['since']} 至 {stats['until']}）",
        f"- 需人介入：{stats['needs_human_total']} 次（失败或需人工的任务 {stats['failed_jobs']}，周边告警 {stats['needs_human_alerts']}）",
        f"- 待办有用率：{_pct(stats['today_useful_rate'])}（今日待办出现过 {stats['today_shown']} 项，被完成、推迟或放弃 {stats['today_acted']} 项）",
        f"- 系统自嗨率：{_pct(stats['self_talk_rate'])}（收件 {stats['inbox_lines']} 行，讲系统自身 {stats['system_lines']} 行；关键词粗估）",
        "",
    ])


def to_json(stats: dict[str, Any]) -> str:
    return json.dumps(stats, ensure_ascii=False, indent=2)
