"""Second-brain 收件加工流水线：分拣至 notes/sources/people/projects、拆长来源、补链、去重与更新 index.md。"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Iterable

from hushai_agent import clock, mechanical
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
) -> dict[str, Any]:
    """创建一条新的原子笔记并登记进 index.md（不覆写既有文件）。"""
    notes_dir = hub_dir / "notes"
    slug = slugify(title, prefix="note")
    target_path = allocate_unique_path(notes_dir, slug)
    rel_path = f"notes/{target_path.name}"

    sents = _split_sentences(body_text)
    summary = sents[:3]
    if len(summary) < 3:
        # 保持至多 3 行摘要，绝不编造
        pass
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
        if roots == [cfg.vault]:
            upsert_index_entry(
                cfg,
                IndexEntry(topic=topic, title=title, target=rel_path, brief=summary[0][:50]),
                max_lines=max_index_lines,
                write=True,
            )
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
) -> dict[str, Any]:
    """创建 sources/<slug>.md 来源档案，并保持未提供字段为 UNVERIFIED。"""
    sources_dir = hub_dir / "sources"
    slug = slugify(title, prefix="src")
    target_path = allocate_unique_path(sources_dir, slug)
    rel_path = f"sources/{target_path.name}"

    m_auth = AUTHOR_RE.search(body_text)
    author = m_auth.group(1).strip() if m_auth else UNVERIFIED

    points = _split_sentences(URL_RE.sub("", body_text))[:5]
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
    )
    if not dry_run:
        atomic_write(target_path, doc.render(), roots)
        if roots == [cfg.vault]:
            upsert_index_entry(
                cfg,
                IndexEntry(topic="来源", title=title, target=rel_path, brief=(points[0][:50] if points else UNVERIFIED)),
                max_lines=max_index_lines,
                write=True,
            )
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
) -> dict[str, Any]:
    """增量更新 people/<name>.md（不覆写既有历史，只追加对话与承诺）。"""
    people_dir = hub_dir / "people"
    slug = slugify(name, prefix="person")
    target_path = people_dir / f"{slug}.md"
    rel_path = f"people/{target_path.name}"

    if target_path.is_file() and roots == [cfg.vault]:
        existing_txt = read_for_prompt(target_path, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(existing_txt)
        talked = [ln[2:].strip() for ln in parsed["sections"].get("聊过什么", []) if ln.strip().startswith("- ") and "暂无记录" not in ln]
        promises = [ln[2:].strip() for ln in parsed["sections"].get("我承诺过什么（及时间）", []) if ln.strip().startswith("- ") and "暂无承诺" not in ln]
        cares = [ln[2:].strip() for ln in parsed["sections"].get("对方关心什么", []) if ln.strip().startswith("- ")]
        who = "\n".join(parsed["sections"].get("是谁", [])).strip() or UNVERIFIED
        how_met = "\n".join(parsed["sections"].get("怎么认识的", [])).strip() or UNVERIFIED
    else:
        talked, promises, cares = [], [], []
        who, how_met = UNVERIFIED, UNVERIFIED

    talk_entry = f"{date_str}：{sanitize_single_line(detail)}"
    if detail and talk_entry not in talked:
        talked.append(talk_entry)
    if promise_text:
        p_entry = f"[ ] {date_str}：{sanitize_single_line(promise_text)}"
        if p_entry not in promises:
            promises.append(p_entry)

    doc = PersonDoc(
        name=name,
        who=who,
        how_met=how_met,
        talked_about=talked,
        promises=promises,
        cares_about=cares,
        rel_path=rel_path,
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
) -> dict[str, Any]:
    """创建或增量更新 projects/<name>.md。"""
    proj_dir = hub_dir / "projects"
    slug = slugify(name, prefix="project")
    target_path = proj_dir / f"{slug}.md"
    rel_path = f"projects/{target_path.name}"

    if target_path.is_file() and roots == [cfg.vault]:
        existing_txt = read_for_prompt(target_path, cfg.vault, cfg.local_only)
        parsed = parse_note_sections(existing_txt)
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

    if "?" in detail or "？" in detail or "问题" in detail:
        q_item = f"{date_str}：{sanitize_single_line(detail)}"
        if q_item not in open_q:
            open_q.append(q_item)
    if "决定" in detail or "选择" in detail or "采用" in detail:
        d_item = f"{date_str}：{sanitize_single_line(detail)}"
        if d_item not in decisions:
            decisions.append(d_item)

    doc = ProjectDoc(
        name=name,
        goal=goal,
        status=status,
        open_questions=open_q,
        decisions=decisions,
        next_step=next_step,
        rel_path=rel_path,
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
    return {"type": "project", "path": rel_path, "name": name}


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
    read_index_first(cfg)

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
            )
            filed.append(res)
            continue

        # 2. 显式项目前缀
        m_proj = PROJECT_PREFIX_RE.match(text)
        if m_proj:
            proj_name = m_proj.group("name").strip()
            proj_body = (m_proj.group("body") or text).strip()
            res = _upsert_project_file(
                cfg, hub_dir, roots, proj_name, proj_body, item_date,
                max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
            )
            filed.append(res)
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
            )
            filed.append(src_res)
            extra_links.append((src_res["path"], f"提取自来源《{src_title}》"))

        # 人物 @提及 与承诺自动同步到 people/
        for pname in PERSON_AT_RE.findall(content_body):
            m_prom = PROMISE_RE.search(content_body)
            filed.append(
                _upsert_person_file(
                    cfg, hub_dir, roots, pname, content_body, item_date,
                    promise_text=m_prom.group(0).strip() if m_prom else None,
                    max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                )
            )

        # 4. 拆分为一事一笔记（长来源或笔记前缀）
        m_note = NOTE_PREFIX_RE.match(content_body)
        note_body = m_note.group("body").strip() if m_note else content_body
        chunks = _split_long_chunks(note_body)
        for idx, chunk in enumerate(chunks):
            ntitle = _extract_title(chunk)
            if len(chunks) > 1 and ntitle == _extract_title(chunks[0]) and idx > 0:
                ntitle = f"{ntitle}（节选 {idx + 1}）"
            filed.append(
                _create_note_file(
                    cfg, hub_dir, roots, ntitle, chunk, item_date,
                    source_ref, domain, my_take=my_take, extra_links=extra_links,
                    max_index_lines=bcfg.index_max_lines, dry_run=dry_run,
                )
            )

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
) -> dict[str, Any]:
    """扫描未加工的收件行与交换区文本并归档到 second-brain，推进 brain-state 游标。"""
    parsed_date = FlexDate.from_any(as_of_date) if as_of_date else clock.today(cfg)
    if parsed_date is None:
        parsed_date = clock.today(cfg)
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
    return result


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

    processed_info: dict[str, Any] = {"filed_count": 0, "auto_linked": []}
    if process_when_enabled and bcfg.enabled and bcfg.daily_enabled:
        processed_info = process_inbox_to_brain(
            cfg, as_of_date=as_of_date, records=records, dry_run=dry_run, confirm_merge=False
        )

    if not has_notes and processed_info.get("filed_count", 0) == 0:
        return {
            "filed_count": 0,
            "auto_linked": [],
            "broken_links": [],
            "underlinked_notes": [],
            "duplicate_candidates": [],
        }

    link_health = check_brain_links(cfg, min_links=bcfg.min_links)
    dups = find_duplicate_notes(cfg)
    return {
        "filed_count": processed_info.get("filed_count", 0),
        "auto_linked": processed_info.get("auto_linked", []),
        "broken_links": link_health["broken_links"],
        "underlinked_notes": link_health["underlinked_notes"],
        "duplicate_candidates": dups,
    }
