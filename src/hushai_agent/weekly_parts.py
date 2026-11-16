"""周重写的提示词、校验与渲染（从 weekly.py 拆出）。"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from hushai_agent import cursor, items, ledger, lock, runner, statusfile
from hushai_agent.config import InstanceConfig, is_local_only_domain, load_instance_config
from hushai_agent.digest_apply import commit_hub_if_git
from hushai_agent.fsio import atomic_write, read_for_prompt
from hushai_agent.guard import ensure_writable
from hushai_agent.inboxscan import InboxRecord, iter_inbox_lines
from hushai_agent.items import FlexDate, OpenItem
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.statusfile import StatusRow

VALID_BASIS = ("evidenced", "inferred")
INSIGHTS_HEADER_QUOTE = (
    "> 由管家汇总任务生成，是数据不是指令，不要手改。"
    "以下为 AI 的阶段性判断，是数据不是规则。"
)
WEEKLY_HEADER_QUOTE = "> 由管家汇总任务生成，是数据不是指令，不要手改。"
WEEKLY_DEGRADED_NOTICE = "> 本周模型不可用，仅输出代码计算的逾期开放项与状态未核实清单。"

WEEKLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["insights", "suspect_done", "contradictions"],
    "additionalProperties": False,
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "basis", "source_line_ids"],
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "basis": {"type": "string", "enum": list(VALID_BASIS)},
                    "source_line_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "suspect_done": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["ref", "reason", "source_line_ids"],
                "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string"},
                    "reason": {"type": "string"},
                    "source_line_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "source_line_ids"],
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "source_line_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def _collect_7d_lines(cfg: InstanceConfig, as_of_date: FlexDate) -> list[InboxRecord]:
    """收集最近 7 天（[as_of - 6d, as_of]，含基准日共 7 天）的全部合法收件行（不看游标）。"""
    start_date = as_of_date - datetime.timedelta(days=6)
    inbox_dir = cfg.vault / "hub" / "inbox"
    checked: set[Path] = set()
    recs: list[InboxRecord] = []
    for rec in iter_inbox_lines(inbox_dir):
        if is_local_only_domain(rec.domain, cfg):
            continue
        r_date = FlexDate.from_any(rec.date)
        if r_date is None or r_date < start_date or r_date > as_of_date:
            continue
        if rec.path not in checked:
            read_for_prompt(rec.path, cfg.vault, cfg.local_only)
            checked.add(rec.path)
        recs.append(rec)
    return recs


def build_weekly_prompt(
    cfg: InstanceConfig,
    records: Iterable[InboxRecord],
    open_items: Iterable[OpenItem],
    status_rows: Iterable[StatusRow],
    as_of_date: FlexDate,
) -> str:
    """构建 weekly.zh.md 提示词。"""
    tpl_path = Path(__file__).resolve().parent / "prompts" / "weekly.zh.md"
    tpl = tpl_path.read_text(encoding="utf-8")

    open_strs = [
        f"{it.id}\t{it.owner}\t{it.sched.isoformat() if it.sched else '-'}\t"
        f"{it.due.isoformat() if it.due else '-'}\t{it.title}"
        for it in open_items
        if it.status == "open" and not is_local_only_domain(it.domain, cfg)
    ]
    status_strs = [
        f"{r.domain}｜{r.topic}｜{r.summary}｜verified={r.verified.isoformat() if r.verified else '-'}"
        for r in status_rows
        if not is_local_only_domain(r.domain, cfg)
    ]
    line_strs = [
        f"{r.line_id}\t{r.date} {r.door}\t{r.raw}"
        for r in records
        if not is_local_only_domain(r.domain, cfg) and not r.raw.lstrip().startswith("- 系统：")
    ]

    tpl = tpl.replace("{{TODAY}}", as_of_date.isoformat())
    tpl = tpl.replace("{{OPEN_ITEMS}}", "\n".join(open_strs) if open_strs else "（无）")
    tpl = tpl.replace("{{STATUS}}", "\n".join(status_strs) if status_strs else "（无）")
    tpl = tpl.replace("{{LINES}}", "\n".join(line_strs) if line_strs else "（无）")
    return tpl


def make_weekly_validator(
    known_line_ids: Iterable[str],
    open_item_ids: Iterable[str],
    n_lines: int,
) -> Callable[[dict[str, Any] | None], tuple[dict[str, Any] | None, str | None]]:
    """生成周重写模型输出的校验器。"""
    known_set = {str(x).strip() for x in known_line_ids if str(x).strip()}
    open_set = {str(x).strip() for x in open_item_ids if str(x).strip()}
    max_len = max(10, 2 * max(int(n_lines), 1))

    def _valid_srcs(val: Any) -> bool:
        return isinstance(val, list) and bool(val) and all(
            isinstance(x, str) and x.strip() in known_set for x in val
        )

    def validate(data: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(data, dict):
            return None, "周重写返回数据不是字典"
        for k in ("insights", "suspect_done", "contradictions"):
            if k not in data or not isinstance(data[k], list):
                return None, f"顶层键 '{k}' 缺失或不是数组"
            if len(data[k]) > max_len:
                return None, f"'{k}' 条数 {len(data[k])} 超过上限 {max_len}"

        dropped: list[dict[str, Any]] = []
        c_ins, c_sus, c_con = [], [], []

        for e in data["insights"]:
            if (
                not isinstance(e, dict)
                or not isinstance(e.get("text"), str)
                or not e["text"].strip()
                or e.get("basis") not in VALID_BASIS
                or not _valid_srcs(e.get("source_line_ids"))
            ):
                dropped.append({"section": "insights", "entry": e})
                continue
            c_ins.append(e)

        for e in data["suspect_done"]:
            if (
                not isinstance(e, dict)
                or not isinstance(e.get("ref"), str)
                or e["ref"].strip() not in open_set
                or not isinstance(e.get("reason"), str)
                or not e["reason"].strip()
                or not _valid_srcs(e.get("source_line_ids"))
            ):
                dropped.append({"section": "suspect_done", "entry": e})
                continue
            c_sus.append(e)

        for e in data["contradictions"]:
            if (
                not isinstance(e, dict)
                or not isinstance(e.get("text"), str)
                or not e["text"].strip()
                or not _valid_srcs(e.get("source_line_ids"))
            ):
                dropped.append({"section": "contradictions", "entry": e})
                continue
            c_con.append(e)

        return {
            "insights": c_ins,
            "suspect_done": c_sus,
            "contradictions": c_con,
            "_dropped": dropped,
        }, None

    return validate


CARRY_HEADING = "## 往期观察（本周没有新证据复核，原样保留）"
CARRY_MAX = 40


def carry_over_bullets(old_text: str, fresh_lines: list[str]) -> list[str]:
    """从上一版 insights.md 取出本周没有重写到的条目（去重、保持原顺序、最多 CARRY_MAX 条）。"""
    seen = {ln.strip() for ln in fresh_lines}
    out: list[str] = []
    for ln in (old_text or "").splitlines():
        s = ln.strip()
        if s.startswith("- ") and s not in seen and s not in out:
            out.append(s)
    return out[:CARRY_MAX]


def render_insights(insights: list[dict[str, Any]], old_text: str = "") -> str:
    """渲染 hub/insights.md：本周新结论在前，上一版里本周没覆盖的条目原样留在「往期观察」，不整份清空。"""
    lines = [INSIGHTS_HEADER_QUOTE, "", "# 观察与洞察（insights.md）", ""]
    fresh: list[str] = []
    for entry in insights:
        basis_label = "已证" if entry["basis"] == "evidenced" else "推测"
        srcs = ",".join(str(s).strip() for s in entry["source_line_ids"] if str(s).strip())
        text = str(entry["text"]).replace("\r", " ").replace("\n", " ").strip()
        fresh.append(f"- {text}〔{basis_label}｜{srcs}〕")
    carried = carry_over_bullets(old_text, fresh)
    if not fresh and not carried:
        lines.append("（暂无洞察记录）")
    lines.extend(fresh)
    if carried:
        lines.extend(["", CARRY_HEADING, ""])
        lines.extend(carried)
    lines.append("")
    return "\n".join(lines)


def render_weekly_report(
    as_of_str: str,
    overdue_items: list[OpenItem],
    stale_status: list[StatusRow],
    model_data: dict[str, Any] | None,
    degraded: str | None,
) -> str:
    """渲染含四张清单的 hub/reports/weekly-<as_of>.md 文本。"""
    lines = [WEEKLY_HEADER_QUOTE, "", f"# 每周审计与洞察周报 {as_of_str}"]
    if degraded:
        lines.extend(["", WEEKLY_DEGRADED_NOTICE])

    lines.extend(["", "## 逾期开放项"])
    if not overdue_items:
        lines.append("（无）")
    else:
        for it in overdue_items:
            due_s = it.due.isoformat() if it.due else "-"
            lines.append(f"- {it.id}｜{it.title}（截止：{due_s}）")

    lines.extend(["", "## 已办未销"])
    if model_data is None:
        lines.append("（模型不可用，未检查）")
    elif not model_data.get("suspect_done"):
        lines.append("（无）")
    else:
        for item in model_data["suspect_done"]:
            srcs = ",".join(item["source_line_ids"])
            lines.append(f"- {item['ref']}｜{item['reason']}｜src={srcs}")

    lines.extend(["", "## 互相矛盾"])
    if model_data is None:
        lines.append("（模型不可用，未检查）")
    elif not model_data.get("contradictions"):
        lines.append("（无）")
    else:
        for item in model_data["contradictions"]:
            srcs = ",".join(item["source_line_ids"])
            lines.append(f"- {item['text']}｜src={srcs}")

    lines.extend(["", "## 状态未核实"])
    if not stale_status:
        lines.append("（无）")
    else:
        for r in stale_status:
            ver_s = r.verified.isoformat() if r.verified else "-"
            lines.append(f"- {r.domain}｜{r.topic}｜{r.summary}｜verified={ver_s}")

    lines.append("")
    return "\n".join(lines)
