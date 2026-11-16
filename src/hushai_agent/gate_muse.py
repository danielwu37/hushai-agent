"""Muse SSH 闸门兼容层（hushai gate ssh）：支持 muse-gate 全部命令、00-我 路径映射、只读根与私密路径排除。"""

from __future__ import annotations

import configparser
import datetime
import fnmatch
import hashlib
import os
import posixpath
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hushai_agent import clock, on_event
from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import append_line_with_header
from hushai_agent.inboxfmt import InboxFormatError, parse_inbox_line
from hushai_agent.paths import get_instance_config_file, get_instance_state_dir

PUT_MAX, READ_MAX, LINE_MAX = 5 * 1024 * 1024, 20 * 1024 * 1024, 1000
RC_OK, RC_ERR, RC_REFUSE, RC_PENDING = 0, 1, 2, 3
DATE_RE = re.compile(r"^20\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
LINE_RE = re.compile(r"^- ([01]\d|2[0-3]):[0-5]\d｜[^｜\n]+｜[^｜\n]+｜产物：[^｜\n]+｜待办：[^｜\n]+$")
DENY_ANY = frozenset({"local", ".secrets", ".git", "node_modules", "__pycache__", ".venv"})
DENY_NAME = ("*.env", ".env", "*token*.json", "spotify_config.json")
AUDIO_EXT = (".m4a", ".mp3", ".flac", ".wav", ".aac", ".ogg")
TEXT_EXT = (".md", ".txt", ".csv", ".tsv", ".json", ".html", ".yaml", ".yml")
DEFAULT_WRITE_ALLOW = ("00-我/交换/入/", "hub/exchange/in/", "music/Playlists/_输入清单/待下载/")

LEGACY_ME_PREFIX_MAP: tuple[tuple[str, str], ...] = (
    ("00-我/收件", "hub/inbox"), ("00-我/日报", "hub/reports"),
    ("00-我/交换/入", "hub/exchange/in"), ("00-我/交换/出", "hub/exchange/out"), ("00-我/交换", "hub/exchange"),
    ("00-我/今日待办.md", "hub/today.md"), ("00-我/开放项.md", "hub/open-items.md"),
    ("00-我/近况.md", "hub/status.md"), ("00-我/近况-索引.md", "hub/status-index.md"),
    ("00-我/我.md", "hub/profile.md"), ("00-我/认知.md", "hub/insights.md"),
    ("00-我/决定.md", "hub/decisions.md"), ("00-我/教训.md", "hub/lessons.md"),
    ("00-我/晨间简报.md", "hub/reports/brief.md"),
    ("00-我/AGENTS.md", "hub/AGENTS.md"), ("00-我/AGENTS-附录.md", "hub/AGENTS.local.md"),
    ("00-我", "hub"),
)
# 2026-11-16 起旧 00-我 的收件、日报、交换区历史文件归档在 legacy 目录；按 00-我/ 旧路径找不到时回落到这里
LEGACY_FALLBACK: tuple[tuple[str, str], ...] = (
    ("hub/inbox", "hub/reports/legacy-00-我/收件"),
    ("hub/reports", "hub/reports/legacy-00-我/日报"),
    ("hub/exchange/in", "hub/exchange/in/legacy"),
    ("hub/exchange/out", "hub/exchange/out/legacy"),
)


@dataclass(frozen=True)
class MuseGateConfig:
    enabled: bool
    read_roots: dict[str, Path]
    private_patterns: tuple[str, ...]
    private_scope: tuple[str, ...]
    local_patterns: tuple[str, ...]
    write_allow: tuple[str, ...]
    log_file: Path


def rclone_match(pat: str, rel: str) -> bool:
    """兼容 muse-gate 的 rclone 过滤规则：/ 开头锚定根，** 跨目录，* 不跨 /。"""
    p = pat.lstrip("/").rstrip("/")
    if rel == p or rel.startswith(p + "/"):
        return True
    rx, i, raw_p = "", 0, pat.lstrip("/")
    while i < len(raw_p):
        if raw_p.startswith("**", i):
            rx, i = rx + ".*", i + 2
        elif raw_p[i] in ("*", "?"):
            rx, i = rx + ("[^/]*" if raw_p[i] == "*" else "[^/]"), i + 1
        else:
            rx, i = rx + re.escape(raw_p[i]), i + 1
    return re.fullmatch(rx, rel) is not None


def load_muse_gate_config(cfg: InstanceConfig) -> MuseGateConfig:
    """从 config.ini 的 [gate] 节读取 read_roots、private_paths_file、write_allow、log_file。"""
    parser = configparser.ConfigParser(interpolation=None)
    cfg_file = get_instance_config_file(cfg.name)
    if cfg_file.is_file():
        parser.read_string(cfg_file.read_text(encoding="utf-8"))

    roots_raw = parser.get("gate", "read_roots", fallback="").strip()
    read_roots = {k.strip().strip("/"): Path(v.strip()) for item in roots_raw.split(",") if "=" in item for k, v in [item.split("=", 1)] if k.strip() and v.strip()}
    priv_file_raw = parser.get("gate", "private_paths_file", fallback="").strip()
    priv_list: list[str] = []
    if priv_file_raw:
        pf = Path(priv_file_raw)
        priv_list.extend([s for ln in pf.read_text(encoding="utf-8", errors="ignore").splitlines() if (s := ln.split("#", 1)[0].strip())] if pf.is_file() else ["/**"])
    scope_raw = parser.get("gate", "private_paths_scope", fallback="").strip()
    private_scope = tuple(p.strip().strip("/") for p in scope_raw.split(",") if p.strip().strip("/"))
    local_list = [s for loc in cfg.local_only if (s := loc.strip().strip("/"))]

    wa_raw = parser.get("gate", "write_allow", fallback="").strip()
    write_allow = tuple(p.strip() for p in wa_raw.split(",") if p.strip()) or DEFAULT_WRITE_ALLOW
    log_raw = parser.get("gate", "log_file", fallback="").strip()
    log_file = Path(log_raw) if log_raw else (get_instance_state_dir(cfg.name) / "gate.log")
    return MuseGateConfig(
        enabled=bool(read_roots or priv_file_raw or wa_raw or log_raw),
        read_roots=read_roots,
        private_patterns=tuple(dict.fromkeys(priv_list)),
        private_scope=private_scope,
        local_patterns=tuple(dict.fromkeys(local_list)),
        write_allow=write_allow,
        log_file=log_file,
    )


def map_legacy_path(norm_rel: str) -> str:
    """将 00-我/... 路径映射为 vault 内的 hub/... 路径；非 00-我 路径原样返回。"""
    clean = norm_rel.strip("/")
    for old_p, new_p in LEGACY_ME_PREFIX_MAP:
        if clean == old_p:
            return new_p
        if clean.startswith(old_p + "/"):
            return new_p + clean[len(old_p) :]
    return clean


def _is_private(rel_path: str, sub_path: str, mcfg: MuseGateConfig) -> bool:
    clean = rel_path.strip("/")
    parts = [p for p in clean.split("/") if p]
    if any(p in DENY_ANY for p in parts) or (parts and any(fnmatch.fnmatch(parts[-1], n) for n in DENY_NAME)):
        return True
    def _hit(pat: str, cand: str) -> bool:
        return bool(cand) and (rclone_match(pat, cand) or (pat.endswith("/**") and rclone_match(pat[:-3], cand)))

    # private_paths_file 的规则：配了 private_paths_scope 时只作用于这些读根（相对根判断），没配时对所有路径生效
    if mcfg.private_scope:
        scoped = [sc for sc in mcfg.private_scope if clean == sc or clean.startswith(sc + "/")]
        if scoped:
            rel_in_scope = clean[len(scoped[0]) :].strip("/")
            if any(_hit(pat, rel_in_scope) for pat in mcfg.private_patterns):
                return True
    else:
        for pat in mcfg.private_patterns:
            if any(_hit(pat, cand) for cand in (clean, sub_path.strip("/"))):
                return True
    # local_only（vault 相对路径）始终生效
    for pat in mcfg.local_patterns:
        if any(_hit(pat, cand) for cand in (clean, sub_path.strip("/"))):
            return True
    return False


def _legacy_candidate(mapped: str) -> str | None:
    for new_p, old_p in LEGACY_FALLBACK:
        if mapped == new_p or mapped.startswith(new_p + "/"):
            return old_p + mapped[len(new_p) :]
    return None


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
    allowed = "、".join(["hub（旧路径 00-我）", "domains", *sorted(mcfg.read_roots)])
    raise PermissionError(f"拒绝：不在可读范围（{allowed}）")


def resolve_path(cfg: InstanceConfig, mcfg: MuseGateConfig, raw_rel: str, allow_missing: bool = False) -> tuple[Path, str]:
    """解析并校验客户端相对路径，返回 (宿主机真实路径, 规范化客户端相对路径)。"""
    if not raw_rel or raw_rel.startswith(("/", "\\")) or "\x00" in raw_rel or os.path.isabs(raw_rel) or ".." in raw_rel.replace("\\", "/").split("/"):
        raise PermissionError("拒绝：路径越界或含 ..")
    norm = posixpath.normpath(raw_rel.replace("\\", "/")).lstrip("/")
    if not norm or norm == "." or norm.startswith("..") or "/.." in norm:
        raise PermissionError("拒绝：路径越界")

    mapped = map_legacy_path(norm)
    if norm.startswith("00-我/") and not (cfg.vault / mapped).exists():
        cand = _legacy_candidate(mapped)
        if cand and (cfg.vault / cand).exists():
            mapped = cand
    root, sub_rel = _match_root(mapped, mcfg, cfg)
    if _is_private(norm, sub_rel, mcfg) or _is_private(mapped, sub_rel, mcfg):
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
            p, norm = resolve_path(cfg, mcfg, args[0])
            if not p.is_dir():
                raise PermissionError("拒绝：不是目录")
            lines: list[str] = []
            entries = {e.name: e for e in p.iterdir()}
            if norm.startswith("00-我"):
                cand = _legacy_candidate(map_legacy_path(norm))
                legacy_dir = cfg.vault / cand if cand else None
                if legacy_dir is not None and legacy_dir.is_dir() and not legacy_dir.is_symlink():
                    for e in legacy_dir.iterdir():
                        entries.setdefault(e.name, e)
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
            _log_gate(cfg, mcfg, raw, RC_OK, "ls")
            return {"ok": True, "stdout": ("\n".join(lines) + "\n") if lines else "", "_muse_text": True}, RC_OK

        if cmd == "read" and len(args) == 1:
            p, norm = resolve_path(cfg, mcfg, args[0])
            if norm.lower().endswith(AUDIO_EXT) or not p.is_file() or p.stat().st_size > READ_MAX:
                raise PermissionError("拒绝：文件不可读、不存在或超过 20 MB")
            _log_gate(cfg, mcfg, raw, RC_OK, "read")
            return {"ok": True, "stdout": p.read_bytes(), "_muse_text": True}, RC_OK

        if cmd == "append-inbox" and len(args) in (1, 2):
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
                on_event.handle(cfg)
            except Exception:
                pass
            sha = hashlib.sha256(target.read_bytes()).hexdigest()[:16] if target.is_file() else "-"
            msg = f"OK {sha} 追加 {len(new_lns)} 行，跳过重复 {len(lines) - len(new_lns)} 行"
            _log_gate(cfg, mcfg, raw, RC_OK, msg)
            return {"ok": True, "stdout": msg + "\n", "accepted": len(new_lns), "_muse_text": True}, RC_OK

        if cmd == "put" and len(args) == 1:
            norm_pre = posixpath.normpath(args[0].replace("\\", "/")).lstrip("/")
            fname = norm_pre.rsplit("/", 1)[-1]
            if fname.startswith(".") or not any(norm_pre.startswith(pfx.rstrip("/") + "/") for pfx in mcfg.write_allow) or args[0].endswith("/"):
                raise PermissionError(f"拒绝：不在写入白名单 ({norm_pre})")
            p, norm = resolve_path(cfg, mcfg, args[0], allow_missing=True)
            if norm.startswith(("00-我/交换/入/", "hub/exchange/in/")) and not fname.endswith(TEXT_EXT):
                raise PermissionError("拒绝：交换/入 只收文本类文件")
            if norm.startswith("music/Playlists/_输入清单/待下载/") and ("/" in norm[len("music/Playlists/_输入清单/待下载/") :] or not fname.endswith(".txt")):
                raise PermissionError("拒绝：音乐待下载只收该目录下的 .txt 清单")
            data = stdin_stream.read(PUT_MAX + 1)
            if not data or len(data) > PUT_MAX:
                raise PermissionError("拒绝：没有内容或超过 5 MB 上限")
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".gate-", dir=str(p.parent))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            sha = hashlib.sha256(data).hexdigest()[:16]
            _log_gate(cfg, mcfg, raw, RC_OK, f"OK {sha}")
            return {"ok": True, "stdout": f"OK {sha}\n", "_muse_text": True}, RC_OK

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
