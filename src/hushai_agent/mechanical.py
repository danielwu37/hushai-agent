"""收件箱机械对账：解析完成/推迟/放弃/更正动作并更新开放项状态。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from hushai_agent.inboxfmt import RESERVED_DOMAINS_EN, RESERVED_DOMAINS_ZH
from hushai_agent.inboxscan import InboxRecord
from hushai_agent.items import FlexDate, ItemFormatError, OpenItem

DONE_RE = re.compile(
    r"^(?:完成\s*|done\s+)(?P<id>\d{6}-\d+)(?:(?:\s*[：:]\s*|\s+)(?P<note>.*))?$",
    re.IGNORECASE,
)
DROP_RE = re.compile(
    r"^(?:放弃\s*|drop\s+)(?P<id>\d{6}-\d+)(?:(?:\s*[：:]\s*|\s+)(?P<note>.*))?$",
    re.IGNORECASE,
)
DEFER_RE = re.compile(
    r"^(?:推迟\s*|defer\s+)(?P<id>\d{6}-\d+)\s*(?:到|to)\s*(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:(?:\s*[：:]\s*|\s+)(?P<note>.*))?$",
    re.IGNORECASE,
)
CORRECT_RE = re.compile(
    r"^(?:更正\s*|correct\s+)(?P<target>(?:\d{6}-\d+|L-[A-Za-z0-9_-]+))"
    r"(?:\s*[：:]\s*|\s+)(?P<note>.+)$",
    re.IGNORECASE,
)

CORRECTION_DOMAINS = frozenset({RESERVED_DOMAINS_ZH[1], RESERVED_DOMAINS_EN[1]})
MEMORY_DOMAINS = frozenset({RESERVED_DOMAINS_ZH[0], RESERVED_DOMAINS_EN[0]})


@dataclass(frozen=True)
class Action:
    """解析出的单条机械对账动作。"""

    kind: str  # "done" | "defer" | "drop" | "correct"
    target: str
    date: FlexDate | None = None
    note: str = ""

    @property
    def action(self) -> str:
        return self.kind

    @property
    def id(self) -> str:
        return self.target

    @property
    def item_id(self) -> str:
        return self.target

    @property
    def to_date(self) -> FlexDate | None:
        return self.date

    @property
    def text(self) -> str:
        return self.note


@dataclass
class AppliedEntry:
    """机械动作应用记录（兼容属性访问、字典下标、元组拆包及编号/line_id 相等比较）。"""

    action: str
    id: str
    line_id: str
    date: FlexDate | None = None
    record: InboxRecord | None = None

    @property
    def kind(self) -> str:
        return self.action

    @property
    def item_id(self) -> str:
        return self.id

    @property
    def target(self) -> str:
        return self.id

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return (self.action, self.id, self.line_id)[key]
        return getattr(self, key)

    def __iter__(self):
        return iter((self.action, self.id, self.line_id))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AppliedEntry):
            return (self.action, self.id, self.line_id) == (
                other.action,
                other.id,
                other.line_id,
            )
        if isinstance(other, str):
            return other in (self.id, self.line_id, self.action)
        if isinstance(other, InboxRecord):
            return self.record == other or self.line_id == other.line_id
        if isinstance(other, tuple):
            base = (self.action, self.id, self.line_id)
            return base[: len(other)] == other
        if isinstance(other, dict):
            return all(getattr(self, k, None) == v for k, v in other.items())
        return False


class _IdSet(set):
    """兼容与 list/tuple/set 直接做相等比较的集合。"""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple, set, frozenset)):
            return set(self) == set(other)
        return super().__eq__(other)


class _RecordList(list):
    """支持通过 InboxRecord 或 line_id 字符串进行 in 成员判断的列表。"""

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return any(
                getattr(x, "line_id", None) == item or getattr(x, "id", None) == item
                for x in self
            ) or super().__contains__(item)
        return super().__contains__(item)


@dataclass
class Outcome:
    """机械对账汇总结果。"""

    applied: list[AppliedEntry] = field(default_factory=_RecordList)
    unmatched: list[AppliedEntry] = field(default_factory=_RecordList)
    corrections: list[InboxRecord] = field(default_factory=_RecordList)
    candidates: list[InboxRecord] = field(default_factory=_RecordList)
    mechanical_ids: set[str] = field(default_factory=_IdSet)
    rest: list[InboxRecord] = field(default_factory=_RecordList)
    items: list[OpenItem] = field(default_factory=list)


def parse_action(did: str) -> Action | None:
    """识别‘做了什么’开头的机械动作（完成/推迟/放弃/更正）；非机械动作或非法推迟返回 None。"""
    if not did or not did.strip():
        return None
    text = did.strip()

    m_done = DONE_RE.match(text)
    if m_done:
        return Action(
            kind="done",
            target=m_done.group("id"),
            note=(m_done.group("note") or "").strip(),
        )

    m_drop = DROP_RE.match(text)
    if m_drop:
        return Action(
            kind="drop",
            target=m_drop.group("id"),
            note=(m_drop.group("note") or "").strip(),
        )

    m_defer = DEFER_RE.match(text)
    if m_defer:
        try:
            target_date = FlexDate.from_any(m_defer.group("date"))
        except ItemFormatError:
            return None
        if target_date is None:
            return None
        return Action(
            kind="defer",
            target=m_defer.group("id"),
            date=target_date,
            note=(m_defer.group("note") or "").strip(),
        )

    m_corr = CORRECT_RE.match(text)
    if m_corr:
        note = (m_corr.group("note") or "").strip()
        if not note:
            return None
        return Action(
            kind="correct",
            target=m_corr.group("target"),
            note=note,
        )

    return None


def apply_actions(
    items: list[OpenItem],
    records: Iterable[InboxRecord],
) -> Outcome:
    """按顺序将收件记录中的机械动作应用到开放项列表，并分流更正、候选记忆与剩余记录。"""
    outcome = Outcome(items=items)
    item_map: dict[str, OpenItem] = {item.id: item for item in items}

    for rec in records:
        act = parse_action(rec.did)
        if act is not None:
            outcome.mechanical_ids.add(rec.line_id)
            if act.kind == "correct":
                outcome.corrections.append(rec)
                continue

            entry = AppliedEntry(
                action=act.kind,
                id=act.target,
                line_id=rec.line_id,
                date=act.date,
                record=rec,
            )
            target_item = item_map.get(act.target)
            if target_item is None or target_item.status != "open":
                outcome.unmatched.append(entry)
                continue

            rec_date = FlexDate.from_any(rec.date)
            if act.kind == "done":
                target_item.status = "done"
                target_item.closed = rec_date
                target_item.verified = rec_date
                setattr(target_item, "_closed_this_round", True)
            elif act.kind == "drop":
                target_item.status = "dropped"
                target_item.closed = rec_date
                target_item.verified = rec_date
                setattr(target_item, "_closed_this_round", True)
            elif act.kind == "defer":
                target_item.sched = act.date
                target_item.verified = rec_date
                target_item.defers += 1

            if rec.line_id not in target_item.src:
                target_item.src.append(rec.line_id)
            outcome.applied.append(entry)
            continue

        if rec.domain in CORRECTION_DOMAINS:
            outcome.corrections.append(rec)
        elif rec.domain in MEMORY_DOMAINS:
            outcome.candidates.append(rec)
        else:
            outcome.rest.append(rec)

    return outcome
