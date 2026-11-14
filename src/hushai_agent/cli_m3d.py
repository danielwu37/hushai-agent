"""M3d 的 CLI 子命令：sync（单向同步）、export（导出备份）、upgrade（模板升级）、uninstall（卸载）。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from hushai_agent import lifecycle, sync
from hushai_agent.config import load_instance_config


def register_m3d_commands(subparsers: Any) -> None:
    """注册 sync、export、upgrade、uninstall。"""
    p_sync = subparsers.add_parser("sync", help="按文件归属执行单向网盘同步（不碰挂载点）")
    p_sync.add_argument("--instance", required=True, help="实例名称")
    p_sync.add_argument(
        "--direction",
        choices=("push", "pull", "both"),
        default="both",
        help="同步方向：push（服务器->网盘）、pull（网盘->服务器）、both（默认）",
    )
    p_sync.add_argument("--dry-run", action="store_true", help="演练模式（向同步工具传 --dry-run）")
    p_sync.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    p_exp = subparsers.add_parser("export", help="将实例数据、配置与状态导出为目录或 .tar.gz")
    p_exp.add_argument("--instance", required=True, help="实例名称")
    p_exp.add_argument("--out", required=True, help="输出路径（目录或 .tar.gz）")
    p_exp.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    p_upg = subparsers.add_parser("upgrade", help="更新框架托管模板（不动画像、补充规则与数据）")
    p_upg.add_argument("--instance", required=True, help="实例名称")
    p_upg.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    p_uni = subparsers.add_parser("uninstall", help="卸载本实例（默认仅演练列出将删目录与命令）")
    p_uni.add_argument("--instance", required=True, help="实例名称")
    p_uni.add_argument("--yes", action="store_true", help="确认执行删除本实例目录")
    p_uni.add_argument("--keep-vault", action="store_true", help="保留 vault 数据目录，只删配置与状态目录")
    p_uni.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")


def dispatch_m3d_command(args: argparse.Namespace) -> int | None:
    """分发 sync、export、upgrade、uninstall；不属于本模块返回 None。"""
    if args.command == "sync":
        cfg = load_instance_config(args.instance)
        summary, rc = sync.run_sync(cfg, direction=args.direction, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            status = "成功" if summary.get("ok") else f"失败（{summary.get('error') or '见详情'}）"
            print(f"同步 [{args.direction}]: {status}")
        return rc

    if args.command == "export":
        cfg = load_instance_config(args.instance)
        res = lifecycle.run_export(cfg, args.out)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"已导出至: {res['path']}（{res['vault_files']} 个文件）")
        return 0

    if args.command == "upgrade":
        cfg = load_instance_config(args.instance)
        res = lifecycle.run_upgrade(cfg)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            if res["updated"]:
                print(f"已更新框架模板: {', '.join(res['updated'])}")
            else:
                print("框架模板已是最新，无需更新。")
        return 0

    if args.command == "uninstall":
        res = lifecycle.run_uninstall(args.instance, yes=args.yes, keep_vault=args.keep_vault)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            if res["dry_run"]:
                print("【演练模式】未删除任何目录。加 --yes 将删除以下本实例目录：")
                for t in res["targets"]:
                    print(f"  - {t}")
            else:
                print(f"已删除本实例目录: {', '.join(res['removed'])}")
            print("如需停用并清理 systemd 服务单元，请手动执行：")
            for cmd in res["systemd_commands"]:
                print(f"  {cmd}")
        return 0

    return None
