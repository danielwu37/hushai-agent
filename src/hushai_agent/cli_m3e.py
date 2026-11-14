"""M3e 的 CLI 子命令：heal（静默自愈健康检查）、capture（会话结束自动记收件）。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from hushai_agent import capture, heal
from hushai_agent.config import load_instance_config


def register_m3e_commands(subparsers: Any) -> None:
    """注册 heal、capture。"""
    p_heal = subparsers.add_parser("heal", help="静默自愈健康检查（白名单确定性恢复与 24h 去重上报）")
    p_heal.add_argument("--instance", required=True, help="实例名称")
    p_heal.add_argument(
        "--before",
        choices=("digest", "weekly"),
        default=None,
        help="任务前置检查目标（digest 或 weekly）",
    )
    p_heal.add_argument("--dry-run", action="store_true", help="演练模式（不执行实际恢复动作、不改文件）")
    p_heal.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    p_cap = subparsers.add_parser("capture", help="从钩子标准输入读取会话 JSONL，纯机械提取最后一条人类消息写入 -auto.md")
    p_cap.add_argument("--instance", required=True, help="实例名称")
    p_cap.add_argument("--door", default=None, help="端编号（默认实例配置的管家端 steward）")
    p_cap.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")


def dispatch_m3e_command(args: argparse.Namespace) -> int | None:
    """分发 heal、capture；不属于本模块返回 None。"""
    if args.command == "heal":
        cfg = load_instance_config(args.instance)
        res = heal.run_heal(cfg, before=args.before, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            for item in res["checks"]:
                st = item["status"].upper()
                detail = item.get("result") or item.get("problem") or ""
                print(f"[{st}] {item['id']} ({item['name']}): {detail}")
        return 0 if res["ok"] else 1

    if args.command == "capture":
        cfg = load_instance_config(args.instance)
        res = capture.run_capture(cfg, door=args.door)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif res.get("written"):
            print(f"已自动采集收件至: {res['inbox_file']}")
        return 0 if res.get("ok") else 1

    return None
