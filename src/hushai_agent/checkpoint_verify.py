"""基于目标文件的副作用核验与幂等修复辅助（不盲信 JSON checkpoint 状态）。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from hushai_agent import brain_cfg, brain_process, brain_routines, brief, candidates, cortex, cursor, items, ledger, report, statusfile
from hushai_agent.brain_cfg import load_brain_state, save_brain_state
from hushai_agent.brain_model import (
    IndexEntry,
    extract_source_refs_from_md,
    parse_index_entries,
    parse_note_sections,
    read_index_first,
    upsert_index_entry,
)
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext
from hushai_agent.daily_state import load_daily_accumulated, update_daily_state
from hushai_agent.digest_apply import append_corrections_idempotent, apply_brain_step, commit_hub_if_git, is_hub_git_dirty
from hushai_agent.fsio import append_line, atomic_write
from hushai_agent.inboxfmt import ALL_RESERVED_DOMAINS
from hushai_agent.inboxscan import iter_inbox_lines
from hushai_agent.items import FlexDate
from hushai_agent import mechanical
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


_SUBDIR_TOPIC = {"notes": "笔记", "sources": "来源", "people": "人物", "projects": "项目"}


def _scan_brain_disk_refs(cfg: InstanceConfig) -> dict[str, list[dict[str, str]]]:
    """扫描 hub/{notes,sources,people,projects}/*.md，按 source_ref 建立磁盘实存档案索引。"""
    hub_dir = cfg.vault / "hub"
    by_ref: dict[str, list[dict[str, str]]] = {}
    for sub in ("notes", "sources", "people", "projects"):
        sub_dir = hub_dir / sub
        if not sub_dir.is_dir():
            continue
        for md in sorted(sub_dir.glob("*.md")):
            try:
                txt = md.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = parse_note_sections(txt)
            title = str(parsed.get("title") or md.stem)
            topic = str(parsed.get("topic") or _SUBDIR_TOPIC.get(sub, "笔记"))
            rel = f"{sub}/{md.name}"
            entry_type = sub[:-1] if sub.endswith("s") else ("person" if sub == "people" else sub)
            info = {"rel": rel, "type": entry_type, "topic": topic, "title": title, "path": rel}
            for ref in extract_source_refs_from_md(txt):
                by_ref.setdefault(ref, []).append(info)
    return by_ref


def _scan_eligible_brain_records(
    cfg: InstanceConfig, req_ids: list[str]
) -> tuple[list[str], dict[str, Any]]:
    """从计划 record_ids 中筛出非保留域、非机械动作且正文非空的第二大脑输入及原记录映射。"""
    if not req_ids:
        return [], {}
    req_set = set(req_ids)
    by_id = {r.line_id: r for r in iter_inbox_lines(cfg.vault / "hub" / "inbox") if r.line_id in req_set}
    eligible: list[str] = []
    rec_map: dict[str, Any] = {}
    for lid in req_ids:
        rec = by_id.get(lid)
        if rec is None:
            eligible.append(lid)
            continue
        if rec.domain in ALL_RESERVED_DOMAINS:
            continue
        if mechanical.parse_action(rec.did) is not None:
            continue
        if str(rec.did or "").strip():
            eligible.append(lid)
            rec_map[lid] = rec
    return eligible, rec_map


def _ensure_brain_index_entries(
    cfg: InstanceConfig,
    items_to_index: list[dict[str, str]],
    max_lines: int,
) -> None:
    """确保每份第二大脑目标文件都在 hub/index.md 中有有效链接。"""
    _, entries = read_index_first(cfg)
    idx_targets = {e.target.strip().lstrip("./") for e in entries}
    for item in items_to_index:
        rel = str(item.get("rel") or item.get("path") or "").strip().lstrip("./")
        if not rel or rel in idx_targets:
            continue
        sub = rel.split("/", 1)[0]
        title = str(item.get("title") or item.get("name") or Path(rel).stem)
        topic = str(item.get("topic") or _SUBDIR_TOPIC.get(sub, "笔记"))
        upsert_index_entry(
            cfg,
            IndexEntry(topic=topic, title=title, target=rel, brief=title[:50]),
            max_lines=max_lines,
            write=True,
        )
        idx_targets.add(rel)


def _capture_brain_manifest(
    cfg: InstanceConfig,
    as_of_str: str,
    filed_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """捕获第二大脑实际落盘文件及 daily/<date>.md 的校验清单。"""
    hub_dir = cfg.vault / "hub"
    seen_rel: set[str] = set()
    filed_manifest: list[dict[str, Any]] = []
    for entry in filed_entries:
        rel = str(entry.get("path") or entry.get("rel") or "").strip().lstrip("./")
        if not rel or rel in seen_rel:
            continue
        fpath = hub_dir / rel
        if not fpath.is_file():
            continue
        seen_rel.add(rel)
        text = fpath.read_text(encoding="utf-8")
        sub = rel.split("/", 1)[0]
        parsed = parse_note_sections(text)
        filed_manifest.append({
            "rel": rel,
            "sha256": sha256_text(text),
            "content": text,
            "topic": str(entry.get("topic") or parsed.get("topic") or _SUBDIR_TOPIC.get(sub, "笔记")),
            "title": str(entry.get("title") or entry.get("name") or parsed.get("title") or Path(rel).stem),
        })

    manifest: dict[str, Any] = {"filed": filed_manifest}
    daily_rel = f"daily/{as_of_str}.md"
    daily_path = hub_dir / daily_rel
    if daily_path.is_file():
        dtext = daily_path.read_text(encoding="utf-8")
        manifest["daily_rel"] = daily_rel
        manifest["daily_sha256"] = sha256_text(dtext)
        manifest["daily_content"] = dtext
    return manifest


def _restore_brain_manifest(cfg: InstanceConfig, manifest: dict[str, Any], max_index_lines: int) -> None:
    """依显式 brain_targets 清单校验并修补磁盘与 index.md 的任何缺失或字节篡改。"""
    hub_dir = cfg.vault / "hub"
    filed = list(manifest.get("filed") or [])
    for spec in filed:
        rel = str(spec.get("rel") or "").strip().lstrip("./")
        sha = str(spec.get("sha256") or "")
        content = str(spec.get("content") or "")
        if not rel:
            continue
        fpath = hub_dir / rel
        if sha and content and file_sha256(fpath) != sha:
            atomic_write(fpath, content, [cfg.vault])
    if filed:
        _ensure_brain_index_entries(cfg, filed, max_index_lines)

    daily_rel = str(manifest.get("daily_rel") or "")
    daily_sha = str(manifest.get("daily_sha256") or "")
    daily_content = str(manifest.get("daily_content") or "")
    if daily_rel and daily_sha and daily_content:
        dpath = hub_dir / daily_rel
        if file_sha256(dpath) != daily_sha:
            atomic_write(dpath, daily_content, [cfg.vault])


def _verify_and_repair_brain(
    cfg: InstanceConfig,
    closing: dict[str, Any],
    bcfg: brain_cfg.BrainConfig,
    as_of_date: FlexDate,
    as_of_str: str,
    brain_mode: str,
    out: dict[str, Any],
) -> None:
    """不信任 JSON 中的 brain_done 与单一游标：实盘核验全部第二大脑目标并修复缺漏。"""
    manifest = dict(closing.get("brain_targets") or {})
    if manifest:
        _restore_brain_manifest(cfg, manifest, bcfg.index_max_lines)

    if not (bcfg.enabled and bcfg.daily_enabled):
        if brain_mode == "daily":
            out["brain"] = apply_brain_step(
                cfg, as_of_date, records=None, dry_run=False, process_when_enabled=False
            )
        return

    hub_dir = cfg.vault / "hub"
    disk_refs = _scan_brain_disk_refs(cfg)
    eligible_ids, eligible_recs = _scan_eligible_brain_records(
        cfg, [str(x) for x in (closing.get("brain_record_ids") or []) if str(x)]
    )
    req_ex = [str(x) for x in (closing.get("brain_exchange_files") or []) if str(x)]

    disk_items_for_req: list[dict[str, str]] = []
    unbacked_ids: list[str] = []
    for lid in eligible_ids:
        hits = disk_refs.get(lid) or []
        rec = eligible_recs.get(lid)
        subtargets_ok = (
            brain_process.expected_targets_exist_for_record(hub_dir, rec, disk_refs)
            if rec is not None
            else bool(hits)
        )
        if not hits or not subtargets_ok:
            unbacked_ids.append(lid)
        if hits:
            disk_items_for_req.extend(hits)

    unbacked_ex: list[str] = []
    for ex_rel in req_ex:
        hits = disk_refs.get(ex_rel) or []
        if not hits:
            unbacked_ex.append(ex_rel)
        else:
            disk_items_for_req.extend(hits)

    if disk_items_for_req:
        _ensure_brain_index_entries(cfg, disk_items_for_req, bcfg.index_max_lines)

    b_state = load_brain_state(cfg)
    seen_ids = set(b_state.get("processed_inbox_ids") or [])
    seen_ex = set(b_state.get("processed_exchange_files") or [])
    evict_ids = [lid for lid in unbacked_ids if lid in seen_ids]
    evict_ex = [ex for ex in unbacked_ex if ex in seen_ex]
    if evict_ids or evict_ex:
        evict_id_set, evict_ex_set = set(evict_ids), set(evict_ex)
        b_state["processed_inbox_ids"] = [x for x in (b_state.get("processed_inbox_ids") or []) if x not in evict_id_set]
        b_state["processed_exchange_files"] = [x for x in (b_state.get("processed_exchange_files") or []) if x not in evict_ex_set]
        save_brain_state(cfg, b_state)
        seen_ids = set(b_state.get("processed_inbox_ids") or [])
        seen_ex = set(b_state.get("processed_exchange_files") or [])

    needs_brain = (
        any(lid not in seen_ids for lid in eligible_ids)
        or any(ex not in seen_ex for ex in req_ex)
        or bool(unbacked_ids)
        or bool(unbacked_ex)
    )
    filed_entries: list[dict[str, Any]] = []
    if needs_brain:
        if brain_mode == "daily":
            res = brain_routines.run_daily_brain(cfg, as_of=as_of_date, dry_run=False, _hold_lock=False)
            out["brain_daily"] = res
            filed_entries = list(res.get("filed") or [])
        else:
            res = apply_brain_step(cfg, as_of_date, records=None, dry_run=False, process_when_enabled=True)
            out["brain"] = res
            filed_entries = list(res.get("filed") or [])

    daily_path = hub_dir / "daily" / f"{as_of_str}.md"
    if brain_mode == "daily" and (eligible_ids or req_ex) and not daily_path.is_file():
        repair_filed = filed_entries or disk_items_for_req
        if repair_filed:
            brain_routines.write_daily_brain_note(cfg, as_of_str, repair_filed, dry_run=False)

    combined_filed = filed_entries or disk_items_for_req
    if combined_filed:
        new_manifest = _capture_brain_manifest(cfg, as_of_str, combined_filed)
        if not manifest:
            closing["brain_targets"] = new_manifest


def verify_closing_steps(cfg: InstanceConfig, ckpt: dict[str, Any]) -> dict[str, Any]:
    """核验收尾步（晨间简报 MD/HTML 刷新、第二大脑实盘目标、旧笔记唤醒、Git 提交）并补齐缺漏。"""
    closing = ckpt.setdefault("closing", {})
    as_of_str = str(ckpt.get("as_of") or "")
    as_of_date = FlexDate.from_any(as_of_str)
    out: dict[str, Any] = {"brief_refreshed": False, "git_committed": False}

    if closing.get("brief_required", True) and not _brief_matches_disk(cfg):
        out["brief_refreshed"] = brief.refresh(cfg)

    brain_mode = str(closing.get("brain_mode") or "")
    bcfg = brain_cfg.load_brain_config(cfg)
    if brain_mode in ("daily", "reconcile") and as_of_date is not None:
        _verify_and_repair_brain(cfg, closing, bcfg, as_of_date, as_of_str, brain_mode, out)

    if closing.get("resurface") and as_of_date is not None and bcfg.enabled and bcfg.resurface_enabled:
        b_state = load_brain_state(cfg)
        already = any(h.get("date") == as_of_str for h in (b_state.get("resurface_history") or []))
        if not already:
            out["brain_resurface"] = brain_routines.run_resurface(
                cfg, as_of=as_of_date, dry_run=False, _hold_lock=False
            )

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
