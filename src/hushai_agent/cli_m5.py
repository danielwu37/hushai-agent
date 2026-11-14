"""M5 的 CLI 子命令：steward（常驻管家会话状态查询、模型切换 claude/agy、重启）。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from hushai_agent import steward
from hushai_agent.config import load_instance_config


def register_m5_commands(subparsers: Any) -> None:
    """注册 steward 子命令。"""
    p_st = subparsers.add_parser("steward", help="管理常驻管家会话与模型切换（claude / agy）")
    p_st.add_argument("steward_action", choices=("status", "use", "restart"), help="动作：status | use | restart")
    p_st.add_argument("backend", nargs="?", choices=("claude", "agy"), default=None, help="use 的目标后端：claude | agy")
    p_st.add_argument("--instance", required=True, help="实例名称")
    p_st.add_argument("--no-restart", action="store_true", help="use：只切换配置与脚本，不立即重启 systemd 服务")
    p_st.add_argument("--dry-run", action="store_true", help="演练模式（不改文件、不实际重启）")
    p_st.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")


def dispatch_m5_command(args: argparse.Namespace) -> int | None:
    """分发 steward 子命令；不属于本模块返回 None。"""
    if args.command != "steward":
        return None

    cfg = load_instance_config(args.instance)
    action = args.steward_action

    if action == "status":
        res = steward.status(cfg)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            tmux_str = "alive" if res["tmux_alive"] else "dead"
            print(f"实例: {res['instance']}")
            print(f"当前后端 (backend): {res['backend']}")
            print(f"服务单元 (service): {res['service']} ({res['service_state']})")
            print(f"tmux 会话 (session): {res['tmux_session']} ({tmux_str})")
            print(f"启动脚本 (script): {res['start_script']}")
        return 0

    if action == "use":
        if not args.backend:
            raise ValueError("steward use 需要指定后端：claude 或 agy")
        res = steward.use_backend(
            cfg,
            args.backend,
            restart=not args.no_restart,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(
                f"已切换管家后端: {res['previous_backend']} -> {res['backend']}"
                f"（收件: {res['inbox_file'] or 'dry-run'}｜服务: {res['restart_detail']}）"
            )
        return 0 if res["ok"] else 1

    if action == "restart":
        res = steward.restart_steward(cfg, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"管家服务重启 ({res['service']}, backend={res['backend']}): {res['detail']}")
        return 0 if res["ok"] else 1

    return None
