"""开放项编号分配、归档分流、渲染与磁盘读写存储。"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Iterable

from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import append_line, atomic_write, read_for_prompt
from hushai_agent.items import (
    ARCHIVE_DAYS,
    HEADER_QUOTE,
    ITEM_ID_RE,
    FlexDate,
    ItemFormatError,
    OpenItem,
    format_item_line,
    parse_item_line,
    parse_items,
)


def allocate_id(
    items: Iterable[OpenItem | str],
    archived_ids: Iterable[str | OpenItem],
    date: datetime.date | str,
) -> str:
    """在给定日期前缀 YYMMDD 下分配下一个编号 YYMMDD-N（综合当前开放项与归档项，永不复用）。"""
    if isinstance(date, datetime.date):
        prefix = date.strftime("%y%m%d")
    elif isinstance(date, str) and re.match(r"^\d{6}$", date.strip()):
        prefix = date.strip()
    elif isinstance(date, str) and re.match(r"^\d{8}$", date.strip()):
        prefix = date.strip()[2:]
    else:
        d_obj = FlexDate.from_any(date)
        if d_obj is None:
            raise ValueError(f"无法从日期分配编号: {date!r}")
        prefix = d_obj.strftime("%y%m%d")

    max_seq = 0
    for pool in (items, archived_ids):
        for entry in pool:
            raw_id = entry.id if isinstance(entry, OpenItem) else str(entry).strip()
            match = ITEM_ID_RE.match(raw_id)
            if match and match.group("ymd") == prefix:
                max_seq = max(max_seq, int(match.group("seq")))
    return f"{prefix}-{max_seq + 1}"


def split_archive(
    items: Iterable[OpenItem],
    as_of: datetime.date | str,
) -> tuple[list[OpenItem], list[OpenItem]]:
    """将关闭超过 14 天的条目分流至 archive，其余保留在 keep。"""
    ref_date = FlexDate.from_any(as_of)
    if ref_date is None:
        raise ValueError(f"非法基准日期: {as_of!r}")

    keep: list[OpenItem] = []
    archive: list[OpenItem] = []
    for item in items:
        if (
            item.status in ("done", "dropped")
            and item.closed is not None
            and (ref_date - item.closed).days > ARCHIVE_DAYS
        ):
            archive.append(item)
        else:
            keep.append(item)
    return keep, archive


def render_open_items(items: Iterable[OpenItem], as_of: datetime.date | str) -> str:
    """将开放项按四节（要我办 / 要我定 / AI 推进 / 近期关闭）渲染为 open-items.md 文本。"""
    keep, _ = split_archive(items, as_of)
    sec_user = [format_item_line(i) for i in keep if i.status == "open" and i.owner == "user"]
    sec_dec = [
        format_item_line(i) for i in keep if i.status == "open" and i.owner == "user_decision"
    ]
    sec_ai = [format_item_line(i) for i in keep if i.status == "open" and i.owner == "ai"]
    sec_closed = [format_item_line(i) for i in keep if i.status in ("done", "dropped")]

    blocks = [
        HEADER_QUOTE,
        "",
        "## 要我办",
        *sec_user,
        "",
        "## 要我定",
        *sec_dec,
        "",
        "## AI 推进",
        *sec_ai,
        "",
        "## 近期关闭",
        *sec_closed,
        "",
    ]
    return "\n".join(blocks)


def _resolve_vault(cfg: InstanceConfig | Path | str) -> Path:
    return cfg.vault if isinstance(cfg, InstanceConfig) else Path(cfg)


def load(cfg: InstanceConfig | Path | str) -> list[OpenItem]:
    """从 <vault>/hub/open-items.md 读取并解析开放项列表。"""
    vault = _resolve_vault(cfg)
    path = vault / "hub" / "open-items.md"
    if not path.is_file():
        return []
    local_only = cfg.local_only if isinstance(cfg, InstanceConfig) else ()
    text = read_for_prompt(path, vault, local_only)
    return parse_items(text)


def load_archived_ids(cfg: InstanceConfig | Path | str) -> set[str]:
    """从归档文件 <vault>/hub/reports/closed-items.md 读取所有已归档编号集合。"""
    vault = _resolve_vault(cfg)
    path = vault / "hub" / "reports" / "closed-items.md"
    if not path.is_file():
        return set()
    local_only = cfg.local_only if isinstance(cfg, InstanceConfig) else ()
    text = read_for_prompt(path, vault, local_only)
    return {item.id for item in parse_items(text)}


def _quarantine_unparsed(vault: Path, open_path: Path) -> None:
    """重写前把 open-items.md 里解析不了的条目行原样存进 reports/quarantine.md，不让它们随重写消失。"""
    if not open_path.is_file():
        return
    bad: list[str] = []
    for raw in open_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("- ["):
            continue
        try:
            parse_item_line(line)
        except ItemFormatError:
            bad.append(line)
    if not bad:
        return
    q_path = vault / "hub" / "reports" / "quarantine.md"
    have = set(q_path.read_text(encoding="utf-8").splitlines()) if q_path.is_file() else set()
    for line in bad:
        if line not in have:
            append_line(q_path, line, [vault])


def save(
    cfg: InstanceConfig | Path | str,
    items: Iterable[OpenItem],
    as_of: datetime.date | str,
) -> tuple[list[OpenItem], list[OpenItem]]:
    """分流保存：先追加归档 closed-items.md，再重写 open-items.md。

    顺序不能反：中途崩溃时宁可同一条在两边各有一份（归档按编号去重），也不能两边都没有。
    """
    vault = _resolve_vault(cfg)
    keep, archive = split_archive(items, as_of)
    open_path = vault / "hub" / "open-items.md"
    if archive:
        archive_path = vault / "hub" / "reports" / "closed-items.md"
        archived = load_archived_ids(vault)
        for item in archive:
            if item.id not in archived:
                append_line(archive_path, format_item_line(item), [vault])
    _quarantine_unparsed(vault, open_path)
    atomic_write(open_path, render_open_items(keep, as_of), [vault])
    return keep, archive
