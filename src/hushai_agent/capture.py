"""会话结束自动收件采集（hushai capture）：配了 [jobs] capture 时由轻档模型理解最后一轮对话再决定记不记、怎么记；
模型不可用时退回机械采集（最后一条人类消息前 120 字 + 维护特征词过滤）。"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent import clock, ledger
from hushai_agent.config import InstanceConfig, load_instance_config
from hushai_agent.config_ext import load_ext
from hushai_agent.fsio import atomic_write
from hushai_agent.paths import get_instance_state_dir
from hushai_agent.vault import add_inbox_entry

THROTTLE_SECONDS = 600
MAX_CHARS = 120
# 管家维护类对话的缺省特征（不分大小写的子串）；[capture] system_markers 可整体替换
DEFAULT_SYSTEM_MARKERS = ("你是 hushai 管家", "你是hushai管家", "hushai-agent", "hushai doctor", "代码审核", "代码审查")


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
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"hushai capture state read warning: {exc}\n")
    return {"sessions": {}}


def _pick_capture_domain(cfg: InstanceConfig, snippet: str) -> str:
    """从句子里精确匹配领域（ASCII 词走词边界避免 AI 误命中 MAIL 等词），未命中时用安全中性默认领域。"""
    for d in cfg.domains:
        if not d:
            continue
        if re.fullmatch(r"[A-Za-z0-9_-]+", d):
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(d)}(?![A-Za-z0-9])", snippet):
                return d
        elif d in snippet:
            return d

    ext = load_ext(cfg)
    if ext.capture_default_domain and ext.capture_default_domain in cfg.domains:
        return ext.capture_default_domain
    for neutral in ("生活", "life"):
        if neutral in cfg.domains:
            return neutral
    return cfg.domains[0]


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

    model_res = _capture_with_model(inst_cfg, tpath, target_door, session_id, state, now_dt)
    if model_res is not None:
        return model_res

    if snippet.startswith(("- 系统：", "自愈：", "管家切换")):
        return {"ok": True, "written": False, "reason": "system-noise", "session_id": session_id}
    markers = load_ext(inst_cfg).capture_system_markers or DEFAULT_SYSTEM_MARKERS
    if any(m.casefold() in snippet.casefold() for m in markers):
        ledger.record(inst_cfg, "job", job="capture", date=now_dt.date().isoformat(), result="ok", note=f"skipped: system-maintenance door={target_door}")
        return {"ok": True, "written": False, "reason": "system-maintenance", "session_id": session_id}

    domain = _pick_capture_domain(inst_cfg, snippet)
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


def _capture_with_model(
    cfg: InstanceConfig, tpath: Path, door: str, session_id: str, state: dict[str, Any], now_dt: datetime.datetime
) -> dict[str, Any] | None:
    """模型判断路径；返回 None 表示没配或调用失败，调用方退回机械采集。"""
    from hushai_agent import capture_model

    human, assistant = capture_model.extract_last_exchange(tpath)
    decision = capture_model.classify(cfg, human, assistant)
    if decision is None:
        return None
    state.setdefault("sessions", {})[session_id] = {
        "last_ts": now_dt.isoformat(), "last_text": decision["did"], "door": door, "mode": "model",
    }
    _save_capture_state(get_instance_state_dir(cfg.name), state)
    if not decision["record"]:
        ledger.record(cfg, "job", job="capture", date=now_dt.date().isoformat(), result="ok", note=f"skipped: model door={door}")
        return {"ok": True, "written": False, "reason": "model-skip", "session_id": session_id}
    todo = decision["todo"] if decision["todo"] not in ("", "无") else None
    inbox_path = add_inbox_entry(
        instance=cfg.name, door=door, domain=decision["domain"], did=decision["did"], todo=todo, is_auto=True,
    )
    return {
        "ok": True, "written": True, "session_id": session_id, "door": door, "domain": decision["domain"],
        "snippet": decision["did"], "inbox_file": str(inbox_path), "mode": "model",
    }
