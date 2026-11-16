"""生命周期管理命令：导出备份（export）、框架模板升级（upgrade）、实例卸载与清理（聚合入口）。"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path
from typing import Any

from hushai_agent.config import InstanceConfig
from hushai_agent.digest_apply import commit_hub_if_git
from hushai_agent.fsio import atomic_write
from hushai_agent.lifecycle_cli import (  # noqa: F401
    _ask_yes_no,
    _confirm_delete_vault_interactive,
    _print_uninstall_result,
    interactive_uninstall,
    uninstall_cli_main,
)
from hushai_agent.lifecycle_uninstall import (  # noqa: F401
    _can_remove_app_dir,
    _default_bin_link,
    _is_safe_delete_dir,
    _resolve_instance_dirs,
    _resolve_instance_dirs_detail,
    _safe_rmtree,
    cleanup_systemd_units,
    find_systemd_units,
    run_purge_all,
    run_uninstall,
)
from hushai_agent.paths import (
    get_instance_config_file,
    get_instance_state_dir,
    get_templates_vault_dir,
)

DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")
DEFAULT_DEPLOY_APP_DIR = Path("/opt/hushai-agent")
UPGRADE_MANAGED_FILES = (
    ".gitignore",
    "hub/AGENTS.md",
    "domains/README.md",
)


def _copy_vault_clean(vault: Path, dst_vault: Path) -> int:
    """复制 vault 内容（排除 .git 目录），返回复制的文件数。"""
    count = 0
    for src in sorted(vault.rglob("*")):
        rel = src.relative_to(vault)
        if ".git" in rel.parts:
            continue
        target = dst_vault / rel
        if src.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif src.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            count += 1
    return count


def run_export(cfg: InstanceConfig, out_path: str | Path) -> dict[str, Any]:
    """将本实例的数据、配置（不含令牌凭据）和账本游标导出为目录或 .tar.gz。"""
    out = Path(out_path).expanduser().resolve()
    cfg_file = get_instance_config_file(cfg.name)
    state_dir = get_instance_state_dir(cfg.name)
    is_tar = out.name.endswith((".tar.gz", ".tgz"))

    if is_tar:
        out.parent.mkdir(parents=True, exist_ok=True)
        stage = state_dir / "_export_stage"
        if stage.exists():
            shutil.rmtree(stage)
        try:
            vault_files = _copy_vault_clean(cfg.vault, stage / "vault")
            (stage / "config").mkdir(parents=True, exist_ok=True)
            if cfg_file.is_file():
                shutil.copy2(cfg_file, stage / "config" / "config.ini")
            (stage / "state").mkdir(parents=True, exist_ok=True)
            for jsonl in sorted(state_dir.glob("*.jsonl")):
                shutil.copy2(jsonl, stage / "state" / jsonl.name)
            with tarfile.open(out, "w:gz") as tar:
                tar.add(stage, arcname=f"hushai-{cfg.name}")
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return {"ok": True, "format": "tar.gz", "path": str(out), "vault_files": vault_files}

    if out.exists() and any(out.iterdir()):
        raise ValueError(f"导出目标目录已存在且非空: {out}")
    out.mkdir(parents=True, exist_ok=True)
    vault_files = _copy_vault_clean(cfg.vault, out / "vault")
    (out / "config").mkdir(parents=True, exist_ok=True)
    if cfg_file.is_file():
        shutil.copy2(cfg_file, out / "config" / "config.ini")
    (out / "state").mkdir(parents=True, exist_ok=True)
    for jsonl in sorted(state_dir.glob("*.jsonl")):
        shutil.copy2(jsonl, out / "state" / jsonl.name)
    return {"ok": True, "format": "directory", "path": str(out), "vault_files": vault_files}


def run_upgrade(cfg: InstanceConfig) -> dict[str, Any]:
    """用内置模板更新框架托管文件（AGENTS.md 等），不动 AGENTS.local.md、profile.md 与数据。"""
    tpl_root = get_templates_vault_dir()
    updated: list[str] = []
    for rel in UPGRADE_MANAGED_FILES:
        src = tpl_root / rel
        if not src.is_file():
            continue
        new_text = src.read_text(encoding="utf-8")
        dst = cfg.vault / rel
        old_text = dst.read_text(encoding="utf-8") if dst.is_file() else None
        if old_text != new_text:
            atomic_write(dst, new_text, [cfg.vault])
            updated.append(rel)
    git_committed = False
    if updated and (cfg.vault / ".git").exists():
        from hushai_agent.adapters import proc

        proc.run(["git", "add", "--", *updated], None, cfg.vault, 15)
        rc, _, _, _ = proc.run(
            ["git", "-c", "user.name=hushAi-agent", "-c", "user.email=hushai@local", "commit", "-m", "upgrade framework templates"],
            None, cfg.vault, 15,
        )
        git_committed = rc == 0 or commit_hub_if_git(cfg.vault, "upgrade framework templates")
    return {"ok": True, "updated": updated, "git_committed": git_committed}
