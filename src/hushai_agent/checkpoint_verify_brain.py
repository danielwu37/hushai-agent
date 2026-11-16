"""检查点恢复中的第二大脑实盘核验与缺失修补（从 checkpoint_verify.py 拆出）。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from hushai_agent import brain_cfg, brain_process, brain_routines, mechanical
from hushai_agent.brain_cfg import load_brain_state, save_brain_state
from hushai_agent.brain_model import (
    IndexEntry,
    extract_source_refs_from_md,
    parse_note_sections,
    read_index_first,
    upsert_index_entry,
)
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write
from hushai_agent.inboxfmt import ALL_RESERVED_DOMAINS
from hushai_agent.inboxscan import iter_inbox_lines
from hushai_agent.items import FlexDate

_SUBDIR_TOPIC = {"notes": "笔记", "sources": "来源", "people": "人物", "projects": "项目"}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


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
        if rec.domain in ALL_RESERVED_DOMAINS or mechanical.parse_action(rec.did) is not None:
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
    _, entries = read_index_first(cfg)
    idx_targets = {e.target.strip().lstrip("./") for e in entries}
    for item in items_to_index:
        rel = str(item.get("rel") or item.get("path") or "").strip().lstrip("./")
        if not rel or rel in idx_targets:
            continue
        sub = rel.split("/", 1)[0]
        title = str(item.get("title") or item.get("name") or Path(rel).stem)
        topic = str(item.get("topic") or _SUBDIR_TOPIC.get(sub, "笔记"))
        upsert_index_entry(cfg, IndexEntry(topic=topic, title=title, target=rel, brief=title[:50]), max_lines=max_lines, write=True)
        idx_targets.add(rel)


def _capture_brain_manifest(cfg: InstanceConfig, as_of_str: str, filed_entries: list[dict[str, Any]]) -> dict[str, Any]:
    hub_dir = cfg.vault / "hub"
    seen_rel: set[str] = set()
    filed_manifest: list[dict[str, Any]] = []
    for entry in filed_entries:
        rel = str(entry.get("path") or entry.get("rel") or "").strip().lstrip("./")
        if not rel or rel in seen_rel or not (fpath := hub_dir / rel).is_file():
            continue
        seen_rel.add(rel)
        text = fpath.read_text(encoding="utf-8")
        sub = rel.split("/", 1)[0]
        parsed = parse_note_sections(text)
        filed_manifest.append({
            "rel": rel, "sha256": _sha256_text(text), "content": text,
            "topic": str(entry.get("topic") or parsed.get("topic") or _SUBDIR_TOPIC.get(sub, "笔记")),
            "title": str(entry.get("title") or entry.get("name") or parsed.get("title") or Path(rel).stem),
        })

    manifest: dict[str, Any] = {"filed": filed_manifest}
    daily_rel = f"daily/{as_of_str}.md"
    daily_path = hub_dir / daily_rel
    if daily_path.is_file():
        dtext = daily_path.read_text(encoding="utf-8")
        manifest.update(daily_rel=daily_rel, daily_sha256=_sha256_text(dtext), daily_content=dtext)
    return manifest


def _restore_brain_manifest(cfg: InstanceConfig, manifest: dict[str, Any], max_index_lines: int) -> None:
    hub_dir = cfg.vault / "hub"
    filed = list(manifest.get("filed") or [])
    for spec in filed:
        rel, sha, content = str(spec.get("rel") or "").strip().lstrip("./"), str(spec.get("sha256") or ""), str(spec.get("content") or "")
        if rel and sha and content and _file_sha256(hub_dir / rel) != sha:
            atomic_write(hub_dir / rel, content, [cfg.vault])
    if filed:
        _ensure_brain_index_entries(cfg, filed, max_index_lines)

    daily_rel, daily_sha, daily_content = str(manifest.get("daily_rel") or ""), str(manifest.get("daily_sha256") or ""), str(manifest.get("daily_content") or "")
    if daily_rel and daily_sha and daily_content and _file_sha256(hub_dir / daily_rel) != daily_sha:
        atomic_write(hub_dir / daily_rel, daily_content, [cfg.vault])


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
    from hushai_agent import digest_apply

    manifest = dict(closing.get("brain_targets") or {})
    if manifest:
        _restore_brain_manifest(cfg, manifest, bcfg.index_max_lines)

    if not (bcfg.enabled and bcfg.daily_enabled):
        if brain_mode == "daily":
            out["brain"] = digest_apply.apply_brain_step(cfg, as_of_date, records=None, dry_run=False, process_when_enabled=False)
        return

    hub_dir = cfg.vault / "hub"
    disk_refs = _scan_brain_disk_refs(cfg)
    eligible_ids, eligible_recs = _scan_eligible_brain_records(cfg, [str(x) for x in (closing.get("brain_record_ids") or []) if str(x)])
    req_ex = [str(x) for x in (closing.get("brain_exchange_files") or []) if str(x)]

    disk_items_for_req, unbacked_ids = [], []
    for lid in eligible_ids:
        hits = disk_refs.get(lid) or []
        rec = eligible_recs.get(lid)
        subtargets_ok = brain_process.expected_targets_exist_for_record(hub_dir, rec, disk_refs) if rec is not None else bool(hits)
        if not hits or not subtargets_ok:
            unbacked_ids.append(lid)
        if hits:
            disk_items_for_req.extend(hits)

    unbacked_ex = []
    for ex_rel in req_ex:
        hits = disk_refs.get(ex_rel) or []
        (disk_items_for_req.extend(hits) if hits else unbacked_ex.append(ex_rel))

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
        seen_ids, seen_ex = set(b_state["processed_inbox_ids"]), set(b_state["processed_exchange_files"])

    needs_brain = any(lid not in seen_ids for lid in eligible_ids) or any(ex not in seen_ex for ex in req_ex) or bool(unbacked_ids) or bool(unbacked_ex)
    filed_entries: list[dict[str, Any]] = []
    if needs_brain:
        if brain_mode == "daily":
            res = brain_routines.run_daily_brain(cfg, as_of=as_of_date, dry_run=False, _hold_lock=False)
            out["brain_daily"] = res
            filed_entries = list(res.get("filed") or [])
        else:
            res = digest_apply.apply_brain_step(cfg, as_of_date, records=None, dry_run=False, process_when_enabled=True)
            out["brain"] = res
            filed_entries = list(res.get("filed") or [])

    if brain_mode == "daily" and (eligible_ids or req_ex) and not (hub_dir / "daily" / f"{as_of_str}.md").is_file():
        repair_filed = filed_entries or disk_items_for_req
        if repair_filed:
            brain_routines.write_daily_brain_note(cfg, as_of_str, repair_filed, dry_run=False)

    combined_filed = filed_entries or disk_items_for_req
    if combined_filed:
        new_manifest = _capture_brain_manifest(cfg, as_of_str, combined_filed)
        if not manifest:
            closing["brain_targets"] = new_manifest
