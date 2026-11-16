"""交互式设置与控制菜单（`hushai config`）：零命令门槛管理全部配置、接入与日常操作。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from hushai_agent import doorkit
from hushai_agent.brain_cfg import load_brain_config
from hushai_agent.cli_config_ext import _submenu_brain, _submenu_sync_and_doors
from hushai_agent.cli_config_io import MenuIO, _prompt
from hushai_agent.cli_config_ops import _submenu_daily_ops, _submenu_maintenance
from hushai_agent.cli_config_sections import _submenu_basic, _submenu_doors_domains
from hushai_agent.cli_config_steward import _submenu_adapters, _submenu_steward
from hushai_agent.config import DEFAULT_LANG, DEFAULT_TIMEZONE, load_instance_config
from hushai_agent.config_edit import list_instances
from hushai_agent.paths import get_instance_config_file

__all__ = [
    "MenuIO",
    "register_config_command",
    "dispatch_config_command",
    "run_config_menu",
]


def register_config_command(subparsers: Any) -> None:
    """注册 `hushai config` 交互式设置菜单命令。"""
    p_cfg = subparsers.add_parser("config", help="打开交互式设置与控制菜单（1-N 编号操作，无需记忆命令）")
    p_cfg.add_argument("--instance", default=None, help="默认进入的实例名称（省略时自动识别或列选）")


def dispatch_config_command(args: argparse.Namespace) -> int | None:
    """分发 `hushai config` 命令；不属于本模块返回 None。"""
    if args.command != "config":
        return None
    return run_config_menu(instance=args.instance)


def _wizard_create_interactive(io: MenuIO, preset_instance: str | None = None) -> str | None:
    io.write("\n--- 初始化新实例（向导）---")
    inst = (
        preset_instance.strip()
        if preset_instance is not None
        else _prompt(io, "实例名（小写字母开头，输入 0 取消）", default="my").strip()
    )
    if not inst or inst == "0":
        io.write("已取消初始化。")
        return None
    vault_default = str(Path.home() / f"hushai-vault-{inst}" if inst != "my" else Path.home() / "hushai-vault")
    vault = _prompt(io, "记忆库路径（绝对路径）", default=vault_default)
    tz = _prompt(io, "时区", default=DEFAULT_TIMEZONE)
    lang = _prompt(io, "默认语言 (zh/en)", default=DEFAULT_LANG)
    steward_name = _prompt(io, "默认常驻管家后端 (claude/agy)", default="claude")
    remote = _prompt(io, "rclone 同步远端（可留空）", default="")
    res = doorkit.run_wizard(
        instance=inst,
        vault=vault,
        timezone=tz,
        lang=lang,
        steward=steward_name,
        remote=remote,
    )
    io.write(f"✓ 实例 {inst} 初始化完成（vault: {res['vault']}｜体检: {'OK' if res['ok'] else 'WARN/FAIL'}）")
    io.write(f"  服务单元已生成在: {res['units_dir']}")
    io.write(f"  端接入包已生成在: {res['doorkit_dir']}")
    return inst


def _select_initial_instance(io: MenuIO, requested: str | None) -> str | None:
    existing = list_instances()
    if requested:
        if get_instance_config_file(requested).is_file():
            return requested
        io.write(f"实例 `{requested}` 尚未初始化。")
        ans = io.read_line("是否现在交互初始化该实例？[y/N]: ").strip().lower()
        if ans in ("y", "yes", "1", "是"):
            return _wizard_create_interactive(io, preset_instance=requested)
        return None

    if len(existing) == 1:
        return existing[0]

    if not existing:
        io.write("当前尚未发现已初始化的 hushAi 实例。")
        io.write("  1. 立即初始化一个新实例")
        io.write("  0. 退出")
        choice = io.read_line("请选择 [1/0]: ").strip()
        if choice == "1":
            return _wizard_create_interactive(io)
        return None

    while True:
        io.write("\n检测到多个实例，请选择要管理的实例：")
        for idx, name in enumerate(existing, start=1):
            io.write(f"  {idx}. {name}")
        io.write("  n. 初始化新实例")
        io.write("  0. 退出")
        raw = io.read_line("输入编号: ").strip().lower()
        if raw in ("0", "q", "exit"):
            return None
        if raw == "n":
            created = _wizard_create_interactive(io)
            if created:
                return created
            existing = list_instances()
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(existing):
            return existing[int(raw) - 1]
        io.write("无效选择，请重新输入。")


def run_config_menu(instance: str | None = None, io: MenuIO | None = None) -> int:
    """运行交互式 `hushai config` 1-N 设置与控制菜单。"""
    cio = io or MenuIO()
    try:
        current = _select_initial_instance(cio, instance)
        if not current:
            return 0

        while True:
            cfg = load_instance_config(current)
            brain = load_brain_config(cfg)
            ad_names = ",".join(sorted(cfg.adapters)) or "未配置"
            cio.write("\n==================================================")
            cio.write(f"  hushAi-agent 设置与控制中心  [实例: {cfg.name}]")
            cio.write(f"  记忆库: {cfg.vault}")
            cio.write(
                f"  时区: {cfg.timezone} | 语言: {cfg.lang} | 适配器: {ad_names} | "
                f"第二大脑总开关: {'已开启' if brain.enabled else '已禁用'}"
            )
            cio.write("==================================================")
            cio.write("  1. 基础与记忆库设置（时区、语言、路径、仅本机目录/领域、篇幅与窗口限制）")
            cio.write("  2. 领域与接入端管理（领域列表、管家端、已知端、审批权、交换区）")
            cio.write("  3. AI 适配器与模型调度（claude/agy/fake、回退链、派活、每日预算、自检）")
            cio.write("  4. 常驻管家管理与跨端路由（查看/切换 claude|agy/重启、@路由）")
            cio.write("  5. 多端接入与同步（rclone 同步、doorkit、systemd 单元、电脑/手机接入速查）")
            cio.write("  6. 第二大脑 (Second-Brain) 设置与操作（开关、引导、Ask、加工、Routine）")
            cio.write("  7. 日常查看与待办操作（今日待办、晨间简报、记收件、销待办、批记忆）")
            cio.write("  8. 手动触发整理与系统维护（reconcile/digest/weekly/heal/doctor/export/upgrade/uninstall）")
            cio.write("  9. 切换或新建实例")
            cio.write("  0. 退出")

            choice = cio.read_line("请选择菜单编号 [0-9]: ").strip()
            if choice in ("0", "q", "exit"):
                cio.write("已退出 hushAi 设置中心。")
                return 0
            submenus = {
                "1": _submenu_basic,
                "2": _submenu_doors_domains,
                "3": _submenu_adapters,
                "4": _submenu_steward,
                "5": _submenu_sync_and_doors,
                "6": _submenu_brain,
                "7": _submenu_daily_ops,
            }
            if choice in submenus:
                submenus[choice](cio, current)
            elif choice == "8":
                _submenu_maintenance(cio, current)
                if not get_instance_config_file(current).is_file():
                    remaining = list_instances()
                    if not remaining:
                        cio.write("当前实例已卸载，已无剩余实例，已退出设置中心。")
                        return 0
                    picked = _select_initial_instance(cio, None)
                    if not picked:
                        return 0
                    current = picked
            elif choice == "9":
                picked = _select_initial_instance(cio, None)
                if picked:
                    current = picked
            else:
                cio.write("请输入 0 到 9 之间的数字。")
    except (EOFError, KeyboardInterrupt):
        cio.write("\n已退出 hushAi 设置中心。")
        return 0
