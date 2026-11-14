"""Second-brain 的 CLI 子命令注册与分发（hushai brain <subcmd>）。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from hushai_agent import brain_ask, brain_cfg, brain_model, brain_onboard, brain_process, brain_routines, clock, install
from hushai_agent.config import load_instance_config


def register_brain_commands(subparsers: Any) -> None:
    """在主解析器上注册 brain 命令组。"""
    p_brain = subparsers.add_parser("brain", help="Second-brain 个人知识库管理与问答")
    bsub = p_brain.add_subparsers(dest="brain_command")

    # 1. onboard
    p_onb = bsub.add_parser("onboard", help="首次运行 5 问引导与初始条目录入（之后自动跳过）")
    p_onb.add_argument("--instance", required=True, help="实例名称")
    p_onb.add_argument("--work", default=None, help="回答 1：平时主要做什么工作或项目")
    p_onb.add_argument("--remember", default=None, help="回答 2：最想更好地记住哪类信息")
    p_onb.add_argument("--notes-location", default=None, help="回答 3：现有笔记保存在哪里")
    p_onb.add_argument("--reading", default=None, help="回答 4：平时主要读看什么内容")
    p_onb.add_argument("--answer-style", default=None, help="回答 5：喜欢的回答风格")
    p_onb.add_argument("--items-file", default=None, help="可选：初始待加工文本文件（每行一条，最多 10 条）")
    p_onb.add_argument("--interactive", action="store_true", help="交互式询问 5 个问题")
    p_onb.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 2. status
    p_st = bsub.add_parser("status", help="查看 second-brain 状态、routine 开关与链接健康")
    p_st.add_argument("--instance", required=True, help="实例名称")
    p_st.add_argument("--show-brain", action="store_true", help="同时打印 HERE'S YOUR BRAIN 总览")
    p_st.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 3. process
    p_proc = bsub.add_parser("process", help="加工未处理收件到 notes/sources/people/projects 并补链")
    p_proc.add_argument("--instance", required=True, help="实例名称")
    p_proc.add_argument("--date", default=None, help="基准日期 YYYY-MM-DD")
    p_proc.add_argument("--yes", action="store_true", help="同意合并检测到的重复笔记")
    p_proc.add_argument("--dry-run", action="store_true", help="演练模式（只写状态目录 dry-run）")
    p_proc.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 4. ask
    p_ask = bsub.add_parser("ask", help="只读笔记回答问题（带原句引用与链接，绝不编造）")
    p_ask.add_argument("question", help="要提问的内容")
    p_ask.add_argument("--instance", required=True, help="实例名称")
    p_ask.add_argument("--limit", type=int, default=5, help="最多返回的参考笔记数（默认 5）")
    p_ask.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 5. daily
    p_daily = bsub.add_parser("daily", help="执行每日 second-brain 归档与 3 行总结（默认禁用，需 enable 或 --force）")
    p_daily.add_argument("--instance", required=True, help="实例名称")
    p_daily.add_argument("--date", default=None, help="基准日期 YYYY-MM-DD")
    p_daily.add_argument("--force", action="store_true", help="即使未启用也强制手动执行一次")
    p_daily.add_argument("--dry-run", action="store_true", help="演练模式")
    p_daily.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 6. weekly
    p_weekly = bsub.add_parser("weekly", help="执行每周 second-brain 回顾写入 hub/weekly.md")
    p_weekly.add_argument("--instance", required=True, help="实例名称")
    p_weekly.add_argument("--date", default=None, help="基准日期 YYYY-MM-DD")
    p_weekly.add_argument("--force", action="store_true", help="即使未启用也强制手动执行一次")
    p_weekly.add_argument("--dry-run", action="store_true", help="演练模式")
    p_weekly.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 7. resurface
    p_res = bsub.add_parser("resurface", help="每日一次唤醒一条与当前工作相关的旧笔记")
    p_res.add_argument("--instance", required=True, help="实例名称")
    p_res.add_argument("--date", default=None, help="基准日期 YYYY-MM-DD")
    p_res.add_argument("--force", action="store_true", help="强制立即唤醒（忽略禁用与同日限制）")
    p_res.add_argument("--dry-run", action="store_true", help="演练模式")
    p_res.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 8. monthly
    p_mon = bsub.add_parser("monthly", help="每月维护：重复检测/合并、修断链、标 6 个月孤立笔记、清理 index.md")
    p_mon.add_argument("--instance", required=True, help="实例名称")
    p_mon.add_argument("--date", default=None, help="基准日期 YYYY-MM-DD")
    p_mon.add_argument("--yes", action="store_true", help="确认合并重复笔记并自动写回修复断链")
    p_mon.add_argument("--force", action="store_true", help="即使未启用也强制手动执行一次")
    p_mon.add_argument("--dry-run", action="store_true", help="演练模式")
    p_mon.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 9. enable / disable
    for act in ("enable", "disable"):
        p_toggle = bsub.add_parser(act, help=f"{'启用' if act == 'enable' else '禁用'}指定的 second-brain routine")
        p_toggle.add_argument("routine", choices=(*brain_cfg.ROUTINE_NAMES, "all"), help="目标 routine")
        p_toggle.add_argument("--instance", required=True, help="实例名称")
        p_toggle.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    # 10. units
    p_units = bsub.add_parser("units", help="生成 second-brain 专用 systemd 单元文件（不自动安装/启用）")
    p_units.add_argument("--instance", required=True, help="实例名称")
    p_units.add_argument("--out", required=True, help="输出目录（必须已存在）")
    p_units.add_argument("--daily-time", default=brain_cfg.DEFAULT_DAILY_TIME, help="每日时间 HH:MM（默认 21:00）")
    p_units.add_argument("--weekly-time", default=brain_cfg.DEFAULT_WEEKLY_TIME, help="周日时间 HH:MM（默认 18:00）")
    p_units.add_argument("--resurface-time", default=brain_cfg.DEFAULT_RESURFACE_TIME, help="唤醒时间 HH:MM（默认 09:00）")
    p_units.add_argument("--memory-max", default=install.DEFAULT_MEMORY_MAX, help="内存上限（默认 512M）")


def dispatch_brain_command(args: argparse.Namespace) -> int | None:
    """分发处理 hushai brain 子命令；不属于本模块返回 None。"""
    if args.command != "brain":
        return None
    sub = getattr(args, "brain_command", None)
    if not sub:
        raise ValueError("请指定 brain 子命令（onboard/status/process/ask/daily/weekly/resurface/monthly/enable/disable/units）")

    cfg = load_instance_config(args.instance)

    if sub == "onboard":
        answers = {
            k: v
            for k, v in (
                ("work", args.work),
                ("remember", args.remember),
                ("notes_location", args.notes_location),
                ("reading_watching", args.reading),
                ("answer_style", args.answer_style),
            )
            if v is not None
        }
        initial_items: list[str] | None = None
        if args.items_file:
            p = Path(args.items_file).expanduser()
            initial_items = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        res = brain_onboard.run_onboard(
            cfg, answers=answers, initial_items=initial_items, interactive=bool(args.interactive)
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(res["output_text"], end="" if res["output_text"].endswith("\n") else "\n")
        return 0

    if sub == "status":
        res = brain_routines.get_brain_status(cfg)
        rt = res["routines"]
        if args.show_brain:
            res["heres_your_brain"] = brain_onboard.render_heres_your_brain(cfg)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"实例: {cfg.name} | 已引导: {res['onboarded']}")
            print(
                f"Routines: daily={rt['daily']}, weekly={rt['weekly']}, "
                f"resurface={rt['resurface']}, monthly={rt['monthly']}"
            )
            print(f"笔记统计: {res['counts']}")
            print(f"断链: {res['broken_links']} | 少于 {res['min_links']} 链的笔记: {res['underlinked_notes']}")
            if args.show_brain:
                print()
                print(res["heres_your_brain"])
        return 0

    if sub == "process":
        as_of = args.date.strip() if args.date and args.date.strip() else clock.today(cfg)
        res = brain_process.process_inbox_to_brain(
            cfg, as_of_date=as_of, dry_run=bool(args.dry_run), confirm_merge=bool(args.yes)
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"加工完成 ({res['date']}): 归档 {res['filed_count']} 条, 自动补链 {len(res['auto_linked'])} 篇")
            if res["underlinked_notes"]:
                print(f"关联不足告警: {len(res['underlinked_notes'])} 篇")
            if res["duplicate_candidates"] and not args.yes:
                print(f"发现疑似重复笔记 {len(res['duplicate_candidates'])} 对（需 --yes 确认合并）")
        return 0

    if sub == "ask":
        res = brain_ask.answer_from_notes(cfg, args.question, max_hits=max(1, args.limit))
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(res["answer_text"])
        return 0

    if sub == "daily":
        res = brain_routines.run_daily_brain(
            cfg, as_of=args.date, force=bool(args.force), dry_run=bool(args.dry_run)
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif not res.get("silent") and res.get("output_text"):
            print(res["output_text"])
        return 0

    if sub == "weekly":
        res = brain_routines.run_weekly_brain(
            cfg, as_of=args.date, force=bool(args.force), dry_run=bool(args.dry_run)
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif res.get("skipped"):
            print(f"weekly 已跳过: {res['skipped']}（需先执行 `hushai brain enable weekly` 或加 --force）")
        else:
            print(f"每周第二大脑回顾已写入: {res['weekly_file']}")
        return 0

    if sub == "resurface":
        res = brain_routines.run_resurface(
            cfg, as_of=args.date, force=bool(args.force), dry_run=bool(args.dry_run)
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif res.get("skipped"):
            print(f"resurface 已跳过: {res['skipped']}")
        elif res.get("found"):
            print(res["output_text"])
        else:
            print(res.get("message", "暂无可唤醒旧笔记"))
        return 0

    if sub == "monthly":
        res = brain_routines.run_monthly_maintenance(
            cfg, as_of=args.date, confirm_merge=bool(args.yes), force=bool(args.force), dry_run=bool(args.dry_run)
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif res.get("skipped"):
            print(f"monthly 已跳过: {res['skipped']}（需先执行 `hushai brain enable monthly` 或加 --force）")
        else:
            print(res["output_text"], end="")
        return 0

    if sub in ("enable", "disable"):
        enabled = sub == "enable"
        updated = brain_cfg.set_brain_routine_enabled(cfg, args.routine, enabled=enabled)
        res = {
            "instance": cfg.name,
            "routine": args.routine,
            "enabled": enabled,
            "state": {
                "enabled": updated.enabled,
                "daily": updated.daily_enabled,
                "weekly": updated.weekly_enabled,
                "resurface": updated.resurface_enabled,
                "monthly": updated.monthly_enabled,
            },
        }
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            label = "启用" if enabled else "禁用"
            print(f"已{label} routine `{args.routine}`：{res['state']}")
        return 0

    if sub == "units":
        written = install.render_brain_units(
            cfg,
            args.out,
            daily_time=args.daily_time,
            weekly_time=args.weekly_time,
            resurface_time=args.resurface_time,
            memory_max=args.memory_max,
        )
        for p in written:
            print(p)
        print("以上单元默认未启用；如需定时运行，请手动核对后执行 systemctl enable --now。")
        return 0

    return None
