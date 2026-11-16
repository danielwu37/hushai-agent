"""Second-brain Markdown 文档数据模型、目录安全与 index.md 读写操作。"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BRAIN_SUBDIRS = ("notes", "sources", "people", "projects", "daily")
UNVERIFIED = "UNVERIFIED"

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def sanitize_single_line(text: str, default: str = "") -> str:
    """清理单行文本，去掉换行与全角竖线，防止破坏结构。"""
    cleaned = str(text or "").replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
    return cleaned or default


def sanitize_block(text: str) -> str:
    """清理多行数据文本（去掉空字节，保留可读文本）。"""
    return str(text or "").replace("\x00", "").strip()


def slugify(text: str, prefix: str = "item") -> str:
    """将标题转为安全的文件名 stem（支持中文与 ASCII，绝不含路径分隔符或 '..'）。"""
    raw = str(text or "").strip().lower()
    out_chars: list[str] = []
    prev_dash = False
    for ch in raw:
        if ("\u4e00" <= ch <= "\u9fff") or ch.isalnum():
            out_chars.append(ch)
            prev_dash = False
        elif not prev_dash:
            out_chars.append("-")
            prev_dash = True
    slug = "".join(out_chars).strip("-")
    if not slug or slug in (".", ".."):
        digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:8]
        slug = f"{prefix}-{digest}"
    if len(slug) > 64:
        digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:6]
        slug = f"{slug[:56].rstrip('-')}-{digest}"
    return slug


def allocate_unique_path(directory: Path, stem: str) -> Path:
    """在 directory 下分配不覆盖已有文件的 .md 路径。"""
    candidate = directory / f"{stem}.md"
    if not candidate.exists():
        return candidate
    idx = 2
    while True:
        cand = directory / f"{stem}-{idx}.md"
        if not cand.exists():
            return cand
        idx += 1


def extract_md_links(text: str) -> list[tuple[str, str]]:
    """提取正文中的 [label](target) 与 [[target]] 链接，返回 [(label, target)]。"""
    links: list[tuple[str, str]] = []
    for label, target in MD_LINK_RE.findall(text):
        t = target.strip()
        if t and not t.startswith(("http://", "https://", "mailto:", "#")):
            links.append((label.strip(), t))
    for target in WIKI_LINK_RE.findall(text):
        t = target.strip()
        if t:
            links.append((t, t if t.endswith(".md") else f"{t}.md"))
    return links


def relative_link(from_file: Path, to_file: Path) -> str:
    """计算从 from_file 所在目录到 to_file 的 POSIX 相对路径。"""
    rel = os.path.relpath(os.fspath(to_file), os.fspath(from_file.parent))
    return Path(rel).as_posix()


@dataclass
class NoteDoc:
    """hub/notes/<slug>.md 原子笔记结构。"""

    title: str
    date: str
    source: str
    status: str = "有出处"
    summary: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    my_take: str = UNVERIFIED
    links: list[tuple[str, str]] = field(default_factory=list)
    rel_path: str = ""
    topic: str = "未分类"

    def render(self, hub_dir: Path) -> str:
        note_path = hub_dir / self.rel_path
        sum_lines = [sanitize_single_line(s) for s in self.summary if sanitize_single_line(s)][:3]
        while len(sum_lines) < 1:
            sum_lines.append(UNVERIFIED)
        fact_lines = [sanitize_single_line(f) for f in self.facts if sanitize_single_line(f)] or [UNVERIFIED]
        take = sanitize_block(self.my_take) or UNVERIFIED
        lines = [
            f"# {sanitize_single_line(self.title, '未命名笔记')}",
            "",
            f"- 日期：{sanitize_single_line(self.date, UNVERIFIED)}",
            f"- 主题：{sanitize_single_line(self.topic, '未分类')}",
            f"- 状态：{sanitize_single_line(self.status, UNVERIFIED)}",
            f"- 来源：{sanitize_single_line(self.source, UNVERIFIED)}",
            "",
            "## 三行摘要",
            "",
            *[f"{i}. {ln}" for i, ln in enumerate(sum_lines[:3], start=1)],
            "",
            "## 关键事实",
            "",
            *[f"- {f}" for f in fact_lines],
            "",
            "## 我的看法",
            "",
            take,
            "",
            "## 关联笔记",
            "",
        ]
        if self.links:
            for target_rel, reason in self.links:
                href = relative_link(note_path, hub_dir / target_rel) if target_rel else target_rel
                lines.append(f"- [{Path(target_rel).stem}]({href}) — {sanitize_single_line(reason, '相关主题关联')}")
        else:
            lines.append("- （待补关联链接，需至少 2 条）")
        lines.append("")
        return "\n".join(lines)


@dataclass
class SourceDoc:
    """hub/sources/<slug>.md 来源档案结构。"""

    title: str
    author: str = UNVERIFIED
    date: str = UNVERIFIED
    link: str = UNVERIFIED
    points: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)
    rel_path: str = ""
    source: str = UNVERIFIED

    def render(self) -> str:
        pts = [sanitize_single_line(p) for p in self.points if sanitize_single_line(p)][:5] or [UNVERIFIED]
        qts = [sanitize_single_line(q) for q in self.quotes if sanitize_single_line(q)] or [
            f"{UNVERIFIED}（未提供带时间戳或页码的原句引用）"
        ]
        lines = [
            f"# {sanitize_single_line(self.title, '未命名来源')}",
            "",
            f"- 作者：{sanitize_single_line(self.author, UNVERIFIED)}",
            f"- 日期：{sanitize_single_line(self.date, UNVERIFIED)}",
            f"- 链接：{sanitize_single_line(self.link, UNVERIFIED)}",
        ]
        src_clean = sanitize_single_line(self.source, UNVERIFIED)
        if src_clean and src_clean != UNVERIFIED:
            lines.append(f"- 来源：{src_clean}")
        lines.extend([
            "", "## 5 个核心要点", "", *[f"{i}. {p}" for i, p in enumerate(pts, start=1)],
            "", "## 值得保留的原句", "", *[f"> {q}" for q in qts], "",
        ])
        return "\n".join(lines)


def _format_sources_line(sources: list[str]) -> str | None:
    cleaned = [sanitize_single_line(s) for s in sources if sanitize_single_line(s) and sanitize_single_line(s) != UNVERIFIED]
    return f"- 来源：{', '.join(dict.fromkeys(cleaned))}" if cleaned else None


@dataclass
class PersonDoc:
    """hub/people/<slug>.md 人物档案结构。"""

    name: str
    who: str = UNVERIFIED
    how_met: str = UNVERIFIED
    talked_about: list[str] = field(default_factory=list)
    promises: list[str] = field(default_factory=list)
    cares_about: list[str] = field(default_factory=list)
    rel_path: str = ""
    sources: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"# {sanitize_single_line(self.name, '未命名联系人')}", ""]
        if src_line := _format_sources_line(self.sources):
            lines.extend([src_line, ""])
        lines.extend([
            "## 是谁", "", sanitize_block(self.who) or UNVERIFIED,
            "", "## 怎么认识的", "", sanitize_block(self.how_met) or UNVERIFIED,
            "", "## 聊过什么", "", *([f"- {sanitize_single_line(x)}" for x in self.talked_about] or ["- （暂无记录）"]),
            "", "## 我承诺过什么（及时间）", "", *([f"- {sanitize_single_line(x)}" for x in self.promises] or ["- （暂无承诺）"]),
            "", "## 对方关心什么", "", *([f"- {sanitize_single_line(x)}" for x in self.cares_about] or [f"- {UNVERIFIED}"]),
            "",
        ])
        return "\n".join(lines)


@dataclass
class ProjectDoc:
    """hub/projects/<slug>.md 项目笔记结构。"""

    name: str
    goal: str = UNVERIFIED
    status: str = "进行中"
    open_questions: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    next_step: str = UNVERIFIED
    links: list[tuple[str, str]] = field(default_factory=list)
    rel_path: str = ""
    sources: list[str] = field(default_factory=list)

    def render(self, hub_dir: Path) -> str:
        proj_path = hub_dir / self.rel_path
        lines = [f"# {sanitize_single_line(self.name, '未命名项目')}", "", f"- 状态：{sanitize_single_line(self.status, UNVERIFIED)}"]
        if src_line := _format_sources_line(self.sources):
            lines.append(src_line)
        lines.extend([
            "", "## 目标", "", sanitize_block(self.goal) or UNVERIFIED,
            "", "## 开放问题", "", *([f"- {sanitize_single_line(q)}" for q in self.open_questions] or ["- （暂无开放问题）"]),
            "", "## 已做决定及原因", "", *([f"- {sanitize_single_line(d)}" for d in self.decisions] or ["- （暂无记录）"]),
            "", "## 下一步", "", sanitize_block(self.next_step) or UNVERIFIED,
            "", "## 相关笔记链接", "",
        ])
        if self.links:
            for target_rel, reason in self.links:
                href = relative_link(proj_path, hub_dir / target_rel)
                lines.append(f"- [{Path(target_rel).stem}]({href}) — {sanitize_single_line(reason, '相关笔记')}")
        else:
            lines.append("- （暂无相关链接）")
        lines.append("")
        return "\n".join(lines)
