"""Second-brain 收件加工流水线：分拣至 notes/sources/people/projects、拆长来源、补链、去重与更新 index.md。"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Iterable

from hushai_agent import clock, ledger, lock, mechanical
from hushai_agent.brain_cfg import BrainConfig, load_brain_config, load_brain_state, save_brain_state
from hushai_agent.brain_model import (
    UNVERIFIED,
    IndexEntry,
    NoteDoc,
    PersonDoc,
    ProjectDoc,
    SourceDoc,
    allocate_unique_path,
    auto_link_notes,
    check_brain_links,
    extract_source_refs_from_md,
    find_duplicate_notes,
    merge_duplicate_notes,
    parse_note_sections,
    read_index_first,
    sanitize_block,
    sanitize_single_line,
    slugify,
    upsert_index_entry,
)
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write, read_for_prompt
from hushai_agent.guard import GuardError
from hushai_agent.inboxfmt import ALL_RESERVED_DOMAINS
from hushai_agent.inboxscan import InboxRecord, iter_inbox_lines
from hushai_agent.items import FlexDate
from hushai_agent.paths import get_instance_state_dir

URL_RE = re.compile(r"https?://[^\s<>\"'）)]+")
PERSON_PREFIX_RE = re.compile(r"^(?:人物|联系人|person)\s*[：:]\s*(?P<name>[^｜|；;\s]+)(?:[｜|；;\s]+(?P<body>.*))?$", re.I)
PROJECT_PREFIX_RE = re.compile(r"^(?:项目|project)\s*[：:]\s*(?P<name>[^｜|；;\s]+)(?:[｜|；;\s]+(?P<body>.*))?$", re.I)
SOURCE_PREFIX_RE = re.compile(r"^(?:来源|文章|视频|书籍|书|source|article|video|book)\s*[：:]\s*(?P<body>.+)$", re.I)
NOTE_PREFIX_RE = re.compile(r"^(?:笔记|想法|note|idea)\s*[：:]\s*(?P<body>.+)$", re.I)
QUOTE_RE = re.compile(r"[“\"「]([^”\"」]{6,})[”\"」](?:\s*[\(（]([^\)）]+)[\)）])?")
PROMISE_RE = re.compile(r"(?:答应|承诺|约定|promised?)\s*([^，。；;\n]+)", re.I)
MY_TAKE_RE = re.compile(r"(?:我的看法|看法|观点|思考|my take|take)\s*[：:]\s*([^\n]+)", re.I)
AUTHOR_RE = re.compile(r"(?:作者|author)\s*[：:]\s*([^｜|；;\n,，]+)", re.I)
PERSON_AT_RE = re.compile(r"@([\u4e00-\u9fffA-Za-z0-9_-]{2,20})")

LONG_SPLIT_CHARS = 320


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[。！？!?；;\n]+", text) if p.strip()]
    return parts or [text.strip()]


def _split_long_chunks(text: str) -> list[str]:
    """将长来源或长段落按换行/要点拆分为多个独立原子想法块（一事一笔记）。"""
    cleaned = sanitize_block(text)
    if not cleaned:
        return []
    blocks = [b.strip(" \t-*•") for b in re.split(r"\n\s*\n+|\n[-*•]\s+", cleaned) if b.strip(" \t-*•")]
    if len(blocks) >= 2:
        return blocks
    if len(cleaned) <= LONG_SPLIT_CHARS:
        return [cleaned]
    sentences = _split_sentences(cleaned)
    if len(sentences) <= 2:
        return [cleaned]
    chunks: list[str] = []
    buf: list[str] = []
    cur_len = 0
    for sent in sentences:
        buf.append(sent)
        cur_len += len(sent)
        if cur_len >= 180 or len(buf) >= 3:
            chunks.append("。".join(buf) + "。")
            buf = []
            cur_len = 0
    if buf:
        chunks.append("。".join(buf) + "。")
    return chunks


def _extract_title(text: str, max_len: int = 36) -> str:
    first_line = sanitize_single_line(text.splitlines()[0] if text.splitlines() else text)
    first_sent = re.split(r"[。！？!?；;]", first_line)[0].strip()
    candidate = re.sub(r"\s+", " ", URL_RE.sub(" ", first_sent)).strip(" ：:-")
    if not candidate:
        candidate = first_sent[:max_len]
    return candidate[:max_len].strip() or "未命名笔记"


def _ensure_index_target(
    cfg: InstanceConfig,
    indexed_targets: set[str] | None,
    entry: IndexEntry,
    max_lines: int,
) -> None:
    """确保 index.md 已登记指定条目（缺漏时自动补登，已有时跳过多余写盘）。"""
    if indexed_targets is None or entry.target not in indexed_targets:
        upsert_index_entry(cfg, entry, max_lines=max_lines, write=True)
        if indexed_targets is not None:
            indexed_targets.add(entry.target)


def _scan_existing_notes_and_sources(
    hub_dir: Path,
) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """扫描已存在的 notes/ 与 sources/ 文件，返回按 (source_ref, title) 与 source_ref 索引的映射。"""
    notes_by_key: dict[tuple[str, str], str] = {}
    notes_dir = hub_dir / "notes"
    if notes_dir.is_dir():
        for md in sorted(notes_dir.glob("*.md")):
            try:
                txt = md.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = f"notes/{md.name}"
            parsed = parse_note_sections(txt)
            title = sanitize_single_line(parsed.get("title") or md.stem)
            for ref in extract_source_refs_from_md(txt):
                notes_by_key.setdefault((ref, title), rel)

    sources_by_ref: dict[str, str] = {}
    sources_dir = hub_dir / "sources"
    if sources_dir.is_dir():
        for md in sorted(sources_dir.glob("*.md")):
            try:
                txt = md.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = f"sources/{md.name}"
            for ref in extract_source_refs_from_md(txt):
                sources_by_ref.setdefault(ref, rel)

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
    """创建一条新的原子笔记并登记进 index.md（按来源+标题幂等去重，不覆写既有文件）。"""
    clean_title = sanitize_single_line(title, "未命名笔记")
    sents = _split_sentences(body_text)
    summary = sents[:3]
    brief_hint = summary[0][:50] if summary else clean_title[:50]

    if not dry_run and roots == [cfg.vault] and source_ref and source_ref != UNVERIFIED:
        existing_rel = (existing_notes or {}).get((source_ref, clean_title))
        if existing_rel and (hub_dir / existing_rel).is_file():
            _ensure_index_target(
                cfg,
                indexed_targets,
                IndexEntry(topic=topic, title=clean_title, target=existing_rel, brief=brief_hint),
                max_index_lines,
            )
            return {
                "type": "note",
                "path": existing_rel,
                "title": clean_title,
                "topic": topic,
                "source": source_ref,
                "already_exists": True,
            }

    notes_dir = hub_dir / "notes"
    slug = slugify(title, prefix="note")
    target_path = allocate_unique_path(notes_dir, slug)
    rel_path = f"notes/{target_path.name}"

    facts = sents[:5] if sents else [UNVERIFIED]

    take = sanitize_block(my_take or "")
    if not take:
        m_take = MY_TAKE_RE.search(body_text)
        take = m_take.group(1).strip() if m_take else f"{UNVERIFIED}（未提供个人看法）"

    doc = NoteDoc(
        title=title,
        date=date_str,
        source=source_ref,
        status="verified" if source_ref and source_ref != UNVERIFIED else UNVERIFIED,
        summary=summary,
        facts=facts,
        my_take=take,
        links=list(extra_links or []),
        rel_path=rel_path,
        topic=topic,
    )
    if not dry_run:
        atomic_write(target_path, doc.render(hub_dir), roots)
        if existing_notes is not None and source_ref and source_ref != UNVERIFIED:
            existing_notes[(source_ref, clean_title)] = rel_path
        if roots == [cfg.vault]:
            upsert_index_entry(
                cfg,
                IndexEntry(topic=topic, title=title, target=rel_path, brief=summary[0][:50]),
                max_lines=max_index_lines,
                write=True,
            )
            if indexed_targets is not None:
                indexed_targets.add(rel_path)
    return {"type": "note", "path": rel_path, "title": title, "topic": topic, "source": source_ref}


def _create_source_file(
    cfg: InstanceConfig,
    hub_dir: Path,
    roots: list[Path],
    title: str,
    body_text: str,
    date_str: str,
    link_url: str,
    source_ref: str,
    max_index_lines: int = 200,
    dry_run: bool = False,
    existing_sources: dict[str, str] | None = None,
    indexed_targets: set[str] | None = None,
) -> dict[str, Any]:
    """创建 sources/<slug>.md 来源档案（按 source_ref 幂等复用），保持未提供字段为 UNVERIFIED。"""
    clean_title = sanitize_single_line(title, "未命名来源")
    points = _split_sentences(URL_RE.sub("", body_text))[:5]
    if not dry_run and roots == [cfg.vault] and source_ref and source_ref != UNVERIFIED:
        existing_rel = (existing_sources or {}).get(source_ref)
        if existing_rel and (hub_dir / existing_rel).is_file():
            existing_link = link_url or source_ref or UNVERIFIED
            try:
                parsed = parse_note_sections((hub_dir / existing_rel).read_text(encoding="utf-8"))
                clean_title = parsed.get("title") or clean_title
            except OSError:
                pass
            _ensure_index_target(
                cfg,
                indexed_targets,
                IndexEntry(topic="来源", title=clean_title, target=existing_rel, brief=(points[0][:50] if points else UNVERIFIED)),
                max_index_lines,
            )
            return {
                "type": "source",
                "path": existing_rel,
                "title": clean_title,
                "link": existing_link,
                "already_exists": True,
            }

    sources_dir = hub_dir / "sources"
    slug = slugify(title, prefix="src")
    target_path = allocate_unique_path(sources_dir, slug)
    rel_path = f"sources/{target_path.name}"

    m_auth = AUTHOR_RE.search(body_text)
    author = m_auth.group(1).strip() if m_auth else UNVERIFIED

    quotes: list[str] = []
    for q_text, ts_or_page in QUOTE_RE.findall(body_text):
        loc = ts_or_page.strip() if ts_or_page and ts_or_page.strip() else UNVERIFIED
        quotes.append(f"\"{q_text.strip()}\" ({loc})")

    doc = SourceDoc(
        title=title,
        author=author,
        date=date_str,
        link=link_url or source_ref or UNVERIFIED,
        points=points,
        quotes=quotes,
        rel_path=rel_path,
        source=source_ref,
    )
    if not dry_run:
        atomic_write(target_path, doc.render(), roots)
        if existing_sources is not None and source_ref and source_ref != UNVERIFIED:
            existing_sources[source_ref] = rel_path
        if roots == [cfg.vault]:
            upsert_index_entry(
                cfg,
                IndexEntry(topic="来源", title=title, target=rel_path, brief=(points[0][:50] if points else UNVERIFIED)),
                max_lines=max_index_lines,
                write=True,
            )
            if indexed_targets is not None:
                indexed_targets.add(rel_path)
    return {"type": "source", "path": rel_path, "title": title, "link": doc.link}


def _upsert_person_file(
    cfg: InstanceConfig,
    hub_dir: Path,
    roots: list[Path],
    name: str,
    detail: str,
    date_str: str,
    promise_text: str | None = None,
    max_index_lines: int = 200,
    dry_run: bool = False,
    source_ref: str = UNVERIFIED,
    indexed_targets: set[str] | None = None,
) -> dict[str, Any]:
    """增量更新 people/<name>.md（不覆写既有历史，按 source_ref 与行内容双重幂等去重）。"""
    people_dir = hub_dir / "people"
    slug = slugify(name, prefix="person")
    target_path = people_dir / f"{slug}.md"
    rel_path = f"people/{target_path.name}"

    existing_sources: list[str] = []
    if target_path.is_file() and roots == [cfg.vault]:
        existing_txt = read_for_prompt(target_path, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(existing_txt)
        existing_sources = extract_source_refs_from_md(existing_txt)
        talked = [ln[2:].strip() for ln in parsed["sections"].get("聊过什么", []) if ln.strip().startswith("- ") and "暂无记录" not in ln]
        promises = [ln[2:].strip() for ln in parsed["sections"].get("我承诺过什么（及时间）", []) if ln.strip().startswith("- ") and "暂无承诺" not in ln]
        cares = [ln[2:].strip() for ln in parsed["sections"].get("对方关心什么", []) if ln.strip().startswith("- ")]
        who = "\n".join(parsed["sections"].get("是谁", [])).strip() or UNVERIFIED
        how_met = "\n".join(parsed["sections"].get("怎么认识的", [])).strip() or UNVERIFIED
    else:
        talked, promises, cares = [], [], []
        who, how_met = UNVERIFIED, UNVERIFIED

    clean_src = sanitize_single_line(source_ref, UNVERIFIED)
    already_recorded = bool(
        not dry_run
        and roots == [cfg.vault]
        and clean_src
        and clean_src != UNVERIFIED
        and clean_src in existing_sources
    )
    if already_recorded:
        _ensure_index_target(
            cfg,
            indexed_targets,
            IndexEntry(topic="人物", title=name, target=rel_path, brief=sanitize_single_line(detail)[:50]),
            max_index_lines,
        )
        return {
            "type": "person",
            "path": rel_path,
            "name": name,
            "promise": promise_text or "",
            "already_exists": True,
        }

    talk_entry = f"{date_str}：{sanitize_single_line(detail)}"
    if detail and talk_entry not in talked:
        talked.append(talk_entry)
    if promise_text:
        p_entry = f"[ ] {date_str}：{sanitize_single_line(promise_text)}"
        if p_entry not in promises:
            promises.append(p_entry)

    sources = list(existing_sources)
    if clean_src and clean_src != UNVERIFIED and clean_src not in sources:
        sources.append(clean_src)

    doc = PersonDoc(
        name=name,
        who=who,
        how_met=how_met,
        talked_about=talked,
        promises=promises,
        cares_about=cares,
        rel_path=rel_path,
        sources=sources,
    )
    if not dry_run:
        atomic_write(target_path, doc.render(), roots)
        if roots == [cfg.vault]:
            upsert_index_entry(
                cfg,
                IndexEntry(topic="人物", title=name, target=rel_path, brief=sanitize_single_line(detail)[:50]),
                max_lines=max_index_lines,
                write=True,
            )
            if indexed_targets is not None:
                indexed_targets.add(rel_path)
    return {"type": "person", "path": rel_path, "name": name, "promise": promise_text or ""}


def _upsert_project_file(
    cfg: InstanceConfig,
    hub_dir: Path,
    roots: list[Path],
    name: str,
    detail: str,
    date_str: str,
    max_index_lines: int = 200,
    dry_run: bool = False,
    source_ref: str = UNVERIFIED,
    indexed_targets: set[str] | None = None,
) -> dict[str, Any]:
    """创建或增量更新 projects/<name>.md（按 source_ref 幂等防重并确保登记 index.md）。"""
    proj_dir = hub_dir / "projects"
    slug = slugify(name, prefix="project")
    target_path = proj_dir / f"{slug}.md"
    rel_path = f"projects/{target_path.name}"

    existing_sources: list[str] = []
    if target_path.is_file() and roots == [cfg.vault]:
        existing_txt = read_for_prompt(target_path, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(existing_txt)
        existing_sources = extract_source_refs_from_md(existing_txt)
        goal = "\n".join(parsed["sections"].get("目标", [])).strip() or UNVERIFIED
        open_q = [ln[2:].strip() for ln in parsed["sections"].get("开放问题", []) if ln.strip().startswith("- ") and "暂无" not in ln]
        decisions = [ln[2:].strip() for ln in parsed["sections"].get("已做决定及原因", []) if ln.strip().startswith("- ") and "暂无" not in ln]
        next_step = "\n".join(parsed["sections"].get("下一步", [])).strip() or sanitize_single_line(detail)
        status = "进行中"
        for ln in parsed["sections"].get("_head", []):
            if ln.startswith("- 状态："):
                status = ln.split("：", 1)[1].strip() or "进行中"
    else:
        goal = sanitize_single_line(detail) or UNVERIFIED
        open_q, decisions = [], []
        next_step = sanitize_single_line(detail) or UNVERIFIED
        status = "进行中"

    clean_src = sanitize_single_line(source_ref, UNVERIFIED)
    already_recorded = bool(
        not dry_run
        and roots == [cfg.vault]
        and clean_src
        and clean_src != UNVERIFIED
        and clean_src in existing_sources
    )
    if already_recorded:
        _ensure_index_target(
            cfg,
            indexed_targets,
            IndexEntry(topic="项目", title=name, target=rel_path, brief=next_step[:50]),
            max_index_lines,
        )
        return {"type": "project", "path": rel_path, "name": name, "already_exists": True}

    if "?" in detail or "？" in detail or "问题" in detail:
        q_item = f"{date_str}：{sanitize_single_line(detail)}"
        if q_item not in open_q:
            open_q.append(q_item)
    if "决定" in detail or "选择" in detail or "采用" in detail:
        d_item = f"{date_str}：{sanitize_single_line(detail)}"
        if d_item not in decisions:
            decisions.append(d_item)

    sources = list(existing_sources)
    if clean_src and clean_src != UNVERIFIED and clean_src not in sources:
        sources.append(clean_src)

    doc = ProjectDoc(
        name=name,
        goal=goal,
        status=status,
        open_questions=open_q,
        decisions=decisions,
        next_step=next_step,
        rel_path=rel_path,
        sources=sources,
    )
    if not dry_run:
        atomic_write(target_path, doc.render(hub_dir), roots)
        if roots == [cfg.vault]:
            upsert_index_entry(
                cfg,
                IndexEntry(topic="项目", title=name, target=rel_path, brief=next_step[:50]),
                max_lines=max_index_lines,
                write=True,
            )
            if indexed_targets is not None:
                indexed_targets.add(rel_path)
    return {"type": "project", "path": rel_path, "name": name}


def _append_new_filed(filed: list[dict[str, Any]], res: dict[str, Any]) -> None:
    if not res.get("already_exists"):
        filed.append(res)


def process_raw_items(
    cfg: InstanceConfig,
    raw_items: Iterable[dict[str, str]],
    as_of_date: datetime.date | str,
    dry_run: bool = False,
    confirm_merge: bool = False,
) -> dict[str, Any]:
    """将一批原始输入（text/domain/date/source_ref）加工为 second-brain 笔记并执行链接与去重检查。"""
    parsed_date = FlexDate.from_any(as_of_date) or clock.today(cfg)
    as_of_str = parsed_date.isoformat()
    bcfg: BrainConfig = load_brain_config(cfg)

    state_dir = get_instance_state_dir(cfg.name)
    hub_dir = (state_dir / "dry-run" / as_of_str / "hub") if dry_run else (cfg.vault / "hub")
    roots = [state_dir] if dry_run else [cfg.vault]

    # 任务前必读 hub/index.md
    _, idx_entries = read_index_first(cfg)
    indexed_targets = {e.target.strip().lstrip("./") for e in idx_entries}

    if not dry_run:
        existing_notes, existing_sources = _scan_existing_notes_and_sources(hub_dir)
    else:
        existing_notes, existing_sources = {}, {}

    filed: list[dict[str, Any]] = []
    for item in raw_items:
        text = sanitize_block(item.get("text", ""))
        if not text:
            continue
        domain = sanitize_single_line(item.get("domain", ""), "未分类")
        item_date = sanitize_single_line(item.get("date", ""), as_of_str)
        source_ref = sanitize_single_line(item.get("source_ref", ""), UNVERIFIED)
        my_take = item.get("my_take")

        # 1. 显式人物前缀或 @人物 / 承诺
        m_person = PERSON_PREFIX_RE.match(text)
        if m_person:
            pname = m_person.group("name").strip()
            pbody = (m_person.group("body") or text).strip()
            m_prom = PROMISE_RE.search(pbody)
            res = _upsert_person_file(
                cfg, hub_dir, roots, pname, pbody, item_date,
                promise_text=m_prom.group(0).strip() if m_prom else None,
                max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                source_ref=source_ref, indexed_targets=indexed_targets,
            )
            _append_new_filed(filed, res)
            continue

        # 2. 显式项目前缀
        m_proj = PROJECT_PREFIX_RE.match(text)
        if m_proj:
            proj_name = m_proj.group("name").strip()
            proj_body = (m_proj.group("body") or text).strip()
            res = _upsert_project_file(
                cfg, hub_dir, roots, proj_name, proj_body, item_date,
                max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                source_ref=source_ref, indexed_targets=indexed_targets,
            )
            _append_new_filed(filed, res)
            continue

        # 3. 含 URL 或显式来源前缀 -> 建 sources/ 档案并按段落拆分原子 notes/
        m_url = URL_RE.search(text)
        m_src = SOURCE_PREFIX_RE.match(text)
        extra_links: list[tuple[str, str]] = []
        content_body = m_src.group("body").strip() if m_src else text

        if m_url or m_src:
            url_str = m_url.group(0) if m_url else ""
            src_title = _extract_title(content_body, max_len=40)
            src_res = _create_source_file(
                cfg, hub_dir, roots, src_title, content_body, item_date,
                url_str, source_ref, max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                existing_sources=existing_sources, indexed_targets=indexed_targets,
            )
            _append_new_filed(filed, src_res)
            extra_links.append((src_res["path"], f"提取自来源《{src_res.get('title') or src_title}》"))

        # 人物 @提及 与承诺自动同步到 people/
        for pname in PERSON_AT_RE.findall(content_body):
            m_prom = PROMISE_RE.search(content_body)
            res_p = _upsert_person_file(
                cfg, hub_dir, roots, pname, content_body, item_date,
                promise_text=m_prom.group(0).strip() if m_prom else None,
                max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                source_ref=source_ref, indexed_targets=indexed_targets,
            )
            _append_new_filed(filed, res_p)

        # 4. 拆分为一事一笔记（长来源或笔记前缀）
        m_note = NOTE_PREFIX_RE.match(content_body)
        note_body = m_note.group("body").strip() if m_note else content_body
        chunks = _split_long_chunks(note_body)
        used_titles_in_item: set[str] = set()
        for idx, chunk in enumerate(chunks):
            base_title = _extract_title(chunk)
            ntitle = f"{base_title}（节选 {idx + 1}）" if base_title in used_titles_in_item else base_title
            used_titles_in_item.add(base_title)
            res_n = _create_note_file(
                cfg, hub_dir, roots, ntitle, chunk, item_date,
                source_ref, domain, my_take=my_take, extra_links=extra_links,
                max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                existing_notes=existing_notes,
                indexed_targets=indexed_targets,
            )
            _append_new_filed(filed, res_n)

    # 链接规则落地：自动补足 >= min_links 条链接，不足则告警
    if not dry_run:
        link_res = auto_link_notes(cfg, min_links=bcfg.min_links, write=True)
        dup_candidates = find_duplicate_notes(cfg)
    else:
        link_res = {"auto_linked": [], "underlinked_notes": []}
        dup_candidates = []

    merged: list[dict[str, Any]] = []
    if confirm_merge and not dry_run:
        for dup in dup_candidates:
            m_res = merge_duplicate_notes(cfg, dup["primary"], dup["duplicate"], confirm=True)
            if m_res.get("merged"):
                merged.append(m_res)

    return {
        "date": as_of_str,
        "dry_run": dry_run,
        "filed": filed,
        "filed_count": len(filed),
        "auto_linked": link_res["auto_linked"],
        "underlinked_notes": link_res["underlinked_notes"],
        "duplicate_candidates": dup_candidates,
        "merged": merged,
    }


def expected_targets_exist_for_record(
    hub_dir: Path,
    rec: InboxRecord,
    disk_refs: dict[str, list[dict[str, str]]],
) -> bool:
    """检查单条收件记录对应的全部预期 second-brain 目标文件是否都真实存在于磁盘。"""
    hits = disk_refs.get(rec.line_id) or []
    if not hits:
        return False
    hit_rels = {info.get("rel") or info.get("path") or "" for info in hits}
    text = sanitize_block(rec.did)
    if rec.artifact and rec.artifact not in ("无", "none", "-"):
        text = f"{text}\n产物：{rec.artifact}"
    if rec.todo and rec.todo not in ("无", "none", "-"):
        text = f"{text}\n待办：{rec.todo}"
    if not text:
        return True

    m_person = PERSON_PREFIX_RE.match(text)
    if m_person:
        pname = m_person.group("name").strip()
        rel = f"people/{slugify(pname, prefix='person')}.md"
        return rel in hit_rels and (hub_dir / rel).is_file()

    m_proj = PROJECT_PREFIX_RE.match(text)
    if m_proj:
        proj_name = m_proj.group("name").strip()
        rel = f"projects/{slugify(proj_name, prefix='project')}.md"
        return rel in hit_rels and (hub_dir / rel).is_file()

    m_url = URL_RE.search(text)
    m_src = SOURCE_PREFIX_RE.match(text)
    content_body = m_src.group("body").strip() if m_src else text
    if (m_url or m_src) and not any(r.startswith("sources/") and (hub_dir / r).is_file() for r in hit_rels):
        return False
    for pname in PERSON_AT_RE.findall(content_body):
        rel = f"people/{slugify(pname, prefix='person')}.md"
        if rel not in hit_rels or not (hub_dir / rel).is_file():
            return False
    return any(r.startswith("notes/") and (hub_dir / r).is_file() for r in hit_rels)


def collect_unprocessed_for_brain(
    cfg: InstanceConfig,
    as_of_date: datetime.date,
    records: Iterable[InboxRecord] | None = None,
) -> tuple[list[dict[str, str]], list[str], list[str]]:
    """收集尚未被 second-brain 加工的非机械收件行与 hub/exchange/in/ 文本文件。"""
    state = load_brain_state(cfg)
    seen_ids = set(state.get("processed_inbox_ids", []))
    seen_ex = set(state.get("processed_exchange_files", []))

    if records is None:
        all_recs = iter_inbox_lines(cfg.vault / "hub" / "inbox")
        cutoff = as_of_date - datetime.timedelta(days=cfg.window_days)
        cand_recs = [
            r for r in all_recs
            if (d := FlexDate.from_any(r.date)) is not None and cutoff <= d <= as_of_date
        ]
    else:
        cand_recs = list(records)

    raw_items: list[dict[str, str]] = []
    new_inbox_ids: list[str] = []
    for rec in cand_recs:
        if rec.line_id in seen_ids:
            continue
        if rec.domain in ALL_RESERVED_DOMAINS:
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
        raw_items.append({
            "text": text,
            "domain": rec.domain,
            "date": rec.date,
            "source_ref": rec.line_id,
        })
        new_inbox_ids.append(rec.line_id)

    ex_in = cfg.vault / "hub" / "exchange" / "in"
    new_ex_files: list[str] = []
    if ex_in.is_dir():
        for fpath in sorted(ex_in.rglob("*")):
            if not fpath.is_file() or fpath.name.startswith("."):
                continue
            if fpath.suffix.lower() not in (".md", ".txt"):
                continue
            rel = fpath.relative_to(cfg.vault).as_posix()
            if rel in seen_ex:
                continue
            try:
                content = read_for_prompt(fpath, cfg.vault, cfg.local_only)
            except (GuardError, OSError, UnicodeDecodeError):
                continue
            if content.strip():
                raw_items.append({
                    "text": content,
                    "domain": "资料",
                    "date": as_of_date.isoformat(),
                    "source_ref": rel,
                })
                new_ex_files.append(rel)

    return raw_items, new_inbox_ids, new_ex_files


def process_inbox_to_brain(
    cfg: InstanceConfig,
    as_of_date: datetime.date | str | None = None,
    records: Iterable[InboxRecord] | None = None,
    dry_run: bool = False,
    confirm_merge: bool = False,
    _hold_lock: bool = True,
) -> dict[str, Any]:
    """扫描未加工的收件行与交换区文本并归档到 second-brain，推进 brain-state 游标。"""
    parsed_date = FlexDate.from_any(as_of_date) if as_of_date else clock.today(cfg)
    if parsed_date is None:
        parsed_date = clock.today(cfg)

    lock_ctx = None
    if _hold_lock and not dry_run:
        try:
            lock_ctx = lock.runner_lock(cfg, wait_seconds=lock.job_wait_seconds())
            lock_ctx.__enter__()
        except lock.LockBusy:
            ledger.record(
                cfg, "job", job="brain-process", date=parsed_date.isoformat(), dry_run=dry_run, result="failed", note="lock-busy"
            )
            raise

    try:
        raw_items, new_ids, new_ex = collect_unprocessed_for_brain(cfg, parsed_date, records=records)
        result = process_raw_items(
            cfg,
            raw_items,
            as_of_date=parsed_date,
            dry_run=dry_run,
            confirm_merge=confirm_merge,
        )
        if not dry_run and (new_ids or new_ex):
            state = load_brain_state(cfg)
            state["processed_inbox_ids"] = list(dict.fromkeys([*state.get("processed_inbox_ids", []), *new_ids]))
            state["processed_exchange_files"] = list(dict.fromkeys([*state.get("processed_exchange_files", []), *new_ex]))
            save_brain_state(cfg, state)
        result["processed_inbox_count"] = len(new_ids)
        result["processed_exchange_count"] = len(new_ex)
        result["processed_inbox_ids"] = list(new_ids)
        result["processed_exchange_files"] = list(new_ex)
        return result
    finally:
        if lock_ctx is not None:
            lock_ctx.__exit__(None, None, None)
            from hushai_agent.reconcile import drain_pending_mechanical

            drain_pending_mechanical(cfg)


def reconcile_brain_step(
    cfg: InstanceConfig,
    as_of_date: datetime.date | str,
    records: Iterable[InboxRecord] | None = None,
    dry_run: bool = False,
    process_when_enabled: bool = True,
) -> dict[str, Any]:
    """供 reconcile / digest_apply 调用的 second-brain 对账步骤。

    - 若 [brain] enabled 且 daily_enabled 为真且 process_when_enabled=True：加工收件并自动补链。
    - 无论是否启用，只要 hub/notes/ 中存在笔记，即轻量检查断链、少于 2 条链接的笔记及重复候选（不强行写文件）。
    """
    bcfg = load_brain_config(cfg)
    notes_dir = cfg.vault / "hub" / "notes"
    has_notes = notes_dir.is_dir() and any(p.suffix == ".md" for p in notes_dir.iterdir() if p.is_file())

    processed_info: dict[str, Any] = {"filed_count": 0, "auto_linked": [], "filed": []}
    if process_when_enabled and bcfg.enabled and bcfg.daily_enabled:
        processed_info = process_inbox_to_brain(
            cfg, as_of_date=as_of_date, records=records, dry_run=dry_run, confirm_merge=False, _hold_lock=False
        )

    if not has_notes and processed_info.get("filed_count", 0) == 0:
        return {
            "filed_count": 0,
            "filed": [],
            "auto_linked": [],
            "broken_links": [],
            "underlinked_notes": [],
            "duplicate_candidates": [],
        }

    link_health = check_brain_links(cfg, min_links=bcfg.min_links)
    dups = find_duplicate_notes(cfg)
    return {
        "filed_count": processed_info.get("filed_count", 0),
        "filed": processed_info.get("filed", []),
        "auto_linked": processed_info.get("auto_linked", []),
        "broken_links": link_health["broken_links"],
        "underlinked_notes": link_health["underlinked_notes"],
        "duplicate_candidates": dups,
    }
