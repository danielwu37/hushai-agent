"""Second-brain 旧笔记唤醒（Resurface）与月度图谱维护（合并重复、修断链、删减冗余索引）。"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path
from typing import Any

from hushai_agent import ledger
from hushai_agent.brain_ask import _extract_best_quote
from hushai_agent.brain_cfg import load_brain_state, save_brain_state
from hushai_agent.brain_model import (
    IndexEntry,
    extract_md_links,
    find_duplicate_notes,
    list_brain_markdown_files,
    merge_duplicate_notes,
    parse_note_sections,
    read_index_first,
    relative_link,
    render_index,
    resolve_hub_link,
    tokenize_text,
)
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write, read_for_prompt
from hushai_agent.guard import GuardError
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir


def _note_date(path: Path, parsed: dict[str, Any], fallback_date: datetime.date) -> datetime.date:
    raw_d = str(parsed.get("date") or "").strip()
    if m := re.match(r"^(\d{4}-\d{2}-\d{2})", raw_d):
        try:
            return datetime.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    try:
        return datetime.date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return fallback_date


def _run_resurface_inner(
    cfg: InstanceConfig,
    bcfg: Any,
    as_of_date: FlexDate,
    as_of_str: str,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    state = load_brain_state(cfg)
    history: list[dict[str, str]] = list(state.get("resurface_history", []))
    if not force and any(h.get("date") == as_of_str for h in history):
        existing = next(h for h in history if h.get("date") == as_of_str)
        return {"skipped": "already-resurfaced-today", "date": as_of_str, "picked": existing}

    read_index_first(cfg)
    ctx_parts: list[str] = []
    today_file = cfg.vault / "hub" / "today.md"
    if today_file.is_file():
        try:
            ctx_parts.append(read_for_prompt(today_file, cfg.vault, cfg.local_only))
        except GuardError:
            pass
    for p in list_brain_markdown_files(cfg, ("projects",)):
        ctx_parts.append(read_for_prompt(p, cfg.vault, cfg.local_only))
    if daily_files := list_brain_markdown_files(cfg, ("daily",)):
        ctx_parts.append(read_for_prompt(daily_files[-1], cfg.vault, cfg.local_only))

    ctx_tokens = tokenize_text("\n".join(ctx_parts))
    recent_picked = {h.get("path", "") for h in history[-14:]}
    candidates: list[tuple[float, int, str, str, str]] = []
    for p in list_brain_markdown_files(cfg, ("notes", "sources")):
        rel_vault = p.relative_to(cfg.vault).as_posix()
        txt = read_for_prompt(p, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(txt)
        age_days = (as_of_date - _note_date(p, parsed, as_of_date)).days
        if age_days < bcfg.resurface_min_age_days or (rel_vault in recent_picked and not force):
            continue
        ntoks = tokenize_text(f"{parsed['title']}\n{txt}")
        shared = len(ctx_tokens & ntoks) if ctx_tokens else 0
        title = parsed["title"] or p.stem
        quote, _ = _extract_best_quote(txt, ctx_tokens or ntoks)
        candidates.append((shared + min(age_days, 30) * 0.05, age_days, rel_vault, title, quote or title))

    if not candidates:
        return {"date": as_of_str, "found": False, "message": "暂无符合条件的旧笔记可唤醒"}

    candidates.sort(key=lambda x: (-x[0], -x[1], x[2]))
    _, age_days, rel_vault, title, quote = candidates[0]
    picked = {"date": as_of_str, "path": rel_vault, "title": title, "age_days": age_days, "quote": quote}
    if not dry_run:
        history.append({"date": as_of_str, "path": rel_vault, "title": title})
        state["resurface_history"] = history[-60:]
        save_brain_state(cfg, state)
        ledger.record(cfg, "job", job="brain-resurface", date=as_of_str, result="ok", note=rel_vault)
    return {
        "date": as_of_str,
        "found": True,
        "picked": picked,
        "output_text": f"今日旧笔记唤醒（{age_days} 天前）：[{title}]({rel_vault})\n> \"{quote}\"",
    }


def _find_dups_compat(cfg: InstanceConfig) -> list[dict[str, Any]]:
    mod = sys.modules.get("hushai_agent.brain_routines")
    fn = getattr(mod, "find_duplicate_notes", None) if mod is not None else None
    return (fn or find_duplicate_notes)(cfg)


def _run_monthly_inner(
    cfg: InstanceConfig,
    bcfg: Any,
    as_of_date: FlexDate,
    as_of_str: str,
    ym_str: str,
    confirm_merge: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    hub_dir = cfg.vault / "hub"
    _, existing_index = read_index_first(cfg)
    changes: list[str] = []

    dups = _find_dups_compat(cfg)
    merged_list, pending_merges = [], []
    for dup in dups:
        if confirm_merge and not dry_run:
            m_res = merge_duplicate_notes(cfg, dup["primary"], dup["duplicate"], confirm=True)
            if m_res.get("merged"):
                merged_list.append(m_res)
                changes.append(f"合并重复笔记：{dup['duplicate']} -> {dup['primary']}（原件归档至 {m_res['archived_to']}）")
        else:
            pending_merges.append(dup)
            changes.append(f"发现重复笔记待确认（需 --yes 才合并）：{dup['duplicate']} ≈ {dup['primary']} (相似度 {dup['similarity']})")

    all_docs = list_brain_markdown_files(cfg, ("notes", "sources", "people", "projects"))
    by_stem: dict[str, Path] = {}
    for p in all_docs:
        by_stem.setdefault(p.stem.lower(), p)

    incoming_count: dict[str, int] = {p.relative_to(hub_dir).as_posix(): 0 for p in all_docs}
    fixed_links, unfixed_links = [], []
    for p in all_docs:
        rel = p.relative_to(hub_dir).as_posix()
        txt = read_for_prompt(p, cfg.vault, cfg.local_only)
        new_txt = txt
        for _, target in extract_md_links(txt):
            resolved = resolve_hub_link(hub_dir, p, target)
            if resolved is not None and resolved.exists():
                r_rel = resolved.relative_to(hub_dir).as_posix()
                if r_rel != rel and r_rel in incoming_count:
                    incoming_count[r_rel] += 1
            else:
                cand_fix = by_stem.get(Path(target).stem.lower())
                if cand_fix is not None and cand_fix != p:
                    new_href = relative_link(p, cand_fix)
                    new_txt = new_txt.replace(f"({target})", f"({new_href})")
                    fixed_links.append({"file": rel, "old": target, "new": new_href})
                    r_rel = cand_fix.relative_to(hub_dir).as_posix()
                    incoming_count[r_rel] = incoming_count.get(r_rel, 0) + 1
                else:
                    unfixed_links.append({"file": rel, "target": target})
        if new_txt != txt:
            if confirm_merge and not dry_run:
                atomic_write(p, new_txt, [cfg.vault])
                changes.append(f"修复断链：{rel} 内 {len([x for x in fixed_links if x['file'] == rel])} 处")
            else:
                changes.append(f"发现可修复断链（加 --yes 自动重写）：{rel}")
    for uf in unfixed_links:
        changes.append(f"发现无法自动修复的断链：{uf['file']} -> {uf['target']}")

    stale_days_cutoff = bcfg.stale_months * 30
    unlinked_6m: list[dict[str, Any]] = []
    for p in list_brain_markdown_files(cfg, ("notes",)):
        rel = p.relative_to(hub_dir).as_posix()
        if incoming_count.get(rel, 0) > 0:
            continue
        parsed = parse_note_sections(read_for_prompt(p, cfg.vault, cfg.local_only))
        age = (as_of_date - _note_date(p, parsed, as_of_date)).days
        if age >= stale_days_cutoff:
            unlinked_6m.append({"file": rel, "title": parsed["title"] or p.stem, "age_days": age})
            changes.append(f"标记 {bcfg.stale_months} 个月以上无人链接笔记：{rel}（已 {age} 天）")

    cleaned_index, seen_targets, removed_idx, added_idx = [], set(), 0, 0
    for e in existing_index:
        norm_t = e.target.strip().lstrip("./")
        if (hub_dir / norm_t).is_file():
            if norm_t not in seen_targets:
                cleaned_index.append(IndexEntry(e.topic, e.title, norm_t, e.brief))
                seen_targets.add(norm_t)
        else:
            removed_idx += 1
    topic_map = {"notes": None, "sources": "来源", "people": "人物", "projects": "项目"}
    for p in all_docs:
        rel = p.relative_to(hub_dir).as_posix()
        if rel not in seen_targets:
            parsed = parse_note_sections(read_for_prompt(p, cfg.vault, cfg.local_only))
            top_dir = rel.split("/", 1)[0]
            cleaned_index.append(IndexEntry(topic_map[top_dir] or parsed["topic"] or "笔记", parsed["title"] or p.stem, rel))
            seen_targets.add(rel)
            added_idx += 1
    if removed_idx or added_idx:
        changes.append(f"清理 index.md：移除 {removed_idx} 条失效索引，补充 {added_idx} 条未登记笔记")
    new_index_text = render_index(cleaned_index, max_lines=bcfg.index_max_lines)

    report_lines = [
        f"# 每月第二大脑维护报告 {ym_str}", "", f"- 维护日期：{as_of_str}",
        f"- 索引保留行数：{len(new_index_text.splitlines())} / {bcfg.index_max_lines}", "",
        "## 本次改动与检查清单", "", *([f"- {c}" for c in changes] or ["- （索引与链接均健康，无需要改动项）"]), "",
    ]
    report_text = "\n".join(report_lines)
    state_dir = get_instance_state_dir(cfg.name)
    if dry_run:
        dry_hub = state_dir / "dry-run" / as_of_str / "hub"
        atomic_write(dry_hub / "index.md", new_index_text, [state_dir])
        atomic_write(dry_hub / "reports" / f"brain-monthly-{ym_str}.md", report_text, [state_dir])
    else:
        atomic_write(hub_dir / "index.md", new_index_text, [cfg.vault])
        atomic_write(hub_dir / "reports" / f"brain-monthly-{ym_str}.md", report_text, [cfg.vault])
        state = load_brain_state(cfg)
        state["monthly_last_run"] = ym_str
        save_brain_state(cfg, state)
        ledger.record(cfg, "job", job="brain-monthly", date=as_of_str, result="ok", note=f"changes={len(changes)}")
    return {
        "date": as_of_str, "dry_run": dry_run, "merged": merged_list, "pending_merges": pending_merges,
        "fixed_links": fixed_links, "unfixed_links": unfixed_links, "unlinked_6m": unlinked_6m,
        "index_removed": removed_idx, "index_added": added_idx, "changes": changes, "output_text": report_text,
    }
