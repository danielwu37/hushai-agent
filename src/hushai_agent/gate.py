"""闸门（hushai gate）：各端写入 vault 的唯一受控入口。输出一律是 JSON 对象。"""

from __future__ import annotations

import datetime
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from hushai_agent import clock, on_event
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext
from hushai_agent.fsio import append_line_with_header, atomic_write, atomic_write_bytes, ensure_dir
from hushai_agent.guard import ensure_writable
from hushai_agent.inboxfmt import (
    InboxFormatError,
    format_inbox_filename,
    is_ignorable_inbox_line,
    parse_inbox_line,
)
from hushai_agent.paths import get_instance_state_dir

MAX_LINES = 200
MAX_LINE_CHARS = 2000
MAX_NAME_CHARS = 120
SSH_REJECT_RC = 2


class GateError(ValueError):
    """闸门拒绝本次请求。"""


def require_door(cfg: InstanceConfig, door: str) -> str:
    """端必须在 [doors] known 里。"""
    if door not in cfg.known_doors:
        raise GateError(f"未知的端: {door}")
    return door


def append_inbox(cfg: InstanceConfig, door: str, text: str, auto: bool = False) -> tuple[dict[str, Any], int]:
    """逐行校验已成形的收件行，合法的追加到该端当天的收件文件，然后触发一次事件处理。"""
    require_door(cfg, door)
    lines = [ln.strip() for ln in text.splitlines() if not is_ignorable_inbox_line(ln)]
    if len(lines) > MAX_LINES:
        raise GateError(f"单次最多 {MAX_LINES} 行，本次 {len(lines)} 行，整批拒绝")
    if any(len(ln) > MAX_LINE_CHARS for ln in lines):
        raise GateError(f"有行超过 {MAX_LINE_CHARS} 字符，整批拒绝")

    good: list[str] = []
    rejected: list[dict[str, str]] = []
    for ln in lines:
        try:
            parse_inbox_line(ln, allowed_domains=cfg.allowed_domains)
            good.append(ln)
        except InboxFormatError as exc:
            rejected.append({"line": ln, "reason": str(exc)})

    reconciled: dict[str, Any] = {}
    if good:
        date_str = clock.now(cfg).strftime("%Y-%m-%d")
        target = cfg.vault / "hub" / "inbox" / format_inbox_filename(date_str, door, auto)
        for ln in good:
            append_line_with_header(target, f"# {date_str} {door}", ln, [cfg.vault])
        try:
            reconciled = on_event.handle(cfg)
        except Exception as exc:  # 事件处理失败不影响已写入
            reconciled = {"error": str(exc)}
    out = {"accepted": len(good), "rejected": rejected, "reconciled": reconciled}
    return out, (0 if good else 1)


def _clean_name(cfg: InstanceConfig, name: str) -> tuple[str, str]:
    """校验交换区文件名，返回 (主名, 带点扩展名)。"""
    raw = (name or "").strip()
    if not raw or len(raw) > MAX_NAME_CHARS:
        raise GateError("文件名为空或过长")
    if "/" in raw or "\\" in raw or raw != os.path.basename(raw):
        raise GateError("文件名不能带路径")
    if raw.startswith(".") or any(ord(ch) < 32 for ch in raw):
        raise GateError("文件名不能以点开头或含控制字符")
    stem, ext = os.path.splitext(raw)
    allowed = load_ext(cfg).exchange_ext
    if not stem or ext.lower().lstrip(".") not in allowed:
        raise GateError(f"扩展名不在允许范围（{','.join(allowed)}）")
    return stem, ext


def put_exchange(cfg: InstanceConfig, door: str, name: str, data: bytes) -> dict[str, Any]:
    """把一个文件存进 hub/exchange/in/<door>/，同名不覆盖，自动加 -1、-2…"""
    require_door(cfg, door)
    stem, ext = _clean_name(cfg, name)
    max_kb = load_ext(cfg).exchange_max_kb
    if len(data) > max_kb * 1024:
        raise GateError(f"文件超过 {max_kb} KB")
    folder = cfg.vault / "hub" / "exchange" / "in" / door
    ensure_dir(folder, [cfg.vault])
    # O_EXCL 先占名：两端同时传同名文件时各得一个序号，不会互相覆盖
    for n in range(0, 1000):
        target = folder / (f"{stem}{ext}" if n == 0 else f"{stem}-{n}{ext}")
        try:
            os.close(os.open(os.fspath(ensure_writable(target, [cfg.vault])), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600))
            break
        except FileExistsError:
            continue
    else:
        raise GateError("同名文件过多")
    atomic_write_bytes(target, data, [cfg.vault])
    return {"path": target.relative_to(cfg.vault).as_posix(), "bytes": len(data)}


def check_inbox(cfg: InstanceConfig, door: str, ack: bool = False) -> dict[str, Any]:
    """列出 hub/exchange/out/to-<door>/ 下这个端还没确认过的文件；--ack 记为已确认。"""
    require_door(cfg, door)
    state_dir = get_instance_state_dir(cfg.name)
    seen_path = state_dir / "gate" / f"{door}.seen.json"
    try:
        seen = json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    if not isinstance(seen, dict):
        seen = {}

    folder = cfg.vault / "hub" / "exchange" / "out" / f"to-{door}"
    tz = clock.now(cfg).tzinfo
    files: list[dict[str, Any]] = []
    if folder.is_dir():
        for entry in sorted(folder.iterdir()):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            st = entry.stat()
            rel = entry.relative_to(cfg.vault).as_posix()
            sig = f"{st.st_size}:{st.st_mtime_ns}"
            if seen.get(rel) == sig:
                continue
            mtime = datetime.datetime.fromtimestamp(st.st_mtime, tz).isoformat(timespec="seconds")
            files.append({"path": rel, "bytes": st.st_size, "mtime": mtime, "_sig": sig})
    if ack and files:
        seen.update({f["path"]: f["_sig"] for f in files})
        atomic_write(seen_path, json.dumps(seen, ensure_ascii=False, indent=1) + "\n", [state_dir])
        from hushai_agent import outbox

        outbox.mark_acked_by_rel_paths(cfg, door, [str(f["path"]) for f in files])
    for f in files:
        del f["_sig"]
    return {"files": files, "count": len(files)}


def parse_ssh_command(raw: str) -> tuple[str, dict[str, Any]]:
    """只接受三种固定写法；其余一律 GateError。--instance、--door 对方给不了。"""
    try:
        tokens = shlex.split(raw or "")
    except ValueError as exc:
        raise GateError("命令无法解析") from exc
    if tokens in (["append-inbox"], ["append-inbox", "--auto"]):
        return "append-inbox", {"auto": len(tokens) == 2}
    if tokens in (["check-inbox"], ["check-inbox", "--ack"]):
        return "check-inbox", {"ack": len(tokens) == 2}
    if len(tokens) == 3 and tokens[:2] == ["put-exchange", "--name"]:
        return "put-exchange", {"name": tokens[2]}
    raise GateError("不允许的命令")


def run(cfg: InstanceConfig, door: str, sub: str, opts: dict[str, Any], stdin: Any = None) -> tuple[dict[str, Any], int]:
    """执行一个闸门子命令，返回 (JSON 对象, 退出码)。"""
    stream = stdin if stdin is not None else sys.stdin.buffer
    try:
        if sub == "ssh":
            raw_cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
            try:
                sub, opts = parse_ssh_command(raw_cmd)
            except GateError as exc:
                from hushai_agent import gate_muse

                if gate_muse.is_muse_ssh_invocation(cfg, raw_cmd):
                    require_door(cfg, door)
                    return gate_muse.run_muse_ssh(cfg, door, raw_cmd, stream)
                return {"error": str(exc)}, SSH_REJECT_RC
        require_door(cfg, door)  # 先验端，再读标准输入
        if sub == "append-inbox":
            raw = stream.read(MAX_LINES * MAX_LINE_CHARS * 4 + 1)
            return append_inbox(cfg, door, raw.decode("utf-8", errors="replace"), bool(opts.get("auto")))
        if sub == "put-exchange":
            limit = load_ext(cfg).exchange_max_kb * 1024
            return put_exchange(cfg, door, str(opts.get("name") or ""), stream.read(limit + 1)), 0
        if sub == "check-inbox":
            return check_inbox(cfg, door, bool(opts.get("ack"))), 0
        return {"error": f"未知子命令: {sub}"}, 1
    except GateError as exc:
        return {"error": str(exc)}, 1
