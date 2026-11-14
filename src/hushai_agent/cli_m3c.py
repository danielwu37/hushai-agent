"""M3c 的 CLI 子命令：candidate（候选记忆）、brief（晨间简报）、serve（待办页面）。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from hushai_agent import brief, candidates, serve
from hushai_agent.brief_html import render_html
from hushai_agent.config import load_instance_config


def register_m3c_commands(subparsers: Any) -> None:
    """注册 candidate、brief、serve。"""
    p_cand = subparsers.add_parser("candidate", help="候选记忆：列出、批准、丢弃")
    p_cand.add_argument("candidate_command", choices=("list", "approve", "discard"))
    p_cand.add_argument("target", nargs="?", default=None, help="候选编号 C-YYMMDD-N")
    p_cand.add_argument("--instance", required=True, help="实例名称")
    p_cand.add_argument("--to", choices=("profile", "rules"), default=None, help="approve：记入画像还是规则")
    p_cand.add_argument("--door", default=None, help="写入端编号（默认取 [doors] steward）")
    p_cand.add_argument("--json", action="store_true", help="list：以 JSON 格式输出")

    p_brief = subparsers.add_parser("brief", help="打印晨间简报（不写文件）")
    p_brief.add_argument("--instance", required=True, help="实例名称")
    fmt = p_brief.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--md", action="store_true")
    fmt.add_argument("--html", action="store_true")

    p_serve = subparsers.add_parser("serve", help="启动待办页面（只监听 127.0.0.1）")
    p_serve.add_argument("--instance", required=True, help="实例名称")
    p_serve.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")


def dispatch_m3c_command(args: argparse.Namespace) -> int | None:
    """分发 candidate、brief、serve；不属于本模块返回 None。"""
    if args.command == "candidate":
        cfg = load_instance_config(args.instance)
        if args.candidate_command == "list":
            rows = [vars(c) for c in candidates.pending(candidates.load(cfg))]
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print("\n".join(f"{r['id']}｜{r['kind']}｜{r['text']}" for r in rows) or "（没有待批的候选记忆）")
            return 0
        if not args.target:
            raise ValueError("请给出候选编号 C-YYMMDD-N")
        if args.candidate_command == "approve" and not args.to:
            raise ValueError("approve 需要 --to profile 或 --to rules")
        door = args.door.strip() if args.door and args.door.strip() else cfg.steward_door
        path = candidates.command(cfg, args.candidate_command, args.target.strip(), args.to or "", door)
        print(f"已写入: {path}")
        return 0

    if args.command == "brief":
        data = brief.build_brief(load_instance_config(args.instance))
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif args.html:
            print(render_html(data, live=False))
        else:
            print(brief.render_md(data), end="")
        return 0

    if args.command == "serve":
        cfg = load_instance_config(args.instance)
        server = serve.make_server(cfg, args.port)
        print(f"待办页面已启动: http://{serve.HOST}:{server.server_address[1]}/?t=<令牌>")
        print(f"令牌在: {serve.token_path(cfg)}（只有本机这个账号能读）", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    return None
