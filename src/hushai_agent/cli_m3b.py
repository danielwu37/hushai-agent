"""M3b 的 CLI 子命令：gate（闸门）、on-event（事件处理）、install units（单元模板）。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from hushai_agent import doorkit, gate, install, on_event
from hushai_agent.config import DEFAULT_LANG, DEFAULT_TIMEZONE, load_instance_config

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

    p_inst = subparsers.add_parser("install", help="生成服务单元、端接入包或运行一站式安装向导")
    p_inst.add_argument("install_command", choices=("units", "doorkit", "wizard"))
    p_inst.add_argument("--instance", default=None, help="实例名称（wizard 省略时可交互输入，默认 my）")
    p_inst.add_argument("--out", default=None, help="units/doorkit：输出目录（必须已存在）")
    p_inst.add_argument("--digest-time", default=None, help="每日汇总时间 HH:MM（默认读取既有单元或 21:30）")
    p_inst.add_argument("--memory-max", default=install.DEFAULT_MEMORY_MAX, help="内存上限，如 256M")
    p_inst.add_argument("--port", type=int, default=install.DEFAULT_PORT, help="待办页面端口")
    p_inst.add_argument("--with-brain", action="store_true", help="units：同时渲染 second-brain 定时器单元（默认不启用）")
    p_inst.add_argument("--adapter", choices=("claude", "agy", "all"), default="all", help="doorkit：适配项")
    p_inst.add_argument("--door", default=None, help="doorkit：端编号（默认管家端）")
    p_inst.add_argument("--vault", default=None, help="wizard：vault 目录路径")
    p_inst.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="wizard：实例时区")
    p_inst.add_argument("--lang", choices=("zh", "en"), default=DEFAULT_LANG, help="wizard：默认语言")
    p_inst.add_argument("--steward", choices=("claude", "agy"), default="claude", help="wizard：管家适配项")
    p_inst.add_argument("--agy-light", default="gemini-3.8-flash-low", help="wizard：agy 轻档模型")
    p_inst.add_argument("--agy-heavy", default="gemini-4-argon-high", help="wizard：agy 重档模型")
    p_inst.add_argument("--claude-light", default="sonnet", help="wizard：claude 轻档模型")
    p_inst.add_argument("--claude-heavy", default="opus", help="wizard：claude 重档模型")
    p_inst.add_argument("--remote", default="", help="wizard：rclone 远端路径（可空）")
    p_inst.add_argument("--no-git", action="store_true", help="wizard：不在 vault 中执行 git init")
    p_inst.add_argument("--json", action="store_true", help="wizard：以 JSON 格式输出结果")


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
        if out.get("_muse_text"):
            if rc == 0:
                payload = out.get("stdout", "")
                if isinstance(payload, bytes):
                    sys.stdout.buffer.write(payload)
                    sys.stdout.buffer.flush()
                else:
                    sys.stdout.write(str(payload))
            else:
                print(out.get("error", "拒绝"), file=sys.stderr)
            return rc
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
        if args.install_command == "wizard":
            interactive = sys.stdin.isatty() and not args.json
            inst = args.instance
            if not inst:
                if interactive:
                    inst = input("实例名（小写字母开头，直接回车默认 my）: ").strip() or "my"
                else:
                    inst = "my"
            vault_arg = args.vault
            if not vault_arg:
                if interactive:
                    def_vault = "~/hushai-vault" if inst == "my" else f"~/hushai-vault-{inst}"
                    vault_arg = input(f"记忆库路径 [回车默认: {def_vault}]: ").strip() or def_vault
                else:
                    raise ValueError("install wizard 需要 --vault")
            res = doorkit.run_wizard(
                instance=inst,
                vault=vault_arg,
                timezone=args.timezone,
                lang=args.lang,
                steward=args.steward,
                agy_light=args.agy_light,
                agy_heavy=args.agy_heavy,
                claude_light=args.claude_light,
                claude_heavy=args.claude_heavy,
                remote=args.remote,
                no_git=args.no_git,
            )
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                print(f"实例 {inst} 已就绪（vault: {res['vault']}）")
                print(f"单元目录: {res['units_dir']}（{len(res['units'])} 个文件）")
                print(f"端接入包: {res['doorkit_dir']}（{len(res['doorkit'])} 个文件）")
                print(f"体检结果: {'OK' if res['ok'] else 'FAIL'}")
                print("\n".join(res["next_steps"]))
            return 0 if res["ok"] else 1

        if not args.instance:
            raise ValueError(f"install {args.install_command} 需要 --instance")
        if not args.out:
            raise ValueError(f"install {args.install_command} 需要 --out")
        cfg = load_instance_config(args.instance)
        if args.install_command == "doorkit":
            written = doorkit.render_doorkit(cfg, args.out, args.adapter, args.door)
            for path in written:
                print(f"已生成: {path}")
            return 0
        written = install.render_units(
            cfg, args.out, args.digest_time, args.memory_max, args.port, with_brain=bool(args.with_brain)
        )
        for path in written:
            print(f"已生成: {path}")
        print("\n".join(install.next_steps(cfg, written)), file=sys.stdout)
        return 0
    return None
