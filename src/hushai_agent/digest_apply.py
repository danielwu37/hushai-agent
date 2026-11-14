"""汇总流水线辅助模块：应用模型结构化输出、格式化候选记忆与更正行、执行 Git 提交。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hushai_agent import items, statusfile
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


def commit_hub_if_git(vault_path: Path, message: str) -> bool:
    """当 vault 是 Git 仓库且 hub/ 下有变更时，执行 git add -A hub 并提交一次。"""
    if not (vault_path / ".git").exists() or shutil.which("git") is None:
        return False

    cwd = os.fspath(vault_path)
    try:
        res_add = subprocess.run(
            ["git", "add", "-A", "hub"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if res_add.returncode != 0:
            return False

        res_diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if res_diff.returncode == 0:
            return False

        res_commit = subprocess.run(
            [
                "git",
                "-c",
                "user.name=hushai",
                "-c",
                "user.email=hushai@localhost",
                "commit",
                "-m",
                message,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return res_commit.returncode == 0
    except OSError:
        return False
