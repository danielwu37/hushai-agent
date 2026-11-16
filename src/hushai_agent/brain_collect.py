"""Second-brain 待加工收件与交换区增量收集（从 brain_process.py 拆出）。"""

from __future__ import annotations

import datetime
from typing import Iterable

from hushai_agent import mechanical
from hushai_agent.brain_cfg import load_brain_state
from hushai_agent.config import InstanceConfig, is_local_only_domain
from hushai_agent.fsio import read_for_prompt
from hushai_agent.guard import GuardError
from hushai_agent.inboxfmt import ALL_RESERVED_DOMAINS
from hushai_agent.inboxscan import InboxRecord, iter_inbox_lines
from hushai_agent.items import FlexDate

MAX_EXCHANGE_FILE_BYTES = 64 * 1024


def collect_unprocessed_for_brain(
    cfg: InstanceConfig,
    as_of_date: datetime.date,
    records: Iterable[InboxRecord] | None = None,
    max_items: int | None = None,
) -> tuple[list[dict[str, str]], list[str], list[str]]:
    """收集尚未加工的非机械收件与 exchange/in/ 顶层非历史文本；遵循 local_only_domains 与门户过滤。"""
    state = load_brain_state(cfg)
    seen_ids = set(state.get("processed_inbox_ids", []))
    seen_ex = set(state.get("processed_exchange_files", []))

    if records is None:
        all_recs = iter_inbox_lines(cfg.vault / "hub" / "inbox")
        cutoff = as_of_date - datetime.timedelta(days=cfg.window_days)
        cand_recs = [r for r in all_recs if (d := FlexDate.from_any(r.date)) is not None and cutoff <= d <= as_of_date]
    else:
        cand_recs = list(records)

    raw_items: list[dict[str, str]] = []
    new_inbox_ids: list[str] = []
    for rec in cand_recs:
        if max_items is not None and len(raw_items) >= max_items:
            break
        if rec.line_id in seen_ids or rec.domain in ALL_RESERVED_DOMAINS or is_local_only_domain(rec.domain, cfg):
            continue
        if rec.auto or rec.door == "heal" or rec.raw.lstrip().startswith("- 系统："):
            continue
        if mechanical.parse_action(rec.did) is not None:
            continue
        try:
            read_for_prompt(rec.path, cfg.vault, cfg.local_only)
        except GuardError:
            continue
        text = rec.did
        if rec.artifact and rec.artifact not in ("无", "none", "-"):
            text = f"{text}\n产物：{rec.artifact}"
        if rec.todo and rec.todo not in ("无", "none", "-"):
            text = f"{text}\n待办：{rec.todo}"
        raw_items.append({"text": text, "domain": rec.domain, "date": rec.date, "source_ref": rec.line_id})
        new_inbox_ids.append(rec.line_id)

    ex_in = cfg.vault / "hub" / "exchange" / "in"
    new_ex_files: list[str] = []
    if ex_in.is_dir():
        for fpath in sorted(ex_in.iterdir()):
            if max_items is not None and len(raw_items) >= max_items:
                break
            if not fpath.is_file() or fpath.name.startswith(".") or fpath.suffix.lower() not in (".md", ".txt"):
                continue
            if "_已处理" in fpath.name or "legacy" in fpath.name.lower():
                continue
            rel = fpath.relative_to(cfg.vault).as_posix()
            if rel in seen_ex:
                continue
            try:
                if fpath.stat().st_size > MAX_EXCHANGE_FILE_BYTES:
                    continue
                content = read_for_prompt(fpath, cfg.vault, cfg.local_only)
            except (GuardError, OSError, UnicodeDecodeError):
                continue
            if content.strip():
                raw_items.append({"text": content, "domain": "资料", "date": as_of_date.isoformat(), "source_ref": rel})
                new_ex_files.append(rel)

    return raw_items, new_inbox_ids, new_ex_files
