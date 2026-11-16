"""Second-brain 重复笔记安全合并与全库反向链接重连（从 brain_links.py 拆出）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hushai_agent.brain_docs import MD_LINK_RE, allocate_unique_path, relative_link
from hushai_agent.brain_index import (
    list_brain_markdown_files,
    parse_note_sections,
    read_index_first,
    render_index,
    resolve_hub_link,
)
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write, read_for_prompt
from hushai_agent.guard import ensure_writable


def _rewrite_incoming_links(cfg: InstanceConfig, primary_path: Path, dup_path: Path) -> int:
    """M-7 修复：合并笔记前将全库指向 duplicate 的反向链接改写为指向 primary。"""
    hub_dir = cfg.vault / "hub"
    dup_real = Path(os.path.realpath(os.fspath(dup_path)))
    primary_real = Path(os.path.realpath(os.fspath(primary_path)))
    updated_count = 0

    for md_path in list_brain_markdown_files(cfg, ("notes", "sources", "people", "projects")):
        md_real = Path(os.path.realpath(os.fspath(md_path)))
        if md_real == dup_real:
            continue
        txt = read_for_prompt(md_path, cfg.vault, cfg.local_only)
        changed = False

        def _repl(match: Any) -> str:
            nonlocal changed
            lbl, tgt = match.group(1), match.group(2)
            resolved = resolve_hub_link(hub_dir, md_path, tgt)
            if resolved is not None and Path(os.path.realpath(os.fspath(resolved))) == dup_real:
                changed = True
                if md_real == primary_real:
                    return lbl
                return f"[{lbl}]({relative_link(md_path, primary_path)})"
            return match.group(0)

        new_txt = MD_LINK_RE.sub(_repl, txt)
        if changed:
            atomic_write(md_path, new_txt, [cfg.vault])
            updated_count += 1
    return updated_count


def merge_duplicate_notes(
    cfg: InstanceConfig,
    primary_rel: str,
    duplicate_rel: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """合并两条重复笔记并更新全库入链。未经 confirm=True 绝不覆盖或删除任何笔记。"""
    hub_dir = cfg.vault / "hub"
    primary_path = hub_dir / primary_rel
    dup_path = hub_dir / duplicate_rel

    if not primary_path.is_file() or not dup_path.is_file():
        return {"merged": False, "reason": "file-missing", "primary": primary_rel, "duplicate": duplicate_rel}
    if not confirm:
        return {
            "merged": False,
            "requires_consent": True,
            "primary": primary_rel,
            "duplicate": duplicate_rel,
            "message": f"检测到疑似重复笔记 {duplicate_rel} -> {primary_rel}；需 --yes 确认后才合并。",
        }

    p_text = read_for_prompt(primary_path, cfg.vault, cfg.local_only)
    d_text = read_for_prompt(dup_path, cfg.vault, cfg.local_only)
    d_parsed = parse_note_sections(d_text)

    arch_path = allocate_unique_path(hub_dir / "reports" / "brain-archive", dup_path.stem)
    atomic_write(arch_path, d_text, [cfg.vault])

    p_lines = p_text.splitlines()
    existing_norm = {ln.strip() for ln in p_lines if ln.strip()}
    extra_facts = [
        ln.strip()
        for sec_name in ("关键事实", "三行摘要")
        for ln in d_parsed["sections"].get(sec_name, [])
        if ln.strip().startswith("- ") and ln.strip() not in existing_norm and not existing_norm.add(ln.strip())
    ]
    if extra_facts:
        p_lines.extend(["", f"## 合并补充（来自 {duplicate_rel}）", "", *extra_facts])
    if d_parsed["source"] and d_parsed["source"] not in p_text:
        p_lines.append(f"- 合并来源：{d_parsed['source']}")
    atomic_write(primary_path, "\n".join(p_lines).rstrip() + "\n", [cfg.vault])

    rewritten_files = _rewrite_incoming_links(cfg, primary_path, dup_path)
    _, entries = read_index_first(cfg)
    new_entries = [e for e in entries if e.target.strip().lstrip("./") != duplicate_rel]
    atomic_write(hub_dir / "index.md", render_index(new_entries), [cfg.vault])

    real_dup = ensure_writable(dup_path, [cfg.vault])
    if real_dup.is_file():
        os.unlink(real_dup)

    return {
        "merged": True,
        "primary": primary_rel,
        "duplicate": duplicate_rel,
        "archived_to": arch_path.relative_to(hub_dir).as_posix(),
        "added_facts": len(extra_facts),
        "rewritten_links": rewritten_files,
    }
