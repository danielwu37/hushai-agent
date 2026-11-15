"""安全修改实例 config.ini 的公共辅助模块（支持多节原子更新、删除键与失败自动回滚）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hushai_agent.config import ConfigError, InstanceConfig, load_instance_config
from hushai_agent.config_ext import ExtConfig, load_ext
from hushai_agent.fsio import atomic_write
from hushai_agent.paths import (
    get_config_root,
    get_instance_config_dir,
    get_instance_config_file,
    is_valid_instance_name,
)
from hushai_agent.validate import check_inbox_lines, check_vault_structure


def list_instances() -> list[str]:
    """扫描配置根目录，返回已初始化 config.ini 的所有合法实例名（按名称排序）。"""
    root = get_config_root()
    if not root.is_dir():
        return []
    found: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_dir() and is_valid_instance_name(entry.name) and (entry / "config.ini").is_file():
            found.append(entry.name)
    return found


def update_ini_text(
    raw_text: str,
    section_updates: dict[str, dict[str, str | None]],
) -> str:
    """在尽量保留原有注释和格式的前提下，更新 INI 多节键值；值为 None 表示删除该键（键名大小写不敏感匹配）。"""
    norm_updates: dict[str, dict[str, tuple[str, str | None]]] = {
        sec: {k.lower(): (k, v) for k, v in kv_map.items()}
        for sec, kv_map in section_updates.items()
    }
    lines = raw_text.splitlines()
    out: list[str] = []
    current_sec: str | None = None
    seen_sections: set[str] = set()
    seen_keys: dict[str, set[str]] = {sec: set() for sec in section_updates}

    def _flush_remaining(sec: str | None) -> None:
        if not sec or sec not in norm_updates:
            return
        for k_low, (orig_k, v) in norm_updates[sec].items():
            if k_low not in seen_keys[sec] and v is not None:
                out.append(f"{orig_k} = {v}")
                seen_keys[sec].add(k_low)

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            _flush_remaining(current_sec)
            sec_name = stripped[1:-1].strip()
            current_sec = sec_name
            seen_sections.add(sec_name)
            out.append(line)
            continue

        if current_sec in norm_updates and "=" in line and not stripped.startswith(("#", ";")):
            raw_k = line.split("=", 1)[0].strip()
            k_low = raw_k.lower()
            if k_low in norm_updates[current_sec]:
                orig_k, new_val = norm_updates[current_sec][k_low]
                seen_keys[current_sec].add(k_low)
                if new_val is not None:
                    out.append(f"{orig_k} = {new_val}")
                continue
        out.append(line)

    _flush_remaining(current_sec)

    for sec, kv_map in section_updates.items():
        if sec in seen_sections:
            continue
        non_null = [(k, v) for k, v in kv_map.items() if v is not None]
        if not non_null:
            continue
        if out and out[-1].strip():
            out.append("")
        out.append(f"[{sec}]")
        for k, v in non_null:
            out.append(f"{k} = {v}")

    return "\n".join(out) + "\n"


def update_ini_sections(
    cfg_file: Path,
    cfg_dir: Path,
    section_updates: dict[str, dict[str, str | None]],
) -> str:
    """读取并原子更新指定 INI 文件，返回更新后的文本。"""
    raw_text = cfg_file.read_text(encoding="utf-8")
    new_text = update_ini_text(raw_text, section_updates)
    atomic_write(cfg_file, new_text, [cfg_dir])
    return new_text


def validate_cross_constraints(
    cfg: InstanceConfig,
    ext: ExtConfig,
    changed_sections: set[str] | None = None,
) -> None:
    """校验跨节业务约束（vault 结构、local_only 路径、适配器引用、审批端归属与既有收件领域兼容）。"""
    vs = check_vault_structure(cfg.vault)
    if vs.level == "FAIL":
        raise ConfigError(f"记忆库路径无效或尚未初始化: {vs.message}")

    invalid_lo: list[str] = []
    for rel in cfg.local_only:
        rel_path = Path(rel)
        parts = rel.replace("\\", "/").split("/")
        if rel_path.is_absolute() or os.path.isabs(rel) or ".." in rel_path.parts or ".." in parts:
            invalid_lo.append(rel)
    if invalid_lo:
        raise ConfigError(f"local_only 路径非法（不能为绝对路径或包含 ..）: {', '.join(invalid_lo)}")

    for job_name, chain in cfg.jobs.items():
        for adapter_name, _ in chain:
            if adapter_name not in cfg.adapters:
                raise ConfigError(f"[jobs].{job_name} 引用了未配置的适配器: {adapter_name}")

    if cfg.dispatch_adapter and cfg.dispatch_adapter not in cfg.adapters:
        raise ConfigError(f"[jobs].dispatch 引用了未配置的适配器: {cfg.dispatch_adapter}")

    for target_door, chain in ext.routing_chains.items():
        for adapter_name, _ in chain:
            if adapter_name not in cfg.adapters:
                raise ConfigError(f"[routing].{target_door} 引用了未配置的适配器: {adapter_name}")

    unknown_app = [d for d in ext.approval_doors if d not in cfg.known_doors]
    if unknown_app:
        raise ConfigError(f"[approval].doors 必须全部包含在 [doors].known 中，未知端: {', '.join(unknown_app)}")

    unknown_trusted = [d for d in ext.trusted_doors if d not in cfg.known_doors]
    if unknown_trusted:
        raise ConfigError(f"[routing].trusted_doors 必须全部包含在 [doors].known 中，未知端: {', '.join(unknown_trusted)}")

    if changed_sections is None or "domains" in changed_sections or "instance" in changed_sections:
        inbox_chk = check_inbox_lines(cfg.vault, allowed_domains=cfg.allowed_domains)
        if inbox_chk.level == "FAIL":
            raise ConfigError(f"修改后的领域与既有收件记录冲突: {inbox_chk.message}")


def apply_config_updates(
    instance: str,
    section_updates: dict[str, dict[str, str | None]],
) -> tuple[InstanceConfig, ExtConfig, Any]:
    """原子修改实例 config.ini 并全量校验（含跨节约束）；若配置非法则自动回滚原文件。"""
    from hushai_agent.brain_cfg import load_brain_config

    cfg_file = get_instance_config_file(instance)
    cfg_dir = get_instance_config_dir(instance)
    original_text = cfg_file.read_text(encoding="utf-8")
    new_text = update_ini_text(original_text, section_updates)
    atomic_write(cfg_file, new_text, [cfg_dir])
    try:
        new_cfg = load_instance_config(instance)
        new_ext = load_ext(new_cfg)
        new_brain = load_brain_config(new_cfg)
        validate_cross_constraints(new_cfg, new_ext, changed_sections=set(section_updates))
    except (ConfigError, ValueError, OSError):
        atomic_write(cfg_file, original_text, [cfg_dir])
        raise
    return new_cfg, new_ext, new_brain
