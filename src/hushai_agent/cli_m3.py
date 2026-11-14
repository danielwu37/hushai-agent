"""M3a 相关的 CLI 子命令注册与分发（adapter test / dispatch）。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from hushai_agent.adapter_cmd import run_adapter_test
from hushai_agent.config import VALID_TIERS
from hushai_agent.dispatch import run_dispatch


def register_m3_commands(subparsers: Any) -> None:
    """在主解析器上注册 adapter 与 dispatch 子命令。"""
    # 1. adapter test
    p_adapter = subparsers.add_parser("adapter", help="适配项检查与测试")
    adapter_sub = p_adapter.add_subparsers(dest="adapter_command")
    p_test = adapter_sub.add_parser("test", help="使用极小提示词与结构自检问答式适配项")
    p_test.add_argument("name", metavar="NAME", help="配置中的适配项名称")
    p_test.add_argument("--instance", required=True, help="实例名称")
    p_test.add_argument(
        "--tier",
        choices=VALID_TIERS,
        default="light",
        help="测试档位（light 或 heavy，默认 light）",
    )
    p_test.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 2. dispatch
    p_dispatch = subparsers.add_parser("dispatch", help="派发独立任务给代理式适配项执行")
    p_dispatch.add_argument("--instance", required=True, help="实例名称")
    p_dispatch.add_argument("--name", required=True, help="任务名（匹配 [a-z0-9][a-z0-9-]{0,40}）")
    task_group = p_dispatch.add_mutually_exclusive_group(required=True)
    task_group.add_argument("--task", default=None, help="已有任务书文件路径")
    task_group.add_argument("--goal", default=None, help="一句话任务目标（用内置模板生成 task.md）")
    p_dispatch.add_argument("--domain", default=None, help="配合 --goal 使用的所属领域")
    p_dispatch.add_argument("--adapter", default=None, help="适配项名字（默认取 [jobs] dispatch）")
    p_dispatch.add_argument(
        "--tier",
        choices=VALID_TIERS,
        default="heavy",
        help="执行档位（light 或 heavy，默认 heavy）",
    )
    p_dispatch.add_argument("--workdir", default=None, help="工作目录（默认 <任务目录>/work/）")
    p_dispatch.add_argument("--timeout", type=int, default=None, help="单次调用超时秒数")
    p_dispatch.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")


def _print_adapter_test(res: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        status = "成功" if res["ok"] else "失败"
        print(f"适配项自检 ({res['adapter']}:{res['tier']}): {status}")
        print(f"模型: {res['model'] or '（无）'}")
        print(f"耗时: {res['duration']} 秒")
        if res["error"]:
            print(f"错误: {res['error']}")
    return 0 if res["ok"] else 1


def _print_dispatch_result(res: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        status = "成功" if res["ok"] else "失败"
        resumed_str = "是" if res["resumed"] else "否"
        print(f"派活结果 ({res['name']}): {status}")
        print(f"是否续跑: {resumed_str}")
        print(f"模型: {res['model'] or '（无）'} (耗时 {res['duration']} 秒)")
        print(f"任务目录: {res['job_dir']}")
        print(f"工作目录: {res['workdir']}")
        print("工作目录文件清单:")
        if res["files"]:
            for rel in res["files"]:
                print(f"  - {rel}")
        else:
            print("  （无）")
        if res["error"]:
            print(f"错误: {res['error']}")
        if res["report_tail"]:
            print("回报内容（末尾 2000 字）:")
            print(res["report_tail"])
    return 0 if res["ok"] else 1


def dispatch_m3_command(args: argparse.Namespace) -> int | None:
    """分发处理 adapter 与 dispatch 命令；若不属于本模块则返回 None。"""
    if args.command == "adapter":
        if args.adapter_command != "test":
            print("错误：请指定子命令 `hushai adapter test`", file=sys.stderr)
            return 1
        res = run_adapter_test(args.instance, args.name, tier=args.tier)
        return _print_adapter_test(res, as_json=bool(args.json))

    if args.command == "dispatch":
        res = run_dispatch(
            cfg=args.instance,
            name=args.name,
            task_file=args.task,
            goal=args.goal,
            domain=args.domain,
            adapter_name=args.adapter,
            tier=args.tier,
            workdir=args.workdir,
            timeout=args.timeout,
        )
        return _print_dispatch_result(res, as_json=bool(args.json))

    return None
