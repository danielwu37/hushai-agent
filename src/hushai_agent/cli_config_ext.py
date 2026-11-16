"""交互式配置菜单子页：5. 多端接入与同步 / 6. 第二大脑设置与操作。"""

from __future__ import annotations

from hushai_agent import doorkit, install, sync
from hushai_agent.brain_ask import answer_from_notes
from hushai_agent.brain_cfg import ROUTINE_NAMES, load_brain_config, set_brain_routine_enabled
from hushai_agent.brain_onboard import render_heres_your_brain, run_onboard
from hushai_agent.brain_process import process_inbox_to_brain
from hushai_agent.brain_routines import (
    get_brain_status,
    run_daily_brain,
    run_monthly_maintenance,
    run_resurface,
    run_weekly_brain,
)
from hushai_agent.cli_config_io import (
    MenuIO,
    _menu_loop,
    _offer_regen_units,
    _prompt,
    _prompt_bool,
    _prompt_non_empty,
    _safe_apply,
)
from hushai_agent.config import load_instance_config
from hushai_agent.config_ext import load_ext


def _submenu_sync_and_doors(io: MenuIO, inst: str) -> None:
    """5. 多端接入与同步子菜单。"""

    def summary() -> list[str]:
        cfg = load_instance_config(inst)
        ext = load_ext(cfg)
        return [
            f"同步配置([sync]): tool={ext.sync_tool}, remote={ext.sync_remote or '（未配置）'}, timeout={ext.sync_timeout}s",
        ]

    def set_sync() -> None:
        ext = load_ext(load_instance_config(inst))
        tool = _prompt_non_empty(io, "同步工具名", ext.sync_tool)
        remote = _prompt(io, "远端路径（如 onedrive:hushai-vault，留空清空）", ext.sync_remote)
        timeout = _prompt_non_empty(io, "同步超时秒数", str(ext.sync_timeout))
        _safe_apply(
            io,
            inst,
            {"sync": {"tool": tool, "remote": remote, "timeout": timeout}},
            "单向同步配置已更新。",
            effect_hint="同步不会自动后台运行；修改后可通过本菜单第 2 项立即执行，或运行 `hushai sync`。",
        )

    def run_sync_now() -> None:
        cfg = load_instance_config(inst)
        direction = _prompt_non_empty(io, "同步方向 (both / push / pull)", "both")
        dry = io.read_line("是否只做演练 (--dry-run)？[y/N]: ").strip().lower() in ("y", "yes", "1")
        summary_res, rc = sync.run_sync(cfg, direction=direction, dry_run=dry)
        io.write(f"同步完成 (rc={rc}): {'成功' if summary_res.get('ok') else summary_res.get('error', '见详情')}")

    def gen_doorkit() -> None:
        cfg = load_instance_config(inst)
        adapter = _prompt_non_empty(io, "选择端适配包 (claude / agy / all)", "all")
        door = _prompt_non_empty(io, "写入收件使用的端编号（如 office、laptop、steward）", cfg.steward_door)
        out_dir = _prompt_non_empty(io, "输出目录路径（需已存在）")
        paths = doorkit.render_doorkit(cfg, out_dir, adapter=adapter, door=door)
        for p in paths:
            io.write(f"✓ 已生成: {p}")

    def gen_units() -> None:
        cfg = load_instance_config(inst)
        out_dir = _prompt_non_empty(io, "systemd 单元输出目录（需已存在）")
        default_dtime = install.detect_existing_digest_time(cfg)
        dtime = _prompt_non_empty(io, "每日汇总时间 HH:MM", default_dtime)
        port = int(_prompt_non_empty(io, "本机待办网页端口", str(install.DEFAULT_PORT)))
        with_brain = _prompt_bool(io, "是否同时生成 second-brain 专用定时器模板", load_brain_config(cfg).enabled)
        written = install.render_units(cfg, out_dir, dtime, install.DEFAULT_MEMORY_MAX, port, with_brain=with_brain)
        for p in written:
            io.write(f"✓ 已生成: {p}")
        io.write("提示：程序永不自动执行 systemctl enable，请按需复制并用 sudo systemctl enable --now 启用。")

    def show_connect_guide() -> None:
        cfg = load_instance_config(inst)
        io.write("\n【多端统一接入速查】")
        io.write("1. 办公室电脑 / 家庭笔记本（Claude Code / Antigravity agy）：")
        io.write("   - 使用本菜单第 3 项生成端接入包（doorkit），配好 SessionStart/Stop 钩子；")
        io.write("   - 远端电脑通过 SSH 强制命令 `hushai gate ssh` 或手动/定时执行 `rclone` 单向同步（菜单第 1-2 项）共用同一份记忆库。")
        io.write("2. 手机 Claude App：")
        io.write(f"   - 当管家后端为 claude 时，打开 Claude App -> Code，直接连入实例 `{cfg.name}` 的远程会话。")
        io.write("3. 手机 Muse / SSH 客户端：")
        io.write(f"   - 在服务器 ~/.ssh/authorized_keys 配置强制命令：`bin/hushai-gate-ssh --instance {cfg.name} --door phone`。")
        io.write("4. 手机浏览器 / Duet 等支持同步目录或网页的应用：")
        io.write(f"   - 运行 `hushai serve --instance {cfg.name}` 并通过 SSH 端口转发到手机浏览器，一键看简报、消待办、批记忆；")
        io.write("   - 网盘类应用通过 `hushai sync`（需手动或自建 cron 执行）推送 `hub/reports/`、`hub/index.md` 与 `hub/notes/` 供只读查阅。")

    _menu_loop(
        io,
        "5. 多端接入与同步",
        summary,
        [
            ("配置网盘单向同步 (rclone 远端与超时)", set_sync),
            ("立即执行单向同步 (both / push / pull)", run_sync_now),
            ("生成电脑端接入包 (doorkit)", gen_doorkit),
            ("生成 systemd 服务与定时器单元", gen_units),
            ("查看电脑 / 手机 (Claude、Muse、Duet) 接入速查", show_connect_guide),
        ],
    )


def _submenu_brain(io: MenuIO, inst: str) -> None:
    """6. 第二大脑（Second-Brain）设置与操作子菜单。"""

    def summary() -> list[str]:
        cfg = load_instance_config(inst)
        st = get_brain_status(cfg)
        rt = st["routines"]
        counts = ", ".join(f"{k}={v}" for k, v in st["counts"].items())
        return [
            f"引导状态: {'已完成' if st['onboarded'] else '未引导'} | 笔记数量: {counts}",
            f"Routine开关: daily={rt['daily']}, weekly={rt['weekly']}, "
            f"resurface={rt['resurface']}, monthly={rt['monthly']}",
            f"少链告警: {st['underlinked_notes']} 条 | 断链: {st['broken_links']} 处",
        ]

    def toggle_routines() -> None:
        cfg = load_instance_config(inst)
        b = load_brain_config(cfg)
        io.write("当前开关：")
        for r in ROUTINE_NAMES:
            io.write(f"  - {r}: {'已启用' if b.is_routine_enabled(r) else '已禁用'}")
        target = _prompt_non_empty(io, "输入要操作的 routine（daily / weekly / resurface / monthly / all）", "all")
        enable = _prompt_bool(io, f"将 `{target}` 设为启用还是禁用", True)
        new_b = set_brain_routine_enabled(cfg, target, enable)
        io.write(
            f"✓ 已更新：daily={new_b.daily_enabled}, weekly={new_b.weekly_enabled}, "
            f"resurface={new_b.resurface_enabled}, monthly={new_b.monthly_enabled}"
        )
        io.write("  [生效方式] daily/resurface 随每晚 digest 运行，weekly 随每周回顾运行；monthly 或自定义时间需安装专用 brain 定时器。")
        if new_b.monthly_enabled or enable:
            _offer_regen_units(io, inst)

    def set_brain_params() -> None:
        b = load_brain_config(load_instance_config(inst))
        d_time = _prompt_non_empty(io, "每日日报时间 (daily_time)", b.daily_time)
        w_day = _prompt_non_empty(io, "每周回顾星期 (weekly_day, 如 Sun/Mon)", b.weekly_day)
        w_time = _prompt_non_empty(io, "每周回顾时间 (weekly_time)", b.weekly_time)
        r_time = _prompt_non_empty(io, "每日唤醒时间 (resurface_time)", b.resurface_time)
        m_time = _prompt_non_empty(io, "每月维护时间 (monthly_time, 每月 1 号)", b.monthly_time)
        min_l = _prompt_non_empty(io, "新笔记最少关联链接数 (min_links)", str(b.min_links))
        stale_m = _prompt_non_empty(io, "孤立笔记告警月数 (stale_months)", str(b.stale_months))
        idx_max = _prompt_non_empty(io, "index.md 最大行数 (index_max_lines)", str(b.index_max_lines))
        _safe_apply(
            io,
            inst,
            {
                "brain": {
                    "daily_time": d_time,
                    "weekly_day": w_day,
                    "weekly_time": w_time,
                    "resurface_time": r_time,
                    "monthly_time": m_time,
                    "min_links": min_l,
                    "stale_months": stale_m,
                    "index_max_lines": idx_max,
                }
            },
            "第二大脑时间与规则参数已更新。",
            effect_hint="链接与行数规则立即生效；定时时间需重新生成并启用 second-brain systemd 定时器才会按新时间触发。",
            prompt_units=True,
        )

    def do_onboard() -> None:
        cfg = load_instance_config(inst)
        res = run_onboard(cfg, interactive=True, prompt_fn=lambda q: io.read_line(f"{q}\n> "))
        if res.get("skipped"):
            io.write(res["greeting"])
            if io.read_line("是否查看当前 HERE'S YOUR BRAIN 总览？[y/N]: ").strip().lower() in ("y", "yes", "1"):
                io.write(render_heres_your_brain(cfg))
        else:
            io.write(res.get("output_text", ""))

    def do_ask() -> None:
        res = answer_from_notes(load_instance_config(inst), _prompt_non_empty(io, "输入要向第二大脑提问的问题"))
        io.write("\n" + res["answer_text"])

    def do_process() -> None:
        cfg = load_instance_config(inst)
        confirm = io.read_line("遇到重复笔记时是否允许自动合并并备档？[y/N]: ").strip().lower() in ("y", "yes", "1")
        res = process_inbox_to_brain(cfg, confirm_merge=confirm)
        io.write(
            f"✓ 加工完成 ({res['date']})：归档 {res['filed_count']} 条 | 自动补链 {len(res['auto_linked'])} 篇 | "
            f"少链告警 {len(res['underlinked_notes'])} 篇 | 重复候选 {len(res['duplicate_candidates'])} 组"
        )

    def do_run_routine() -> None:
        cfg = load_instance_config(inst)
        which = _prompt_non_empty(io, "选择要手动运行的 routine (daily / weekly / resurface / monthly)", "daily")
        force = io.read_line("若该 routine 当前未启用，是否仍单次强制运行 (--force)？[y/N]: ").strip().lower() in ("y", "yes", "1")
        if which == "daily":
            out = run_daily_brain(cfg, force=force)
            if out.get("skipped"):
                io.write(f"daily 跳过: {out['skipped']}")
            elif out.get("silent"):
                io.write("daily: 无新内容，保持静默。")
            else:
                io.write(out.get("output_text", ""))
        elif which == "weekly":
            out = run_weekly_brain(cfg, force=force)
            io.write(f"weekly 完成: {out.get('weekly_file') or out.get('skipped')}")
        elif which == "resurface":
            out = run_resurface(cfg, force=force)
            io.write(out.get("output_text") or out.get("message") or f"resurface 跳过: {out.get('skipped')}")
        elif which == "monthly":
            confirm = io.read_line("是否允许清理并归档重复笔记/断链 (--yes)？[y/N]: ").strip().lower() in ("y", "yes", "1")
            out = run_monthly_maintenance(cfg, confirm_merge=confirm, force=force)
            io.write(out.get("output_text") or f"monthly 跳过: {out.get('skipped')}")
        else:
            raise ValueError(f"不支持的 routine: {which}")

    _menu_loop(
        io,
        "6. 第二大脑 (Second-Brain) 设置与操作",
        summary,
        [
            ("启用或禁用 Routine (daily / weekly / resurface / monthly / all)", toggle_routines),
            ("修改定时时间、最少链接数与 index 行数限制", set_brain_params),
            ("首次运行 5 问引导 / 查看 HERE'S YOUR BRAIN", do_onboard),
            ("向第二大脑提问 (Ask — 纯本地笔记有据回答)", do_ask),
            ("立即加工未处理收件到笔记与关联网络", do_process),
            ("手动运行一次指定 Routine (daily / weekly / resurface / monthly)", do_run_routine),
        ],
    )
