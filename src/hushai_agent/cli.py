"""命令行入口：只做 argparse 参数解析与输出分发。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from hushai_agent import __version__
from hushai_agent.cli_brain import dispatch_brain_command, register_brain_commands
from hushai_agent.cli_loop import dispatch_loop_command, register_loop_commands
from hushai_agent.cli_m3 import dispatch_m3_command, register_m3_commands
from hushai_agent.cli_m3b import dispatch_m3b_command, register_m3b_commands
from hushai_agent.cli_m3c import dispatch_m3c_command, register_m3c_commands
from hushai_agent.cli_m3d import dispatch_m3d_command, register_m3d_commands
from hushai_agent.cli_m3e import dispatch_m3e_command, register_m3e_commands
from hushai_agent.cli_m5 import dispatch_m5_command, register_m5_commands
from hushai_agent.config import DEFAULT_LANG, DEFAULT_TIMEZONE, ConfigError
from hushai_agent.doctor import run_doctor
from hushai_agent.guard import GuardError
from hushai_agent.inboxfmt import InboxFormatError
from hushai_agent.items import ItemFormatError
from hushai_agent.lock import LockBusy
from hushai_agent.validate import validate_vault
from hushai_agent.vault import add_inbox_entry, init_instance


def _print_check_report(report: dict[str, Any], as_json: bool) -> int:
    """输出 doctor 或 validate 的检查报告并返回退出码。"""
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report.get("checks", []):
            print(f"[{item['level']}] {item['id']}: {item['message']}")
    return 0 if report.get("ok", False) else 1


def build_parser() -> argparse.ArgumentParser:
    """构建 hushai 命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="hushai",
        description="hushAi-agent：以本地 Markdown 文件夹为唯一记忆的个人 AI 框架",
    )
    subparsers = parser.add_subparsers(dest="command")

    # 1. version
    subparsers.add_parser("version", help="打印版本信息")

    # 2. init
    p_init = subparsers.add_parser("init", help="从模板初始化实例与 vault")
    p_init.add_argument("--instance", required=True, help="实例名称")
    p_init.add_argument("--vault", required=True, help="vault 目录路径")
    p_init.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"实例时区（默认 {DEFAULT_TIMEZONE}）",
    )
    p_init.add_argument(
        "--lang",
        choices=("zh", "en"),
        default=DEFAULT_LANG,
        help=f"默认语言（zh 或 en，默认 {DEFAULT_LANG}）",
    )
    p_init.add_argument(
        "--no-git",
        action="store_true",
        help="不在 vault 中执行 git init",
    )

    # 3. doctor
    p_doctor = subparsers.add_parser("doctor", help="对实例执行体检")
    p_doctor.add_argument("--instance", required=True, help="实例名称")
    p_doctor.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 4. validate
    p_validate = subparsers.add_parser("validate", help="按规范校验任意 vault 目录")
    p_validate.add_argument("path", metavar="PATH", help="要校验的 vault 目录路径")
    p_validate.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 5. inbox add
    p_inbox = subparsers.add_parser("inbox", help="收件箱操作")
    inbox_sub = p_inbox.add_subparsers(dest="inbox_command")
    p_add = inbox_sub.add_parser("add", help="向当天收件文件追加一条记录")
    p_add.add_argument("--instance", required=True, help="实例名称")
    p_add.add_argument("--door", required=True, help="端编号")
    p_add.add_argument("--domain", required=True, help="所属领域")
    p_add.add_argument("--did", required=True, help="做了什么")
    p_add.add_argument("--artifact", default=None, help="产物路径或说明")
    p_add.add_argument("--todo", default=None, help="下一步待办")
    p_add.add_argument("--time", default=None, help="指定时间 HH:MM（默认当前实例时区时间）")
    p_add.add_argument("--auto", action="store_true", help="写入 -auto.md 自动记录文件")

    register_loop_commands(subparsers)
    register_m3_commands(subparsers)
    register_m3b_commands(subparsers)
    register_m3c_commands(subparsers)
    register_m3d_commands(subparsers)
    register_m3e_commands(subparsers)
    register_m5_commands(subparsers)
    register_brain_commands(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """命令行主函数。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help(sys.stderr)
        return 1

    try:
        if args.command == "version":
            print(f"hushAi-agent {__version__}")
            return 0

        if args.command == "init":
            result = init_instance(
                instance=args.instance,
                vault=args.vault,
                timezone=args.timezone,
                lang=args.lang,
                no_git=args.no_git,
            )
            if result.created_files:
                print(f"已补齐 {len(result.created_files)} 个文件:")
                for rel in result.created_files:
                    print(f"  + {rel}")
            else:
                print("所有模板文件均已存在，未覆盖任何文件。")
            if result.git_notice:
                print(result.git_notice)
            return 0

        if args.command == "doctor":
            report = run_doctor(args.instance)
            return _print_check_report(report, as_json=args.json)

        if args.command == "validate":
            report = validate_vault(args.path)
            return _print_check_report(report, as_json=args.json)

        if args.command == "inbox":
            if args.inbox_command != "add":
                print("错误：请指定子命令 `hushai inbox add`", file=sys.stderr)
                return 1
            inbox_path = add_inbox_entry(
                instance=args.instance,
                door=args.door,
                domain=args.domain,
                did=args.did,
                artifact=args.artifact,
                todo=args.todo,
                time_str=args.time,
                is_auto=args.auto,
            )
            print(f"已追加收件至: {inbox_path}")
            return 0

        loop_rc = dispatch_loop_command(args)
        if loop_rc is not None:
            return loop_rc

        for handler in (
            dispatch_m3_command,
            dispatch_m3b_command,
            dispatch_m3c_command,
            dispatch_m3d_command,
            dispatch_m3e_command,
            dispatch_m5_command,
            dispatch_brain_command,
        ):
            rc = handler(args)
            if rc is not None:
                return rc

        print(f"未知命令: {args.command}", file=sys.stderr)
        return 1

    except (
        ConfigError,
        InboxFormatError,
        ItemFormatError,
        GuardError,
        LockBusy,
        ValueError,
        OSError,
        RuntimeError,
    ) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
