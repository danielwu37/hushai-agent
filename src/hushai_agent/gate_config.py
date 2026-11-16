"""Muse 闸门配置解析、路径归一化、旧目录映射与私密路径规则判断。"""

from __future__ import annotations

import configparser
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from hushai_agent.config import InstanceConfig
from hushai_agent.paths import get_instance_config_file, get_instance_state_dir

DEFAULT_DENY_ANY = frozenset({"local", ".secrets", ".git", "node_modules", "__pycache__", ".venv"})
DEFAULT_DENY_NAME = ("*.env", ".env", "*token*.json", "*secret*", "*.key", "*.pem")
DEFAULT_WRITE_ALLOW = ("hub/exchange/in/",)
DEFAULT_LEGACY_PREFIX_MAP: tuple[tuple[str, str], ...] = ()
DEFAULT_LEGACY_FALLBACK: tuple[tuple[str, str], ...] = ()
DEFAULT_TXT_ONLY_DIRS: tuple[str, ...] = ()


@dataclass(frozen=True)
class MuseGateConfig:
    enabled: bool
    read_roots: dict[str, Path]
    private_patterns: tuple[str, ...]
    private_scope: tuple[str, ...]
    local_patterns: tuple[str, ...]
    write_allow: tuple[str, ...]
    deny_names: tuple[str, ...]
    legacy_prefix_map: tuple[tuple[str, str], ...]
    legacy_fallback: tuple[tuple[str, str], ...]
    txt_only_dirs: tuple[str, ...]
    log_file: Path


def _parse_pairs(raw: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in raw.replace("\n", ",").split(","):
        seg = item.strip()
        if "=" not in seg:
            continue
        k, v = (part.strip().strip("/") for part in seg.split("=", 1))
        if k and v:
            pairs.append((k, v))
    return tuple(pairs)


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
    """从 config.ini 的 [gate] 节读取读写根、隐私排除、自定义映射与限制目录配置。"""
    parser = configparser.ConfigParser(interpolation=None)
    cfg_file = get_instance_config_file(cfg.name)
    if cfg_file.is_file():
        parser.read_string(cfg_file.read_text(encoding="utf-8"))

    roots_raw = parser.get("gate", "read_roots", fallback="").strip()
    read_roots = {
        k.strip().strip("/"): Path(v.strip())
        for item in roots_raw.split(",")
        if "=" in item
        for k, v in [item.split("=", 1)]
        if k.strip() and v.strip()
    }
    priv_file_raw = parser.get("gate", "private_paths_file", fallback="").strip()
    priv_list: list[str] = []
    if priv_file_raw:
        pf = Path(priv_file_raw)
        if pf.is_file():
            priv_list.extend(
                [s for ln in pf.read_text(encoding="utf-8", errors="ignore").splitlines() if (s := ln.split("#", 1)[0].strip())]
            )
        else:
            priv_list.append("/**")
    scope_raw = parser.get("gate", "private_paths_scope", fallback="").strip()
    private_scope = tuple(p.strip().strip("/") for p in scope_raw.split(",") if p.strip().strip("/"))
    local_list = [s for loc in cfg.local_only if (s := loc.strip().strip("/"))]
    wa_raw = parser.get("gate", "write_allow", fallback="").strip()
    write_allow = tuple(p.strip() for p in wa_raw.split(",") if p.strip()) or DEFAULT_WRITE_ALLOW
    deny_extra = tuple(p.strip() for p in parser.get("gate", "deny_names", fallback="").split(",") if p.strip())
    deny_names = tuple(dict.fromkeys((*DEFAULT_DENY_NAME, *deny_extra)))
    prefix_map = _parse_pairs(parser.get("gate", "legacy_prefix_map", fallback="")) or DEFAULT_LEGACY_PREFIX_MAP
    fallback_map = _parse_pairs(parser.get("gate", "legacy_fallback", fallback="")) or DEFAULT_LEGACY_FALLBACK
    txt_dirs_raw = parser.get("gate", "txt_only_dirs", fallback="").strip()
    txt_only_dirs = tuple(p.strip().lstrip("/") for p in txt_dirs_raw.split(",") if p.strip()) or DEFAULT_TXT_ONLY_DIRS
    log_raw = parser.get("gate", "log_file", fallback="").strip()
    log_file = Path(log_raw) if log_raw else (get_instance_state_dir(cfg.name) / "gate.log")

    return MuseGateConfig(
        enabled=bool(read_roots or priv_file_raw or wa_raw or log_raw),
        read_roots=read_roots,
        private_patterns=tuple(dict.fromkeys(priv_list)),
        private_scope=private_scope,
        local_patterns=tuple(dict.fromkeys(local_list)),
        write_allow=write_allow,
        deny_names=deny_names,
        legacy_prefix_map=prefix_map,
        legacy_fallback=fallback_map,
        txt_only_dirs=txt_only_dirs,
        log_file=log_file,
    )


def map_legacy_path(
    norm_rel: str,
    prefix_map: Mapping[str, str] | Sequence[tuple[str, str]] = DEFAULT_LEGACY_PREFIX_MAP,
) -> str:
    """按配置映射表将历史目录路径映射为当前 vault 路径；未配置或未命中原样返回。"""
    clean = norm_rel.strip("/")
    raw_items = prefix_map.items() if isinstance(prefix_map, Mapping) else prefix_map
    pairs = [(k.strip("/"), v.strip("/")) for k, v in raw_items if k.strip("/")]
    for old_p, new_p in sorted(pairs, key=lambda kv: len(kv[0]), reverse=True):
        if clean == old_p:
            return new_p
        if clean.startswith(old_p + "/"):
            return new_p + clean[len(old_p) :]
    return clean


def legacy_candidate(
    mapped: str,
    fallback_map: Mapping[str, str] | Sequence[tuple[str, str]] = DEFAULT_LEGACY_FALLBACK,
) -> str | None:
    """仅当配置了 legacy_fallback 时，将新路径回落到归档历史目录只读查找。"""
    clean_mapped = mapped.strip("/")
    raw_items = fallback_map.items() if isinstance(fallback_map, Mapping) else fallback_map
    pairs = [(k.strip("/"), v.strip("/")) for k, v in raw_items if k.strip("/")]
    for new_p, old_p in sorted(pairs, key=lambda kv: len(kv[0]), reverse=True):
        if clean_mapped == new_p or clean_mapped.startswith(new_p + "/"):
            return old_p + clean_mapped[len(new_p) :]
    return None


def is_private_path(rel_path: str, sub_path: str, mcfg: MuseGateConfig) -> bool:
    """检查目标路径是否命中核心屏蔽词、private_paths_file 或 [privacy] local_only。"""
    clean = rel_path.strip("/")
    parts = [p for p in clean.split("/") if p]
    if any(p in DEFAULT_DENY_ANY for p in parts) or (parts and any(fnmatch.fnmatch(parts[-1], n) for n in mcfg.deny_names)):
        return True

    def _hit(pat: str, cand: str) -> bool:
        return bool(cand) and (rclone_match(pat, cand) or (pat.endswith("/**") and rclone_match(pat[:-3], cand)))

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

    for pat in mcfg.local_patterns:
        if any(_hit(pat, cand) for cand in (clean, sub_path.strip("/"))):
            return True
    return False
