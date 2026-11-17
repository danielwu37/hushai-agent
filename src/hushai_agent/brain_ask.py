"""第二大脑检索（卡帕西 LLM Wiki 的 query 前半段）：纯本地打分，不调模型。

返回最相关的几页（路径、摘要、命中原句）和它们链出去的相关页；
由正在对话的 AI 读页作答。值得留下的答案写成收件行「笔记：……」，下次编译回填进页面。
"""

from __future__ import annotations

import datetime
import hashlib
import re
from typing import Any

from hushai_agent.brain_page import TYPE_LABELS, Page, load_pages, name_map, norm, wikilinks
from hushai_agent.config import InstanceConfig, is_local_only_domain

REVISIT_MIN_DAYS = 14
STOP = set("的了吗呢吧啊是在和与及或我你他她它们这那个些什么怎么如何哪为何请问一下有没有关于现在目前")
EXCERPT_MAX = 120


def tokens(question: str) -> list[str]:
    """英文和数字按词，中文按相邻两字（去掉虚词）。"""
    q = question.lower()
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9._-]+", q)]
    for run in re.findall(r"[一-鿿]+", q):
        run = "".join(ch for ch in run if ch not in STOP)
        words += [run[i:i + 2] for i in range(len(run) - 1)] or ([run] if run else [])
    return list(dict.fromkeys(words))


def _score(page: Page, toks: list[str], qnorm: str) -> int:
    names = " ".join(page.names()).lower()
    tags = " ".join(page.meta.get("tags") or []).lower()
    summary = str(page.meta.get("summary") or "").lower()
    body = page.body.lower()
    score = sum(20 for n in page.names() if len(norm(n)) >= 2 and norm(n) in qnorm)
    for t in toks:
        score += 5 * (t in names) + 3 * (t in tags) + 2 * (t in summary) + min(body.count(t), 3)
    return score


def _excerpts(page: Page, toks: list[str]) -> list[str]:
    ranked = []
    for line in page.body.splitlines():
        hits = sum(1 for t in toks if t in line.lower())
        if hits and line.strip():
            ranked.append((hits, line.strip()[:EXCERPT_MAX]))
    return [text for _, text in sorted(ranked, key=lambda t: -t[0])[:3]]


def ask(cfg: InstanceConfig, question: str, limit: int = 5) -> dict[str, Any]:
    pages = load_pages(cfg.vault / "hub")
    toks, qnorm = tokens(question), norm(question)
    ranked = sorted(((s, p) for p in pages if (s := _score(p, toks, qnorm)) > 0), key=lambda t: (-t[0], t[1].title))
    top = [p for _, p in ranked[:max(1, limit)]]
    names = name_map(pages)
    seen = {p.rel for p in top}
    related: list[str] = []
    for page in top:
        for target in wikilinks(page.body):
            hit = names.get(norm(target))
            if hit is not None and hit.rel not in seen:
                seen.add(hit.rel)
                related.append(f"{hit.title}（hub/{hit.rel}）")
    hits = [{
        "title": p.title, "path": f"hub/{p.rel}", "type": TYPE_LABELS.get(p.kind, p.kind),
        "summary": p.meta.get("summary") or "", "status": p.meta.get("status") or "",
        "updated": p.meta.get("updated") or "", "excerpts": _excerpts(p, toks),
    } for p in top]
    return {"question": question, "hits": hits, "related": related[:5]}


def render_text(res: dict[str, Any]) -> str:
    if not res["hits"]:
        return f"第二大脑里没有找到与「{res['question']}」相关的页面（状态未核实）。"
    lines = [f"与「{res['question']}」最相关的页面（请打开原页再作答，不要只凭摘要）："]
    for i, h in enumerate(res["hits"], 1):
        lines.append(f"{i}. {h['title']}｜{h['type']}｜{h['status']}｜更新 {h['updated']}｜{h['path']}")
        lines.append(f"   {h['summary']}")
        lines += [f"   > {x}" for x in h["excerpts"]]
    if res["related"]:
        lines.append("相关：" + "；".join(res["related"]))
    return "\n".join(lines)


def brief_section(cfg: InstanceConfig, today: datetime.date) -> dict[str, Any] | None:
    """晨间简报用：昨天以来新建或更新的页，加一页按日期轮换的旧页重温。没有页面时返回 None。

    简报会同步到网盘，隔离领域的页一律不出现。
    """
    pages = [p for p in load_pages(cfg.vault / "hub")
             if p.kind != "source" and not is_local_only_domain(str(p.meta.get("domain") or ""), cfg)]
    if not pages:
        return None
    since = (today - datetime.timedelta(days=1)).isoformat()
    fresh = [p.title for p in sorted(pages, key=lambda p: str(p.meta.get("updated") or ""), reverse=True)
             if str(p.meta.get("updated") or "") >= since][:5]
    old_cut = (today - datetime.timedelta(days=REVISIT_MIN_DAYS)).isoformat()
    old = sorted((p for p in pages if "" < str(p.meta.get("updated") or "") < old_cut), key=lambda p: p.rel)
    revisit = None
    if old:
        pick = old[int(hashlib.sha256(today.isoformat().encode()).hexdigest(), 16) % len(old)]
        revisit = {"title": pick.title, "summary": pick.meta.get("summary") or "", "path": f"hub/{pick.rel}"}
    if not fresh and revisit is None:
        return None
    return {"fresh": fresh, "revisit": revisit}
