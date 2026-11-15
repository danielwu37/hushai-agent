"""生命周期管理命令：导出备份（export）、框架模板升级（upgrade）、实例卸载（uninstall）。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Callable

from hushai_agent.config import ConfigError, InstanceConfig, load_instance_config
from hushai_agent.config_edit import list_instances
from hushai_agent.digest_apply import commit_hub_if_git
from hushai_agent.fsio import atomic_write
from hushai_agent.paths import (
    get_config_root,
    get_instance_config_dir,
    get_instance_config_file,
    get_instance_state_dir,
    get_state_root,
    get_templates_vault_dir,
)

DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")
DEFAULT_DEPLOY_APP_DIR = Path("/opt/hushai-agent")
_PROTECTED_DEV_REPO = Path("/root/hushai-agent")

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
            ["git", "-c", "user.name=hushAi-agent", "-c", "user.email=hushai@local",
             "commit", "-m", "upgrade framework templates"],
            None, cfg.vault, 15,
        )
        git_committed = rc == 0 or commit_hub_if_git(cfg.vault, "upgrade framework templates")
    return {"ok": True, "updated": updated, "git_committed": git_committed}


def _default_bin_link() -> Path:
    return Path.home() / ".local" / "bin" / "hushai"


def _is_safe_delete_dir(path: Path) -> bool:
    resolved = path.resolve()
    if len(resolved.parts) < 3:
        return False
    protected = {
        Path("/"),
        Path.home().resolve(),
        Path("/opt").resolve(),
        Path("/root").resolve(),
        Path("/home").resolve(),
        Path("/tmp").resolve(),
        Path("/etc").resolve(),
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


def find_systemd_units(
    instance: str | None = None,
    systemd_dir: Path | str | None = None,
) -> tuple[list[Path], list[Path]]:
    """查找 systemd 目录及 *.wants 子目录下属于指定实例（或全部 hushai-*）的单元与软链。"""
    sdir = Path(systemd_dir) if systemd_dir is not None else DEFAULT_SYSTEMD_DIR
    prefix = f"hushai-{instance}-" if instance else "hushai-"
    if not sdir.is_dir():
        return [], []
    unit_files = sorted(p for p in sdir.iterdir() if p.name.startswith(prefix) and not p.is_dir())
    symlinks = sorted(
        p
        for wants in sdir.glob("*.wants")
        if wants.is_dir()
        for p in wants.iterdir()
        if p.name.startswith(prefix)
    )
    return unit_files, symlinks


def cleanup_systemd_units(
    instance: str | None = None,
    *,
    yes: bool = False,
    systemd_dir: Path | str | None = None,
) -> dict[str, Any]:
    """停用并删除本机 hushai systemd 服务/定时器/路径单元（无 systemd 运行态时安全降级为直接删文件）。"""
    sdir = Path(systemd_dir) if systemd_dir is not None else DEFAULT_SYSTEMD_DIR
    unit_files, symlinks = find_systemd_units(instance, systemd_dir=sdir)
    all_paths = symlinks + unit_files
    found = [str(p) for p in all_paths]
    prefix = f"hushai-{instance}-*" if instance else "hushai-*"
    if instance:
        disable_hint = (
            f"sudo systemctl disable --now hushai-{instance}-watch.path "
            f"hushai-{instance}-digest.timer hushai-{instance}-weekly.timer hushai-{instance}-serve.service"
        )
    else:
        disable_hint = "sudo systemctl disable --now 'hushai-*'"
    commands = [
        disable_hint,
        f"sudo rm -f {sdir / prefix} {sdir / '*.wants' / prefix}",
        "sudo systemctl daemon-reload",
    ]
    removed: list[str] = []
    errors: list[str] = []
    if yes and all_paths:
        is_live_systemd = Path("/run/systemd/system").is_dir() and sdir.resolve() == DEFAULT_SYSTEMD_DIR.resolve()
        unit_names = [
            p.name for p in unit_files if p.name.endswith((".service", ".timer", ".path"))
        ]
        if is_live_systemd and unit_names:
            subprocess.run(
                ["systemctl", "disable", "--now", *unit_names],
                capture_output=True,
                text=True,
                check=False,
            )
        for path in all_paths:
            try:
                path.unlink(missing_ok=True)
                removed.append(str(path))
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        if is_live_systemd and removed:
            subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True, check=False)

    return {
        "found": found,
        "removed": removed,
        "errors": errors,
        "commands": commands,
    }


def _resolve_instance_dirs(instance: str, keep_vault: bool) -> tuple[list[Path], str | None]:
    dirs, _vault, load_error = _resolve_instance_dirs_detail(instance, keep_vault=keep_vault)
    return dirs, load_error


def _resolve_instance_dirs_detail(
    instance: str, keep_vault: bool
) -> tuple[list[Path], Path | None, str | None]:
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

    removed_paths: list[str] = []
    errors: list[str] = []
    if yes:
        for path in targets:
            if path.is_dir():
                _safe_rmtree(path, removed_paths, errors)

    sd_info = cleanup_systemd_units(
        instance,
        yes=bool(yes and remove_systemd),
        systemd_dir=systemd_dir,
    )
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
    """彻底清理本机全部 hushai 实例配置/状态、systemd 单元及可选软链/部署目录（默认永久保留 vault）。"""
    instances = list_instances()
    dir_targets: list[Path] = []
    seen_dirs: set[str] = set()
    preserved_vaults: list[str] = []
    seen_vaults: set[str] = set()
    errors: list[str] = []

    for inst in instances:
        inst_dirs, vault_dir, load_err = _resolve_instance_dirs_detail(inst, keep_vault=keep_vault)
        if load_err:
            errors.append(f"实例 {inst} 配置异常（已按配置/状态目录清理）: {load_err}")
        if keep_vault and vault_dir is not None:
            vkey = str(vault_dir)
            if vkey not in seen_vaults:
                seen_vaults.add(vkey)
                preserved_vaults.append(vkey)
        for d in inst_dirs:
            key = str(d)
            if key not in seen_dirs:
                seen_dirs.add(key)
                dir_targets.append(d)

    # 若 config 根目录存在残余目录也纳入预览与清理，保留已有列表顺序
    cfg_root = get_config_root().resolve()
    state_root = get_state_root().resolve()

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

    sd_info = cleanup_systemd_units(
        None,
        yes=bool(yes and remove_systemd),
        systemd_dir=systemd_dir,
    )
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

    app_path = Path(app_dir).expanduser() if app_dir is not None else DEFAULT_DEPLOY_APP_DIR
    app_target: str | None = None
    app_dir_removed = False
    app_dir_skipped_reason: str | None = None
    if remove_app_dir and app_path.exists():
        can_rm, reason = _can_remove_app_dir(app_path)
        if can_rm:
            app_target = str(app_path.resolve())
            if yes:
                app_dir_removed = _safe_rmtree(app_path.resolve(), removed_paths, errors)
        else:
            app_dir_skipped_reason = reason

    return {
        "ok": len(errors) == 0,
        "dry_run": not yes,
        "keep_vault": keep_vault,
        "preserved_vaults": preserved_vaults,
        "instances": instances,
        "targets": [str(d) for d in dir_targets],
        "removed": removed_paths,
        "systemd": sd_info,
        "bin_link_target": bin_link_target,
        "bin_link_removed": bin_link_removed,
        "app_dir_target": app_target,
        "app_dir_removed": app_dir_removed,
        "app_dir_skipped_reason": app_dir_skipped_reason,
        "errors": errors,
    }


def _ask_yes_no(read_line: Callable[[str], str], prompt: str, default: bool) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    raw = read_line(f"{prompt} {hint}: ").strip().lower()
    if not raw:
        return default
    if raw in ("y", "yes", "1", "是"):
        return True
    if raw in ("n", "no", "0", "否"):
        return False
    raise ValueError(f"无法识别的输入: {raw}")


def _print_uninstall_result(io_write: Callable[[str], None], res: dict[str, Any], sd_info: dict[str, Any]) -> None:
    if res.get("removed"):
        io_write("✓ 已删除目录:")
        for p in res["removed"]:
            io_write(f"  - {p}")
    else:
        io_write("（未发现需要删除的实例数据目录）")
    if res.get("keep_vault", True) and res.get("preserved_vaults"):
        io_write("✓ 已永久保留记忆库 vault（个人笔记安全未动）:")
        for p in res["preserved_vaults"]:
            io_write(f"  - {p}")
    if sd_info.get("removed"):
        io_write("✓ 已清理 systemd 单元:")
        for p in sd_info["removed"]:
            io_write(f"  - {p}")
    if res.get("bin_link_removed"):
        io_write(f"✓ 已移除命令软链: {res.get('bin_link_target')}")
    if res.get("app_dir_skipped_reason"):
        io_write(f"! {res['app_dir_skipped_reason']}")
    if res.get("errors"):
        io_write("! 部分项目未能自动清理（可能需要 sudo 权限）:")
        for err in res["errors"]:
            io_write(f"  - {err}")
        for cmd in sd_info.get("commands", []):
            io_write(f"    {cmd}")


def _confirm_delete_vault_interactive(
    read_line: Callable[[str], str],
    io_write: Callable[[str], None],
    vault_paths: list[str],
) -> bool:
    """二次强确认是否销毁 vault。返回 True 表示保留 vault，仅精确输入 DELETE-VAULT 时返回 False。"""
    if vault_paths:
        io_write("⚠️ 安全保护：以下 vault 记忆库存放你的个人核心资料，卸载默认永久保留：")
        for vp in vault_paths:
            io_write(f"  - {vp}")
    want_wipe = _ask_yes_no(
        read_line,
        "是否确定要连同上述 vault 记忆库一并永久销毁？（正常卸载或重装程序选 n 保留即可）",
        default=False,
    )
    if not want_wipe:
        return True
    wipe_token = read_line(
        "⚠️ 危险二次确认：销毁后个人笔记不可恢复！请输入 DELETE-VAULT 确认删除记忆库（直接回车或其它输入仍保留 vault）: "
    ).strip()
    if wipe_token != "DELETE-VAULT":
        io_write("未输入 DELETE-VAULT，已自动改为永久保留 vault。")
        return True
    return False


def interactive_uninstall(
    read_line: Callable[[str], str],
    io_write: Callable[[str], None],
    *,
    current_instance: str | None = None,
    systemd_dir: Path | str | None = None,
    bin_link: Path | str | None = None,
    app_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    """交互式一键卸载向导：供 hushai config 菜单与 bin/hushai-uninstall 共用（默认永久保留 vault）。"""
    instances = list_instances()
    all_units, all_symlinks = find_systemd_units(None, systemd_dir=systemd_dir)
    blink = Path(bin_link).expanduser() if bin_link is not None else _default_bin_link()
    has_blink = blink.is_symlink() or blink.is_file()
    app_path = Path(app_dir).expanduser() if app_dir is not None else DEFAULT_DEPLOY_APP_DIR
    can_rm_app, _ = _can_remove_app_dir(app_path) if app_path.exists() else (False, None)

    io_write("\n--- 一键卸载 / 环境清理（默认永久保留 vault 记忆库）---")
    io_write(f"  已发现实例: {', '.join(instances) if instances else '（无）'}")
    io_write(f"  本机 systemd 文件/软链: {len(all_units) + len(all_symlinks)} 个")
    io_write(f"  命令软链: {blink if has_blink else '（未检测到）'}")
    if can_rm_app:
        io_write(f"  部署目录: {app_path.resolve()}")

    io_write(f"  1. 卸载指定实例{f'（当前: {current_instance}）' if current_instance else ''}")
    io_write("  2. 彻底清理程序环境（清理全部实例配置/状态 + systemd + 命令软链，默认保留 vault）")
    io_write("  0. 取消返回")
    mode = read_line("请选择操作 [1/2/0]: ").strip()
    if mode in ("0", "", "q", "back"):
        io_write("已取消卸载。")
        return None

    if mode == "1":
        target_inst = current_instance
        if not target_inst:
            if not instances:
                io_write("当前没有可卸载的实例。")
                return None
            if len(instances) == 1:
                target_inst = instances[0]
            else:
                for idx, name in enumerate(instances, start=1):
                    io_write(f"  {idx}. {name}")
                pick = read_line("选择要卸载的实例编号或名称: ").strip()
                if pick.isdigit() and 1 <= int(pick) <= len(instances):
                    target_inst = instances[int(pick) - 1]
                else:
                    target_inst = pick
        if not target_inst or not get_instance_config_file(target_inst).is_file():
            io_write(f"未找到实例: {target_inst}")
            return None

        _, vault_dir, _ = _resolve_instance_dirs_detail(target_inst, keep_vault=True)
        vault_list = [str(vault_dir)] if vault_dir is not None else []
        rm_systemd = _ask_yes_no(read_line, "是否同时清理该实例的 systemd 服务与定时器？", default=True)
        keep_vault = _confirm_delete_vault_interactive(read_line, io_write, vault_list)
        preview = run_uninstall(
            target_inst,
            yes=False,
            keep_vault=keep_vault,
            remove_systemd=rm_systemd,
            systemd_dir=systemd_dir,
        )
        io_write("\n以下内容将被永久删除：")
        for p in preview["targets"]:
            io_write(f"  - [目录] {p}")
        for p in preview["systemd_targets"]:
            io_write(f"  - [systemd] {p}")
        if keep_vault and preview.get("preserved_vault"):
            io_write(f"  - [安全保留] 记忆库 {preview['preserved_vault']}")
        confirm = read_line(f"请输入实例名 '{target_inst}' 确认卸载（其他输入取消）: ").strip()
        if confirm != target_inst:
            io_write("确认名称不匹配，已取消卸载。")
            return None
        res = run_uninstall(
            target_inst,
            yes=True,
            keep_vault=keep_vault,
            remove_systemd=rm_systemd,
            systemd_dir=systemd_dir,
        )
        _print_uninstall_result(
            io_write,
            res,
            {"removed": res["systemd_removed"], "commands": res["systemd_commands"]},
        )
        return res

    if mode == "2":
        vault_list: list[str] = []
        seen_v: set[str] = set()
        for inst in instances:
            _, vdir, _ = _resolve_instance_dirs_detail(inst, keep_vault=True)
            if vdir is not None and str(vdir) not in seen_v:
                seen_v.add(str(vdir))
                vault_list.append(str(vdir))
        rm_systemd = _ask_yes_no(read_line, "是否清理本机全部 hushai-* systemd 单元？", default=True)
        rm_blink = _ask_yes_no(read_line, f"是否移除命令软链 {blink}？", default=True) if has_blink else False
        rm_app = _ask_yes_no(read_line, f"是否删除部署目录 {app_path.resolve()}？", default=False) if can_rm_app else False
        keep_vault = _confirm_delete_vault_interactive(read_line, io_write, vault_list)
        preview = run_purge_all(
            yes=False,
            keep_vault=keep_vault,
            remove_systemd=rm_systemd,
            remove_bin_link=rm_blink,
            remove_app_dir=rm_app,
            systemd_dir=systemd_dir,
            bin_link=blink,
            app_dir=app_path,
        )
        io_write("\n以下内容将被永久删除：")
        for p in preview["targets"]:
            io_write(f"  - [目录] {p}")
        for p in preview["systemd"]["found"]:
            io_write(f"  - [systemd] {p}")
        if rm_blink and preview["bin_link_target"]:
            io_write(f"  - [软链] {preview['bin_link_target']}")
        if rm_app and preview["app_dir_target"]:
            io_write(f"  - [部署代码] {preview['app_dir_target']}")
        if keep_vault and preview.get("preserved_vaults"):
            for vp in preview["preserved_vaults"]:
                io_write(f"  - [安全保留] 记忆库 {vp}")
        confirm = read_line("请输入 RESET 确认执行清理（其他输入取消）: ").strip()
        if confirm != "RESET":
            io_write("未输入 RESET，已取消清理。")
            return None
        res = run_purge_all(
            yes=True,
            keep_vault=keep_vault,
            remove_systemd=rm_systemd,
            remove_bin_link=rm_blink,
            remove_app_dir=rm_app,
            systemd_dir=systemd_dir,
            bin_link=blink,
            app_dir=app_path,
        )
        _print_uninstall_result(io_write, res, res["systemd"])
        return res

    io_write("无效选项，已取消。")
    return None


def uninstall_cli_main(argv: list[str] | None = None) -> int:
    """独立脚本 `bin/hushai-uninstall` 的 CLI 主入口（默认永久保留 vault）。"""
    parser = argparse.ArgumentParser(
        prog="hushai-uninstall",
        description="hushAi-agent 一键卸载与环境清理脚本（默认永久保留 vault，不带参数进入交互引导，加 --yes 执行清理）",
    )
    parser.add_argument("--instance", default=None, help="只卸载指定实例")
    parser.add_argument("--all", action="store_true", help="重置全部实例的程序配置/状态与环境（默认保留 vault）")
    vgroup = parser.add_mutually_exclusive_group()
    vgroup.add_argument("--keep-vault", action="store_true", help="（默认行为）永久保留 vault 记忆库目录")
    vgroup.add_argument("--delete-vault", action="store_true", help="【危险】连同 vault 记忆库目录一起永久删除")
    parser.add_argument("--no-systemd", action="store_true", help="跳过清理 systemd 单元")
    parser.add_argument("--remove-bin-link", action="store_true", help="同时删除 ~/.local/bin/hushai 软链")
    parser.add_argument("--remove-app", action="store_true", help="同时删除部署目录（如 /opt/hushai-agent）")
    parser.add_argument("--yes", action="store_true", help="确认执行删除（默认仅演练预览）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    parser.add_argument("--systemd-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--bin-link", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--app-dir", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    has_action_flags = any([
        args.instance,
        args.all,
        args.yes,
        args.json,
        args.keep_vault,
        args.delete_vault,
        args.remove_bin_link,
        args.remove_app,
    ])
    if not has_action_flags and sys.stdin.isatty():
        try:
            interactive_uninstall(
                input,
                print,
                systemd_dir=args.systemd_dir,
                bin_link=args.bin_link,
                app_dir=args.app_dir,
            )
            return 0
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 0

    keep_vault = not args.delete_vault
    if args.instance and not args.all:
        res = run_uninstall(
            args.instance,
            yes=args.yes,
            keep_vault=keep_vault,
            remove_systemd=not args.no_systemd,
            systemd_dir=args.systemd_dir,
        )
        sd_info = {"removed": res["systemd_removed"], "commands": res["systemd_commands"]}
    else:
        res = run_purge_all(
            yes=args.yes,
            keep_vault=keep_vault,
            remove_systemd=not args.no_systemd,
            remove_bin_link=args.remove_bin_link,
            remove_app_dir=args.remove_app,
            systemd_dir=args.systemd_dir,
            bin_link=args.bin_link,
            app_dir=args.app_dir,
        )
        sd_info = res["systemd"]

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif res["dry_run"]:
        print("【演练模式】未删除任何文件。追加 --yes 执行清理以下目标：")
        for p in res.get("targets", []):
            print(f"  - [目录] {p}")
        for p in sd_info.get("found", res.get("systemd_targets", [])):
            print(f"  - [systemd] {p}")
        if res.get("bin_link_target") and args.remove_bin_link:
            print(f"  - [软链] {res['bin_link_target']}")
        if res.get("app_dir_target"):
            print(f"  - [部署目录] {res['app_dir_target']}")
        if keep_vault and res.get("preserved_vaults"):
            for vp in res["preserved_vaults"]:
                print(f"  - [安全保留] 记忆库 {vp}")
    else:
        _print_uninstall_result(print, res, sd_info)
    return 0 if res.get("ok", True) else 1
