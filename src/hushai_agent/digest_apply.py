"""汇总流水线辅助模块：应用模型结构化输出、格式化候选记忆与更正行、执行 Git 提交。"""

from __future__ import annotations

import re
from typing import Any

from hushai_agent import items, statusfile
from hushai_agent.config import InstanceConfig
from hushai_agent.gitutil import commit_hub_if_git, is_hub_git_dirty  # noqa: F401
from hushai_agent.fsio import append_line
from hushai_agent.inboxscan import InboxRecord
from hushai_agent.items import FlexDate, OpenItem
from hushai_agent.statusfile import StatusRow


def apply_model_output(
    all_items: list[OpenItem],
    archived_ids: set[str],
    status_rows: list[StatusRow],
    model_data: dict[str, Any] | None,
    as_of_date: FlexDate,
    pre_open_ids: set[str] | None = None,
) -> dict[str, Any]:
    """将模型返回的 items 与 status_rows 应用到内存中的开放项和近况列表。"""
    created_items: list[OpenItem] = []
    closed_items: list[OpenItem] = []
    deferred_items: list[OpenItem] = []
    ignored_refs: list[str] = []
    touched_closed: list[str] = []

    if model_data is None:
        return {
            "created": created_items,
            "closed": closed_items,
            "deferred": deferred_items,
            "ignored": ignored_refs,
            "touched_closed": touched_closed,
        }

    item_map: dict[str, OpenItem] = {it.id: it for it in all_items}
    closed_this_round_ids = {
        it.id
        for it in all_items
        if it.status != "open"
        and (
            (pre_open_ids is not None and it.id in pre_open_ids)
            or getattr(it, "_closed_this_round", False)
        )
    }

    for entry in model_data.get("items", []):
        ref = entry.get("ref")
        status = entry.get("status", "open")
        sched = FlexDate.from_any(entry.get("scheduled_date"))
        due = FlexDate.from_any(entry.get("due_date"))
        srcs = [str(s).strip() for s in entry.get("source_line_ids", []) if str(s).strip()]
        rec = str(entry.get("recommendation") or "").strip() if entry.get("owner") == "user_decision" else ""

        if ref is None or not str(ref).strip():
            if status in ("open", "deferred"):
                new_id = items.allocate_id(all_items, archived_ids, as_of_date)
                new_item = OpenItem(
                    id=new_id,
                    title=entry["title"],
                    status="open",
                    owner=entry["owner"],
                    sched=sched,
                    due=due,
                    domain=entry["domain"],
                    verified=as_of_date,
                    closed=None,
                    src=srcs,
                    rec=rec,
                )
                all_items.append(new_item)
                item_map[new_id] = new_item
                created_items.append(new_item)
            continue

        ref_id = str(ref).strip()
        target = item_map.get(ref_id)
        if target is None:
            ignored_refs.append(ref_id)
            continue
        if ref_id in closed_this_round_ids:
            for sid in srcs:
                if sid not in target.src:
                    target.src.append(sid)
            if ref_id not in touched_closed:
                touched_closed.append(ref_id)
            continue
        if target.status != "open":
            ignored_refs.append(ref_id)
            continue

        if status == "done":
            target.status = "done"
            target.closed = as_of_date
            closed_items.append(target)
        elif status == "dropped":
            target.status = "dropped"
            target.closed = as_of_date
            closed_items.append(target)
        elif status in ("deferred", "open"):
            target.status = "open"
            if sched is not None:
                target.sched = sched
            if due is not None:
                target.due = due
            if rec:
                target.rec = rec
            if status == "deferred":
                target.defers += 1
            if status == "deferred" or sched is not None:
                deferred_items.append(target)

        for sid in srcs:
            if sid not in target.src:
                target.src.append(sid)
        target.verified = as_of_date

    for s_row in model_data.get("status_rows", []):
        statusfile.upsert(
            status_rows,
            domain=s_row["domain"],
            topic=s_row["topic"],
            summary=s_row["summary"],
            src_ids=s_row["source_line_ids"],
            date=as_of_date,
        )

    return {
        "created": created_items,
        "closed": closed_items,
        "deferred": deferred_items,
        "ignored": ignored_refs,
        "touched_closed": touched_closed,
    }


def build_candidate_and_correction_lines(
    as_of_date: FlexDate,
    mech_candidates: list[InboxRecord],
    mech_corrections: list[InboxRecord],
    model_data: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """生成待追加到 candidates.md 与 corrections.md 的单行记录列表。"""
    date_s = as_of_date.isoformat()
    cand_lines: list[str] = []
    for rec in mech_candidates:
        clean_text = rec.did.replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
        cand_lines.append(f"- {date_s}｜inbox｜{clean_text}｜src={rec.line_id}")

    if model_data is not None:
        for cand in model_data.get("memory_candidates", []):
            kind = str(cand.get("kind", "insight")).strip()
            clean_text = (
                str(cand.get("text", ""))
                .replace("\r", " ")
                .replace("\n", " ")
                .replace("｜", "|")
                .strip()
            )
            srcs = [str(s).strip() for s in cand.get("source_line_ids", []) if str(s).strip()]
            cand_lines.append(f"- {date_s}｜{kind}｜{clean_text}｜src={','.join(srcs)}")

    corr_lines: list[str] = []
    for rec in mech_corrections:
        clean_did = rec.did.replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
        corr_lines.append(f"- {date_s}｜{clean_did}｜src={rec.line_id}")

    return cand_lines, corr_lines


_CORR_SRC_RE = re.compile(r"｜src=(?P<src>[^\s｜]+)\s*$")


def append_corrections_idempotent(
    corr_file: Path,
    corr_lines: list[str],
    roots: list[Path],
) -> int:
    """按目标文件 hub/reports/corrections.md 既有内容与 src 编号去重，幂等追加更正行。"""
    if not corr_lines:
        return 0
    existing_lines: set[str] = set()
    existing_srcs: set[str] = set()
    if corr_file.is_file():
        for raw in corr_file.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s:
                continue
            existing_lines.add(s)
            m = _CORR_SRC_RE.search(s)
            if m:
                existing_srcs.add(m.group("src").strip())

    added = 0
    for line in corr_lines:
        s = line.strip()
        if not s or s in existing_lines:
            continue
        m = _CORR_SRC_RE.search(s)
        src_id = m.group("src").strip() if m else ""
        if src_id and src_id in existing_srcs:
            continue
        append_line(corr_file, line, roots)
        existing_lines.add(s)
        if src_id:
            existing_srcs.add(src_id)
        added += 1
    return added


def build_append_targets_manifest(
    cand_approvals: list[dict[str, str]],
    cortex_res: dict[str, Any],
) -> dict[str, Any]:
    """整理 profile.md / AGENTS.local.md / decisions.md / lessons.md 的计划追加清单。"""
    return {
        "candidate_approvals": list(cand_approvals),
        "decisions": list(cortex_res.get("planned_decisions") or []),
        "lessons": list(cortex_res.get("planned_lessons") or []),
    }


def build_closing_plan(
    cfg: InstanceConfig,
    as_of_date: FlexDate,
    rest_records: list[InboxRecord],
    *,
    commit_msg: str,
    ledger_job: str,
) -> dict[str, Any]:
    """构造显式收尾计划（晨间简报刷新与 Git 提交；第二大脑不在这条路径上）。"""
    return {
        "brief_required": True,
        "commit_msg": commit_msg,
        "ledger_job": ledger_job,
    }
