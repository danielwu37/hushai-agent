"""收件收集步：按时间窗口、已对账游标与领域隔离规则筛选待处理的收件记录。"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Iterable

from hushai_agent import cursor
from hushai_agent.config import DEFAULT_WINDOW_DAYS, InstanceConfig, is_local_only_domain
from hushai_agent.fsio import read_for_prompt
from hushai_agent.inboxscan import InboxRecord, iter_inbox_lines
from hushai_agent.items import FlexDate


def exclude_local_only_domains(
    cfg: InstanceConfig,
    records: Iterable[InboxRecord],
) -> list[InboxRecord]:
    """剔除属于 [privacy] local_only_domains 的收件行，避免送入外部模型。"""
    return [r for r in records if not is_local_only_domain(r.domain, cfg)]


def collect(
    cfg: InstanceConfig,
    as_of: datetime.date | str,
    *,
    include_local_domains: bool = False,
) -> list[InboxRecord]:
    """收集文件日期在 [as_of - window_days, as_of] 内且未进入游标的合法收件行。

    默认排除 [privacy] local_only_domains 下的行；机械对账调用可传 include_local_domains=True，
    之后再剔除本地领域，确保只走本地机械对账而绝不送外模型。
    """
    as_of_date = FlexDate.from_any(as_of)
    if as_of_date is None:
        raise ValueError(f"非法基准日期 as_of: {as_of!r}")

    window_days = getattr(cfg, "window_days", DEFAULT_WINDOW_DAYS)
    start_date = as_of_date - datetime.timedelta(days=window_days)
    digested_ids = cursor.load_ids(cfg)
    inbox_dir = cfg.vault / "hub" / "inbox"

    checked_files: set[Path] = set()
    records: list[InboxRecord] = []

    for rec in iter_inbox_lines(inbox_dir):
        rec_date = FlexDate.from_any(rec.date)
        if rec_date is None:
            continue
        if rec_date < start_date or rec_date > as_of_date:
            continue
        if rec.line_id in digested_ids:
            continue
        if not include_local_domains and is_local_only_domain(rec.domain, cfg):
            continue
        if rec.path not in checked_files:
            read_for_prompt(rec.path, cfg.vault, cfg.local_only)
            checked_files.add(rec.path)
        records.append(rec)

    return records
