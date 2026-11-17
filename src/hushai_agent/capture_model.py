"""会话采集的模型判断：把最后一轮对话交给轻档模型（wu 为 sonnet），由它决定记不记、记成哪一句。

模型不可用、预算用完或锁忙时返回 None，由 capture 退回机械采集。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hushai_agent import runner
from hushai_agent.capture import _extract_text_from_content, _is_human_turn
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext

JOB = "capture"
HUMAN_MAX = 4000
ASSISTANT_MAX = 2000
DID_MAX = 60
TODO_MAX = 40
MODEL_TIMEOUT = 60
LOCK_WAIT = 5.0

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "record": {"type": "boolean"},
        "domain": {"type": "string"},
        "did": {"type": "string"},
        "todo": {"type": "string"},
    },
    "required": ["record", "domain", "did", "todo"],
}


def extract_last_exchange(transcript_path: Path) -> tuple[str, str]:
    """返回 (最后一条人类消息全文, 其后 AI 最后一段文字回复)，各自截到上限。"""
    if not transcript_path.is_file():
        return "", ""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", ""
    human, assistant = "", ""
    for raw in lines:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ok, text = _is_human_turn(obj)
        if ok and text:
            human, assistant = text, ""
            continue
        msg = obj.get("message")
        role = msg.get("role") if isinstance(msg, dict) else None
        if (obj.get("type") == "assistant" or role == "assistant") and isinstance(msg, dict):
            txt = _extract_text_from_content(msg.get("content"))
            if txt:
                assistant = txt
    return human[:HUMAN_MAX], assistant[-ASSISTANT_MAX:]


def _clean(text: Any, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).replace("｜", "|").strip()
    return s[:limit]


def make_validator(cfg: InstanceConfig):
    allowed = set(cfg.domains)

    def _validate(data: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(data, dict) or not isinstance(data.get("record"), bool):
            return None, "缺少布尔字段 record"
        domain = str(data.get("domain") or "").strip()
        if domain not in allowed:
            return None, f"领域不在允许列表: {domain!r}"
        did = _clean(data.get("did"), DID_MAX)
        if data["record"] and not did:
            return None, "record 为真但 did 为空"
        todo = _clean(data.get("todo"), TODO_MAX) or "无"
        return {"record": data["record"], "domain": domain, "did": did, "todo": todo}, None

    return _validate


def classify(cfg: InstanceConfig, human: str, assistant: str) -> dict[str, Any] | None:
    """调模型判断；没配 [jobs] capture、失败或锁忙都返回 None。"""
    chain = runner._get_job_chain(cfg, JOB)
    if not chain or not human.strip():
        return None
    tpl = (Path(__file__).resolve().parent / "prompts" / "capture.zh.md").read_text(encoding="utf-8")
    prompt = (
        tpl.replace("{{DOMAINS}}", "、".join(cfg.domains))
        .replace("{{HUMAN}}", human)
        .replace("{{ASSISTANT}}", assistant or "（无）")
    )
    data, _info = runner.run_model(
        cfg, JOB, prompt, SCHEMA, {"job": JOB}, make_validator(cfg),
        timeout=MODEL_TIMEOUT, chain=chain, budget=load_ext(cfg).capture_max_daily, lock_wait=LOCK_WAIT,
    )
    return data
