"""Second-brain 收件加工流水线：分拣、拆长来源、增量补链与游标推进（防冷启动雪崩与领域越权）。"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Iterable

from hushai_agent import clock, ledger, lock
from hushai_agent.brain_cfg import BrainConfig, load_brain_config, load_brain_state, save_brain_state
from hushai_agent.brain_collect import MAX_EXCHANGE_FILE_BYTES, collect_unprocessed_for_brain  # noqa: F401
from hushai_agent.brain_items import (  # noqa: F401
    AUTHOR_RE,
    LONG_SPLIT_CHARS,
    MY_TAKE_RE,
    NOTE_PREFIX_RE,
    PERSON_AT_RE,
    PERSON_PREFIX_RE,
    PROJECT_PREFIX_RE,
    PROMISE_RE,
    QUOTE_RE,
    SOURCE_PREFIX_RE,
    URL_RE,
    _create_note_file,
    _create_source_file,
    _extract_title,
    _scan_existing_notes_and_sources,
    _split_long_chunks,
    _split_sentences,
    _upsert_person_file,
    _upsert_project_file,
)
from hushai_agent.brain_model import (
    UNVERIFIED,
    auto_link_notes,
    check_brain_links,
    find_duplicate_notes,
    merge_duplicate_notes,
    read_index_first,
    sanitize_block,
    sanitize_single_line,
    slugify,
)
from hushai_agent.config import InstanceConfig, is_local_only_domain
from hushai_agent.inboxscan import InboxRecord
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir


def _append_new_filed(filed: list[dict[str, Any]], res: dict[str, Any]) -> None:
    if not res.get("already_exists"):
        filed.append(res)


def process_raw_items(
    cfg: InstanceConfig,
    raw_items: Iterable[dict[str, str]],
    as_of_date: datetime.date | str,
    dry_run: bool = False,
    confirm_merge: bool = False,
) -> dict[str, Any]:
    """将一批原始输入加工为 second-brain 笔记，并对本批新增笔记做增量补链与去重检测。"""
    parsed_date = FlexDate.from_any(as_of_date) or clock.today(cfg)
    as_of_str = parsed_date.isoformat()
    bcfg: BrainConfig = load_brain_config(cfg)

    state_dir = get_instance_state_dir(cfg.name)
    hub_dir = (state_dir / "dry-run" / as_of_str / "hub") if dry_run else (cfg.vault / "hub")
    roots = [state_dir] if dry_run else [cfg.vault]

    _, idx_entries = read_index_first(cfg)
    indexed_targets = {e.target.strip().lstrip("./") for e in idx_entries}
    existing_notes, existing_sources = _scan_existing_notes_and_sources(hub_dir) if not dry_run else ({}, {})

    filed: list[dict[str, Any]] = []
    new_note_rels: set[str] = set()
    for item in raw_items:
        text = sanitize_block(item.get("text", ""))
        if not text:
            continue
        domain = sanitize_single_line(item.get("domain", ""), "未分类")
        if is_local_only_domain(domain, cfg):
            continue
        item_date = sanitize_single_line(item.get("date", ""), as_of_str)
        source_ref = sanitize_single_line(item.get("source_ref", ""), UNVERIFIED)
        my_take = item.get("my_take")

        if m_person := PERSON_PREFIX_RE.match(text):
            pname, pbody = m_person.group("name").strip(), (m_person.group("body") or text).strip()
            m_prom = PROMISE_RE.search(pbody)
            _append_new_filed(filed, _upsert_person_file(
                cfg, hub_dir, roots, pname, pbody, item_date,
                promise_text=m_prom.group(0).strip() if m_prom else None,
                max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                source_ref=source_ref, indexed_targets=indexed_targets,
            ))
            continue

        if m_proj := PROJECT_PREFIX_RE.match(text):
            _append_new_filed(filed, _upsert_project_file(
                cfg, hub_dir, roots, m_proj.group("name").strip(), (m_proj.group("body") or text).strip(), item_date,
                max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                source_ref=source_ref, indexed_targets=indexed_targets,
            ))
            continue

        m_url, m_src = URL_RE.search(text), SOURCE_PREFIX_RE.match(text)
        extra_links: list[tuple[str, str]] = []
        content_body = m_src.group("body").strip() if m_src else text

        if m_url or m_src:
            src_title = _extract_title(content_body, max_len=40)
            src_res = _create_source_file(
                cfg, hub_dir, roots, src_title, content_body, item_date, m_url.group(0) if m_url else "",
                source_ref, max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                existing_sources=existing_sources, indexed_targets=indexed_targets,
            )
            _append_new_filed(filed, src_res)
            extra_links.append((src_res["path"], f"提取自来源《{src_res.get('title') or src_title}》"))

        for pname in PERSON_AT_RE.findall(content_body):
            m_prom = PROMISE_RE.search(content_body)
            _append_new_filed(filed, _upsert_person_file(
                cfg, hub_dir, roots, pname, content_body, item_date,
                promise_text=m_prom.group(0).strip() if m_prom else None,
                max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                source_ref=source_ref, indexed_targets=indexed_targets,
            ))

        m_note = NOTE_PREFIX_RE.match(content_body)
        note_body = m_note.group("body").strip() if m_note else content_body
        used_titles: set[str] = set()
        for idx, chunk in enumerate(_split_long_chunks(note_body)):
            base_title = _extract_title(chunk)
            ntitle = f"{base_title}（节选 {idx + 1}）" if base_title in used_titles else base_title
            used_titles.add(base_title)
            res_n = _create_note_file(
                cfg, hub_dir, roots, ntitle, chunk, item_date, source_ref, domain,
                my_take=my_take, extra_links=extra_links, max_index_lines=bcfg.index_max_lines,
                dry_run=dry_run, existing_notes=existing_notes, indexed_targets=indexed_targets,
            )
            _append_new_filed(filed, res_n)
            if not res_n.get("already_exists"):
                new_note_rels.add(res_n["path"])

    if not dry_run and new_note_rels:
        link_res = auto_link_notes(cfg, min_links=bcfg.min_links, write=True, target_rels=new_note_rels)
        dup_candidates = find_duplicate_notes(cfg, target_rels=new_note_rels)
    else:
        link_res = {"auto_linked": [], "underlinked_notes": []}
        dup_candidates = []

    merged = []
    if confirm_merge and not dry_run:
        for dup in dup_candidates:
            m_res = merge_duplicate_notes(cfg, dup["primary"], dup["duplicate"], confirm=True)
            if m_res.get("merged"):
                merged.append(m_res)

    return {
        "date": as_of_str,
        "dry_run": dry_run,
        "filed": filed,
        "filed_count": len(filed),
        "auto_linked": link_res["auto_linked"],
        "underlinked_notes": link_res["underlinked_notes"],
        "duplicate_candidates": dup_candidates,
        "merged": merged,
    }


def expected_targets_exist_for_record(hub_dir: Path, rec: InboxRecord, disk_refs: dict[str, list[dict[str, str]]]) -> bool:
    hits = disk_refs.get(rec.line_id) or []
    if not hits:
        return False
    hit_rels = {info.get("rel") or info.get("path") or "" for info in hits}
    text = sanitize_block(rec.did)
    if rec.artifact and rec.artifact not in ("无", "none", "-"):
        text = f"{text}\n产物：{rec.artifact}"
    if rec.todo and rec.todo not in ("无", "none", "-"):
        text = f"{text}\n待办：{rec.todo}"
    if not text:
        return True
    if m_person := PERSON_PREFIX_RE.match(text):
        rel = f"people/{slugify(m_person.group('name').strip(), prefix='person')}.md"
        return rel in hit_rels and (hub_dir / rel).is_file()
    if m_proj := PROJECT_PREFIX_RE.match(text):
        rel = f"projects/{slugify(m_proj.group('name').strip(), prefix='project')}.md"
        return rel in hit_rels and (hub_dir / rel).is_file()
    m_url, m_src = URL_RE.search(text), SOURCE_PREFIX_RE.match(text)
    content_body = m_src.group("body").strip() if m_src else text
    if (m_url or m_src) and not any(r.startswith("sources/") and (hub_dir / r).is_file() for r in hit_rels):
        return False
    for pname in PERSON_AT_RE.findall(content_body):
        rel = f"people/{slugify(pname, prefix='person')}.md"
        if rel not in hit_rels or not (hub_dir / rel).is_file():
            return False
    return any(r.startswith("notes/") and (hub_dir / r).is_file() for r in hit_rels)


def process_inbox_to_brain(
    cfg: InstanceConfig,
    as_of_date: datetime.date | str | None = None,
    records: Iterable[InboxRecord] | None = None,
    dry_run: bool = False,
    confirm_merge: bool = False,
    _hold_lock: bool = True,
    max_items: int | None = None,
) -> dict[str, Any]:
    parsed_date = FlexDate.from_any(as_of_date) or clock.today(cfg) if as_of_date else clock.today(cfg)
    lock_ctx = None
    if _hold_lock and not dry_run:
        try:
            lock_ctx = lock.runner_lock(cfg, wait_seconds=lock.job_wait_seconds())
            lock_ctx.__enter__()
        except lock.LockBusy:
            ledger.record(cfg, "job", job="brain-process", date=parsed_date.isoformat(), dry_run=dry_run, result="failed", note="lock-busy")
            raise
    try:
        raw_items, new_ids, new_ex = collect_unprocessed_for_brain(cfg, parsed_date, records=records, max_items=max_items)
        result = process_raw_items(cfg, raw_items, as_of_date=parsed_date, dry_run=dry_run, confirm_merge=confirm_merge)
        if not dry_run and (new_ids or new_ex):
            state = load_brain_state(cfg)
            state["processed_inbox_ids"] = list(dict.fromkeys([*state.get("processed_inbox_ids", []), *new_ids]))
            state["processed_exchange_files"] = list(dict.fromkeys([*state.get("processed_exchange_files", []), *new_ex]))
            save_brain_state(cfg, state)
        result.update(processed_inbox_count=len(new_ids), processed_exchange_count=len(new_ex), processed_inbox_ids=list(new_ids), processed_exchange_files=list(new_ex))
        return result
    finally:
        if lock_ctx is not None:
            lock_ctx.__exit__(None, None, None)
            from hushai_agent.reconcile import drain_pending_mechanical

            drain_pending_mechanical(cfg)


def reconcile_brain_step(
    cfg: InstanceConfig,
    as_of_date: datetime.date | str,
    records: Iterable[InboxRecord] | None = None,
    dry_run: bool = False,
    process_when_enabled: bool = True,
) -> dict[str, Any]:
    """供 reconcile / digest_apply 调用的增量对账（跳过 O(n²) 全库去重扫描，由周/月维护统一巡检）。"""
    bcfg = load_brain_config(cfg)
    notes_dir = cfg.vault / "hub" / "notes"
    has_notes = notes_dir.is_dir() and any(p.suffix == ".md" for p in notes_dir.iterdir() if p.is_file())
    processed_info: dict[str, Any] = {"filed_count": 0, "auto_linked": [], "filed": []}
    if process_when_enabled and bcfg.enabled and bcfg.daily_enabled:
        processed_info = process_inbox_to_brain(
            cfg, as_of_date=as_of_date, records=records, dry_run=dry_run, confirm_merge=False, _hold_lock=False
        )
    if not has_notes and processed_info.get("filed_count", 0) == 0:
        return {"filed_count": 0, "filed": [], "auto_linked": [], "broken_links": [], "underlinked_notes": [], "duplicate_candidates": []}
    link_health = check_brain_links(cfg, min_links=bcfg.min_links)
    return {
        "filed_count": processed_info.get("filed_count", 0),
        "filed": processed_info.get("filed", []),
        "auto_linked": processed_info.get("auto_linked", []),
        "broken_links": link_health["broken_links"],
        "underlinked_notes": link_health["underlinked_notes"],
        "duplicate_candidates": [],
    }
