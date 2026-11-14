"""会话结束自动收件采集（hushai capture）：从钩子标准输入读取会话 JSONL，纯机械提取最后一条人类消息前 80 字写入 -auto.md。"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent import clock
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.fsio import atomic_write
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.vault import add_inbox_entry

THROTTLE_SECONDS = 600
MAX_CHARS = 80


def _extract_text_from_content(content: Any) -> str:
    """从消息 content（字符串或块列表）中提取纯文本，忽略 tool_result 等非文本块。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str) and block.strip():
                parts.append(block.strip())
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype in ("text", "input_text", None) and isinstance(block.get("text"), str):
                    txt = block["text"].strip()
                    if txt:
                        parts.append(txt)
        return " ".join(parts).strip()
    return ""


def _is_human_turn(obj: dict[str, Any]) -> tuple[bool, str]:
    """判断单行 JSONL 是否为人类发出的消息，并返回其文本内容。"""
    if obj.get("isMeta") or obj.get("isSidechain"):
        return False, ""
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False, ""

    top_type = obj.get("type") or obj.get("role")
    msg = obj.get("message")
    msg_role = msg.get("role") if isinstance(msg, dict) else None
    if top_type not in ("user", "human") and msg_role not in ("user", "human"):
        return False, ""

    raw_content = msg.get("content") if isinstance(msg, dict) and "content" in msg else obj.get("content")
    if raw_content is None and isinstance(obj.get("text"), str):
        raw_content = obj["text"]

    text = _extract_text_from_content(raw_content)
    if not text or text.startswith(("<command-", "<local-command-", "<system-reminder>")):
        return False, ""
    return True, text


def extract_last_human_snippet(transcript_path: Path, max_chars: int = MAX_CHARS) -> str:
    """读取会话记录文件（JSONL），返回最后一条人类消息的前 max_chars 个字符（单行）。"""
    if not transcript_path.is_file():
        return ""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    last_text = ""
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ok, text = _is_human_turn(obj)
        if ok and text:
            last_text = text

    if not last_text:
        return ""
    compact = re.sub(r"\s+", " ", last_text).replace("｜", "|").strip()
    return compact[:max_chars]


def _load_capture_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "capture-state.json"
    if not path.is_file():
        return {"sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"sessions": {}}


def _save_capture_state(state_dir: Path, data: dict[str, Any]) -> None:
    path = state_dir / "capture-state.json"
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", [state_dir])


def _parse_iso(ts_str: str, tz: ZoneInfo) -> datetime.datetime | None:
    if not ts_str:
        return None
    norm = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
    try:
        dt = datetime.datetime.fromisoformat(norm)
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def run_capture(
    cfg: InstanceConfig | str,
    door: str | None = None,
    stdin_text: str | None = None,
) -> dict[str, Any]:
    """执行一次会话结束自动采集：读取钩子 JSON、检查 10 分钟节流、写入 <日期>-<door>-auto.md。"""
    inst_cfg = cfg if isinstance(cfg, InstanceConfig) else load_instance_config(cfg)
    target_door = (door or inst_cfg.steward_door).strip()
    raw_in = stdin_text if stdin_text is not None else sys.stdin.read()
    if not raw_in or not raw_in.strip():
        return {"ok": True, "written": False, "reason": "empty-stdin"}

    try:
        payload = json.loads(raw_in)
    except json.JSONDecodeError:
        return {"ok": False, "written": False, "reason": "invalid-json"}
    if not isinstance(payload, dict):
        return {"ok": False, "written": False, "reason": "invalid-payload"}

    session_id = str(payload.get("session_id") or "").strip() or "default"
    transcript_raw = str(payload.get("transcript_path") or "").strip()
    if not transcript_raw:
        return {"ok": True, "written": False, "reason": "missing-transcript-path", "session_id": session_id}

    tpath = Path(transcript_raw).expanduser()
    snippet = extract_last_human_snippet(tpath, max_chars=MAX_CHARS)
    if not snippet:
        return {"ok": True, "written": False, "reason": "no-human-message", "session_id": session_id}

    state_dir = get_instance_state_dir(inst_cfg.name)
    state = _load_capture_state(state_dir)
    sessions = state.setdefault("sessions", {})
    now_dt = clock.now(inst_cfg)
    tz = ZoneInfo(inst_cfg.timezone)

    prev = sessions.get(session_id)
    if isinstance(prev, dict):
        last_dt = _parse_iso(str(prev.get("last_ts") or ""), tz)
        if last_dt is not None:
            elapsed = (now_dt - last_dt).total_seconds()
            if 0 <= elapsed < THROTTLE_SECONDS:
                return {
                    "ok": True,
                    "written": False,
                    "reason": "throttled",
                    "session_id": session_id,
                    "elapsed_seconds": round(elapsed, 1),
                }

    domain = inst_cfg.domains[0]
    inbox_path = add_inbox_entry(
        instance=inst_cfg.name,
        door=target_door,
        domain=domain,
        did=snippet,
        is_auto=True,
    )
    sessions[session_id] = {
        "last_ts": now_dt.isoformat(),
        "last_text": snippet,
        "door": target_door,
    }
    _save_capture_state(state_dir, state)
    return {
        "ok": True,
        "written": True,
        "session_id": session_id,
        "door": target_door,
        "domain": domain,
        "snippet": snippet,
        "inbox_file": str(inbox_path),
    }
