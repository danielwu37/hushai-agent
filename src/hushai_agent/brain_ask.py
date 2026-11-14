"""Second-brain 只读检索问答（Ask 模式）：只从本人笔记作答，绝不调模型编造。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hushai_agent.brain_model import (
    BRAIN_SUBDIRS,
    list_brain_markdown_files,
    parse_note_sections,
    read_index_first,
    sanitize_single_line,
    tokenize_text,
)
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import read_for_prompt
from hushai_agent.guard import GuardError

NEGATION_WORDS = ("不要", "不应", "不宜", "不可", "不能", "不是", "反对", "放弃", "取消", "停止", "无效", "不适合", "暂停", "not ", "never", "avoid", "stop", "drop")
AFFIRM_WORDS = ("应该", "推荐", "建议", "采用", "选择", "支持", "有效", "适合", "坚持", "决定", "使用", "should", "use", "adopt", "prefer", "recommend")


def _extract_best_quote(text: str, query_tokens: set[str]) -> tuple[str, float]:
    """从笔记正文选出与问题最匹配的原文片段（按句/行返回，不做任何改写）。"""
    best_line = ""
    best_score = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "<!--")):
            continue
        if line in ("## 三行摘要", "## 关键事实", "## 我的看法", "## 关联笔记", "## 5 个核心要点", "## 值得保留的原句"):
            continue
        line_clean = line.lstrip("-*0123456789. ").strip()
        if not line_clean:
            continue
        ltoks = tokenize_text(line_clean)
        overlap = len(query_tokens & ltoks)
        if overlap > 0:
            score = overlap / max(1, len(query_tokens) ** 0.5)
            if score > best_score:
                best_score = score
                best_line = line_clean
    if not best_line:
        for raw in text.splitlines():
            line = raw.strip()
            if line and not line.startswith(("#", ">", "<!--")):
                best_line = line.lstrip("-* ").strip()
                break
    return best_line, best_score


NEG_PHRASE_RE = re.compile(
    r"(?:不要|不应|不宜|不可|不能|不是|反对|放弃|取消|停止|无效|不适合|暂停|not\s+|never\s+|avoid\s+|stop\s+|drop\s+)[^\n，。；;,.]{0,16}",
    re.I,
)


def _polarity(text: str) -> str:
    low = text.lower()
    has_neg = any(w in low for w in NEGATION_WORDS)
    stripped_neg = NEG_PHRASE_RE.sub(" ", low) if has_neg else low
    has_aff = any(w in stripped_neg for w in AFFIRM_WORDS)
    if has_neg and not has_aff:
        return "neg"
    if has_aff and not has_neg:
        return "aff"
    if has_neg:
        return "neg"
    return "neutral"


def _detect_disagreements(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """检查高相关命中笔记之间是否存在观点或事实冲突。"""
    disagreements: list[dict[str, Any]] = []
    for i in range(len(hits)):
        for j in range(i + 1, len(hits)):
            a, b = hits[i], hits[j]
            shared = a["matched_tokens"] & b["matched_tokens"]
            if not shared:
                continue
            pol_a = _polarity(a["quote"] + " " + a["snippet_context"])
            pol_b = _polarity(b["quote"] + " " + b["snippet_context"])
            if (pol_a == "neg" and pol_b == "aff") or (pol_a == "aff" and pol_b == "neg"):
                disagreements.append({
                    "note_a": {"title": a["title"], "path": a["rel_path"], "quote": a["quote"]},
                    "note_b": {"title": b["title"], "path": b["rel_path"], "quote": b["quote"]},
                    "shared_topic": "、".join(sorted(shared)[:4]),
                })
    return disagreements


def _suggest_what_to_save(question: str) -> list[str]:
    q = sanitize_single_line(question)
    suggestions = [
        f"如果这是阅读/视频中的概念，用 `hushai inbox add` 存入带来源链接与原句的资料（会自动建入 `hub/sources/` 与 `hub/notes/`）。",
        f"如果涉及具体项目或待定事项，以 `项目：<项目名>｜{q}` 格式记录进收件箱（会建入 `hub/projects/`）。",
        f"如果与某位联系人有关，以 `人物：<姓名>｜…` 格式记录其背景、沟通内容与承诺（会建入 `hub/people/`）。",
    ]
    return suggestions


def answer_from_notes(
    cfg: InstanceConfig,
    question: str,
    max_hits: int = 5,
) -> dict[str, Any]:
    """仅从允许读取的笔记回答问题，附原句引用与链接；检测冲突或提示未覆盖。"""
    q_clean = sanitize_single_line(question)
    if not q_clean:
        raise ValueError("问题不能为空")

    # 按要求：所有任务开始前先读 index.md
    _, index_entries = read_index_first(cfg)
    query_tokens = tokenize_text(q_clean)
    if not query_tokens:
        query_tokens = {q_clean.lower()}

    candidate_paths: list[Path] = list_brain_markdown_files(cfg, BRAIN_SUBDIRS)
    for extra_rel in ("hub/decisions.md", "hub/lessons.md", "hub/insights.md", "hub/status.md"):
        p = cfg.vault / extra_rel
        if p.is_file():
            candidate_paths.append(p)

    domains_dir = cfg.vault / "domains"
    if domains_dir.is_dir():
        for p in sorted(domains_dir.rglob("*.md")):
            if p.is_file() and p.name != "README.md":
                candidate_paths.append(p)

    # 也把 index.md 中的链接目标文本纳入评分权重参考
    index_titles = {e.target: f"{e.topic} {e.title} {e.brief}" for e in index_entries}

    hits: list[dict[str, Any]] = []
    seen_real: set[Path] = set()
    for path in candidate_paths:
        try:
            real_p = path.resolve()
            if real_p in seen_real:
                continue
            seen_real.add(real_p)
            content = read_for_prompt(path, cfg.vault, cfg.local_only)
        except (GuardError, OSError, UnicodeDecodeError):
            continue

        rel_vault = path.relative_to(cfg.vault).as_posix()
        rel_hub = path.relative_to(cfg.vault / "hub").as_posix() if path.is_relative_to(cfg.vault / "hub") else rel_vault
        parsed = parse_note_sections(content)
        title = parsed["title"] or path.stem
        idx_hint = index_titles.get(rel_hub, "")

        doc_tokens = tokenize_text(f"{title}\n{idx_hint}\n{content}")
        matched = query_tokens & doc_tokens
        if not matched:
            continue

        quote, q_score = _extract_best_quote(content, query_tokens)
        title_bonus = len(query_tokens & tokenize_text(title)) * 1.5
        score = len(matched) + title_bonus + q_score

        # 过滤微弱偶然单字匹配（当问题本身有多个词元时要求至少命中 >= 1 个多字词元或 >= 2 个词元）
        multi_char_matched = [t for t in matched if len(t) >= 2]
        if len(query_tokens) >= 3 and not multi_char_matched and len(matched) < 2:
            continue

        hits.append({
            "title": title,
            "rel_path": rel_vault,
            "link": f"[{title}]({rel_vault})",
            "quote": quote,
            "snippet_context": content[:400],
            "score": round(score, 3),
            "matched_tokens": matched,
            "date": parsed["date"],
        })

    hits.sort(key=lambda h: (-h["score"], h["rel_path"]))
    top_hits = hits[:max_hits]
    disagreements = _detect_disagreements(top_hits) if len(top_hits) >= 2 else []

    if not top_hits:
        suggestions = _suggest_what_to_save(q_clean)
        lines = [
            f"问题：{q_clean}",
            "",
            "你的笔记中暂未覆盖这个问题（未找到相关笔记，不作猜测或编造）。",
            "",
            "建议保存的内容：",
            *[f"- {s}" for s in suggestions],
        ]
        return {
            "question": q_clean,
            "covered": False,
            "hits": [],
            "disagreements": [],
            "suggestions": suggestions,
            "answer_text": "\n".join(lines),
        }

    formatted_hits = [
        {
            "title": h["title"],
            "path": h["rel_path"],
            "link": h["link"],
            "quote": h["quote"],
            "date": h["date"],
            "score": h["score"],
        }
        for h in top_hits
    ]

    lines = [f"问题：{q_clean}", "", "根据你的笔记，找到以下依据："]
    for idx, h in enumerate(formatted_hits, start=1):
        lines.append(f"{idx}. {h['link']}")
        lines.append(f"   > \"{h['quote']}\"")

    if disagreements:
        lines.extend(["", "注意：以下笔记之间存在不同说法或冲突，并列展示供核对："])
        for d in disagreements:
            lines.append(f"- 冲突主题（{d['shared_topic']}）：")
            lines.append(f"  - [{d['note_a']['title']}]({d['note_a']['path']})：\"{d['note_a']['quote']}\"")
            lines.append(f"  - [{d['note_b']['title']}]({d['note_b']['path']})：\"{d['note_b']['quote']}\"")

    return {
        "question": q_clean,
        "covered": True,
        "hits": formatted_hits,
        "disagreements": disagreements,
        "suggestions": [],
        "answer_text": "\n".join(lines),
    }
