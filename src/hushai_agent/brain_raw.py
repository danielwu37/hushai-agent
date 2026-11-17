"""第二大脑的原料层（卡帕西 LLM Wiki 的 raw）：只收显式交给它的东西，原件只读。

两种原料：
1. 收件行：「做了什么」以 `笔记：`、`来源：`、`人物：`、`项目：`、`入脑：` 开头，日期不早于 [brain] since；
2. 投放文件：`hub/exchange/in/入脑/` 顶层的 .md / .txt，以及各端交换区 `hub/exchange/in/<端>/入脑-*.md`
   （Muse 经闸门 put-exchange 存的文件），≤64 KB，按内容哈希记账，改了会重编。
隔离领域（健康等）的行、heal 端的行、local_only 路径下的文件一律不收。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hushai_agent.brain_page import DROP_DIR
from hushai_agent.config import InstanceConfig, is_local_only_domain
from hushai_agent.fsio import read_for_prompt
from hushai_agent.inboxscan import InboxRecord, iter_inbox_lines

PREFIX_RE = re.compile(r"^(笔记|来源|人物|项目|入脑)\s*[:：]\s*")
LINE_BATCH = 15
FILE_MAX_BYTES = 64 * 1024
FILE_CHARS = 12000
FILE_SUFFIXES = (".md", ".txt")
SKIP_DOORS = ("heal",)


@dataclass
class RawBatch:
    """一次模型调用的原料。refs 是写进页面「来源」小节的引用。"""

    kind: str
    text: str
    refs: list[str]
    line_ids: list[str] = field(default_factory=list)
    file_rel: str = ""
    file_sha: str = ""
    file_name: str = ""


def line_ref(rec: InboxRecord) -> str:
    door = f"{rec.door}-auto" if rec.auto else rec.door
    return f"收件 {rec.date} {door} {rec.time_str}：{rec.did[:80]}"


def pending_lines(cfg: InstanceConfig, state: dict[str, Any], since: str) -> list[InboxRecord]:
    """带入脑前缀、未编译过、不在隔离领域的收件行（按日期先后）。"""
    done = set(state.get("done_lines") or [])
    out = []
    for rec in iter_inbox_lines(cfg.vault / "hub" / "inbox"):
        if since and str(rec.date) < since:
            continue
        if rec.line_id in done or rec.door in SKIP_DOORS or is_local_only_domain(rec.domain, cfg):
            continue
        if PREFIX_RE.match(rec.did.strip()):
            out.append(rec)
    return out


def _drop_candidates(hub: Path) -> list[Path]:
    folder = hub / DROP_DIR
    out = sorted(folder.iterdir()) if folder.is_dir() else []
    exchange_in = hub / "exchange" / "in"
    if exchange_in.is_dir():
        for door_dir in sorted(exchange_in.iterdir()):
            if door_dir.is_dir() and door_dir != folder and door_dir.name not in ("legacy", "_已处理"):
                out += sorted(door_dir.glob("入脑-*"))
    return out


def pending_files(cfg: InstanceConfig, state: dict[str, Any]) -> tuple[list[tuple[str, str, Path]], list[str]]:
    """返回 ([(相对 hub 的路径, sha256, 绝对路径)], [跳过原因])。"""
    hub = cfg.vault / "hub"
    done = state.get("done_files") or {}
    todo: list[tuple[str, str, Path]] = []
    skipped: list[str] = []
    for path in _drop_candidates(hub):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in FILE_SUFFIXES:
            continue
        rel = path.relative_to(hub).as_posix()
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > FILE_MAX_BYTES:
            skipped.append(f"{rel}：超过 64 KB，请拆分后再放")
            continue
        sha = hashlib.sha256(data).hexdigest()
        if done.get(rel) != sha:
            todo.append((rel, sha, path))
    return todo, skipped


def build_batches(cfg: InstanceConfig, state: dict[str, Any], since: str) -> tuple[list[RawBatch], list[str]]:
    """把待编译原料切成批：收件行每 15 行一批，每个文件单独一批。"""
    batches: list[RawBatch] = []
    lines = pending_lines(cfg, state, since)
    for start in range(0, len(lines), LINE_BATCH):
        chunk = lines[start:start + LINE_BATCH]
        text = "\n".join(
            f"[{i + 1}] {r.date} {r.time_str}｜{r.domain}｜{r.did}｜待办：{r.todo}" for i, r in enumerate(chunk)
        )
        batches.append(RawBatch("lines", text, [line_ref(r) for r in chunk], [r.line_id for r in chunk]))
    files, skipped = pending_files(cfg, state)
    for rel, sha, path in files:
        try:
            text = read_for_prompt(path, cfg.vault, cfg.local_only)
        except (OSError, UnicodeDecodeError, ValueError, PermissionError) as exc:
            skipped.append(f"{rel}：读取被拒或失败（{type(exc).__name__}）")
            continue
        if len(text) > FILE_CHARS:
            text = text[:FILE_CHARS] + f"\n\n（原文共 {len(text)} 字，此处只给前 {FILE_CHARS} 字）"
        batches.append(RawBatch("file", text, [f"原件 {rel}"], file_rel=rel, file_sha=sha, file_name=path.stem))
    return batches, skipped
