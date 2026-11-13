"""M2 每日闭环相关的 CLI 子命令注册与分发（today / todo / ledger / digest / weekly）。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from hushai_agent import clock, digest, ledger, weekly
from hushai_agent.config import load_instance_config
from hushai_agent.todo import get_today_status, run_todo_action


def register_loop_commands(subparsers: Any) -> None:
    """在主解析器上注册 today、todo、ledger、digest、weekly 五个子命令。"""
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

    # 4. digest
    p_digest = subparsers.add_parser("digest", help="执行每日收件对账与派生视图汇总")
    p_digest.add_argument("--instance", required=True, help="实例名称")
    p_digest.add_argument("--date", default=None, help="汇总基准日期 YYYY-MM-DD（默认当日）")
    p_digest.add_argument("--dry-run", action="store_true", help="演练模式（只写状态目录 dry-run）")
    p_digest.add_argument("--json", action="store_true", help="以 JSON 格式输出摘要")

    # 5. weekly
    p_weekly = subparsers.add_parser("weekly", help="执行每周洞察重写与审计周报生成")
    p_weekly.add_argument("--instance", required=True, help="实例名称")
    p_weekly.add_argument("--date", default=None, help="基准日期 YYYY-MM-DD（默认当日）")
    p_weekly.add_argument("--dry-run", action="store_true", help="演练模式（只写状态目录 dry-run）")
    p_weekly.add_argument("--json", action="store_true", help="以 JSON 格式输出摘要")


def _print_digest_summary(summary: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    deg = summary.get("degraded") or "否"
    print(f"汇总日期: {summary['date']} (演练: {summary['dry_run']})")
    print(f"新增收件行: {summary['new_lines']}")
    print(f"机械动作应用数: {summary['mechanical_applied']}")
    print(f"新建开放项: {len(summary.get('created', []))} 条")
    print(f"关闭开放项: {len(summary.get('closed', []))} 条")
    print(f"明日待办 ({summary['for_date']}): {summary['today_count']} 条")
    print(f"是否降级: {deg}")
    return 0


def _print_weekly_summary(summary: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    deg = summary.get("degraded") or "否"
    print(f"周报日期: {summary['date']} (演练: {summary['dry_run']})")
    print(f"逾期开放项: {summary['overdue_count']} 条")
    print(f"状态未核实: {summary['stale_count']} 条")
    print(f"重写洞察: {summary['insights_count']} 条")
    print(f"是否降级: {deg}")
    return 0


def dispatch_loop_command(args: argparse.Namespace) -> int | None:
    """分发处理 today、todo、ledger、digest、weekly 命令；若不属于本模块则返回 None。"""
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

    if args.command == "digest":
        cfg = load_instance_config(args.instance)
        as_of = args.date.strip() if args.date and args.date.strip() else clock.today(cfg)
        summary = digest.run_digest(cfg, as_of=as_of, dry_run=bool(args.dry_run))
        return _print_digest_summary(summary, as_json=bool(args.json))

    if args.command == "weekly":
        cfg = load_instance_config(args.instance)
        as_of = args.date.strip() if args.date and args.date.strip() else clock.today(cfg)
        summary = weekly.run_weekly(cfg, as_of=as_of, dry_run=bool(args.dry_run))
        return _print_weekly_summary(summary, as_json=bool(args.json))

    return None
