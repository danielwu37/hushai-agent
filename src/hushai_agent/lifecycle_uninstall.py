"""实例卸载、systemd 清理与环境彻底清理核心逻辑（从 lifecycle.py 拆出）。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from hushai_agent.config import ConfigError, load_instance_config
from hushai_agent.config_edit import list_instances
from hushai_agent.paths import (
    get_config_root,
    get_instance_config_dir,
    get_instance_state_dir,
    get_state_root,
)

_PROTECTED_DEV_REPO = Path("/root/hushai-agent")


def _lifecycle_defaults() -> tuple[Path, Path]:
    mod = sys.modules.get("hushai_agent.lifecycle")
    sdir = getattr(mod, "DEFAULT_SYSTEMD_DIR", Path("/etc/systemd/system")) if mod else Path("/etc/systemd/system")
    appdir = getattr(mod, "DEFAULT_DEPLOY_APP_DIR", Path("/opt/hushai-agent")) if mod else Path("/opt/hushai-agent")
    return sdir, appdir


def _default_bin_link() -> Path:
    return Path.home() / ".local" / "bin" / "hushai"


def _is_safe_delete_dir(path: Path) -> bool:
    resolved = path.resolve()
    if len(resolved.parts) < 3:
        return False
    protected = {
        Path("/"), Path.home().resolve(), Path("/opt").resolve(),
        Path("/root").resolve(), Path("/home").resolve(),
        Path("/tmp").resolve(), Path("/etc").resolve(),
    }
    return resolved not in protected


def _safe_rmtree(path: Path, removed: list[str], errors: list[str]) -> bool:
    if not path.exists():
        return False
    if not _is_safe_delete_dir(path):
        errors.append(f"安全护栏拒绝删除路径: {path}")
        return False
    try:
        shutil.rmtree(path)
        removed.append(str(path))
        return True
    except OSError as exc:
        errors.append(f"{path}: {exc}")
        return False


def _can_remove_app_dir(app_dir: Path) -> tuple[bool, str | None]:
    resolved = app_dir.resolve()
    if resolved == _PROTECTED_DEV_REPO.resolve():
        return False, f"安全护栏保护开发仓库，不自动删除: {resolved}"
    if not _is_safe_delete_dir(resolved):
        return False, f"安全护栏拒绝删除根目录路径: {resolved}"
    if not ((resolved / "bin" / "hushai").is_file() or (resolved / "src" / "hushai_agent").is_dir()):
        return False, f"目录不是可识别的 hushai-agent 部署目录，已跳过: {resolved}"
    return True, None


def find_systemd_units(instance: str | None = None, systemd_dir: Path | str | None = None) -> tuple[list[Path], list[Path]]:
    """查找 systemd 目录及 *.wants 子目录下属于指定实例（或全部 hushai-*）的单元与软链。"""
    def_sdir, _ = _lifecycle_defaults()
    sdir = Path(systemd_dir) if systemd_dir is not None else def_sdir
    prefix = f"hushai-{instance}-" if instance else "hushai-"
    if not sdir.is_dir():
        return [], []
    unit_files = sorted(p for p in sdir.iterdir() if p.name.startswith(prefix) and not p.is_dir())
    symlinks = sorted(
        p for wants in sdir.glob("*.wants") if wants.is_dir()
        for p in wants.iterdir() if p.name.startswith(prefix)
    )
    return unit_files, symlinks


def cleanup_systemd_units(
    instance: str | None = None,
    *,
    yes: bool = False,
    systemd_dir: Path | str | None = None,
) -> dict[str, Any]:
    """停用并删除本机 hushai systemd 服务/定时器/路径单元（无 systemd 运行态时安全降级）。"""
    def_sdir, _ = _lifecycle_defaults()
    sdir = Path(systemd_dir) if systemd_dir is not None else def_sdir
    unit_files, symlinks = find_systemd_units(instance, systemd_dir=sdir)
    all_paths = symlinks + unit_files
    found = [str(p) for p in all_paths]
    prefix = f"hushai-{instance}-*" if instance else "hushai-*"
    disable_hint = (
        f"sudo systemctl disable --now hushai-{instance}-watch.path "
        f"hushai-{instance}-digest.timer hushai-{instance}-weekly.timer hushai-{instance}-serve.service"
    ) if instance else "sudo systemctl disable --now 'hushai-*'"
    commands = [disable_hint, f"sudo rm -f {sdir / prefix} {sdir / '*.wants' / prefix}", "sudo systemctl daemon-reload"]
    removed: list[str] = []
    errors: list[str] = []
    if yes and all_paths:
        is_live_systemd = Path("/run/systemd/system").is_dir() and sdir.resolve() == def_sdir.resolve()
        unit_names = [p.name for p in unit_files if p.name.endswith((".service", ".timer", ".path"))]
        if is_live_systemd and unit_names:
            subprocess.run(["systemctl", "disable", "--now", *unit_names], capture_output=True, text=True, check=False)
        for path in all_paths:
            try:
                path.unlink(missing_ok=True)
                removed.append(str(path))
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        if is_live_systemd and removed:
            subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True, check=False)
    return {"found": found, "removed": removed, "errors": errors, "commands": commands}


def _resolve_instance_dirs_detail(instance: str, keep_vault: bool) -> tuple[list[Path], Path | None, str | None]:
    cfg_dir = get_instance_config_dir(instance).resolve()
    state_dir = get_instance_state_dir(instance).resolve()
    targets: list[Path] = [cfg_dir, state_dir]
    vault_dir: Path | None = None
    load_error: str | None = None
    try:
        cfg = load_instance_config(instance)
        vault_dir = cfg.vault.resolve()
        if not keep_vault:
            targets.append(vault_dir)
    except (ConfigError, ValueError, OSError) as exc:
        load_error = str(exc)
    return targets, vault_dir, load_error


def _resolve_instance_dirs(instance: str, keep_vault: bool) -> tuple[list[Path], str | None]:
    dirs, _vault, load_error = _resolve_instance_dirs_detail(instance, keep_vault=keep_vault)
    return dirs, load_error


def run_uninstall(
    instance: str,
    yes: bool = False,
    keep_vault: bool = True,
    remove_systemd: bool = False,
    systemd_dir: Path | str | None = None,
) -> dict[str, Any]:
    """卸载本实例：默认永久保留 vault 记忆库；不带 --yes 仅演练预览，加 --yes 才真正删除。"""
    cfg = load_instance_config(instance)
    cfg_dir = get_instance_config_dir(instance).resolve()
    state_dir = get_instance_state_dir(instance).resolve()
    vault_dir = cfg.vault.resolve()

    targets = [cfg_dir, state_dir]
    preserved_vaults = [str(vault_dir)] if keep_vault else []
    if not keep_vault:
        targets.append(vault_dir)

    removed_paths, errors = [], []
    if yes:
        for path in targets:
            if path.is_dir():
                _safe_rmtree(path, removed_paths, errors)

    sd_info = cleanup_systemd_units(instance, yes=bool(yes and remove_systemd), systemd_dir=systemd_dir)
    errors.extend(sd_info["errors"])
    return {
        "ok": len(errors) == 0,
        "instance": instance,
        "dry_run": not yes,
        "keep_vault": keep_vault,
        "preserved_vault": str(vault_dir) if keep_vault else None,
        "preserved_vaults": preserved_vaults,
        "targets": [str(p) for p in targets],
        "removed": removed_paths,
        "systemd_commands": sd_info["commands"],
        "systemd_targets": sd_info["found"],
        "systemd_removed": sd_info["removed"],
        "systemd_errors": sd_info["errors"],
        "errors": errors,
    }


def run_purge_all(
    *,
    yes: bool = False,
    keep_vault: bool = True,
    remove_systemd: bool = True,
    remove_bin_link: bool = False,
    remove_app_dir: bool = False,
    systemd_dir: Path | str | None = None,
    bin_link: Path | str | None = None,
    app_dir: Path | str | None = None,
) -> dict[str, Any]:
    """彻底清理本机全部 hushai 实例配置/状态、systemd 单元及可选软链/部署目录（默认保留 vault）。"""
    _, def_appdir = _lifecycle_defaults()
    instances = list_instances()
    dir_targets, seen_dirs, preserved_vaults, seen_vaults, errors = [], set(), [], set(), []

    for inst in instances:
        inst_dirs, vault_dir, load_err = _resolve_instance_dirs_detail(inst, keep_vault=keep_vault)
        if load_err:
            errors.append(f"实例 {inst} 配置异常（已按配置/状态目录清理）: {load_err}")
        if keep_vault and vault_dir is not None and str(vault_dir) not in seen_vaults:
            seen_vaults.add(str(vault_dir))
            preserved_vaults.append(str(vault_dir))
        for d in inst_dirs:
            if str(d) not in seen_dirs:
                seen_dirs.add(str(d))
                dir_targets.append(d)

    cfg_root, state_root = get_config_root().resolve(), get_state_root().resolve()
    removed_paths: list[str] = []
    if yes:
        for d in dir_targets:
            if d.is_dir():
                _safe_rmtree(d, removed_paths, errors)
        for root in (cfg_root, state_root):
            try:
                if root.is_dir() and not any(root.iterdir()):
                    root.rmdir()
            except OSError:
                pass

    sd_info = cleanup_systemd_units(None, yes=bool(yes and remove_systemd), systemd_dir=systemd_dir)
    errors.extend(sd_info["errors"])

    blink = Path(bin_link).expanduser() if bin_link is not None else _default_bin_link()
    bin_link_target = str(blink) if (blink.is_symlink() or blink.is_file()) else None
    bin_link_removed = False
    if yes and remove_bin_link and bin_link_target is not None:
        try:
            blink.unlink(missing_ok=True)
            bin_link_removed = True
        except OSError as exc:
            errors.append(f"{blink}: {exc}")

    app_path = Path(app_dir).expanduser() if app_dir is not None else def_appdir
    app_target, app_dir_removed, app_dir_skipped_reason = None, False, None
    if remove_app_dir and app_path.exists():
        can_rm, reason = _can_remove_app_dir(app_path)
        if can_rm:
            app_target = str(app_path.resolve())
            if yes:
                app_dir_removed = _safe_rmtree(app_path.resolve(), removed_paths, errors)
        else:
            app_dir_skipped_reason = reason

    return {
        "ok": len(errors) == 0, "dry_run": not yes, "keep_vault": keep_vault,
        "preserved_vaults": preserved_vaults, "instances": instances,
        "targets": [str(d) for d in dir_targets], "removed": removed_paths, "systemd": sd_info,
        "bin_link_target": bin_link_target, "bin_link_removed": bin_link_removed,
        "app_dir_target": app_target, "app_dir_removed": app_dir_removed,
        "app_dir_skipped_reason": app_dir_skipped_reason, "errors": errors,
    }
