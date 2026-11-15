"""Outbox 的 CLI 子命令：status / list / flush / recover / retry / archive。"""

from __future__ import annotations

import argparse
import json
from typing import Any

from hushai_agent import outbox
from hushai_agent.config import load_instance_config


def register_outbox_commands(subparsers: Any) -> None:
    """注册 hushai outbox 子命令。"""
    p_ob = subparsers.add_parser("outbox", help="查看、投递、恢复与归档纯文件型 Outbox（at-least-once）")
    p_ob.add_argument(
        "outbox_action",
        choices=("status", "list", "flush", "recover", "retry", "archive"),
        help="动作：status | list | flush | recover | retry | archive",
    )
    p_ob.add_argument("--instance", required=True, help="实例名称")
    p_ob.add_argument("--status", default=None, choices=("pending", "sending", "sent", "failed", "archive"), help="list：按状态过滤")
    p_ob.add_argument("--channel", default=None, help="flush：仅投递指定通道")
    p_ob.add_argument("--limit", type=int, default=50, help="flush：本轮最大投递封数")
    p_ob.add_argument("--stale-seconds", type=int, default=None, help="recover/flush：判定 sending 超时秒数")
    p_ob.add_argument("--id", default=None, help="retry：指定重排的 message_id")
    p_ob.add_argument("--extra-attempts", type=int, default=3, help="retry：增加的额外重试次数上限")
    p_ob.add_argument("--days", type=int, default=outbox.DEFAULT_ARCHIVE_RETAIN_DAYS, help="archive：保留天数")
    p_ob.add_argument("--include-failed", action="store_true", help="archive：同时归档终态 failed 信封")
    p_ob.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")


def dispatch_outbox_command(args: argparse.Namespace) -> int | None:
    """分发 hushai outbox 子命令；不属于本模块返回 None。"""
    if args.command != "outbox":
        return None

    cfg = load_instance_config(args.instance)
    action = args.outbox_action

    if action in ("status", "list"):
        res = outbox.list_envelopes(cfg, status=args.status)
        if args.json:
            out_payload = {"delivery_semantics": res["delivery_semantics"], "counts": res["counts"]} if action == "status" else res
            print(json.dumps(out_payload, ensure_ascii=False, indent=2))
        else:
            c = res["counts"]
            print(
                f"Outbox [{res['delivery_semantics']}]: "
                f"pending={c['pending']} sending={c['sending']} sent={c['sent']} failed={c['failed']} archive={c['archive']}"
            )
            if action == "list":
                for env in res["envelopes"]:
                    if env.get("corrupted"):
                        print(f"  - [corrupt] {env.get('file')}")
                    else:
                        print(f"  - [{env['status']}] {env['message_id']} channel={env['channel']} target={env['target']} attempts={env.get('attempts', 0)}")
        return 0

    if action == "flush":
        res = outbox.flush(cfg, channel_filter=args.channel, stale_seconds=args.stale_seconds, limit=args.limit)
    elif action == "recover":
        res = outbox.recover_stale_sending(cfg, stale_seconds=args.stale_seconds)
    elif action == "retry":
        res = outbox.retry_failed(cfg, message_id=args.id, extra_attempts=args.extra_attempts)
    elif action == "archive":
        res = outbox.archive_old(cfg, retain_days=args.days, include_failed=args.include_failed)
    else:
        return None

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        summary = ", ".join(f"{k}={v}" for k, v in res.items())
        print(f"Outbox {action}: {summary}")
    return 0
