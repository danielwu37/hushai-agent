"""交互式配置菜单子页：5. 多端接入与同步 / 6. 第二大脑设置与操作。"""

from __future__ import annotations

from hushai_agent import brain_ask, brain_cfg, brain_lint, doorkit, install, sync
from hushai_agent.brain_ingest import run_ingest
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
        with_brain = _prompt_bool(io, "是否同时生成第二大脑编译定时器", brain_cfg.load_brain_config(cfg).enabled)
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
    """6. 第二大脑：开关、编译、检索、体检。"""

    def summary() -> list[str]:
        cfg = load_instance_config(inst)
        b = brain_cfg.load_brain_config(cfg)
        rep = brain_lint.lint(cfg, b)
        last = brain_cfg.load_state(cfg).get("last_run") or {}
        return [
            f"开关: {'已开启' if b.enabled else '已关闭'} | 收件起点: {b.since or '-'} | 每晚 {b.time} 编译",
            f"页面: {rep['pages']} | 待编译: 收件 {rep['pending_lines']} 行、文件 {rep['pending_files']} 个 | 体检: {rep['level']}",
            f"上次编译: {last.get('at', '无')}",
        ]

    def toggle() -> None:
        cfg = load_instance_config(inst)
        enable = _prompt_bool(io, "第二大脑开关", brain_cfg.load_brain_config(cfg).enabled)
        since = _prompt(io, "从哪天的收件行开始收（YYYY-MM-DD，回车为今天）", "") if enable else ""
        new_b = brain_cfg.set_enabled(cfg, enable, since=since or None)
        io.write(f"✓ 第二大脑已{'开启' if new_b.enabled else '关闭'}，收件起点 {new_b.since or '-'}")
        if new_b.enabled:
            _offer_regen_units(io, inst)

    def set_params() -> None:
        b = brain_cfg.load_brain_config(load_instance_config(inst))
        _safe_apply(
            io,
            inst,
            {"brain": {
                "time": _prompt_non_empty(io, "每晚编译时间 HH:MM", b.time),
                "max_daily_calls": _prompt_non_empty(io, "每日模型调用上限（单独预算）", str(b.max_daily_calls)),
                "max_calls_per_run": _prompt_non_empty(io, "每次编译最多调用次数", str(b.max_calls_per_run)),
                "index_max_lines": _prompt_non_empty(io, "index.md 最大行数", str(b.index_max_lines)),
                "stale_days": _prompt_non_empty(io, "多少天没更新算待复核", str(b.stale_days)),
            }},
            "第二大脑参数已更新。",
            effect_hint="上限立即生效；编译时间需重新生成并安装第二大脑定时器。",
            prompt_units=True,
        )

    def do_ingest() -> None:
        res = run_ingest(load_instance_config(inst), force=True)
        io.write(f"✓ 编译 {res.get('done', 0)}/{res.get('pending', 0)} 批 | 新建 {len(res.get('created', []))} | "
                 f"更新 {len(res.get('updated', []))}" + (f" | 中止: {res['degraded']}" if res.get("degraded") else ""))

    def do_ask() -> None:
        res = brain_ask.ask(load_instance_config(inst), _prompt_non_empty(io, "要查什么"))
        io.write("\n" + brain_ask.render_text(res))

    def do_lint() -> None:
        io.write(brain_lint.render_text(brain_lint.lint(load_instance_config(inst))))

    _menu_loop(
        io,
        "6. 第二大脑",
        summary,
        [
            ("开启或关闭", toggle),
            ("修改编译时间、预算与目录行数", set_params),
            ("立即编译一次待处理材料（调模型）", do_ingest),
            ("检索页面（本地，不调模型）", do_ask),
            ("体检（断链、重名、孤页、待复核）", do_lint),
        ],
    )
