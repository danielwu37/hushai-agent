"""Second-brain 各类实体文件写入（notes/sources/people/projects）与文本分块解析。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hushai_agent.brain_model import (
    UNVERIFIED,
    IndexEntry,
    NoteDoc,
    PersonDoc,
    ProjectDoc,
    SourceDoc,
    allocate_unique_path,
    extract_source_refs_from_md,
    parse_note_sections,
    sanitize_block,
    sanitize_single_line,
    slugify,
    upsert_index_entry,
)
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write, read_for_prompt

URL_RE = re.compile(r"https?://[^\s<>\"'）)]+")
PERSON_PREFIX_RE = re.compile(r"^(?:人物|联系人|person)\s*[：:]\s*(?P<name>[^｜|；;\s]+)(?:[｜|；;\s]+(?P<body>.*))?$", re.I)
PROJECT_PREFIX_RE = re.compile(r"^(?:项目|project)\s*[：:]\s*(?P<name>[^｜|；;\s]+)(?:[｜|；;\s]+(?P<body>.*))?$", re.I)
SOURCE_PREFIX_RE = re.compile(r"^(?:来源|文章|视频|书籍|书|source|article|video|book)\s*[：:]\s*(?P<body>.+)$", re.I)
NOTE_PREFIX_RE = re.compile(r"^(?:笔记|想法|note|idea)\s*[：:]\s*(?P<body>.+)$", re.I)
QUOTE_RE = re.compile(r"[“\"「]([^”\"」]{6,})[”\"」](?:\s*[\(（]([^\)）]+)[\)）])?")
PROMISE_RE = re.compile(r"(?:答应|承诺|约定|promised?)\s*([^，。；;\n]+)", re.I)
MY_TAKE_RE = re.compile(r"(?:我的看法|看法|观点|思考|my take|take)\s*[：:]\s*([^\n]+)", re.I)
AUTHOR_RE = re.compile(r"(?:作者|author)\s*[：:]\s*([^｜|；;\n,，]+)", re.I)
# 排除邮箱（如 user@gmail.com）误命中人物页
PERSON_AT_RE = re.compile(r"(?<![A-Za-z0-9._%+-])@([\u4e00-\u9fffA-Za-z0-9_-]{2,20})(?!\.[A-Za-z]{2,})")

LONG_SPLIT_CHARS = 320
MAX_CHUNKS_PER_ITEM = 6


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[。！？!?；;\n]+", text) if p.strip()]
    return parts or [text.strip()]


def _split_long_chunks(text: str) -> list[str]:
    cleaned = sanitize_block(text)
    if not cleaned:
        return []
    blocks = [b.strip(" \t-*•") for b in re.split(r"\n\s*\n+|\n[-*•]\s+", cleaned) if b.strip(" \t-*•")]
    if len(blocks) >= 2:
        return blocks[:MAX_CHUNKS_PER_ITEM]
    if len(cleaned) <= LONG_SPLIT_CHARS:
        return [cleaned]
    sentences = _split_sentences(cleaned)
    if len(sentences) <= 2:
        return [cleaned]
    chunks, buf, cur_len = [], [], 0
    for sent in sentences:
        buf.append(sent)
        cur_len += len(sent)
        if cur_len >= 180 or len(buf) >= 3:
            chunks.append("。".join(buf) + "。")
            buf, cur_len = [], 0
    if buf:
        chunks.append("。".join(buf) + "。")
    return chunks[:MAX_CHUNKS_PER_ITEM]


def _extract_title(text: str, max_len: int = 36) -> str:
    first_line = sanitize_single_line(text.splitlines()[0] if text.splitlines() else text)
    first_sent = re.split(r"[。！？!?；;]", first_line)[0].strip()
    candidate = re.sub(r"\s+", " ", URL_RE.sub(" ", first_sent)).strip(" ：:-") or first_sent[:max_len]
    return candidate[:max_len].strip() or "未命名笔记"


def _ensure_index_target(cfg: InstanceConfig, indexed_targets: set[str] | None, entry: IndexEntry, max_lines: int) -> None:
    if indexed_targets is None or entry.target not in indexed_targets:
        upsert_index_entry(cfg, entry, max_lines=max_lines, write=True)
        if indexed_targets is not None:
            indexed_targets.add(entry.target)


def _scan_existing_notes_and_sources(hub_dir: Path) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    notes_by_key: dict[tuple[str, str], str] = {}
    notes_dir = hub_dir / "notes"
    if notes_dir.is_dir():
        for md in sorted(notes_dir.glob("*.md")):
            try:
                txt = md.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = parse_note_sections(txt)
            title = sanitize_single_line(parsed.get("title") or md.stem)
            for ref in extract_source_refs_from_md(txt):
                notes_by_key.setdefault((ref, title), f"notes/{md.name}")

    sources_by_ref: dict[str, str] = {}
    sources_dir = hub_dir / "sources"
    if sources_dir.is_dir():
        for md in sorted(sources_dir.glob("*.md")):
            try:
                txt = md.read_text(encoding="utf-8")
            except OSError:
                continue
            for ref in extract_source_refs_from_md(txt):
                sources_by_ref.setdefault(ref, f"sources/{md.name}")
    return notes_by_key, sources_by_ref


def _create_note_file(
    cfg: InstanceConfig,
    hub_dir: Path,
    roots: list[Path],
    title: str,
    body_text: str,
    date_str: str,
    source_ref: str,
    topic: str,
    my_take: str | None = None,
    extra_links: list[tuple[str, str]] | None = None,
    max_index_lines: int = 200,
    dry_run: bool = False,
    existing_notes: dict[tuple[str, str], str] | None = None,
    indexed_targets: set[str] | None = None,
) -> dict[str, Any]:
    """创建原子笔记（仅标注 '有出处' 或 'UNVERIFIED'，不将未经核查的摘录伪标为已核实）。"""
    clean_title = sanitize_single_line(title, "未命名笔记")
    sents = _split_sentences(body_text)
    summary = sents[:3]
    brief_hint = summary[0][:50] if summary else clean_title[:50]

    if not dry_run and roots == [cfg.vault] and source_ref and source_ref != UNVERIFIED:
        existing_rel = (existing_notes or {}).get((source_ref, clean_title))
        if existing_rel and (hub_dir / existing_rel).is_file():
            _ensure_index_target(cfg, indexed_targets, IndexEntry(topic, clean_title, existing_rel, brief_hint), max_index_lines)
            return {"type": "note", "path": existing_rel, "title": clean_title, "topic": topic, "source": source_ref, "already_exists": True}

    target_path = allocate_unique_path(hub_dir / "notes", slugify(title, prefix="note"))
    rel_path = f"notes/{target_path.name}"
    take = sanitize_block(my_take or "")
    if not take:
        m_take = MY_TAKE_RE.search(body_text)
        take = m_take.group(1).strip() if m_take else f"{UNVERIFIED}（未提供个人看法）"

    has_provenance = bool(source_ref and source_ref != UNVERIFIED)
    doc = NoteDoc(
        title=title, date=date_str, source=source_ref,
        status="有出处" if has_provenance else UNVERIFIED,
        summary=summary, facts=sents[:5] if sents else [UNVERIFIED],
        my_take=take, links=list(extra_links or []), rel_path=rel_path, topic=topic,
    )
    if not dry_run:
        atomic_write(target_path, doc.render(hub_dir), roots)
        if existing_notes is not None and has_provenance:
            existing_notes[(source_ref, clean_title)] = rel_path
        if roots == [cfg.vault]:
            upsert_index_entry(cfg, IndexEntry(topic, title, rel_path, summary[0][:50]), max_lines=max_index_lines, write=True)
            if indexed_targets is not None:
                indexed_targets.add(rel_path)
    return {"type": "note", "path": rel_path, "title": title, "topic": topic, "source": source_ref}


def _create_source_file(
    cfg: InstanceConfig, hub_dir: Path, roots: list[Path], title: str, body_text: str, date_str: str,
    link_url: str, source_ref: str, max_index_lines: int = 200, dry_run: bool = False,
    existing_sources: dict[str, str] | None = None, indexed_targets: set[str] | None = None,
) -> dict[str, Any]:
    clean_title = sanitize_single_line(title, "未命名来源")
    points = _split_sentences(URL_RE.sub("", body_text))[:5]
    if not dry_run and roots == [cfg.vault] and source_ref and source_ref != UNVERIFIED:
        existing_rel = (existing_sources or {}).get(source_ref)
        if existing_rel and (hub_dir / existing_rel).is_file():
            try:
                parsed = parse_note_sections((hub_dir / existing_rel).read_text(encoding="utf-8"))
                clean_title = parsed.get("title") or clean_title
            except OSError:
                pass
            _ensure_index_target(cfg, indexed_targets, IndexEntry("来源", clean_title, existing_rel, points[0][:50] if points else UNVERIFIED), max_index_lines)
            return {"type": "source", "path": existing_rel, "title": clean_title, "link": link_url or source_ref or UNVERIFIED, "already_exists": True}

    target_path = allocate_unique_path(hub_dir / "sources", slugify(title, prefix="src"))
    rel_path = f"sources/{target_path.name}"
    m_auth = AUTHOR_RE.search(body_text)
    quotes = [f"\"{qt.strip()}\" ({loc.strip() if loc and loc.strip() else UNVERIFIED})" for qt, loc in QUOTE_RE.findall(body_text)]
    doc = SourceDoc(title=title, author=m_auth.group(1).strip() if m_auth else UNVERIFIED, date=date_str, link=link_url or source_ref or UNVERIFIED, points=points, quotes=quotes, rel_path=rel_path, source=source_ref)
    if not dry_run:
        atomic_write(target_path, doc.render(), roots)
        if existing_sources is not None and source_ref and source_ref != UNVERIFIED:
            existing_sources[source_ref] = rel_path
        if roots == [cfg.vault]:
            upsert_index_entry(cfg, IndexEntry("来源", title, rel_path, points[0][:50] if points else UNVERIFIED), max_lines=max_index_lines, write=True)
            if indexed_targets is not None:
                indexed_targets.add(rel_path)
    return {"type": "source", "path": rel_path, "title": title, "link": doc.link}


def _upsert_person_file(
    cfg: InstanceConfig, hub_dir: Path, roots: list[Path], name: str, detail: str, date_str: str,
    promise_text: str | None = None, max_index_lines: int = 200, dry_run: bool = False,
    source_ref: str = UNVERIFIED, indexed_targets: set[str] | None = None,
) -> dict[str, Any]:
    target_path = hub_dir / "people" / f"{slugify(name, prefix='person')}.md"
    rel_path = f"people/{target_path.name}"
    existing_sources, talked, promises, cares, who, how_met = [], [], [], [], UNVERIFIED, UNVERIFIED
    if target_path.is_file() and roots == [cfg.vault]:
        existing_txt = read_for_prompt(target_path, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(existing_txt)
        existing_sources = extract_source_refs_from_md(existing_txt)
        talked = [ln[2:].strip() for ln in parsed["sections"].get("聊过什么", []) if ln.strip().startswith("- ") and "暂无记录" not in ln]
        promises = [ln[2:].strip() for ln in parsed["sections"].get("我承诺过什么（及时间）", []) if ln.strip().startswith("- ") and "暂无承诺" not in ln]
        cares = [ln[2:].strip() for ln in parsed["sections"].get("对方关心什么", []) if ln.strip().startswith("- ")]
        who = "\n".join(parsed["sections"].get("是谁", [])).strip() or UNVERIFIED
        how_met = "\n".join(parsed["sections"].get("怎么认识的", [])).strip() or UNVERIFIED

    clean_src = sanitize_single_line(source_ref, UNVERIFIED)
    if not dry_run and roots == [cfg.vault] and clean_src and clean_src != UNVERIFIED and clean_src in existing_sources:
        _ensure_index_target(cfg, indexed_targets, IndexEntry("人物", name, rel_path, sanitize_single_line(detail)[:50]), max_index_lines)
        return {"type": "person", "path": rel_path, "name": name, "promise": promise_text or "", "already_exists": True}

    talk_entry = f"{date_str}：{sanitize_single_line(detail)}"
    if detail and talk_entry not in talked:
        talked.append(talk_entry)
    if promise_text and (p_entry := f"[ ] {date_str}：{sanitize_single_line(promise_text)}") not in promises:
        promises.append(p_entry)
    sources = list(existing_sources)
    if clean_src and clean_src != UNVERIFIED and clean_src not in sources:
        sources.append(clean_src)

    doc = PersonDoc(name=name, who=who, how_met=how_met, talked_about=talked, promises=promises, cares_about=cares, rel_path=rel_path, sources=sources)
    if not dry_run:
        atomic_write(target_path, doc.render(), roots)
        if roots == [cfg.vault]:
            upsert_index_entry(cfg, IndexEntry("人物", name, rel_path, sanitize_single_line(detail)[:50]), max_lines=max_index_lines, write=True)
            if indexed_targets is not None:
                indexed_targets.add(rel_path)
    return {"type": "person", "path": rel_path, "name": name, "promise": promise_text or ""}


def _upsert_project_file(
    cfg: InstanceConfig, hub_dir: Path, roots: list[Path], name: str, detail: str, date_str: str,
    max_index_lines: int = 200, dry_run: bool = False, source_ref: str = UNVERIFIED, indexed_targets: set[str] | None = None,
) -> dict[str, Any]:
    target_path = hub_dir / "projects" / f"{slugify(name, prefix='project')}.md"
    rel_path = f"projects/{target_path.name}"
    existing_sources, goal, open_q, decisions, next_step, status = [], sanitize_single_line(detail) or UNVERIFIED, [], [], sanitize_single_line(detail) or UNVERIFIED, "进行中"
    if target_path.is_file() and roots == [cfg.vault]:
        existing_txt = read_for_prompt(target_path, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(existing_txt)
        existing_sources = extract_source_refs_from_md(existing_txt)
        goal = "\n".join(parsed["sections"].get("目标", [])).strip() or UNVERIFIED
        open_q = [ln[2:].strip() for ln in parsed["sections"].get("开放问题", []) if ln.strip().startswith("- ") and "暂无" not in ln]
        decisions = [ln[2:].strip() for ln in parsed["sections"].get("已做决定及原因", []) if ln.strip().startswith("- ") and "暂无" not in ln]
        next_step = "\n".join(parsed["sections"].get("下一步", [])).strip() or sanitize_single_line(detail)
        for ln in parsed["sections"].get("_head", []):
            if ln.startswith("- 状态："):
                status = ln.split("：", 1)[1].strip() or "进行中"

    clean_src = sanitize_single_line(source_ref, UNVERIFIED)
    if not dry_run and roots == [cfg.vault] and clean_src and clean_src != UNVERIFIED and clean_src in existing_sources:
        _ensure_index_target(cfg, indexed_targets, IndexEntry("项目", name, rel_path, next_step[:50]), max_index_lines)
        return {"type": "project", "path": rel_path, "name": name, "already_exists": True}
    if any(k in detail for k in ("?", "？", "问题")) and (q_item := f"{date_str}：{sanitize_single_line(detail)}") not in open_q:
        open_q.append(q_item)
    if any(k in detail for k in ("决定", "选择", "采用")) and (d_item := f"{date_str}：{sanitize_single_line(detail)}") not in decisions:
        decisions.append(d_item)
    sources = list(existing_sources)
    if clean_src and clean_src != UNVERIFIED and clean_src not in sources:
        sources.append(clean_src)

    doc = ProjectDoc(name=name, goal=goal, status=status, open_questions=open_q, decisions=decisions, next_step=next_step, rel_path=rel_path, sources=sources)
    if not dry_run:
        atomic_write(target_path, doc.render(hub_dir), roots)
        if roots == [cfg.vault]:
            upsert_index_entry(cfg, IndexEntry("项目", name, rel_path, next_step[:50]), max_lines=max_index_lines, write=True)
            if indexed_targets is not None:
                indexed_targets.add(rel_path)
    return {"type": "project", "path": rel_path, "name": name}
