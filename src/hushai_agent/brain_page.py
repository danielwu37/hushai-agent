"""第二大脑页面模型：Obsidian 兼容的 Markdown 页（YAML 头 + 正文 + 来源 + 我的笔记）。

一页的三块：
- 正文：AI 维护，编译时整体重写；
- `## 来源`：代码维护，只增不删，保证每条说法都能追到收件行或原件；
- `## 我的笔记`：使用者手写，代码原样保留，AI 永远看不到也改不到。
文件名即标题（Obsidian 的 [[标题]] 按文件名解析），全库唯一。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hushai_agent.fsio import atomic_write

TYPE_DIRS = {"project": "projects", "topic": "notes", "person": "people", "source": "sources"}
TYPE_LABELS = {"project": "项目", "topic": "主题", "person": "人物", "source": "来源"}
LOG_DIR = "daily"
DROP_DIR = "exchange/in/入脑"
REFS_HEADING = "## 来源"
USER_HEADING = "## 我的笔记"
STATUS_SOURCED = "有出处"
STATUS_VERIFIED = "已核实"
TITLE_MAX = 40
LIST_KEYS = ("aliases", "tags")
META_ORDER = ("type", "title", "aliases", "domain", "tags", "summary", "status", "created", "updated", "verified", "locked")

_BAD_TITLE = re.compile(r"[\\/:*?\"<>|#^\[\]{}\r\n\t,，]")
_WIKILINK = re.compile(r"\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")
_NORM_DROP = re.compile(r"[\s\-_·・.。、，,：:（）()《》<>\"'“”‘’!?！？]+")


@dataclass
class Page:
    rel: str
    meta: dict[str, Any]
    body: str = ""
    refs: list[str] = field(default_factory=list)
    user: str = ""

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or Path(self.rel).stem)

    @property
    def kind(self) -> str:
        return str(self.meta.get("type") or "")

    def names(self) -> list[str]:
        """标题、别名与文件名，用于按名找页和解析 [[链接]]。"""
        return [self.title, Path(self.rel).stem, *[str(a) for a in self.meta.get("aliases") or []]]


def clean_title(raw: Any) -> str:
    """去掉文件名与 Obsidian 链接里不能用的字符，压空白，截到 40 字。"""
    text = _BAD_TITLE.sub(" ", str(raw or ""))
    text = re.sub(r"\s+", " ", text).strip().lstrip(".")
    return text[:TITLE_MAX].strip()


def norm(name: str) -> str:
    """按名查页的归一键：小写、去空白与常见标点。"""
    return _NORM_DROP.sub("", str(name or "").lower())


def wikilinks(text: str) -> list[str]:
    """正文里的 [[目标]]（去掉 #小节 与 |显示名）。"""
    return [m.strip() for m in _WIKILINK.findall(text or "")]


def _parse_value(key: str, raw: str) -> Any:
    raw = raw.strip()
    if key in LIST_KEYS:
        inner = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
        return [x.strip().strip("\"'") for x in inner.split(",") if x.strip().strip("\"'")]
    if key == "locked":
        return raw.lower() in ("true", "yes", "1")
    return raw.strip("\"'")


def parse_page(rel: str, text: str) -> Page:
    """解析一页；没有 YAML 头的当作整页正文。"""
    meta: dict[str, Any] = {}
    rest = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if ":" in line and not line.startswith((" ", "#")):
                    key, val = line.split(":", 1)
                    meta[key.strip()] = _parse_value(key.strip(), val)
            rest = text[end + 4:].lstrip("\n")
    user = ""
    lines = rest.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == USER_HEADING:
            user = "\n".join(lines[i + 1:]).strip("\n")
            lines = lines[:i]
            break
    refs: list[str] = []
    for i, line in enumerate(lines):
        if line.strip() == REFS_HEADING:
            tail = lines[i + 1:]
            refs = [t[2:].strip() for t in tail if t.startswith("- ")]
            lines = lines[:i]
            break
    return Page(rel=rel, meta=meta, body="\n".join(lines).strip(), refs=refs, user=user)


def _fmt_value(val: Any) -> str:
    if isinstance(val, list):
        return "[" + ", ".join(clean_title(v) for v in val if clean_title(v)) + "]"
    if isinstance(val, bool):
        return "true" if val else "false"
    return re.sub(r"\s+", " ", str(val)).strip()


def render_page(page: Page) -> str:
    """按固定键序写 YAML 头，再写正文、来源、我的笔记。"""
    keys = [k for k in META_ORDER if k in page.meta] + sorted(k for k in page.meta if k not in META_ORDER)
    head = ["---", *[f"{k}: {_fmt_value(page.meta[k])}" for k in keys], "---", ""]
    parts = ["\n".join(head), page.body.strip(), "", REFS_HEADING, *[f"- {r}" for r in page.refs], "", USER_HEADING]
    out = "\n".join(parts) + "\n"
    if page.user.strip():
        out += "\n" + page.user.strip("\n") + "\n"
    return out


def load_pages(hub: Path) -> list[Page]:
    """读出四个页面目录下的全部 .md（不递归，读不了的跳过）。"""
    pages: list[Page] = []
    for kind, sub in TYPE_DIRS.items():
        folder = hub / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            try:
                page = parse_page(f"{sub}/{path.name}", path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            page.meta.setdefault("type", kind)
            pages.append(page)
    return pages


def name_map(pages: list[Page]) -> dict[str, Page]:
    """归一名 -> 页；同名时先到先得（重名由 lint 报出）。"""
    out: dict[str, Page] = {}
    for page in pages:
        for name in page.names():
            key = norm(name)
            if key and key not in out:
                out[key] = page
    return out


def write_page(vault: Path, page: Page) -> Path:
    """原子写回 hub/<rel>。"""
    return atomic_write(vault / "hub" / page.rel, render_page(page), [vault])
