"""M3b 的 CLI 子命令：gate（闸门）、on-event（事件处理）、install units（单元模板）。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from hushai_agent import gate, install, on_event
from hushai_agent.config import load_instance_config

GATE_SUBS = ("append-inbox", "put-exchange", "check-inbox", "ssh")


def register_m3b_commands(subparsers: Any) -> None:
    """注册 gate、on-event、install。"""
    p_gate = subparsers.add_parser("gate", help="各端写入 vault 的受控入口（输出 JSON）")
    p_gate.add_argument("gate_command", choices=GATE_SUBS)
    p_gate.add_argument("--instance", required=True, help="实例名称")
    p_gate.add_argument("--door", required=True, help="端编号（必须在 [doors] known 里）")
    p_gate.add_argument("--auto", action="store_true", help="append-inbox：写入 -auto 文件")
    p_gate.add_argument("--name", default=None, help="put-exchange：文件名")
    p_gate.add_argument("--ack", action="store_true", help="check-inbox：把列出的记为已确认")

    p_event = subparsers.add_parser("on-event", help="收件有变化时跑一遍：即时机械对账加跨端路由")
    p_event.add_argument("--instance", required=True, help="实例名称")
    p_event.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    p_inst = subparsers.add_parser("install", help="生成服务单元模板（不安装、不启用）")
    p_inst.add_argument("install_command", choices=("units",))
    p_inst.add_argument("--instance", required=True, help="实例名称")
    p_inst.add_argument("--out", required=True, help="输出目录（必须已存在）")
    p_inst.add_argument("--digest-time", default=install.DEFAULT_DIGEST_TIME, help="每日汇总时间 HH:MM")
    p_inst.add_argument("--memory-max", default=install.DEFAULT_MEMORY_MAX, help="内存上限，如 256M")
    p_inst.add_argument("--port", type=int, default=install.DEFAULT_PORT, help="待办页面端口")


def dispatch_m3b_command(args: argparse.Namespace) -> int | None:
    """分发 gate、on-event、install；不属于本模块返回 None。"""
    if args.command == "gate":
        try:
            cfg = load_instance_config(args.instance)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            return 1
        opts = {"auto": args.auto, "name": args.name, "ack": args.ack}
        out, rc = gate.run(cfg, args.door, args.gate_command, opts)
        print(json.dumps(out, ensure_ascii=False))
        return rc

    if args.command == "on-event":
        cfg = load_instance_config(args.instance)  # 只有配置读不出来才返回 1（由主入口处理）
        out = on_event.handle(cfg)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            mech = out.get("mechanical", {})
            route = out.get("routing", {})
            print(f"机械对账: {mech.get('error') or mech.get('skipped') or mech.get('mechanical_applied', 0)}")
            print(f"路由: {route.get('error') or route.get('skipped') or route.get('routed', 0)}")
        return 0

    if args.command == "install":
        cfg = load_instance_config(args.instance)
        written = install.render_units(cfg, args.out, args.digest_time, args.memory_max, args.port)
        for path in written:
            print(f"已生成: {path}")
        print("\n".join(install.next_steps(cfg, written)), file=sys.stdout)
        return 0
    return None
