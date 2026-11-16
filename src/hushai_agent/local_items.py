"""本机隔离领域（[privacy] local_only_domains）的待办：不进模型，由代码直接落成开放项。"""

from __future__ import annotations

from typing import Iterable

from hushai_agent import items
from hushai_agent.config import InstanceConfig, is_local_only_domain
from hushai_agent.inboxscan import InboxRecord
from hushai_agent.items import FlexDate, OpenItem

EMPTY_TODO = frozenset({"", "无", "none", "-"})


def create_local_only_items(
    cfg: InstanceConfig,
    all_items: list[OpenItem],
    archived_ids: set[str],
    rest: Iterable[InboxRecord],
    as_of_date: FlexDate,
) -> list[OpenItem]:
    """隔离领域里带「待办」的非机械行各建一条开放项（标题取待办原文，来源行已建过则跳过），返回新建项。"""
    known_src = {s for it in all_items for s in it.src}
    created: list[OpenItem] = []
    for rec in rest:
        todo = (rec.todo or "").strip()
        if not is_local_only_domain(rec.domain, cfg) or todo.lower() in EMPTY_TODO or rec.line_id in known_src:
            continue
        item = OpenItem(
            id=items.allocate_id(all_items, archived_ids, as_of_date),
            title=todo,
            owner="user",
            domain=rec.domain,
            verified=as_of_date,
            src=[rec.line_id],
        )
        all_items.append(item)
        known_src.add(rec.line_id)
        created.append(item)
    return created
