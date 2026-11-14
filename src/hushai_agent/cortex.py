"""Cortex 结构化档案：决定（hub/decisions.md）与教训（hub/lessons.md）的解析、校验与机械对账追加。"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import append_line_with_header, atomic_write
from hushai_agent.inboxscan import InboxRecord
from hushai_agent.paths import get_instance_state_dir

DECISION_HEADER = "# 决定记录（decisions.md）"
LESSON_HEADER = "# 教训记录（lessons.md）"

DECISION_PREFIX_RE = re.compile(r"^(?:决定\s*[：:]\s*|decision\s*:\s*)(?P<body>.+)$", re.IGNORECASE)
LESSON_PREFIX_RE = re.compile(r"^(?:教训\s*[：:]\s*|lesson\s*:\s*)(?P<body>.+)$", re.IGNORECASE)
SPLIT_SUB_RE = re.compile(r"\s*(?:\||；|;)\s*")


@dataclass(frozen=True)
class DecisionEntry:
    """单条决定记录：日期｜决定｜理由｜批准人｜来源收件行编号。"""

    date: str
    decision: str
    reason: str
    approver: str
    src: str

    def format(self) -> str:
        return f"- {self.date}｜{self.decision}｜{self.reason}｜{self.approver}｜{self.src}"


@dataclass(frozen=True)
class LessonEntry:
    """单条教训记录：日期｜现象｜根因｜防线。"""

    date: str
    phenomenon: str
    cause: str
    defense: str
    src: str = ""

    def format(self) -> str:
        return f"- {self.date}｜{self.phenomenon}｜{self.cause}｜{self.defense}"


def _clean(val: str, default: str = "-") -> str:
    cleaned = str(val).replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
    return cleaned or default


def _is_ignorable(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith(("#", ">")) or stripped.startswith("（")


def _strip_bullet(seg: str) -> str:
    s = seg.strip()
    return s[2:].strip() if s.startswith("- ") else s


def _valid_date(date_str: str) -> bool:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return False
    try:
        datetime.date.fromisoformat(date_str)
        return True
    except ValueError:
        return False


def parse_decision_line(line: str) -> DecisionEntry:
    """解析并校验单行决定记录（5 段：日期｜决定｜理由｜批准人｜来源收件行编号）。"""
    parts = [p.strip() for p in line.strip().split("｜")]
    if len(parts) != 5:
        raise ValueError(f"decisions.md 行必须以‘｜’分为 5 段（当前 {len(parts)} 段）")
    date_str = _strip_bullet(parts[0])
    if not _valid_date(date_str):
        raise ValueError(f"非法日期: {date_str}")
    decision, reason, approver, src = parts[1], parts[2], parts[3], parts[4]
    if not decision or not reason or not approver or not src:
        raise ValueError("decisions.md 各段均不能为空")
    norm_src = src[4:].strip() if src.startswith("src=") else src
    return DecisionEntry(date=date_str, decision=decision, reason=reason, approver=approver, src=norm_src)


def parse_lesson_line(line: str) -> LessonEntry:
    """解析并校验单行教训记录（4 段：日期｜现象｜根因｜防线，兼容可选第 5 段来源编号）。"""
    parts = [p.strip() for p in line.strip().split("｜")]
    if len(parts) not in (4, 5):
        raise ValueError(f"lessons.md 行必须以‘｜’分为 4 段（当前 {len(parts)} 段）")
    date_str = _strip_bullet(parts[0])
    if not _valid_date(date_str):
        raise ValueError(f"非法日期: {date_str}")
    phenomenon, cause, defense = parts[1], parts[2], parts[3]
    if not phenomenon or not cause or not defense:
        raise ValueError("lessons.md 各段均不能为空")
    raw_src = parts[4] if len(parts) == 5 else ""
    norm_src = raw_src[4:].strip() if raw_src.startswith("src=") else raw_src
    return LessonEntry(date=date_str, phenomenon=phenomenon, cause=cause, defense=defense, src=norm_src)


def check_decisions_errors(vault_path: Path) -> list[str]:
    """检查 hub/decisions.md 中所有记录行的格式错误。"""
    fpath = vault_path / "hub" / "decisions.md"
    if not fpath.is_file():
        return ["hub/decisions.md 不存在"]
    errors: list[str] = []
    for lineno, raw in enumerate(fpath.read_text(encoding="utf-8").splitlines(), start=1):
        if _is_ignorable(raw):
            continue
        try:
            parse_decision_line(raw)
        except ValueError as exc:
            errors.append(f"decisions.md:{lineno}: {exc}")
    return errors


def check_lessons_errors(vault_path: Path) -> list[str]:
    """检查 hub/lessons.md 中所有记录行的格式错误。"""
    fpath = vault_path / "hub" / "lessons.md"
    if not fpath.is_file():
        return ["hub/lessons.md 不存在"]
    errors: list[str] = []
    for lineno, raw in enumerate(fpath.read_text(encoding="utf-8").splitlines(), start=1):
        if _is_ignorable(raw):
            continue
        try:
            parse_lesson_line(raw)
        except ValueError as exc:
            errors.append(f"lessons.md:{lineno}: {exc}")
    return errors


def parse_inbox_decision(body: str, date_str: str, default_approver: str, line_id: str) -> DecisionEntry:
    """从收件行‘决定：…’正文提取字段（支持 理由：/批准人： 键值或 | / ； 分段）。"""
    chunks = [c.strip() for c in SPLIT_SUB_RE.split(body.strip()) if c.strip()]
    decision = ""
    reason = "-"
    approver = default_approver or "user"
    positional: list[str] = []
    for chunk in chunks:
        m_kv = re.match(r"^(?P<k>决定|理由|原因|批准人|批准|decision|reason|approver)\s*[：:]\s*(?P<v>.+)$", chunk, re.I)
        if m_kv:
            k = m_kv.group("k").lower()
            v = m_kv.group("v").strip()
            if k in ("决定", "decision"):
                decision = v
            elif k in ("理由", "原因", "reason"):
                reason = v
            elif k in ("批准人", "批准", "approver"):
                approver = v
        else:
            positional.append(chunk)
    if not decision and positional:
        decision = positional.pop(0)
    if reason == "-" and positional:
        reason = positional.pop(0)
    if approver == (default_approver or "user") and positional:
        approver = positional.pop(0)
    return DecisionEntry(
        date=date_str,
        decision=_clean(decision, body.strip()),
        reason=_clean(reason, "-"),
        approver=_clean(approver, default_approver or "user"),
        src=_clean(line_id, "-"),
    )


def parse_inbox_lesson(body: str, date_str: str, line_id: str) -> LessonEntry:
    """从收件行‘教训：…’正文提取字段（支持 现象：/根因：/防线： 键值或 | / ； 分段）。"""
    chunks = [c.strip() for c in SPLIT_SUB_RE.split(body.strip()) if c.strip()]
    phenomenon = ""
    cause = "-"
    defense = "-"
    positional: list[str] = []
    for chunk in chunks:
        m_kv = re.match(
            r"^(?P<k>现象|问题|根因|原因|防线|措施|phenomenon|cause|defense)\s*[：:]\s*(?P<v>.+)$",
            chunk,
            re.I,
        )
        if m_kv:
            k = m_kv.group("k").lower()
            v = m_kv.group("v").strip()
            if k in ("现象", "问题", "phenomenon"):
                phenomenon = v
            elif k in ("根因", "原因", "cause"):
                cause = v
            elif k in ("防线", "措施", "defense"):
                defense = v
        else:
            positional.append(chunk)
    if not phenomenon and positional:
        phenomenon = positional.pop(0)
    if cause == "-" and positional:
        cause = positional.pop(0)
    if defense == "-" and positional:
        defense = positional.pop(0)
    return LessonEntry(
        date=date_str,
        phenomenon=_clean(phenomenon, body.strip()),
        cause=_clean(cause, "-"),
        defense=_clean(defense, "-"),
        src=_clean(line_id, ""),
    )


def _load_seen_ids(cfg: InstanceConfig) -> tuple[set[str], set[str]]:
    """读取已写入 decisions.md 与 lessons.md 的收件行编号集合。"""
    dec_ids: set[str] = set()
    les_ids: set[str] = set()
    dec_file = cfg.vault / "hub" / "decisions.md"
    if dec_file.is_file():
        for raw in dec_file.read_text(encoding="utf-8").splitlines():
            if not _is_ignorable(raw):
                try:
                    dec_ids.add(parse_decision_line(raw).src)
                except ValueError:
                    pass
    les_file = cfg.vault / "hub" / "lessons.md"
    if les_file.is_file():
        for raw in les_file.read_text(encoding="utf-8").splitlines():
            if not _is_ignorable(raw):
                try:
                    ent = parse_lesson_line(raw)
                    if ent.src:
                        les_ids.add(ent.src)
                except ValueError:
                    pass
    state_file = get_instance_state_dir(cfg.name) / "cortex-ids.json"
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            dec_ids.update(data.get("decisions", []))
            les_ids.update(data.get("lessons", []))
        except (OSError, json.JSONDecodeError):
            pass
    return dec_ids, les_ids


def _save_seen_ids(cfg: InstanceConfig, dec_ids: set[str], les_ids: set[str]) -> None:
    state_dir = get_instance_state_dir(cfg.name)
    state_file = state_dir / "cortex-ids.json"
    payload = json.dumps(
        {"decisions": sorted(dec_ids), "lessons": sorted(les_ids)},
        ensure_ascii=False,
        indent=2,
    )
    atomic_write(state_file, payload + "\n", [state_dir])


def apply_mech(cfg: InstanceConfig, mech: Any, write: bool = True) -> dict[str, int]:
    """将机械对账分流出的决定与教训幂等追加到对应文件（按收件行编号去重）。"""
    from hushai_agent.mechanical import AppliedEntry

    cortex_actions: list[tuple[Any, InboxRecord]] = getattr(mech, "cortex_actions", [])
    if not cortex_actions:
        return {"decisions_added": 0, "lessons_added": 0}

    dec_ids, les_ids = _load_seen_ids(cfg)
    dec_added = les_added = 0
    dec_file = cfg.vault / "hub" / "decisions.md"
    les_file = cfg.vault / "hub" / "lessons.md"

    for act, rec in cortex_actions:
        date_str = rec.date.isoformat() if hasattr(rec.date, "isoformat") else str(rec.date)
        if act.kind == "decision":
            if rec.line_id not in dec_ids:
                entry_obj = parse_inbox_decision(act.note, date_str, rec.door, rec.line_id)
                if write:
                    append_line_with_header(dec_file, DECISION_HEADER, entry_obj.format(), [cfg.vault])
                dec_ids.add(rec.line_id)
                dec_added += 1
            mech.applied.append(
                AppliedEntry(action="decision", id=rec.line_id, line_id=rec.line_id, record=rec)
            )
        elif act.kind == "lesson":
            if rec.line_id not in les_ids:
                entry_obj = parse_inbox_lesson(act.note, date_str, rec.line_id)
                if write:
                    append_line_with_header(les_file, LESSON_HEADER, entry_obj.format(), [cfg.vault])
                les_ids.add(rec.line_id)
                les_added += 1
            mech.applied.append(
                AppliedEntry(action="lesson", id=rec.line_id, line_id=rec.line_id, record=rec)
            )

    if write and (dec_added or les_added):
        _save_seen_ids(cfg, dec_ids, les_ids)
    return {"decisions_added": dec_added, "lessons_added": les_added}
