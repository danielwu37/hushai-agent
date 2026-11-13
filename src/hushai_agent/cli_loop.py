"""M2 每日闭环相关的 CLI 子命令注册与分发（today / todo / ledger）。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from hushai_agent import ledger
from hushai_agent.config import load_instance_config
from hushai_agent.todo import get_today_status, run_todo_action


def register_loop_commands(subparsers: Any) -> None:
    """在主解析器上注册 today、todo、ledger 三个子命令。"""
    # 1. today
    p_today = subparsers.add_parser("today", help="查看今日待办并叠加未对账收件动作")
    p_today.add_argument("--instance", required=True, help="实例名称")
    p_today.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 2. todo done|defer|drop
    p_todo = subparsers.add_parser("todo", help="将今日待办的完成、推迟、放弃回写至收件箱")
    p_todo.add_argument(
        "todo_action",
        choices=("done", "defer", "drop"),
        help="待办动作：done（完成）、defer（推迟）、drop（放弃）",
    )
    p_todo.add_argument("target", help="today.md 中的序号 N 或开放项编号 YYMMDD-N")
    p_todo.add_argument("--instance", required=True, help="实例名称")
    p_todo.add_argument("--to", dest="to_date", default=None, help="推迟目标日期 YYYY-MM-DD")
    p_todo.add_argument("--door", default=None, help="写入端编号（默认取 [doors] steward）")

    # 3. ledger
    p_ledger = subparsers.add_parser("ledger", help="查看实例最近的运行账本记录")
    p_ledger.add_argument("--instance", required=True, help="实例名称")
    p_ledger.add_argument("--limit", type=int, default=None, help="最多显示的最近记录条数")
    p_ledger.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")


def dispatch_loop_command(args: argparse.Namespace) -> int | None:
    """分发处理 today、todo、ledger 命令；若不属于本模块命令则返回 None。"""
    if args.command == "today":
        payload, lines = get_today_status(args.instance)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for line in lines:
                print(line)
        return 0

    if args.command == "todo":
        inbox_path = run_todo_action(
            instance=args.instance,
            action=args.todo_action,
            target=args.target,
            to_date=args.to_date,
            door=args.door,
        )
        print(f"已写入待办动作至: {inbox_path}")
        return 0

    if args.command == "ledger":
        cfg = load_instance_config(args.instance)
        rows = ledger.read(cfg, limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(json.dumps(row, ensure_ascii=False))
        return 0

    return None
