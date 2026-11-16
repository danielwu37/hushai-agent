"""Cortex 结构化档案解析、格式检查与条目模型（decisions.md / lessons.md）。"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

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
    chunks = [c.strip() for c in SPLIT_SUB_RE.split(body.strip()) if c.strip()]
    decision, reason, approver, positional = "", "-", default_approver or "user", []
    for chunk in chunks:
        if m_kv := re.match(r"^(?P<k>决定|理由|原因|批准人|批准|decision|reason|approver)\s*[：:]\s*(?P<v>.+)$", chunk, re.I):
            k, v = m_kv.group("k").lower(), m_kv.group("v").strip()
            if k in ("决定", "decision"):
                decision = v
            elif k in ("理由", "原因", "reason"):
                reason = v
            else:
                approver = v
        else:
            positional.append(chunk)
    if not decision and positional:
        decision = positional.pop(0)
    if reason == "-" and positional:
        reason = positional.pop(0)
    if approver == (default_approver or "user") and positional:
        approver = positional.pop(0)
    return DecisionEntry(date_str, _clean(decision, body.strip()), _clean(reason, "-"), _clean(approver, default_approver or "user"), _clean(line_id, "-"))


def parse_inbox_lesson(body: str, date_str: str, line_id: str) -> LessonEntry:
    chunks = [c.strip() for c in SPLIT_SUB_RE.split(body.strip()) if c.strip()]
    phenomenon, cause, defense, positional = "", "-", "-", []
    for chunk in chunks:
        if m_kv := re.match(r"^(?P<k>现象|问题|根因|原因|防线|措施|phenomenon|cause|defense)\s*[：:]\s*(?P<v>.+)$", chunk, re.I):
            k, v = m_kv.group("k").lower(), m_kv.group("v").strip()
            if k in ("现象", "问题", "phenomenon"):
                phenomenon = v
            elif k in ("根因", "原因", "cause"):
                cause = v
            else:
                defense = v
        else:
            positional.append(chunk)
    if not phenomenon and positional:
        phenomenon = positional.pop(0)
    if cause == "-" and positional:
        cause = positional.pop(0)
    if defense == "-" and positional:
        defense = positional.pop(0)
    return LessonEntry(date_str, _clean(phenomenon, body.strip()), _clean(cause, "-"), _clean(defense, "-"), _clean(line_id, ""))
