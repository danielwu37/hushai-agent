"""Cortex 结构化档案：决定（hub/decisions.md）与教训（hub/lessons.md）的机械对账追加与去重。"""

from __future__ import annotations

import json
import sys
from typing import Any

from hushai_agent.config import InstanceConfig
from hushai_agent.cortex_parse import (  # noqa: F401
    DECISION_HEADER,
    DECISION_PREFIX_RE,
    LESSON_HEADER,
    LESSON_PREFIX_RE,
    SPLIT_SUB_RE,
    DecisionEntry,
    LessonEntry,
    _clean,
    _is_ignorable,
    _strip_bullet,
    _valid_date,
    check_decisions_errors,
    check_lessons_errors,
    parse_decision_line,
    parse_inbox_decision,
    parse_inbox_lesson,
    parse_lesson_line,
)
from hushai_agent.fsio import append_line_with_header, atomic_write
from hushai_agent.inboxscan import InboxRecord
from hushai_agent.paths import get_instance_state_dir


def _load_seen_ids(cfg: InstanceConfig) -> tuple[set[str], set[str]]:
    dec_ids: set[str] = set()
    les_ids: set[str] = set()
    dec_file = cfg.vault / "hub" / "decisions.md"
    if dec_file.is_file():
        for raw in dec_file.read_text(encoding="utf-8").splitlines():
            if not _is_ignorable(raw):
                try:
                    dec_ids.add(parse_decision_line(raw).src)
                except ValueError:
                    continue
    les_file = cfg.vault / "hub" / "lessons.md"
    if les_file.is_file():
        for raw in les_file.read_text(encoding="utf-8").splitlines():
            if not _is_ignorable(raw):
                try:
                    if ent_src := parse_lesson_line(raw).src:
                        les_ids.add(ent_src)
                except ValueError:
                    continue
    state_file = get_instance_state_dir(cfg.name) / "cortex-ids.json"
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            dec_ids.update(data.get("decisions", []))
            les_ids.update(data.get("lessons", []))
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"hushai cortex-ids read warning: {exc}\n")
    return dec_ids, les_ids


def _save_seen_ids(cfg: InstanceConfig, dec_ids: set[str], les_ids: set[str]) -> None:
    state_dir = get_instance_state_dir(cfg.name)
    state_file = state_dir / "cortex-ids.json"
    payload = json.dumps({"decisions": sorted(dec_ids), "lessons": sorted(les_ids)}, ensure_ascii=False, indent=2)
    atomic_write(state_file, payload + "\n", [state_dir])


def ensure_entries_on_disk(
    cfg: InstanceConfig,
    decisions: list[dict[str, str]],
    lessons: list[dict[str, str]],
) -> None:
    """幂等确保清单中的决定与教训写入 decisions.md / lessons.md 及 cortex-ids.json。"""
    if not decisions and not lessons:
        return
    dec_ids, les_ids = _load_seen_ids(cfg)
    dec_file = cfg.vault / "hub" / "decisions.md"
    les_file = cfg.vault / "hub" / "lessons.md"
    existing_dec = {ln.strip() for ln in dec_file.read_text(encoding="utf-8").splitlines() if ln.strip()} if dec_file.is_file() else set()
    existing_les = {ln.strip() for ln in les_file.read_text(encoding="utf-8").splitlines() if ln.strip()} if les_file.is_file() else set()

    state_dirty = False
    for spec in decisions:
        line, src = str(spec.get("line") or "").strip(), str(spec.get("src") or "").strip()
        if line and line not in existing_dec:
            append_line_with_header(dec_file, DECISION_HEADER, line, [cfg.vault])
            existing_dec.add(line)
        if src and src not in dec_ids:
            dec_ids.add(src)
            state_dirty = True

    for spec in lessons:
        line, src = str(spec.get("line") or "").strip(), str(spec.get("src") or "").strip()
        if line and line not in existing_les:
            append_line_with_header(les_file, LESSON_HEADER, line, [cfg.vault])
            existing_les.add(line)
        if src and src not in les_ids:
            les_ids.add(src)
            state_dirty = True

    if state_dirty:
        _save_seen_ids(cfg, dec_ids, les_ids)


def apply_mech(cfg: InstanceConfig, mech: Any, write: bool = True) -> dict[str, Any]:
    """将机械对账分流出的决定与教训幂等追加到对应文件。"""
    from hushai_agent.mechanical import AppliedEntry

    cortex_actions: list[tuple[Any, InboxRecord]] = getattr(mech, "cortex_actions", [])
    if not cortex_actions:
        return {"decisions_added": 0, "lessons_added": 0, "planned_decisions": [], "planned_lessons": []}

    dec_ids, les_ids = _load_seen_ids(cfg)
    dec_added = les_added = 0
    planned_decisions, planned_lessons = [], []
    dec_file = cfg.vault / "hub" / "decisions.md"
    les_file = cfg.vault / "hub" / "lessons.md"

    for act, rec in cortex_actions:
        date_str = rec.date.isoformat() if hasattr(rec.date, "isoformat") else str(rec.date)
        if act.kind == "decision":
            formatted = parse_inbox_decision(act.note, date_str, rec.door, rec.line_id).format()
            planned_decisions.append({"src": rec.line_id, "line": formatted})
            if rec.line_id not in dec_ids:
                if write:
                    append_line_with_header(dec_file, DECISION_HEADER, formatted, [cfg.vault])
                dec_ids.add(rec.line_id)
                dec_added += 1
            mech.applied.append(AppliedEntry(action="decision", id=rec.line_id, line_id=rec.line_id, record=rec))
        elif act.kind == "lesson":
            formatted = parse_inbox_lesson(act.note, date_str, rec.line_id).format()
            planned_lessons.append({"src": rec.line_id, "line": formatted})
            if rec.line_id not in les_ids:
                if write:
                    append_line_with_header(les_file, LESSON_HEADER, formatted, [cfg.vault])
                les_ids.add(rec.line_id)
                les_added += 1
            mech.applied.append(AppliedEntry(action="lesson", id=rec.line_id, line_id=rec.line_id, record=rec))

    if write and (dec_added or les_added):
        _save_seen_ids(cfg, dec_ids, les_ids)
    return {
        "decisions_added": dec_added,
        "lessons_added": les_added,
        "planned_decisions": planned_decisions,
        "planned_lessons": planned_lessons,
    }
