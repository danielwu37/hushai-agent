"""alert 子命令：系统告警的写入、列出、撤下与会话开场主动报告（不进 hub/inbox）。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from hushai_agent import alerts
from hushai_agent.config import load_instance_config

LEVELS = ("info", "warning", "needs_human")


def register_alert_commands(subparsers: Any) -> None:
    """注册 alert add/list/resolve/active。"""
    p = subparsers.add_parser("alert", help="系统告警：写入、列出、撤下（只进 <state>/alerts.jsonl，不进收件）")
    sub = p.add_subparsers(dest="alert_cmd", required=True)
    p_add = sub.add_parser("add", help="写一条系统告警（周边任务用它代替写管家收件）")
    p_add.add_argument("--instance", required=True, help="实例名称")
    p_add.add_argument("--level", choices=LEVELS, default="warning", help="级别：warning 及以上会主动报")
    p_add.add_argument("--source", required=True, help="来源标识，如 ext:china-news；恢复时按它撤下")
    p_add.add_argument("--message", required=True, help="一句话说明问题")
    p_add.add_argument("--suggestion", default="", help="建议怎么处理")
    p_res = sub.add_parser("resolve", help="撤下来源以 --source 开头的未恢复告警")
    p_res.add_argument("--instance", required=True, help="实例名称")
    p_res.add_argument("--source", required=True, help="来源前缀")
    p_list = sub.add_parser("list", help="列出未恢复的告警（JSON）")
    p_list.add_argument("--instance", required=True, help="实例名称")
    p_act = sub.add_parser("active", help="会话开场钩子用：有未恢复告警时打印主动报告，没有就静默")
    p_act.add_argument("--instance", required=True, help="实例名称")


def dispatch_alert_command(args: argparse.Namespace) -> int | None:
    """分发 alert；不属于本模块返回 None。"""
    if args.command != "alert":
        return None
    cfg = load_instance_config(args.instance)
    if args.alert_cmd == "add":
        kind = args.source.split(":", 1)[0] or "ext"
        alerts.record_alert(cfg, kind=kind, level=args.level, message=args.message,
                            suggestion=args.suggestion, source=args.source)
        return 0
    if args.alert_cmd == "resolve":
        print(json.dumps({"resolved": alerts.resolve_alerts(cfg, args.source)}, ensure_ascii=False))
        return 0
    if args.alert_cmd == "list":
        print(json.dumps(alerts.active_alerts(cfg), ensure_ascii=False, indent=2))
        return 0
    text = alerts.render_active(cfg)
    if text:
        print(text)
    return 0
