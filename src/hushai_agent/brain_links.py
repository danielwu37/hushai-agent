"""Second-brain 词元相似度、断链体检、增量自动补链与重复笔记安全合并（含反链更新）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hushai_agent.brain_docs import UNVERIFIED, extract_md_links, relative_link, slugify
from hushai_agent.brain_index import list_brain_markdown_files, parse_note_sections, resolve_hub_link
from hushai_agent.brain_merge import merge_duplicate_notes  # noqa: F401
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write, read_for_prompt

STOP_CJK = frozenset("的了在是我有和与这那个人们也都要会去来把被让给对从到及或并而但又很更最吗呢吧啊么多什怎哪谁")
STOP_EN = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "from", "are", "was", "were",
        "have", "has", "had", "not", "but", "you", "your", "about", "what", "when",
        "where", "which", "who", "how", "why", "into", "onto", "over", "under",
        "unverified", "verified", "true", "false", "null", "none", "https", "http",
    }
)


def tokenize_text(text: str) -> set[str]:
    """提取中英文检索/相似度词元（英文词元 + CJK 二元组与实词单字）。"""
    raw = str(text or "").lower()
    raw = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", raw)
    raw = re.sub(r"\bl-\d{8}-[a-z0-9_-]+\b", " ", raw)
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9][a-z0-9_-]*", raw):
        if len(word) >= 2 and word not in STOP_EN:
            tokens.add(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", raw):
        filtered = [ch for ch in run if ch not in STOP_CJK]
        if len(filtered) == 1:
            tokens.add(filtered[0])
        for i in range(len(filtered)):
            tokens.add(filtered[i])
            if i + 1 < len(filtered):
                tokens.add(filtered[i] + filtered[i + 1])
    return tokens


def similarity_score(text_a: str, text_b: str) -> float:
    """计算两段文本的综合相似度（Jaccard 与重合率均值，0.0~1.0）。"""
    ta, tb = tokenize_text(text_a), tokenize_text(text_b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if inter == 0:
        return 0.0
    return 0.65 * (inter / len(ta | tb)) + 0.35 * (inter / min(len(ta), len(tb)))


def extract_semantic_body(text: str) -> str:
    """提取笔记正文语义部分（剔除顶部元数据、关联笔记章节与 UNVERIFIED 占位）。"""
    parsed = parse_note_sections(text)
    parts: list[str] = [parsed["title"]] if parsed["title"] else []
    for sec_name, lines in parsed["sections"].items():
        if sec_name in ("_head", "关联笔记"):
            continue
        for ln in lines:
            s = ln.strip()
            if s and UNVERIFIED not in s and "（待补关联链接" not in s:
                parts.append(s)
    return "\n".join(parts)


def check_brain_links(cfg: InstanceConfig, min_links: int = 2) -> dict[str, Any]:
    """检查 hub/notes 等笔记的断链情况与少于 min_links 条链接的笔记。"""
    hub_dir = cfg.vault / "hub"
    note_files = list_brain_markdown_files(cfg, ("notes",))
    all_files = list_brain_markdown_files(cfg, ("notes", "sources", "people", "projects"))
    broken, underlinked = [], []

    for path in all_files:
        text = read_for_prompt(path, cfg.vault, cfg.local_only)
        rel = path.relative_to(hub_dir).as_posix()
        valid_targets: set[str] = set()
        for _, target in extract_md_links(text):
            resolved = resolve_hub_link(hub_dir, path, target)
            if resolved is None or not resolved.exists():
                broken.append({"file": rel, "target": target})
            elif (res_rel := resolved.relative_to(hub_dir).as_posix()) != rel:
                valid_targets.add(res_rel)
        if path in note_files and len(valid_targets) < min_links:
            underlinked.append({"file": rel, "valid_links": len(valid_targets), "required": min_links})

    return {"notes_count": len(note_files), "total_docs": len(all_files), "broken_links": broken, "underlinked_notes": underlinked}


def auto_link_notes(
    cfg: InstanceConfig,
    min_links: int = 2,
    write: bool = True,
    target_rels: set[str] | None = None,
) -> dict[str, Any]:
    """为关联链接不足 min_links 的 notes/*.md 补全同主题链接；target_rels 仅扫描指定新增笔记以避开 O(n²) 重写。"""
    hub_dir = cfg.vault / "hub"
    note_files = list_brain_markdown_files(cfg, ("notes",))
    pool_files = list_brain_markdown_files(cfg, ("notes", "sources", "people", "projects"))
    pool_info: list[tuple[Path, str, str, set[str]]] = []
    for p in pool_files:
        txt = read_for_prompt(p, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(txt)
        pool_info.append((p, p.relative_to(hub_dir).as_posix(), parsed["title"] or p.stem, tokenize_text(extract_semantic_body(txt))))

    auto_linked: list[dict[str, Any]] = []
    still_underlinked: list[dict[str, Any]] = []

    for note_path in note_files:
        rel = note_path.relative_to(hub_dir).as_posix()
        if target_rels is not None and rel not in target_rels:
            continue
        txt = read_for_prompt(note_path, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(txt)
        existing_valid: set[str] = set()
        for _, tgt in parsed["links"]:
            res = resolve_hub_link(hub_dir, note_path, tgt)
            if res is not None and res.exists() and (r_rel := res.relative_to(hub_dir).as_posix()) != rel:
                existing_valid.add(r_rel)
        if len(existing_valid) >= min_links:
            continue

        self_toks = tokenize_text(extract_semantic_body(txt))
        scored: list[tuple[float, list[str], Path, str, str]] = []
        for cand_p, cand_rel, cand_title, cand_toks in pool_info:
            if cand_rel == rel or cand_rel in existing_valid:
                continue
            shared = sorted(w for w in (self_toks & cand_toks) if len(w) >= 2)
            scored.append((len(shared) + (0.5 if cand_rel.startswith("notes/") else 0.0), shared, cand_p, cand_rel, cand_title))
        scored.sort(key=lambda x: (-x[0], x[3]))
        chosen = scored[: min_links - len(existing_valid)]

        if chosen and write:
            added_lines: list[str] = []
            for _, shared, cand_p, cand_rel, cand_title in chosen:
                href = relative_link(note_path, cand_p)
                reason = f"共同关键词「{'、'.join(shared[:3])}」关联" if shared else f"同属个人知识库主题脉络（与《{cand_title}》互补）"
                added_lines.append(f"- [{cand_title}]({href}) — {reason}")
                existing_valid.add(cand_rel)
            cleaned = [ln for ln in txt.splitlines() if "（待补关联链接" not in ln]
            if "## 关联笔记" not in cleaned:
                cleaned.extend(["", "## 关联笔记", ""])
            atomic_write(note_path, "\n".join(cleaned + added_lines).rstrip() + "\n", [cfg.vault])
            auto_linked.append({"file": rel, "added": len(added_lines)})
        elif chosen:
            for _, _, _, cand_rel, _ in chosen:
                existing_valid.add(cand_rel)
            auto_linked.append({"file": rel, "added": len(chosen), "dry_run": True})
        if len(existing_valid) < min_links:
            still_underlinked.append({"file": rel, "valid_links": len(existing_valid), "required": min_links})

    return {"auto_linked": auto_linked, "underlinked_notes": still_underlinked}


def find_duplicate_notes(
    cfg: InstanceConfig,
    threshold: float = 0.72,
    target_rels: set[str] | None = None,
) -> list[dict[str, Any]]:
    """检测 hub/notes/ 中语义重复笔记对；target_rels 非空时只与新笔记比对以降低复杂度。"""
    hub_dir = cfg.vault / "hub"
    note_files = list_brain_markdown_files(cfg, ("notes",))
    items_data: list[tuple[str, str, str]] = []
    for p in note_files:
        txt = read_for_prompt(p, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(txt)
        title = parsed["title"] or p.stem
        rel = p.relative_to(hub_dir).as_posix()
        body_core = "\n".join(parsed["sections"].get("三行摘要", []) + parsed["sections"].get("关键事实", []))
        items_data.append((rel, title, f"{title}\n{body_core or txt}"))

    dups: list[dict[str, Any]] = []
    for i in range(len(items_data)):
        for j in range(i + 1, len(items_data)):
            rel_a, title_a, text_a = items_data[i]
            rel_b, title_b, text_b = items_data[j]
            if target_rels is not None and rel_a not in target_rels and rel_b not in target_rels:
                continue
            sim = 1.0 if slugify(title_a, "a") == slugify(title_b, "b") else similarity_score(text_a, text_b)
            if sim >= threshold:
                dups.append({"primary": rel_a, "duplicate": rel_b, "primary_title": title_a, "duplicate_title": title_b, "similarity": round(sim, 3)})
    return dups
