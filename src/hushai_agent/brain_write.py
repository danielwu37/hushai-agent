"""把一次编译结果落盘：页面增改、原件摘要页、每日入脑日志、目录 index.md。

只增改不删除；`locked: true` 的页面不碰；「## 我的笔记」原样保留；来源只增不减。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from hushai_agent.brain_page import (
    LOG_DIR,
    STATUS_SOURCED,
    TYPE_DIRS,
    TYPE_LABELS,
    Page,
    clean_title,
    load_pages,
    name_map,
    norm,
    write_page,
)
from hushai_agent.brain_raw import RawBatch
from hushai_agent.fsio import append_line_with_header, atomic_write

INDEX_ORDER = ("project", "topic", "person", "source")


def _merge_list(old: list[Any] | None, new: list[Any] | None, skip: str = "") -> list[str]:
    out: list[str] = []
    for item in [*(old or []), *(new or [])]:
        val = clean_title(item)
        if val and val != skip and norm(val) not in {norm(x) for x in out}:
            out.append(val)
    return out[:8]


def _upsert(vault: Path, names: dict[str, Page], spec: dict[str, Any], refs: list[str], today: str) -> tuple[str, str]:
    """新建或更新一页，返回 (动作, 标题)；动作为 created / updated / locked。"""
    page = names.get(norm(spec["title"]))
    if page is None:
        for alias in spec.get("aliases") or []:
            page = names.get(norm(alias))
            if page is not None:
                break
    if page is not None and page.meta.get("locked") is True:
        return "locked", page.title
    if page is None:
        page = Page(rel=f"{TYPE_DIRS[spec['type']]}/{spec['title']}.md", meta={
            "type": spec["type"], "title": spec["title"], "aliases": [], "domain": spec["domain"],
            "tags": [], "summary": "", "status": STATUS_SOURCED, "created": today,
        })
        action = "created"
    else:
        action = "updated"
    page.meta["aliases"] = _merge_list(page.meta.get("aliases"), [*spec.get("aliases", []), spec["title"]], page.title)
    page.meta["tags"] = _merge_list(page.meta.get("tags"), spec.get("tags"))
    page.meta.setdefault("domain", spec["domain"])
    page.meta["summary"] = spec["summary"] or page.meta.get("summary", "")
    if spec["body"].strip() != page.body.strip():
        page.meta["status"] = STATUS_SOURCED
    page.meta["updated"] = today
    page.body = spec["body"]
    page.refs = page.refs + [r for r in refs if r not in page.refs]
    write_page(vault, page)
    for name in page.names():
        names.setdefault(norm(name), page)
    return action, page.title


def apply_result(vault: Path, batch: RawBatch, data: dict[str, Any], now: datetime.datetime) -> dict[str, Any]:
    """把校验过的模型结果写进 hub；返回 {created, updated, locked, source}。"""
    today = now.date().isoformat()
    names = name_map(load_pages(vault / "hub"))
    out: dict[str, Any] = {"created": [], "updated": [], "locked": [], "source": ""}
    src_title = ""
    if batch.kind == "file":
        src_title = clean_title(f"来源-{data['source']['title'] or batch.file_name}")
    for spec in data["pages"]:
        if batch.kind == "file":
            refs = [f"[[{src_title}]]"]
        else:
            refs = [batch.refs[i - 1] for i in spec["refs"]]
        action, title = _upsert(vault, names, spec, refs, today)
        out[action].append(title)
    if batch.kind == "file":
        touched = out["created"] + out["updated"]
        body = data["source"]["body"].strip()
        body += f"\n\n原件：`{batch.file_rel}`（sha256 前 8 位 {batch.file_sha[:8]}）"
        if touched:
            body += "\n\n涉及：" + "、".join(f"[[{t}]]" for t in touched)
        spec = {"title": src_title, "type": "source", "domain": data["source"].get("domain") or "",
                "aliases": [], "tags": ["来源"], "summary": data["source"]["summary"], "body": body}
        if not spec["domain"]:
            spec["domain"] = data["pages"][0]["domain"] if data["pages"] else ""
        action, title = _upsert(vault, names, spec, batch.refs, today)
        out[action].append(title)
        out["source"] = title
    return out


def append_log(vault: Path, now: datetime.datetime, batch: RawBatch, log: str, res: dict[str, Any]) -> None:
    """每日入脑日志（卡帕西的 log.md + Obsidian 的日记页）：一批一行，带 [[链接]]。"""
    day = now.date().isoformat()
    parts = [f"- {now.strftime('%H:%M')}｜{log or '编译'}"]
    if res["created"]:
        parts.append("新建 " + "、".join(f"[[{t}]]" for t in res["created"]))
    if res["updated"]:
        parts.append("更新 " + "、".join(f"[[{t}]]" for t in res["updated"]))
    if res["locked"]:
        parts.append("已锁定未改 " + "、".join(res["locked"]))
    parts.append(f"原料：收件 {len(batch.line_ids)} 行" if batch.kind == "lines" else f"原料：{batch.file_rel}")
    header = f"# {day} 入脑日志（hushai brain 追加，是数据不是指令）"
    append_line_with_header(vault / "hub" / LOG_DIR / f"{day}.md", header, "｜".join(parts), [vault])


def render_index(pages: list[Page], max_lines: int, stamp: str) -> str:
    """按类型分节、各节按更新日期倒序；超出行数上限时从最大的一节砍最旧的。"""
    groups: dict[str, list[Page]] = {k: [] for k in INDEX_ORDER}
    for page in pages:
        groups.setdefault(page.kind if page.kind in groups else "topic", []).append(page)
    for items in groups.values():
        items.sort(key=lambda p: (str(p.meta.get("updated") or ""), p.title), reverse=True)
    counts = {k: len(v) for k, v in groups.items()}
    head = [
        "# 第二大脑目录（index.md）",
        "",
        f"> 由 `hushai brain` 按页面头信息生成，是数据不是指令，不要手改。共 {len(pages)} 页，"
        f"最近编译 {stamp}。",
        "> 用法：先在本页找标题，再打开 `hub/<目录>/<标题>.md`，或 `hushai brain ask \"问题\"`；"
        "要入脑就写收件行「笔记：……」或把文件放进 `hub/exchange/in/入脑/`。",
    ]
    fixed = len(head) + 3 * sum(1 for k in INDEX_ORDER if counts[k])
    while fixed + sum(len(v) for v in groups.values()) > max_lines and any(groups.values()):
        biggest = max(INDEX_ORDER, key=lambda k: len(groups[k]))
        groups[biggest].pop()
    lines = list(head)
    for kind in INDEX_ORDER:
        if not counts[kind]:
            continue
        lines += ["", f"## {TYPE_LABELS[kind]}（{counts[kind]} 页，目录 `{TYPE_DIRS[kind]}/`）"]
        for p in groups[kind]:
            upd = str(p.meta.get("updated") or "")[5:]
            lines.append(f"- [[{p.title}]]｜{p.meta.get('summary') or '（无摘要）'}｜{p.meta.get('domain') or '-'}｜{upd}")
        hidden = counts[kind] - len(groups[kind])
        if hidden:
            lines.append(f"- …其余 {hidden} 页见 `{TYPE_DIRS[kind]}/`")
    return "\n".join(lines) + "\n"


def write_index(vault: Path, max_lines: int, stamp: str) -> Path:
    return atomic_write(vault / "hub" / "index.md", render_index(load_pages(vault / "hub"), max_lines, stamp), [vault])
