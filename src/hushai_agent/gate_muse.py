"""Muse SSH 闸门兼容层（hushai gate ssh）：支持 ping/ls/read/append-inbox/put/verify 及配置化路径护栏。"""

from __future__ import annotations

import datetime
import hashlib
import os
import posixpath
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent import clock, digest, ledger, lock
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext
from hushai_agent.fsio import append_line_with_header
from hushai_agent.gate_config import (  # noqa: F401
    MuseGateConfig,
    is_private_path,
    legacy_candidate,
    load_muse_gate_config,
    map_legacy_path,
    rclone_match,
)
from hushai_agent.inboxfmt import InboxFormatError, parse_inbox_line
from hushai_agent.paths import get_instance_state_dir

PUT_MAX, READ_MAX, LINE_MAX = 5 * 1024 * 1024, 20 * 1024 * 1024, 1000
RC_OK, RC_ERR, RC_REFUSE, RC_PENDING = 0, 1, 2, 3
DATE_RE = re.compile(r"^20\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
LINE_RE = re.compile(r"^- ([01]\d|2[0-3]):[0-5]\d｜[^｜\n]+｜[^｜\n]+｜产物：[^｜\n]+｜待办：[^｜\n]+$")
AUDIO_EXT = (".m4a", ".mp3", ".flac", ".wav", ".aac", ".ogg")
TEXT_EXT = (".md", ".txt", ".csv", ".tsv", ".json", ".html", ".yaml", ".yml")


def _log_gate(cfg: InstanceConfig, mcfg: MuseGateConfig, raw: str, rc: int, detail: str = "") -> None:
    ts = datetime.datetime.now(ZoneInfo(cfg.timezone)).strftime("%F %T")
    ip = (os.environ.get("SSH_CLIENT") or "local").split()[0]
    line = f"{ts}\t{ip}\t{raw[:300]}\trc={rc}\t{detail[:200]}\n"
    for target in (mcfg.log_file, get_instance_state_dir(cfg.name) / "gate.log"):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(line)
            break
        except OSError:
            continue


def _match_root(mapped: str, mcfg: MuseGateConfig, cfg: InstanceConfig) -> tuple[Path, str]:
    if mapped in ("hub", "domains") or mapped.startswith(("hub/", "domains/")):
        return cfg.vault, mapped
    for prefix in sorted(mcfg.read_roots, key=len, reverse=True):
        if mapped == prefix or mapped.startswith(prefix + "/"):
            return mcfg.read_roots[prefix], mapped[len(prefix) + 1 :] if mapped != prefix else ""
    allowed = "、".join(["hub", "domains", *sorted(mcfg.read_roots)])
    raise PermissionError(f"拒绝：不在可读范围（{allowed}）")


def resolve_path(cfg: InstanceConfig, mcfg: MuseGateConfig, raw_rel: str, allow_missing: bool = False) -> tuple[Path, str]:
    """解析并校验客户端相对路径；allow_missing=True（写入场景）绝不回落 legacy 历史归档。"""
    if not raw_rel or raw_rel.startswith(("/", "\\")) or "\x00" in raw_rel or os.path.isabs(raw_rel) or ".." in raw_rel.replace("\\", "/").split("/"):
        raise PermissionError("拒绝：路径越界或含 ..")
    norm = posixpath.normpath(raw_rel.replace("\\", "/")).lstrip("/")
    if not norm or norm == "." or norm.startswith("..") or "/.." in norm:
        raise PermissionError("拒绝：路径越界")

    mapped = map_legacy_path(norm, mcfg.legacy_prefix_map)
    if not allow_missing and mapped != norm and not (cfg.vault / mapped).exists():
        cand = legacy_candidate(mapped, mcfg.legacy_fallback)
        if cand and (cfg.vault / cand).exists():
            mapped = cand

    root, sub_rel = _match_root(mapped, mcfg, cfg)
    if is_private_path(norm, sub_rel, mcfg) or is_private_path(mapped, sub_rel, mcfg):
        raise PermissionError(f"拒绝：私密或受保护路径不开放 ({norm})")
    if not root.exists() or root.is_symlink():
        raise FileNotFoundError(f"错误：根目录不可用 ({norm})")

    root_real, cur, sub_parts = root.resolve(), root, [p for p in sub_rel.split("/") if p]
    for idx, seg in enumerate(sub_parts):
        cur = cur / seg
        if cur.is_symlink():
            raise PermissionError(f"拒绝：不跟随符号链接 ({norm})")
        if not cur.exists():
            if allow_missing:
                continue
            raise PermissionError(f"拒绝：文件不存在 ({norm})")
        if idx == len(sub_parts) - 1 and (real := cur.resolve()) != root_real and root_real not in real.parents:
            raise PermissionError("拒绝：路径越界")

    check_target = cur if cur.exists() else cur.parent
    target_real = check_target.resolve() if check_target.exists() else root_real
    if target_real != root_real and root_real not in target_real.parents:
        raise PermissionError("拒绝：路径越界")
    return cur, norm


def explain_line(ln: str) -> str:
    """给出收件行不合格的具体原因，方便对端自行改正。"""
    if len(ln) > LINE_MAX:
        return f"超过 {LINE_MAX} 字"
    if not re.match(r"^- ([01]\d|2[0-3]):[0-5]\d｜", ln):
        return "开头须为「- HH:MM｜」（减号、空格、24 小时制时间、全角竖线）"
    parts = ln[2:].split("｜")
    if len(parts) != 5:
        return f"须恰好 5 段、用全角竖线｜分隔，现在是 {len(parts)} 段"
    if not parts[3].startswith("产物："):
        return "第 4 段须以「产物：」开头（全角冒号），没有写「产物：无」"
    if not parts[4].startswith("待办："):
        return "第 5 段须以「待办：」开头（全角冒号），没有写「待办：无」"
    if not parts[1].strip() or not parts[2].strip() or not parts[3][3:].strip() or not parts[4][3:].strip():
        return "有空段"
    return "含换行或其他非法字符"


def is_muse_ssh_invocation(cfg: InstanceConfig, raw_cmd: str) -> bool:
    """判断本次 SSH 调用是否应走 muse-gate 兼容文本协议。"""
    if load_muse_gate_config(cfg).enabled:
        return True
    try:
        argv = shlex.split(raw_cmd or "")
    except ValueError:
        return False
    return bool(argv) and (
        argv[0] in ("ping", "ls", "read", "put", "verify")
        or (argv[0] == "append-inbox" and len(argv) >= 2 and bool(DATE_RE.match(argv[1])))
    )


def _write_non_overwriting(dest: Path, data: bytes) -> Path:
    """写入临时文件并通过硬链接 / O_EXCL 分配不覆写序号（-1/-2/...）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gate-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        stem, suffix = dest.stem, dest.suffix
        for idx in range(0, 1000):
            cand = dest if idx == 0 else dest.with_name(f"{stem}-{idx}{suffix}")
            try:
                os.link(tmp, cand)
                return cand
            except FileExistsError:
                continue
        raise RuntimeError("不覆写序号分配耗尽")
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _handle_ls(cfg: InstanceConfig, mcfg: MuseGateConfig, arg: str, tz: ZoneInfo) -> str:
    p, norm = resolve_path(cfg, mcfg, arg)
    if not p.is_dir():
        raise PermissionError("拒绝：不是目录")
    entries = {e.name: e for e in p.iterdir()}
    mapped = map_legacy_path(norm, mcfg.legacy_prefix_map)
    if mapped != norm:
        cand = legacy_candidate(mapped, mcfg.legacy_fallback)
        legacy_dir = cfg.vault / cand if cand else None
        if legacy_dir is not None and legacy_dir.is_dir() and not legacy_dir.is_symlink():
            for e in legacy_dir.iterdir():
                entries.setdefault(e.name, e)
    lines: list[str] = []
    for entry in sorted(entries.values(), key=lambda e: e.name):
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        try:
            resolve_path(cfg, mcfg, f"{norm}/{entry.name}")
        except PermissionError:
            continue
        st = entry.stat()
        mtime = datetime.datetime.fromtimestamp(st.st_mtime, tz).strftime("%F %H:%M")
        lines.append(f"{'d' if entry.is_dir() else 'f'}\t{st.st_size}\t{mtime}\t{entry.name}")
    return ("\n".join(lines) + "\n") if lines else ""


def _handle_append_inbox(cfg: InstanceConfig, mcfg: MuseGateConfig, door: str, raw: str, args: list[str], stdin_stream: Any) -> dict[str, Any]:
    date_str, auto = args[0], (len(args) == 2 and args[1] == "auto")
    if not (DATE_RE.match(date_str) and clock.is_real_date(date_str)) or (len(args) == 2 and not auto):
        raise PermissionError("拒绝：用法 append-inbox YYYY-MM-DD [auto]")
    lines = [ln.rstrip("\r") for ln in stdin_stream.read(256 * 1024 + 1).decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        raise PermissionError("拒绝：没有内容")
    for i, ln in enumerate(lines, 1):
        if len(ln) > LINE_MAX or not LINE_RE.match(ln):
            raise PermissionError(f"拒绝：第 {i} 行不符合收件五段式（{explain_line(ln)}）：{ln[:200]}")
        try:
            parse_inbox_line(ln, allowed_domains=cfg.allowed_domains)
        except InboxFormatError as exc:
            raise PermissionError(f"拒绝：第 {i} 行领域校验未通过（{exc}）") from exc

    suffix = "-auto" if auto else ""
    target = cfg.vault / "hub" / "inbox" / f"{date_str}-{door}{suffix}.md"
    have = set(target.read_text(encoding="utf-8").splitlines()) if target.is_file() else set()
    new_lns = [ln for ln in lines if ln not in have]
    for ln in new_lns:
        append_line_with_header(target, f"# {date_str} {door}{suffix}", ln, [cfg.vault])

    try:
        digest.run_mechanical_only(cfg)
        if load_ext(cfg).routing_enabled:
            lock.mark_dirty(cfg, "routing")
    except Exception as exc:
        _log_gate(cfg, mcfg, raw, RC_ERR, f"reconcile_err={exc}")
        ledger.record(cfg, "job", job="gate-reconcile", date=clock.today(cfg).isoformat(), result="failed", note=str(exc)[:200])

    sha = hashlib.sha256(target.read_bytes()).hexdigest()[:16] if target.is_file() else "-"
    msg = f"OK {sha} 追加 {len(new_lns)} 行，跳过重复 {len(lines) - len(new_lns)} 行"
    _log_gate(cfg, mcfg, raw, RC_OK, msg)
    return {"ok": True, "stdout": msg + "\n", "accepted": len(new_lns), "_muse_text": True}


def _handle_put(cfg: InstanceConfig, mcfg: MuseGateConfig, raw: str, rel_arg: str, stdin_stream: Any) -> dict[str, Any]:
    norm_pre = posixpath.normpath(rel_arg.replace("\\", "/")).lstrip("/")
    fname = norm_pre.rsplit("/", 1)[-1]
    if fname.startswith(".") or not any(norm_pre.startswith(pfx.rstrip("/") + "/") for pfx in mcfg.write_allow) or rel_arg.endswith("/"):
        raise PermissionError(f"拒绝：不在写入白名单 ({norm_pre})")
    p, norm = resolve_path(cfg, mcfg, rel_arg, allow_missing=True)
    mapped = map_legacy_path(norm, mcfg.legacy_prefix_map)
    in_exchange = mapped.startswith("hub/exchange/in/") or norm.startswith("hub/exchange/in/")
    if in_exchange and not fname.endswith(TEXT_EXT):
        raise PermissionError("拒绝：交换/入 只收文本类文件")
    for txt_dir in mcfg.txt_only_dirs:
        pfx = txt_dir.rstrip("/") + "/"
        if norm.startswith(pfx) and ("/" in norm[len(pfx) :] or not fname.endswith(".txt")):
            raise PermissionError("拒绝：只收该目录下的 .txt 清单")
    data = stdin_stream.read(PUT_MAX + 1)
    if not data or len(data) > PUT_MAX:
        raise PermissionError("拒绝：没有内容或超过 5 MB 上限")

    if in_exchange:
        final_path = _write_non_overwriting(p, data)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".gate-", dir=str(p.parent))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        final_path = p

    sha = hashlib.sha256(data).hexdigest()[:16]
    suffix = f" {final_path.name}" if final_path != p else ""
    _log_gate(cfg, mcfg, raw, RC_OK, f"OK {sha}{suffix}")
    return {"ok": True, "stdout": f"OK {sha}{suffix}\n", "_muse_text": True}


def run_muse_ssh(cfg: InstanceConfig, door: str, raw_cmd: str, stdin_stream: Any) -> tuple[dict[str, Any], int]:
    """执行 muse-gate 兼容命令，返回 (结果字典, 退出码)。"""
    mcfg, raw = load_muse_gate_config(cfg), (raw_cmd or "").strip()
    try:
        argv = shlex.split(raw) if raw else []
    except ValueError:
        argv = []
    cmd, args, tz = (argv[0], argv[1:], ZoneInfo(cfg.timezone)) if argv else ("", [], ZoneInfo(cfg.timezone))

    try:
        if cmd == "ping" and len(args) == 0:
            _log_gate(cfg, mcfg, raw, RC_OK, "pong")
            return {"ok": True, "stdout": "pong\n", "_muse_text": True}, RC_OK
        if cmd == "ls" and len(args) == 1:
            stdout = _handle_ls(cfg, mcfg, args[0], tz)
            _log_gate(cfg, mcfg, raw, RC_OK, "ls")
            return {"ok": True, "stdout": stdout, "_muse_text": True}, RC_OK
        if cmd == "read" and len(args) == 1:
            p, norm = resolve_path(cfg, mcfg, args[0])
            if norm.lower().endswith(AUDIO_EXT) or not p.is_file() or p.stat().st_size > READ_MAX:
                raise PermissionError("拒绝：文件不可读、不存在或超过 20 MB")
            _log_gate(cfg, mcfg, raw, RC_OK, "read")
            return {"ok": True, "stdout": p.read_bytes(), "_muse_text": True}, RC_OK
        if cmd == "append-inbox" and len(args) in (1, 2):
            return _handle_append_inbox(cfg, mcfg, door, raw, args, stdin_stream), RC_OK
        if cmd == "put" and len(args) == 1:
            return _handle_put(cfg, mcfg, raw, args[0], stdin_stream), RC_OK
        if cmd == "verify" and len(args) == 1:
            p, _ = resolve_path(cfg, mcfg, args[0])
            if not p.is_file():
                raise PermissionError("拒绝：文件不存在")
            sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            _log_gate(cfg, mcfg, raw, RC_OK, f"OK {sha}")
            return {"ok": True, "stdout": f"OK {sha}\n", "_muse_text": True}, RC_OK
        raise PermissionError("拒绝：不认识的命令；可用：ping、ls、read、append-inbox、put、verify")
    except PermissionError as exc:
        _log_gate(cfg, mcfg, raw, RC_REFUSE, str(exc))
        return {"error": str(exc), "_muse_text": True}, RC_REFUSE
    except Exception as exc:  # noqa: BLE001
        _log_gate(cfg, mcfg, raw, RC_ERR, f"出错：{exc}")
        return {"error": f"出错：{exc}", "_muse_text": True}, RC_ERR
