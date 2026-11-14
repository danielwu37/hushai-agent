"""Second-brain 首次运行 5 问引导（onboarding）与 HERE'S YOUR BRAIN 输出渲染。"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from hushai_agent import clock
from hushai_agent.brain_cfg import load_brain_state, save_brain_state
from hushai_agent.brain_model import (
    BRAIN_SUBDIRS,
    list_brain_markdown_files,
    parse_note_sections,
    read_index_first,
    sanitize_single_line,
)
from hushai_agent.brain_process import process_inbox_to_brain, process_raw_items
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import read_for_prompt

ONBOARD_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("work", "1/5 你平时主要做什么工作或项目？"),
    ("remember", "2/5 你最想更好地记住哪类信息？"),
    ("notes_location", "3/5 你现在的笔记主要保存在哪里？"),
    ("reading_watching", "4/5 你平时主要读/看什么内容（文章、视频、书籍、帖子等）？"),
    ("answer_style", "5/5 你喜欢什么样的回答风格（简短要点、带原句引用还是详细展开）？"),
)

MAX_INITIAL_ITEMS = 10
RETURNING_GREETING = "第二大脑已就绪（跳过首次引导），请直接记录新内容或使用 `hushai brain ask` 提问。"


def render_heres_your_brain(cfg: InstanceConfig, max_notes: int = 5) -> str:
    """生成以 'HERE'S YOUR BRAIN' 开头的结构总览：文件夹地图、index.md 与前 5 条笔记。"""
    hub_dir = cfg.vault / "hub"
    index_text, _ = read_index_first(cfg)

    folder_lines = [
        "hub/",
        "  ├── index.md      （主题索引，每主题一行，不超过 200 行）",
        "  ├── notes/        （加工后的原子小笔记，每条至少 2 条关联链接）",
        "  ├── sources/      （文章/视频/书籍/帖子来源档案与原句）",
        "  ├── people/       （人物档案、对话记录与承诺时间）",
        "  ├── projects/     （项目目标、状态、开放问题、决定与下一步）",
        "  └── daily/        （每日归档与三行日报）",
    ]
    counts = []
    for sub in BRAIN_SUBDIRS:
        sub_dir = hub_dir / sub
        n = len([p for p in sub_dir.glob("*.md") if p.is_file()]) if sub_dir.is_dir() else 0
        counts.append(f"{sub}/={n}")

    note_files = list_brain_markdown_files(cfg, ("notes", "sources", "people", "projects"))[:max_notes]
    notes_blocks: list[str] = []
    if not note_files:
        notes_blocks.append("（暂无笔记，可向收件箱投递最多 10 条内容后自动加工展示）")
    else:
        for idx, p in enumerate(note_files, start=1):
            txt = read_for_prompt(p, cfg.vault, cfg.local_only)
            parsed = parse_note_sections(txt)
            rel = p.relative_to(hub_dir).as_posix()
            title = parsed["title"] or p.stem
            link_targets = [f"{lbl}({tgt})" for lbl, tgt in parsed["links"]]
            conn_str = "，".join(link_targets) if link_targets else "暂无关联"
            notes_blocks.append(f"{idx}. [{title}](hub/{rel}) — 关联：{conn_str}")

    lines = [
        "HERE'S YOUR BRAIN",
        "",
        "## 文件夹地图（Folder Map）",
        *folder_lines,
        f"当前数量：{', '.join(counts)}",
        "",
        "## index.md",
        index_text.strip() or "（索引为空）",
        "",
        "## 前 5 条笔记及关联",
        *notes_blocks,
    ]
    return "\n".join(lines) + "\n"


def run_onboard(
    cfg: InstanceConfig,
    answers: dict[str, str] | None = None,
    initial_items: Iterable[str] | None = None,
    interactive: bool = False,
    prompt_fn: Callable[[str], str] | None = None,
    as_of_date: datetime.date | str | None = None,
) -> dict[str, Any]:
    """执行首次运行 5 问引导；第二天及以后跳过问题，只回一句话开场。"""
    state = load_brain_state(cfg)
    if state.get("onboarded"):
        return {
            "skipped": True,
            "onboarded": True,
            "greeting": RETURNING_GREETING,
            "preferences": state.get("preferences", {}),
            "output_text": RETURNING_GREETING,
        }

    collected: dict[str, str] = {}
    provided = dict(answers or {})
    ask_fn = prompt_fn or (lambda q: input(q + " "))

    for key, question in ONBOARD_QUESTIONS:
        if key in provided and sanitize_single_line(provided[key]):
            collected[key] = sanitize_single_line(provided[key])
        elif interactive and (prompt_fn is not None or sys.stdin.isatty()):
            collected[key] = sanitize_single_line(ask_fn(question), "未填写")
        else:
            collected[key] = sanitize_single_line(provided.get(key, ""), "未填写")

    now_dt = clock.now(cfg)
    date_str = (
        as_of_date.isoformat()
        if isinstance(as_of_date, datetime.date)
        else (str(as_of_date).strip() if as_of_date else now_dt.date().isoformat())
    )

    state["onboarded"] = True
    state["onboarded_at"] = now_dt.isoformat(timespec="seconds")
    state["preferences"] = collected
    save_brain_state(cfg, state)

    processed_summary: dict[str, Any] = {"filed_count": 0, "filed": []}
    raw_list = [sanitize_single_line(x) for x in (initial_items or []) if sanitize_single_line(x)]
    if raw_list:
        limited = raw_list[:MAX_INITIAL_ITEMS]
        raw_objs = [
            {"text": txt, "domain": "学习", "date": date_str, "source_ref": f"onboard-{i+1}"}
            for i, txt in enumerate(limited)
        ]
        processed_summary = process_raw_items(cfg, raw_objs, as_of_date=date_str)
    else:
        # 也检查是否已有未加工收件，最多处理前 10 条
        inbox_res = process_inbox_to_brain(cfg, as_of_date=date_str)
        processed_summary = inbox_res

    output_text = render_heres_your_brain(cfg, max_notes=5)
    state = load_brain_state(cfg)
    state["initial_setup_shown"] = True
    save_brain_state(cfg, state)

    return {
        "skipped": False,
        "onboarded": True,
        "questions_asked": len(ONBOARD_QUESTIONS),
        "preferences": collected,
        "processed_count": processed_summary.get("filed_count", 0),
        "output_text": output_text,
    }
