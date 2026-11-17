"""基于目标文件的副作用核验与幂等修复辅助（不盲信 JSON checkpoint 状态）。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from hushai_agent import brief, candidates, cortex, cursor, items, ledger, report, statusfile
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext
from hushai_agent.daily_state import load_daily_accumulated, update_daily_state
from hushai_agent.digest_apply import append_corrections_idempotent
from hushai_agent.gitutil import commit_hub_if_git, is_hub_git_dirty
from hushai_agent.fsio import append_line, atomic_write
from hushai_agent.inboxscan import iter_inbox_lines
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.statusfile import StatusRow
from hushai_agent.vault import add_inbox_entry


def sha256_text(text: str) -> str:
    """计算 UTF-8 文本的 SHA-256 摘要，用于目标文件内容一致校验。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str | None:
    """读取文件实际字节计算 SHA-256；文件不存在或不可读返回 None。"""
    if not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def verify_open_items_and_archive(vault: Path, spec: dict[str, Any]) -> None:
    """直接核验 closed-items.md 与 open-items.md 目标文件，缺漏或内容不一致时幂等修复。"""
    if not spec:
        return
    archived_specs = list(spec.get("archived_lines") or [])
    if archived_specs:
        archive_path = vault / "hub" / "reports" / "closed-items.md"
        existing_archived = items.load_archived_ids(vault)
        for entry in archived_specs:
            item_id = str(entry.get("id") or "")
            line = str(entry.get("line") or "")
            if item_id and line and item_id not in existing_archived:
                append_line(archive_path, line, [vault])
                existing_archived.add(item_id)

    expected_sha = str(spec.get("sha256") or "")
    content = str(spec.get("content") or "")
    open_path = vault / "hub" / "open-items.md"
    if expected_sha and file_sha256(open_path) != expected_sha:
        from hushai_agent.items_store import _quarantine_unparsed

        _quarantine_unparsed(vault, open_path)
        atomic_write(open_path, content, [vault])


def verify_atomic_targets(vault: Path, targets: dict[str, dict[str, Any]]) -> None:
    """按目标文件字节 SHA-256 逐一核验 atomic_write 产出（today / status / index 等），不一致则补写。"""
    for rel, spec in (targets or {}).items():
        expected_sha = str(spec.get("sha256") or "")
        content = str(spec.get("content") or "")
        target_path = vault / rel
        if expected_sha and file_sha256(target_path) != expected_sha:
            atomic_write(target_path, content, [vault])


def verify_append_targets(cfg: InstanceConfig, ckpt: dict[str, Any]) -> None:
    """核验并幂等补齐 corrections / status-archive / candidates / profile / AGENTS.local / decisions / lessons。"""
    vault = cfg.vault
    corr_lines = list(ckpt.get("corrections") or [])
    if corr_lines:
        append_corrections_idempotent(vault / "hub" / "reports" / "corrections.md", corr_lines, [vault])

    archive_rows_raw = list(ckpt.get("status_archive") or [])
    if archive_rows_raw:
        rows = [
            StatusRow(
                domain=str(r["domain"]),
                topic=str(r["topic"]),
                summary=str(r["summary"]),
                verified=FlexDate.from_any(r.get("verified")),
                src=list(r.get("src") or []),
            )
            for r in archive_rows_raw
        ]
        statusfile.append_status_archive(vault / "hub" / "reports" / "status-archive.md", rows, [vault])

    cand_spec = ckpt.get("candidates_add") or {}
    cand_lines = list(cand_spec.get("lines") or [])
    if cand_lines:
        expected_pairs = {(str(p[0]), str(p[1])) for p in (cand_spec.get("expected_pairs") or [])}
        existing_pairs = {(c.text, c.src) for c in candidates.load(cfg)}
        missing_expected = expected_pairs - existing_pairs
        if missing_expected or not expected_pairs:
            as_of_d = FlexDate.from_any(ckpt.get("as_of")) or ckpt.get("as_of")
            limit = int(cand_spec.get("limit") or load_ext(cfg).candidates_pending)
            n_user = int(cand_spec.get("n_user") or 0)
            candidates.add(cfg, cand_lines, n_user, as_of_d, limit, write=True)

    app_targets = ckpt.get("append_targets") or {}
    approvals = list(app_targets.get("candidate_approvals") or [])
    if approvals:
        by_id = {c.id: c for c in candidates.load(cfg)}
        for spec in approvals:
            cid = str(spec.get("id") or "")
            status = str(spec.get("status") or "")
            target = str(spec.get("target") or "")
            text = str(spec.get("text") or "")
            date_str = str(spec.get("date") or ckpt.get("as_of") or "")
            if not cid:
                continue
            cand_obj = by_id.get(cid)
            if cand_obj is not None and cand_obj.status == "pending":
                action = "approve" if status == "approved" else "discard"
                candidates.decide(cfg, cid, action, target or "profile", date_str, write=True)
                by_id = {c.id: c for c in candidates.load(cfg)}
            if status == "approved" and target and text:
                candidates.ensure_approved_target_line(cfg, target, cid, text, date_str)

    cortex.ensure_entries_on_disk(
        cfg,
        list(app_targets.get("decisions") or []),
        list(app_targets.get("lessons") or []),
    )


def verify_daily_state_and_cursor(cfg: InstanceConfig, ckpt: dict[str, Any]) -> None:
    """核验 <state>/daily/<date>.json、hub/reports/daily-<date>.md 与 digested.jsonl 游标。"""
    state_dir = get_instance_state_dir(cfg.name)
    ds = ckpt.get("daily_state") or {}
    as_of_str = str(ds.get("as_of") or ckpt.get("as_of") or "")
    run_id = str(ckpt.get("run_id") or ds.get("run_id") or "")
    if as_of_str and ds.get("cur_facts"):
        acc = load_daily_accumulated(state_dir, as_of_str)
        if run_id not in (acc.get("applied_runs") or []):
            acc = update_daily_state(
                state_dir,
                dict(ds["cur_facts"]),
                dry_run=False,
                is_mechanical=bool(ds.get("is_mechanical")),
                run_id=run_id,
            )
        daily_rel = str(ds.get("daily_report_rel") or f"hub/reports/daily-{as_of_str}.md")
        daily_path = cfg.vault / daily_rel
        expected_daily = str(ds.get("daily_report_text") or "")
        expected_sha = str(ds.get("daily_report_sha256") or "")
        if not expected_daily:
            expected_daily = report.render_daily(acc)
            expected_sha = sha256_text(expected_daily)
        if file_sha256(daily_path) != expected_sha:
            atomic_write(daily_path, expected_daily, [cfg.vault])

    advance_ids = [str(x).strip() for x in (ckpt.get("advance_ids") or []) if str(x).strip()]
    if advance_ids:
        existing_ids = cursor.load_ids(cfg)
        missing_ids = [lid for lid in advance_ids if lid not in existing_ids]
        if missing_ids:
            cursor.append(cfg, missing_ids, as_of_str)


def _brief_matches_disk(cfg: InstanceConfig) -> bool:
    """核验 morning-brief.md 与 .html 均存在，且二者字节内容均与当前库态一致。"""
    from hushai_agent.brief_html import render_html

    reports = cfg.vault / "hub" / "reports"
    md_path = reports / "morning-brief.md"
    html_path = reports / "morning-brief.html"
    if not md_path.is_file() or not html_path.is_file():
        return False
    try:
        data = brief.build_brief(cfg)
        expected_md = brief.render_md(data)
        expected_html = render_html(data, live=False)
        return (
            md_path.read_text(encoding="utf-8") == expected_md
            and html_path.read_text(encoding="utf-8") == expected_html
        )
    except Exception:
        return False


def verify_closing_steps(cfg: InstanceConfig, ckpt: dict[str, Any]) -> dict[str, Any]:
    """核验收尾步（晨间简报 MD/HTML 刷新、Git 提交）并补齐缺漏。"""
    closing = ckpt.setdefault("closing", {})
    as_of_str = str(ckpt.get("as_of") or "")
    out: dict[str, Any] = {"brief_refreshed": False, "git_committed": False}

    if closing.get("brief_required", True) and not _brief_matches_disk(cfg):
        out["brief_refreshed"] = brief.refresh(cfg)

    commit_msg = str(closing.get("commit_msg") or f"recover {as_of_str}")
    if is_hub_git_dirty(cfg.vault):
        out["git_committed"] = commit_hub_if_git(cfg.vault, commit_msg)

    return out


def verify_routing_targets(cfg: InstanceConfig, spec: dict[str, Any]) -> None:
    """直接核验 @路由 回复文件、管家收件回执（按 artifact 查目标收件）与 routed.jsonl。"""
    reply_rel = str(spec["reply_rel"])
    reply_content = str(spec["reply_content"])
    reply_sha = str(spec["reply_sha256"])
    reply_path = cfg.vault / reply_rel
    if file_sha256(reply_path) != reply_sha:
        atomic_write(reply_path, reply_content, [cfg.vault])

    steward_door = str(spec["steward_door"])
    target_door = str(spec["target_door"])
    inbox_dir = cfg.vault / "hub" / "inbox"
    receipt_exists = any(
        r.door == steward_door and r.artifact == reply_rel
        for r in iter_inbox_lines(inbox_dir)
    )
    if not receipt_exists:
        domain = str(spec["receipt_domain"])
        brief_text = str(spec["receipt_brief"])
        add_inbox_entry(cfg.name, steward_door, domain, f"回复 @{target_door}：{brief_text}", artifact=reply_rel)

    state_dir = get_instance_state_dir(cfg.name)
    routed_path = state_dir / "routed.jsonl"
    line_id = str(spec["line_id"])
    target = str(spec["target"])
    already_ok = False
    if routed_path.is_file():
        import json

        for raw in routed_path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except ValueError:
                continue
            if row.get("line_id") == line_id and row.get("result") == "ok":
                already_ok = True
                break
    if not already_ok:
        import json

        row_payload = {"line_id": line_id, "target": target, "ts": str(spec["ts"]), "result": "ok"}
        append_line(routed_path, json.dumps(row_payload, ensure_ascii=False), [state_dir])

    from hushai_agent import outbox

    outbox.enqueue(
        cfg,
        channel="exchange_out",
        target=target_door,
        payload={"rel_path": reply_rel, "line_id": line_id, "target": target},
        dedupe_key=f"route:{line_id}:{reply_rel}",
        immediate_sent=True,
    )


def unlink_if_exists(path: Path, roots: list[Path]) -> None:
    """按路径护栏安全删除已完成校验的检查点文件。"""
    from hushai_agent.guard import ensure_writable

    try:
        os.unlink(ensure_writable(path, roots))
    except FileNotFoundError:
        pass
