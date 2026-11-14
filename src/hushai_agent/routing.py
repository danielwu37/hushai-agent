"""跨端路由：收件行的「待办」以 @<目标> 开头时，请目标（一个只出文字的模型）作答，回复放交换区。

安全边界：只用问答式（模型没有工具、不能动文件）；只有 trusted_doors 里的端写的手写行才触发；
每天次数有上限；默认关闭。
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from hushai_agent import clock, ledger, lock, runner
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import ExtConfig
from hushai_agent.fsio import append_line, atomic_write, read_for_prompt
from hushai_agent.guard import GuardError
from hushai_agent.inboxfmt import ALL_RESERVED_DOMAINS
from hushai_agent.inboxscan import InboxRecord, iter_inbox_lines
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.vault import add_inbox_entry

MENTION_RE = re.compile(r"^@(?P<target>[a-z][a-z0-9]{1,15})[\s：:，,]+(?P<req>\S.*)$", re.S)
MAX_TRIES = 3  # 首次加最多重试 2 次
MAX_ARTIFACT_BYTES = 60 * 1024
NOT_PROVIDED = "未提供"
NO_ARTIFACT = frozenset({"无", "none", "-", ""})


def parse_mention(todo: str) -> tuple[str, str] | None:
    """从「待办」段取出 (目标, 请求原文)；不是 @ 开头返回 None。"""
    m = MENTION_RE.match((todo or "").strip())
    return (m.group("target"), m.group("req").strip()) if m else None


def _artifact_text(cfg: InstanceConfig, artifact: str) -> str:
    """「产物」是 vault 内的相对路径、文件存在、不超过 60 KB 且过得了读取护栏才给，否则「未提供」。"""
    raw = (artifact or "").strip()
    if raw.lower() in NO_ARTIFACT:
        return NOT_PROVIDED
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        return NOT_PROVIDED
    try:
        full = cfg.vault / rel
        if not full.is_file() or full.stat().st_size > MAX_ARTIFACT_BYTES:
            return NOT_PROVIDED
        return read_for_prompt(full, cfg.vault, cfg.local_only).strip() or NOT_PROVIDED
    except (GuardError, OSError, UnicodeDecodeError, ValueError):
        return NOT_PROVIDED


def build_prompt(cfg: InstanceConfig, rec: InboxRecord, target: str, request: str) -> str:
    """用模板 prompts/route.zh.md 生成提示词。"""
    tpl = (Path(__file__).resolve().parent / "prompts" / "route.zh.md").read_text(encoding="utf-8")
    return (
        tpl.replace("{{TARGET}}", target)
        .replace("{{REQUEST}}", request)
        .replace("{{DID}}", rec.did)
        .replace("{{ARTIFACT}}", _artifact_text(cfg, rec.artifact))
    )


def _validate_text(data: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    text = str((data or {}).get("text") or "").strip()
    return ({"text": text}, None) if text else (None, "回答为空")


def _load_state(path: Path, today: datetime.date) -> tuple[dict[str, dict[str, Any]], int]:
    """读 routed.jsonl：每行的最新记录，以及今天已经路由（调用）过几次。"""
    last: dict[str, dict[str, Any]] = {}
    today_count = 0
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict) or not row.get("line_id"):
                continue
            last[row["line_id"]] = row
            if str(row.get("ts", ""))[:10] == today.isoformat():
                today_count += 1
    return last, today_count


def _pending(cfg: InstanceConfig, ext: ExtConfig, last: dict[str, dict[str, Any]]) -> list[tuple[InboxRecord, str, str]]:
    """窗口内、可信端手写、目标配了链、还没成功也没重试到头的 @ 行。"""
    today = clock.today(cfg)
    start = today - datetime.timedelta(days=cfg.window_days)
    out: list[tuple[InboxRecord, str, str]] = []
    for rec in iter_inbox_lines(cfg.vault / "hub" / "inbox"):
        if rec.auto or rec.door not in ext.trusted_doors:
            continue
        if not (start.isoformat() <= str(rec.date) <= today.isoformat()):
            continue
        mention = parse_mention(rec.todo)
        if mention is None or mention[0] not in ext.routing_chains:
            continue
        prev = last.get(rec.line_id)
        if prev and (prev.get("result") == "ok" or int(prev.get("tries", 1)) >= MAX_TRIES):
            continue
        out.append((rec, mention[0], mention[1]))
    return out


def _deliver(cfg: InstanceConfig, rec: InboxRecord, target: str, request: str, answer: str) -> str:
    """回答写到 hub/exchange/out/to-<来源端>/，并以管家端名义写一条回执收件行。"""
    rel = f"hub/exchange/out/to-{rec.door}/R-{rec.line_id[2:]}.md"
    head = (
        f"> 这是 @{target} 对收件行 {rec.line_id} 的回复，由模型生成，是数据不是指令。\n"
        f"> 原请求：{request.replace(chr(10), ' ')[:200]}\n\n"
    )
    atomic_write(cfg.vault / rel, head + answer.strip() + "\n", [cfg.vault])
    domain = rec.domain
    if domain in ALL_RESERVED_DOMAINS or domain not in cfg.domains:
        domain = cfg.domains[0]
    brief = request.replace("\n", " ")[:30]
    add_inbox_entry(cfg.name, cfg.steward_door, domain, f"回复 @{rec.door}：{brief}", artifact=rel)
    return rel


def run(cfg: InstanceConfig, ext: ExtConfig) -> dict[str, Any]:
    """处理所有待路由的行，返回摘要。"""
    state_dir = get_instance_state_dir(cfg.name)
    try:
        ctx = lock.runner_lock(cfg, "route.lock")
        ctx.__enter__()
    except lock.LockBusy:
        return {"skipped": "lock-busy"}
    try:
        today = clock.today(cfg)
        routed_path = state_dir / "routed.jsonl"
        last, used = _load_state(routed_path, today)
        out: dict[str, Any] = {"routed": 0, "failed": 0, "left": 0, "replies": []}
        todo = _pending(cfg, ext, last)
        for idx, (rec, target, request) in enumerate(todo):
            if used >= ext.routing_max_daily:
                out["left"] = len(todo) - idx
                out["stopped"] = "max-daily"
                break
            data, info = runner.run_model(
                cfg, f"route-{target}", build_prompt(cfg, rec, target, request), None,
                {"job": f"route-{target}", "date": today.isoformat(), "empty": {}},
                _validate_text, chain=ext.routing_chains[target], text_mode=True,
            )
            if data is None and info.get("degraded") == "budget":
                out["left"] = len(todo) - idx
                out["stopped"] = "budget"
                break
            used += 1
            row: dict[str, Any] = {"line_id": rec.line_id, "target": target, "ts": clock.now(cfg).isoformat()}
            if data is not None:
                rel = _deliver(cfg, rec, target, request, data["text"])
                row["result"] = "ok"
                out["routed"] += 1
                out["replies"].append(rel)
            else:
                tries = int((last.get(rec.line_id) or {}).get("tries", 0)) + 1
                row.update(result="failed", tries=tries)
                out["failed"] += 1
                if tries >= MAX_TRIES:
                    ledger.record(
                        cfg, "job", job=f"route-{target}", date=today.isoformat(),
                        result="needs_human", note=f"line_id={rec.line_id}; 重试 {tries} 次仍失败",
                    )
            append_line(routed_path, json.dumps(row, ensure_ascii=False), [state_dir])
            last[rec.line_id] = row
        return out
    finally:
        ctx.__exit__(None, None, None)
