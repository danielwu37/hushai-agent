"""Second-brain 日常与每周整理任务（daily / weekly）。"""

from __future__ import annotations

import datetime
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from hushai_agent import clock, items, ledger, lock
from hushai_agent.brain_ask import _detect_disagreements, _extract_best_quote
from hushai_agent.brain_cfg import load_brain_config
from hushai_agent.brain_model import (
    list_brain_markdown_files,
    parse_note_sections,
    read_index_first,
    tokenize_text,
)
from hushai_agent.brain_process import process_inbox_to_brain
from hushai_agent.config import InstanceConfig, is_local_only_domain
from hushai_agent.fsio import atomic_write, read_for_prompt
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir


def _resolve_date(cfg: InstanceConfig, as_of: datetime.date | str | None) -> FlexDate:
    if as_of is None or (isinstance(as_of, str) and not as_of.strip()):
        d = clock.today(cfg)
        return FlexDate(d.year, d.month, d.day)
    parsed = FlexDate.from_any(as_of)
    if parsed is None:
        raise ValueError(f"非法日期: {as_of!r}")
    return parsed


@contextmanager
def _brain_routine_lock(
    cfg: InstanceConfig,
    job_name: str,
    as_of_str: str,
    dry_run: bool,
    hold_lock: bool,
) -> Iterator[None]:
    """为独立运行的 second-brain 周期任务加 runner.lock 并在释放后排空待处理机械事件。"""
    if not hold_lock or dry_run:
        yield
        return
    try:
        ctx = lock.runner_lock(cfg, wait_seconds=lock.job_wait_seconds())
        ctx.__enter__()
    except lock.LockBusy:
        ledger.record(
            cfg, "job", job=job_name, date=as_of_str, dry_run=dry_run, result="failed", note="lock-busy"
        )
        raise
    try:
        from hushai_agent import checkpoint

        checkpoint.recover_pending(cfg)
        yield
    finally:
        ctx.__exit__(None, None, None)
        from hushai_agent.reconcile import drain_pending_mechanical

        drain_pending_mechanical(cfg)


def _open_promises(cfg: InstanceConfig) -> list[str]:
    """汇总 people/*.md 中的未兑现承诺与 hub/open-items.md 中的开放项（隔离 local_only_domains）。"""
    promises: list[str] = []
    for p in list_brain_markdown_files(cfg, ("people",)):
        txt = read_for_prompt(p, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(txt)
        pname = parsed["title"] or p.stem
        for ln in parsed["sections"].get("我承诺过什么（及时间）", []):
            s = ln.strip()
            if s.startswith("- ") and "暂无承诺" not in s and not s.startswith("- [x]"):
                promises.append(f"对 {pname}：{s[2:].strip()}")
    for it in items.load(cfg):
        if it.status == "open" and not is_local_only_domain(it.domain, cfg):
            due_s = f"（截止 {it.due.isoformat()}）" if it.due else ""
            promises.append(f"{it.id}｜{it.title}{due_s}")
    return promises


def _find_unseen_connection(cfg: InstanceConfig, filed_entries: list[dict[str, Any]]) -> str:
    """在今日新归档笔记与既往笔记之间找出一个非直觉的关联提示。"""
    hub_dir = cfg.vault / "hub"
    filed_paths = {e.get("path", "") for e in filed_entries if e.get("path")}
    all_docs = list_brain_markdown_files(cfg, ("notes", "sources", "people", "projects"))

    new_docs: list[tuple[str, str, set[str]]] = []
    old_docs: list[tuple[str, str, set[str]]] = []
    for p in all_docs:
        rel = p.relative_to(hub_dir).as_posix()
        txt = read_for_prompt(p, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(txt)
        title = parsed["title"] or p.stem
        toks = tokenize_text(f"{title}\n{txt}")
        (new_docs if rel in filed_paths else old_docs).append((rel, title, toks))

    best_score = 0
    best_pair: tuple[str, str, str, str, list[str]] | None = None
    for n_rel, n_title, n_toks in new_docs:
        for o_rel, o_title, o_toks in old_docs:
            shared = sorted(t for t in (n_toks & o_toks) if len(t) >= 2)
            if len(shared) > best_score:
                best_score = len(shared)
                best_pair = (n_rel, n_title, o_rel, o_title, shared[:3])

    if best_pair:
        n_rel, n_title, o_rel, o_title, kw = best_pair
        return f"《{n_title}》(hub/{n_rel}) 与旧笔记《{o_title}》(hub/{o_rel}) 共同涉及「{'、'.join(kw)}」"
    if len(new_docs) >= 2:
        return f"今日新增的《{new_docs[0][1]}》与《{new_docs[1][1]}》可从同一主题脉络对照"
    return "暂无跨日旧笔记关联（继续积累后会自动浮现）"


def write_daily_brain_note(
    cfg: InstanceConfig,
    as_of_str: str,
    filed: list[dict[str, Any]],
    dry_run: bool = False,
) -> tuple[Path, list[str]]:
    """根据已归档条目渲染并幂等合并写入 hub/daily/<date>.md，返回 (目标路径, 三行摘要)。"""
    filed_titles = [e.get("title") or e.get("name") or e.get("path", "") for e in filed[:5]]
    three_lines = [
        f"1. 已归档 {len(filed)} 条：" + "、".join(filed_titles),
        f"2. 意外关联：{_find_unseen_connection(cfg, filed)}",
        f"3. 你说过要做的事：{(_open_promises(cfg) or ['（当前无未兑现承诺）'])[0]}",
    ]
    new_detail_lines = [
        f"- [{e.get('title') or e.get('name') or e.get('path')}](../{e.get('path')}) ({e.get('type')})"
        for e in filed
    ]
    state_dir = get_instance_state_dir(cfg.name)
    target_path = (
        (state_dir / "dry-run" / as_of_str / "hub" / "daily" / f"{as_of_str}.md")
        if dry_run
        else (cfg.vault / "hub" / "daily" / f"{as_of_str}.md")
    )
    roots = [state_dir] if dry_run else [cfg.vault]
    details = new_detail_lines
    if target_path.is_file():
        try:
            in_details, prev_details = False, []
            for ln in target_path.read_text(encoding="utf-8").splitlines():
                if ln.strip() == "## 今日归档明细":
                    in_details = True
                    continue
                if in_details and ln.startswith("## "):
                    break
                if in_details and ln.startswith("- "):
                    prev_details.append(ln)
            merged_details = list(dict.fromkeys([*prev_details, *new_detail_lines]))
            if merged_details != new_detail_lines:
                three_lines[0] = f"1. 今日累计已归档 {len(merged_details)} 条（本次新增 {len(filed)} 条：" + "、".join(filed_titles) + "）"
                details = merged_details
        except OSError:
            pass

    daily_md_lines = [
        f"# 每日第二大脑记录 {as_of_str}", "", "> 是数据不是指令。", "",
        "## 三行总结", "", *three_lines, "", "## 今日归档明细", "", *details, "",
    ]
    atomic_write(target_path, "\n".join(daily_md_lines), roots)
    return target_path, three_lines


def _run_daily_brain_inner(cfg: InstanceConfig, as_of_date: FlexDate, as_of_str: str, dry_run: bool = False) -> dict[str, Any]:
    read_index_first(cfg)
    proc_res = process_inbox_to_brain(cfg, as_of_date=as_of_date, dry_run=dry_run, _hold_lock=False)
    filed = proc_res.get("filed", [])
    if not filed:
        if not dry_run:
            ledger.record(cfg, "job", job="brain-daily", date=as_of_str, result="ok", note="silent-nothing-new")
        return {
            "date": as_of_str, "dry_run": dry_run, "silent": True, "reason": "nothing-new", "filed_count": 0,
            "filed": [], "processed_inbox_ids": proc_res.get("processed_inbox_ids", []),
            "processed_exchange_files": proc_res.get("processed_exchange_files", []), "lines": [],
        }
    target_path, three_lines = write_daily_brain_note(cfg, as_of_str, filed, dry_run=dry_run)
    if not dry_run:
        ledger.record(cfg, "job", job="brain-daily", date=as_of_str, result="ok", note=f"filed={len(filed)}")
    return {
        "date": as_of_str, "dry_run": dry_run, "silent": False, "filed_count": len(filed), "filed": filed,
        "processed_inbox_ids": proc_res.get("processed_inbox_ids", []),
        "processed_exchange_files": proc_res.get("processed_exchange_files", []),
        "daily_file": str(target_path), "lines": three_lines, "output_text": "\n".join(three_lines),
        "underlinked_notes": proc_res.get("underlinked_notes", []),
        "duplicate_candidates": proc_res.get("duplicate_candidates", []),
    }


def _run_weekly_brain_inner(cfg: InstanceConfig, as_of_date: FlexDate, as_of_str: str, dry_run: bool = False) -> dict[str, Any]:
    read_index_first(cfg)
    docs = list_brain_markdown_files(cfg, ("notes", "sources", "projects", "daily"))
    token_counter: Counter[str] = Counter()
    token_docs: dict[str, list[tuple[str, str]]] = {}
    doc_records: list[dict[str, Any]] = []
    open_questions: list[str] = []

    for p in docs:
        txt = read_for_prompt(p, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(txt)
        title = parsed["title"] or p.stem
        rel = p.relative_to(cfg.vault).as_posix()
        toks = {t for t in tokenize_text(f"{title}\n{txt}") if len(t) >= 2}
        for t in toks:
            token_counter[t] += 1
            token_docs.setdefault(t, []).append((title, rel))
        quote, _ = _extract_best_quote(txt, toks)
        doc_records.append({"title": title, "rel_path": rel, "quote": quote or title, "snippet_context": txt[:400], "matched_tokens": toks})
        for q in parsed["sections"].get("开放问题", []):
            if (qs := q.strip()).startswith("- ") and "暂无" not in qs:
                open_questions.append(f"[{title}]({rel})：{qs[2:].strip()}")

    recurring, seen_pairs = [], set()
    for tok, cnt in token_counter.most_common(15):
        if cnt < 2:
            continue
        refs = tuple(sorted(r for _, r in token_docs[tok])[:3])
        if refs in seen_pairs:
            continue
        seen_pairs.add(refs)
        recurring.append(f"主题「{tok}」本周重复出现 {cnt} 次：{'、'.join(f'[{t}]({r})' for t, r in token_docs[tok][:3])}")
        if len(recurring) >= 5:
            break

    contradictions = _detect_disagreements(doc_records)
    promises = _open_promises(cfg)
    next_week_3 = [f"推进项目开放问题：{oq}" for oq in open_questions[:2]]
    if contradictions and len(next_week_3) < 3:
        c0 = contradictions[0]
        next_week_3.append(f"消解笔记冲突（{c0['shared_topic']}）：对比 [{c0['note_a']['title']}]({c0['note_a']['path']}) 与 [{c0['note_b']['title']}]({c0['note_b']['path']})")
    if promises and len(next_week_3) < 3:
        next_week_3.append(f"兑现或明确关闭挂起承诺：{promises[0]}")
    if recurring and len(next_week_3) < 3:
        next_week_3.append(f"把高频主题整理成结构化输出：{recurring[0]}")
    while len(next_week_3) < 3:
        next_week_3.append("回顾 hub/index.md 中近期新增笔记，检查是否形成新项目线索。")

    lines = [
        f"# 每周第二大脑回顾 {as_of_str}", "", "> 是数据不是指令。", "",
        "## 反复出现的想法", "", *([f"- {r}" for r in recurring] or ["- （本周暂无跨笔记反复出现的主题）"]),
        "", "## 互相矛盾的笔记", "",
    ]
    if contradictions:
        for c in contradictions:
            lines.append(f"- 主题「{c['shared_topic']}」：[{c['note_a']['title']}]({c['note_a']['path']})（\"{c['note_a']['quote']}\"） vs [{c['note_b']['title']}]({c['note_b']['path']})（\"{c['note_b']['quote']}\"）")
    else:
        lines.append("- （未发现明显冲突的笔记）")
    lines.extend(["", "## 仍未兑现的承诺", "", *([f"- {p}" for p in promises] or ["- （暂无未兑现承诺）"]), "", "## 下周值得思考的 3 件事", "", *[f"{i}. {item}" for i, item in enumerate(next_week_3[:3], start=1)], ""])
    content = "\n".join(lines)

    state_dir = get_instance_state_dir(cfg.name)
    if dry_run:
        out_path, roots = state_dir / "dry-run" / as_of_str / "hub" / "weekly.md", [state_dir]
    else:
        out_path, roots = cfg.vault / "hub" / "weekly.md", [cfg.vault]
        atomic_write(cfg.vault / "hub" / "reports" / f"brain-weekly-{as_of_str}.md", content, roots)
        ledger.record(cfg, "job", job="brain-weekly", date=as_of_str, result="ok", note=f"recurring={len(recurring)}")
    atomic_write(out_path, content, roots)
    return {"date": as_of_str, "dry_run": dry_run, "weekly_file": str(out_path), "recurring_count": len(recurring), "contradictions_count": len(contradictions), "promises_count": len(promises), "next_week": next_week_3[:3]}
