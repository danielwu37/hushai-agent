"""交互式一键卸载向导与 bin/hushai-uninstall CLI 主入口（从 lifecycle.py 拆出）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from hushai_agent.config_edit import list_instances
from hushai_agent.lifecycle_uninstall import (
    _can_remove_app_dir,
    _default_bin_link,
    _lifecycle_defaults,
    _resolve_instance_dirs_detail,
    find_systemd_units,
    run_purge_all,
    run_uninstall,
)
from hushai_agent.paths import get_instance_config_file


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


def _confirm_delete_vault_interactive(read_line: Callable[[str], str], io_write: Callable[[str], None], vault_paths: list[str]) -> bool:
    if vault_paths:
        io_write("⚠️ 安全保护：以下 vault 记忆库存放你的个人核心资料，卸载默认永久保留：")
        for vp in vault_paths:
            io_write(f"  - {vp}")
    if not _ask_yes_no(read_line, "是否确定要连同上述 vault 记忆库一并永久销毁？（正常卸载或重装程序选 n 保留即可）", default=False):
        return True
    wipe_token = read_line("⚠️ 危险二次确认：销毁后个人笔记不可恢复！请输入 DELETE-VAULT 确认删除记忆库（直接回车或其它输入仍保留 vault）: ").strip()
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
    _, def_appdir = _lifecycle_defaults()
    instances = list_instances()
    all_units, all_symlinks = find_systemd_units(None, systemd_dir=systemd_dir)
    blink = Path(bin_link).expanduser() if bin_link is not None else _default_bin_link()
    has_blink = blink.is_symlink() or blink.is_file()
    app_path = Path(app_dir).expanduser() if app_dir is not None else def_appdir
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
                target_inst = instances[int(pick) - 1] if (pick.isdigit() and 1 <= int(pick) <= len(instances)) else pick
        if not target_inst or not get_instance_config_file(target_inst).is_file():
            io_write(f"未找到实例: {target_inst}")
            return None

        _, vault_dir, _ = _resolve_instance_dirs_detail(target_inst, keep_vault=True)
        rm_systemd = _ask_yes_no(read_line, "是否同时清理该实例的 systemd 服务与定时器？", default=True)
        keep_vault = _confirm_delete_vault_interactive(read_line, io_write, [str(vault_dir)] if vault_dir else [])
        preview = run_uninstall(target_inst, yes=False, keep_vault=keep_vault, remove_systemd=rm_systemd, systemd_dir=systemd_dir)
        io_write("\n以下内容将被永久删除：")
        for p in preview["targets"]:
            io_write(f"  - [目录] {p}")
        for p in preview["systemd_targets"]:
            io_write(f"  - [systemd] {p}")
        if keep_vault and preview.get("preserved_vault"):
            io_write(f"  - [安全保留] 记忆库 {preview['preserved_vault']}")
        if read_line(f"请输入实例名 '{target_inst}' 确认卸载（其他输入取消）: ").strip() != target_inst:
            io_write("确认名称不匹配，已取消卸载。")
            return None
        res = run_uninstall(target_inst, yes=True, keep_vault=keep_vault, remove_systemd=rm_systemd, systemd_dir=systemd_dir)
        _print_uninstall_result(io_write, res, {"removed": res["systemd_removed"], "commands": res["systemd_commands"]})
        return res

    if mode == "2":
        vault_list, seen_v = [], set()
        for inst in instances:
            _, vdir, _ = _resolve_instance_dirs_detail(inst, keep_vault=True)
            if vdir is not None and str(vdir) not in seen_v:
                seen_v.add(str(vdir))
                vault_list.append(str(vdir))
        rm_systemd = _ask_yes_no(read_line, "是否清理本机全部 hushai-* systemd 单元？", default=True)
        rm_blink = _ask_yes_no(read_line, f"是否移除命令软链 {blink}？", default=True) if has_blink else False
        rm_app = _ask_yes_no(read_line, f"是否删除部署目录 {app_path.resolve()}？", default=False) if can_rm_app else False
        keep_vault = _confirm_delete_vault_interactive(read_line, io_write, vault_list)
        preview = run_purge_all(yes=False, keep_vault=keep_vault, remove_systemd=rm_systemd, remove_bin_link=rm_blink, remove_app_dir=rm_app, systemd_dir=systemd_dir, bin_link=blink, app_dir=app_path)
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
        if read_line("请输入 RESET 确认执行清理（其他输入取消）: ").strip() != "RESET":
            io_write("未输入 RESET，已取消清理。")
            return None
        res = run_purge_all(yes=True, keep_vault=keep_vault, remove_systemd=rm_systemd, remove_bin_link=rm_blink, remove_app_dir=rm_app, systemd_dir=systemd_dir, bin_link=blink, app_dir=app_path)
        _print_uninstall_result(io_write, res, res["systemd"])
        return res

    io_write("无效选项，已取消。")
    return None


def uninstall_cli_main(argv: list[str] | None = None) -> int:
    """独立脚本 `bin/hushai-uninstall` 的 CLI 主入口（默认永久保留 vault）。"""
    parser = argparse.ArgumentParser(prog="hushai-uninstall", description="hushAi-agent 一键卸载与环境清理脚本（默认永久保留 vault）")
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

    if not any([args.instance, args.all, args.yes, args.json, args.keep_vault, args.delete_vault, args.remove_bin_link, args.remove_app]) and sys.stdin.isatty():
        try:
            interactive_uninstall(input, print, systemd_dir=args.systemd_dir, bin_link=args.bin_link, app_dir=args.app_dir)
            return 0
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 0

    keep_vault = not args.delete_vault
    if args.instance and not args.all:
        res = run_uninstall(args.instance, yes=args.yes, keep_vault=keep_vault, remove_systemd=not args.no_systemd, systemd_dir=args.systemd_dir)
        sd_info = {"removed": res["systemd_removed"], "commands": res["systemd_commands"]}
    else:
        res = run_purge_all(yes=args.yes, keep_vault=keep_vault, remove_systemd=not args.no_systemd, remove_bin_link=args.remove_bin_link, remove_app_dir=args.remove_app, systemd_dir=args.systemd_dir, bin_link=args.bin_link, app_dir=args.app_dir)
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
