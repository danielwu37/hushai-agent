"""汇总流水线判断步：构建提示词、定义 JSON Schema 与校验模型输出。"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import read_for_prompt
from hushai_agent.inboxscan import InboxRecord
from hushai_agent.items import VALID_OWNERS, FlexDate, ItemFormatError, OpenItem

VALID_JUDGE_STATUSES = ("open", "done", "deferred", "dropped")
VALID_CANDIDATE_KINDS = ("convention", "insight")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items", "status_rows", "memory_candidates"],
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "ref",
                    "title",
                    "owner",
                    "status",
                    "scheduled_date",
                    "due_date",
                    "domain",
                    "source_line_ids",
                ],
                "additionalProperties": False,
                "properties": {
                    "ref": {"type": ["string", "null"]},
                    "title": {"type": "string"},
                    "owner": {"type": "string", "enum": list(VALID_OWNERS)},
                    "status": {"type": "string", "enum": list(VALID_JUDGE_STATUSES)},
                    "scheduled_date": {"type": ["string", "null"]},
                    "due_date": {"type": ["string", "null"]},
                    "domain": {"type": "string"},
                    "recommendation": {"type": ["string", "null"]},
                    "source_line_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "status_rows": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["domain", "topic", "summary", "source_line_ids"],
                "additionalProperties": False,
                "properties": {
                    "domain": {"type": "string"},
                    "topic": {"type": "string"},
                    "summary": {"type": "string"},
                    "source_line_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "memory_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "text", "source_line_ids"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": list(VALID_CANDIDATE_KINDS)},
                    "text": {"type": "string"},
                    "source_line_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}


def _load_prompt_template(name: str) -> str:
    """读取包内内置提示词模板。"""
    prompt_path = Path(__file__).resolve().parent / "prompts" / name
    return prompt_path.read_text(encoding="utf-8")


def build_prompt(
    cfg: InstanceConfig,
    records: Iterable[InboxRecord],
    items: Iterable[OpenItem],
    as_of: datetime.date | str,
    pre_open_ids: set[str] | None = None,
) -> str:
    """将对账前开放的开放项、本轮收件行、基准日与领域列表填充至 digest.zh.md 提示词模板。"""
    as_of_date = FlexDate.from_any(as_of)
    if as_of_date is None:
        raise ValueError(f"非法基准日期 as_of: {as_of!r}")

    checked_paths: set[Path] = set()
    line_strs: list[str] = []
    for rec in records:
        if rec.path not in checked_paths and rec.path.exists():
            read_for_prompt(rec.path, cfg.vault, cfg.local_only)
            checked_paths.add(rec.path)
        line_strs.append(f"{rec.line_id}\t{rec.date} {rec.door}\t{rec.raw}")

    open_strs: list[str] = []
    for it in items:
        was_open = (
            (pre_open_ids is not None and it.id in pre_open_ids)
            or getattr(it, "_closed_this_round", False)
        )
        if it.status != "open" and not was_open:
            continue
        is_closed_now = it.status != "open" and was_open
        owner_col = f"{it.owner} closed-this-round" if is_closed_now else it.owner
        sched_s = it.sched.isoformat() if it.sched else "-"
        due_s = it.due.isoformat() if it.due else "-"
        open_strs.append(f"{it.id}\t{owner_col}\t{sched_s}\t{due_s}\t{it.title}")

    tpl = _load_prompt_template("digest.zh.md")
    tpl = tpl.replace("{{TODAY}}", as_of_date.isoformat())
    tpl = tpl.replace("{{DOMAINS}}", ", ".join(cfg.domains))
    tpl = tpl.replace("{{OPEN_ITEMS}}", "\n".join(open_strs) if open_strs else "（无）")
    tpl = tpl.replace("{{LINES}}", "\n".join(line_strs) if line_strs else "（无）")
    return tpl


def _is_valid_opt_date(val: Any) -> bool:
    """检查可选日期字段是否为 None 或合法的 YYYY-MM-DD 字符串。"""
    if val is None:
        return True
    if not isinstance(val, str):
        return False
    try:
        parsed = FlexDate.from_any(val)
    except ItemFormatError:
        return False
    return parsed is not None and val.strip() == parsed.isoformat()


def _valid_source_ids(val: Any, known_ids: set[str]) -> bool:
    """检查 source_line_ids 是否为非空字符串列表且全部在已知编号集合内。"""
    if not isinstance(val, list) or not val:
        return False
    return all(isinstance(x, str) and x.strip() in known_ids for x in val)


def make_validator(
    known_line_ids: Iterable[str],
    item_ids: Iterable[str],
    domains: Iterable[str],
    n_lines: int,
) -> Callable[[dict[str, Any] | None], tuple[dict[str, Any] | None, str | None]]:
    """生成供 runner.run_model 使用的结构与引用校验函数。"""
    known_set = {str(x).strip() for x in known_line_ids if str(x).strip()}
    item_id_set = {str(x).strip() for x in item_ids if str(x).strip()}
    domain_set = {str(x).strip() for x in domains if str(x).strip()}
    max_items = max(10, 2 * max(int(n_lines), 0))

    def validate(data: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(data, dict):
            return None, "模型返回数据不是字典"
        for key in ("items", "status_rows", "memory_candidates"):
            if key not in data or not isinstance(data[key], list):
                return None, f"顶层键 '{key}' 缺失或不是数组"
        if len(data["items"]) > max_items:
            return None, f"items 条数 {len(data['items'])} 超过上限 {max_items}"

        dropped: list[dict[str, Any]] = []
        clean_items: list[dict[str, Any]] = []
        for entry in data["items"]:
            if not isinstance(entry, dict):
                dropped.append({"section": "items", "reason": "not-dict", "entry": entry})
                continue
            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                dropped.append({"section": "items", "reason": "empty-title", "entry": entry})
                continue
            if entry.get("owner") not in VALID_OWNERS or entry.get("status") not in VALID_JUDGE_STATUSES:
                dropped.append({"section": "items", "reason": "invalid-enum", "entry": entry})
                continue
            if entry.get("domain") not in domain_set:
                dropped.append({"section": "items", "reason": "invalid-domain", "entry": entry})
                continue
            if not _is_valid_opt_date(entry.get("scheduled_date")) or not _is_valid_opt_date(entry.get("due_date")):
                dropped.append({"section": "items", "reason": "invalid-date", "entry": entry})
                continue
            ref = entry.get("ref")
            if ref is not None and (not isinstance(ref, str) or ref.strip() not in item_id_set):
                dropped.append({"section": "items", "reason": "unknown-ref", "entry": entry})
                continue
            if not _valid_source_ids(entry.get("source_line_ids"), known_set):
                dropped.append({"section": "items", "reason": "invalid-source-line-ids", "entry": entry})
                continue
            clean_items.append(entry)

        clean_status: list[dict[str, Any]] = []
        for entry in data["status_rows"]:
            if not isinstance(entry, dict):
                dropped.append({"section": "status_rows", "reason": "not-dict", "entry": entry})
                continue
            if entry.get("domain") not in domain_set:
                dropped.append({"section": "status_rows", "reason": "invalid-domain", "entry": entry})
                continue
            topic, summary = entry.get("topic"), entry.get("summary")
            if not isinstance(topic, str) or not topic.strip() or not isinstance(summary, str) or not summary.strip():
                dropped.append({"section": "status_rows", "reason": "empty-topic-or-summary", "entry": entry})
                continue
            if not _valid_source_ids(entry.get("source_line_ids"), known_set):
                dropped.append({"section": "status_rows", "reason": "invalid-source-line-ids", "entry": entry})
                continue
            clean_status.append(entry)

        clean_mem: list[dict[str, Any]] = []
        for entry in data["memory_candidates"]:
            if not isinstance(entry, dict):
                dropped.append({"section": "memory_candidates", "reason": "not-dict", "entry": entry})
                continue
            if entry.get("kind") not in VALID_CANDIDATE_KINDS:
                dropped.append({"section": "memory_candidates", "reason": "invalid-enum", "entry": entry})
                continue
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                dropped.append({"section": "memory_candidates", "reason": "empty-text", "entry": entry})
                continue
            if not _valid_source_ids(entry.get("source_line_ids"), known_set):
                dropped.append({"section": "memory_candidates", "reason": "invalid-source-line-ids", "entry": entry})
                continue
            clean_mem.append(entry)

        return {
            "items": clean_items,
            "status_rows": clean_status,
            "memory_candidates": clean_mem,
            "_dropped": dropped,
        }, None

    return validate
