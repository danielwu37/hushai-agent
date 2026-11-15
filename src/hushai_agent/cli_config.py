"""交互式设置与控制菜单（`hushai config`）：零命令门槛管理全部配置、接入与日常操作。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hushai_agent import brief, candidates, digest, doctor, doorkit, heal, install, lifecycle, steward, sync, weekly
from hushai_agent.adapter_cmd import run_adapter_test
from hushai_agent.brain_ask import answer_from_notes
from hushai_agent.brain_cfg import (
    ROUTINE_NAMES,
    load_brain_config,
    set_brain_routine_enabled,
)
from hushai_agent.brain_onboard import render_heres_your_brain, run_onboard
from hushai_agent.brain_process import process_inbox_to_brain
from hushai_agent.brain_routines import (
    get_brain_status,
    run_daily_brain,
    run_monthly_maintenance,
    run_resurface,
    run_weekly_brain,
)
from hushai_agent.config import (
    DEFAULT_DOMAINS_EN,
    DEFAULT_DOMAINS_ZH,
    DEFAULT_LANG,
    DEFAULT_TIMEZONE,
    ConfigError,
    load_instance_config,
)
from hushai_agent.config_edit import apply_config_updates, list_instances
from hushai_agent.config_ext import load_ext
from hushai_agent.paths import get_instance_config_dir, get_instance_config_file
from hushai_agent.todo import get_today_status, run_todo_action
from hushai_agent.vault import add_inbox_entry, init_instance


@dataclass
class MenuIO:
    """可替换的终端读写通道，便于单元测试驱动菜单交互。"""

    read_line: Callable[[str], str] = input
    write: Callable[[str], None] = print


def register_config_command(subparsers: Any) -> None:
    """注册 `hushai config` 交互式设置菜单命令。"""
    p_cfg = subparsers.add_parser("config", help="打开交互式设置与控制菜单（1-N 编号操作，无需记忆命令）")
    p_cfg.add_argument("--instance", default=None, help="默认进入的实例名称（省略时自动识别或列选）")


def dispatch_config_command(args: argparse.Namespace) -> int | None:
    """分发 `hushai config` 命令；不属于本模块返回 None。"""
    if args.command != "config":
        return None
    return run_config_menu(instance=args.instance)


def _prompt(io: MenuIO, text: str, default: str | None = None) -> str:
    suffix = f" [回车保留: {default}]" if default not in (None, "") else ""
    raw = io.read_line(f"{text}{suffix}: ").strip()
    if not raw and default is not None:
        return default
    return raw


def _prompt_non_empty(io: MenuIO, text: str, default: str = "") -> str:
    val = _prompt(io, text, default=default if default else None).strip()
    if not val:
        raise ValueError(f"{text} 不能为空")
    return val


def _prompt_bool(io: MenuIO, label: str, current: bool) -> bool:
    cur_str = "开启(true)" if current else "关闭(false)"
    raw = io.read_line(f"{label}（当前: {cur_str}，输入 y 开启 / n 关闭 / 回车保持）: ").strip().lower()
    if not raw:
        return current
    if raw in ("y", "yes", "1", "true", "on", "是", "开"):
        return True
    if raw in ("n", "no", "0", "false", "off", "否", "关"):
        return False
    raise ValueError(f"无法识别的开关输入: {raw}")


def _safe_apply(
    io: MenuIO,
    instance: str,
    updates: dict[str, dict[str, str | None]],
    ok_msg: str = "设置已保存并校验通过。",
    effect_hint: str = "立即生效（下次命令或网页请求自动读取新配置）",
    prompt_units: bool = False,
) -> bool:
    try:
        apply_config_updates(instance, updates)
        io.write(f"✓ {ok_msg}")
        if effect_hint:
            io.write(f"  [生效方式] {effect_hint}")
        if prompt_units:
            _offer_regen_units(io, instance)
        return True
    except (ConfigError, ValueError, OSError) as exc:
        io.write(f"✗ 修改未保存（已自动回滚原配置）: {exc}")
        return False


def _offer_regen_units(io: MenuIO, instance: str) -> None:
    cfg = load_instance_config(instance)
    bcfg = load_brain_config(cfg)
    units_dir = get_instance_config_dir(instance) / "units"
    ans = io.read_line(f"是否立即重新生成 systemd 单元到 {units_dir}？[y/N]: ").strip().lower()
    if ans not in ("y", "yes", "1", "是"):
        return
    units_dir.mkdir(parents=True, exist_ok=True)
    dtime = install.detect_existing_digest_time(cfg)
    with_brain = bcfg.enabled
    written = install.render_units(cfg, units_dir, digest_time=dtime, with_brain=with_brain)
    io.write(f"✓ 已重新生成 {len(written)} 个单元文件至: {units_dir}")
    for line in install.next_steps(cfg, written):
        io.write(f"  {line}")


def _run_guarded(io: MenuIO, action: Callable[[], None]) -> None:
    try:
        action()
    except (ConfigError, ValueError, OSError, RuntimeError) as exc:
        io.write(f"✗ 操作失败: {exc}")


def _wizard_create_interactive(io: MenuIO, preset_instance: str | None = None) -> str | None:
    io.write("\n--- 初始化新实例（向导）---")
    inst = (preset_instance or io.read_line("实例名（小写字母开头，如 my）: ").strip()).strip()
    if not inst:
        io.write("已取消初始化。")
        return None
    vault_default = str(Path.home() / f"hushai-vault-{inst}" if inst != "my" else Path.home() / "hushai-vault")
    vault = _prompt(io, "记忆库路径（绝对路径）", default=vault_default)
    tz = _prompt(io, "时区", default=DEFAULT_TIMEZONE)
    lang = _prompt(io, "默认语言 (zh/en)", default=DEFAULT_LANG)
    steward_name = _prompt(io, "默认常驻管家后端 (claude/agy)", default="claude")
    remote = _prompt(io, "rclone 同步远端（可留空）", default="")
    res = doorkit.run_wizard(
        instance=inst,
        vault=vault,
        timezone=tz,
        lang=lang,
        steward=steward_name,
        remote=remote,
    )
    io.write(f"✓ 实例 {inst} 初始化完成（vault: {res['vault']}｜体检: {'OK' if res['ok'] else 'WARN/FAIL'}）")
    io.write(f"  服务单元已生成在: {res['units_dir']}")
    io.write(f"  端接入包已生成在: {res['doorkit_dir']}")
    return inst


def _select_initial_instance(io: MenuIO, requested: str | None) -> str | None:
    existing = list_instances()
    if requested:
        if get_instance_config_file(requested).is_file():
            return requested
        io.write(f"实例 `{requested}` 尚未初始化。")
        ans = io.read_line("是否现在交互初始化该实例？[y/N]: ").strip().lower()
        if ans in ("y", "yes", "1", "是"):
            return _wizard_create_interactive(io, preset_instance=requested)
        return None

    if len(existing) == 1:
        return existing[0]

    if not existing:
        io.write("当前尚未发现已初始化的 hushAi 实例。")
        io.write("  1. 立即初始化一个新实例")
        io.write("  0. 退出")
        choice = io.read_line("请选择 [1/0]: ").strip()
        if choice == "1":
            return _wizard_create_interactive(io)
        return None

    while True:
        io.write("\n检测到多个实例，请选择要管理的实例：")
        for idx, name in enumerate(existing, start=1):
            io.write(f"  {idx}. {name}")
        io.write("  n. 初始化新实例")
        io.write("  0. 退出")
        raw = io.read_line("输入编号: ").strip().lower()
        if raw in ("0", "q", "exit"):
            return None
        if raw == "n":
            created = _wizard_create_interactive(io)
            if created:
                return created
            existing = list_instances()
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(existing):
            return existing[int(raw) - 1]
        io.write("无效选择，请重新输入。")


def _menu_loop(
    io: MenuIO,
    title: str,
    render_summary: Callable[[], list[str]],
    items: list[tuple[str, Callable[[], None]]],
) -> None:
    while True:
        io.write(f"\n--- {title} ---")
        for line in render_summary():
            io.write(f"  {line}")
        for idx, (label, _) in enumerate(items, start=1):
            io.write(f"  {idx}. {label}")
        io.write("  0. 返回上级菜单")
        raw = io.read_line("请选择编号: ").strip()
        if raw in ("0", "q", "back"):
            return
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            _run_guarded(io, items[int(raw) - 1][1])
        else:
            io.write("请输入有效编号。")


# ---------------------------------------------------------------------------
# 1. 基础与记忆库设置
# ---------------------------------------------------------------------------
def _submenu_basic(io: MenuIO, inst: str) -> None:
    def summary() -> list[str]:
        cfg = load_instance_config(inst)
        ext = load_ext(cfg)
        return [
            f"记忆库(vault): {cfg.vault} | 时区: {cfg.timezone} | 语言: {cfg.lang}",
            f"仅本机目录(local_only): {','.join(cfg.local_only) or '（无）'}",
            f"上限: today={cfg.today_items}, status_index={cfg.status_index_lines}, "
            f"profile={cfg.profile_lines}, agents={cfg.agents_lines}, candidates={ext.candidates_pending}",
            f"窗口: window_days={cfg.window_days}, stale_days={cfg.stale_days}, status_archive_days={cfg.status_archive_days}",
        ]

    def set_timezone() -> None:
        cfg = load_instance_config(inst)
        val = _prompt_non_empty(io, "输入新时区（如 Asia/Shanghai、America/Los_Angeles）", cfg.timezone)
        _safe_apply(
            io,
            inst,
            {"instance": {"timezone": val}},
            f"时区已更新为 {val}",
            effect_hint="CLI 与待办网页立即生效；已安装的 systemd 定时器需重新生成并复制启用才会按新时区触发。",
            prompt_units=True,
        )

    def set_lang() -> None:
        cfg = load_instance_config(inst)
        val = _prompt_non_empty(io, "输入语言 (zh 或 en)", cfg.lang).lower()
        updates: dict[str, dict[str, str | None]] = {"instance": {"lang": val}}
        sync_dom = io.read_line("是否同步将领域恢复为该语言的默认领域？[y/N]: ").strip().lower()
        if sync_dom in ("y", "yes", "1"):
            domains = DEFAULT_DOMAINS_EN if val == "en" else DEFAULT_DOMAINS_ZH
            updates["domains"] = {"names": ",".join(domains)}
        _safe_apply(io, inst, updates, f"语言已更新为 {val}")

    def set_vault() -> None:
        cfg = load_instance_config(inst)
        val = _prompt_non_empty(io, "输入记忆库绝对路径", str(cfg.vault))
        vpath = Path(val).expanduser()
        if not (vpath / "hub" / "AGENTS.md").is_file():
            ans = io.read_line(f"路径 `{vpath}` 尚未初始化为记忆库，是否立即初始化？[y/N]: ").strip().lower()
            if ans in ("y", "yes", "1", "是"):
                init_instance(inst, vpath, timezone=cfg.timezone, lang=cfg.lang)
                io.write(f"✓ 已初始化记忆库目录: {vpath}")
        _safe_apply(
            io,
            inst,
            {"instance": {"vault": val}},
            f"记忆库路径已更新为 {val}",
            effect_hint="CLI 与待办网页立即生效；watch.path 与 steward.service 的工作目录需重新生成单元并重启服务。",
            prompt_units=True,
        )

    def set_local_only() -> None:
        cfg = load_instance_config(inst)
        val = _prompt(io, "输入仅本机相对目录（逗号分隔，绝不传给外部 AI 或同步）", ",".join(cfg.local_only))
        _safe_apply(io, inst, {"privacy": {"local_only": val}}, f"local_only 已更新为: {val or '（空）'}")

    def set_limits() -> None:
        cfg = load_instance_config(inst)
        ext = load_ext(cfg)
        today_n = _prompt_non_empty(io, "今日待办条数上限 (today_items)", str(cfg.today_items))
        idx_n = _prompt_non_empty(io, "近况索引行数上限 (status_index_lines)", str(cfg.status_index_lines))
        prof_n = _prompt_non_empty(io, "画像行数上限 (profile_lines)", str(cfg.profile_lines))
        ag_n = _prompt_non_empty(io, "AGENTS 行数上限 (agents_lines)", str(cfg.agents_lines))
        cand_n = _prompt_non_empty(io, "待批候选记忆上限 (candidates_pending)", str(ext.candidates_pending))
        _safe_apply(
            io,
            inst,
            {
                "limits": {
                    "today_items": today_n,
                    "status_index_lines": idx_n,
                    "profile_lines": prof_n,
                    "agents_lines": ag_n,
                    "candidates_pending": cand_n,
                }
            },
            "篇幅与待办上限已更新。",
        )

    def set_windows() -> None:
        cfg = load_instance_config(inst)
        w = _prompt_non_empty(io, "近期扫描窗口天数 (window_days)", str(cfg.window_days))
        s = _prompt_non_empty(io, "项目陈旧告警天数 (stale_days)", str(cfg.stale_days))
        a = _prompt_non_empty(io, "近况归档天数 (status_archive_days)", str(cfg.status_archive_days))
        _safe_apply(
            io,
            inst,
            {"jobs": {"window_days": w, "stale_days": s, "status_archive_days": a}},
            "整理与归档窗口天数已更新。",
        )

    _menu_loop(
        io,
        "1. 基础与记忆库设置",
        summary,
        [
            ("修改时区", set_timezone),
            ("修改语言 (zh / en)", set_lang),
            ("修改记忆库 (vault) 路径", set_vault),
            ("修改「仅本机」隐私目录 (local_only)", set_local_only),
            ("修改篇幅与待办条数上限", set_limits),
            ("修改整理扫描与归档窗口天数", set_windows),
        ],
    )


# ---------------------------------------------------------------------------
# 2. 领域与接入端管理
# ---------------------------------------------------------------------------
def _submenu_doors_domains(io: MenuIO, inst: str) -> None:
    def summary() -> list[str]:
        cfg = load_instance_config(inst)
        ext = load_ext(cfg)
        return [
            f"领域([domains] names): {','.join(cfg.domains)}",
            f"管家端([doors] steward): {cfg.steward_door} | 已知端(known): {','.join(cfg.known_doors)}",
            f"有权批准记忆的端([approval] doors): {','.join(ext.approval_doors)}",
            f"交换区([gate]): max_kb={ext.exchange_max_kb}, ext={','.join(ext.exchange_ext)}",
        ]

    def set_domains() -> None:
        cfg = load_instance_config(inst)
        val = _prompt_non_empty(io, "输入全部领域（逗号分隔，如 工作,家庭,生活,学习,健康）", ",".join(cfg.domains))
        _safe_apply(io, inst, {"domains": {"names": val}}, f"领域已更新为: {val}")

    def set_steward_door() -> None:
        cfg = load_instance_config(inst)
        val = _prompt_non_empty(io, "输入管家端编号", cfg.steward_door)
        known = list(cfg.known_doors)
        if val not in known:
            known.append(val)
        _safe_apply(
            io,
            inst,
            {"doors": {"steward": val, "known": ",".join(known)}},
            f"管家端已设为 {val}",
            effect_hint="CLI 与管家切换立即生效；若已生成 doorkit 接入包，请到菜单 5 重新生成以更新钩子里的端编号。",
        )

    def set_known_doors() -> None:
        cfg = load_instance_config(inst)
        io.write("提示：多端建议按设备/入口命名，如 steward,office,laptop,phone,muse,web")
        val = _prompt_non_empty(io, "输入全部已知接入端（逗号分隔）", ",".join(cfg.known_doors))
        _safe_apply(io, inst, {"doors": {"known": val}}, f"已知接入端已更新为: {val}")

    def set_approval_doors() -> None:
        cfg = load_instance_config(inst)
        ext = load_ext(cfg)
        val = _prompt_non_empty(io, "输入有权批准/丢弃候选记忆的端（逗号分隔）", ",".join(ext.approval_doors))
        _safe_apply(io, inst, {"approval": {"doors": val}}, f"审批权限端已更新为: {val}")

    def set_gate_limits() -> None:
        ext = load_ext(load_instance_config(inst))
        exts = _prompt_non_empty(io, "允许投递的交换区扩展名（逗号分隔）", ",".join(ext.exchange_ext))
        max_kb = _prompt_non_empty(io, "单文件体积上限 KB (exchange_max_kb)", str(ext.exchange_max_kb))
        _safe_apply(
            io,
            inst,
            {"gate": {"exchange_ext": exts, "exchange_max_kb": max_kb}},
            "交换区扩展名与大小限制已更新。",
        )

    _menu_loop(
        io,
        "2. 领域与接入端管理",
        summary,
        [
            ("修改领域列表", set_domains),
            ("设置管家端编号", set_steward_door),
            ("设置已知接入端 (known doors：电脑/手机/浏览器)", set_known_doors),
            ("设置可批准候选记忆的端", set_approval_doors),
            ("设置交换区扩展名与体积上限", set_gate_limits),
        ],
    )


# ---------------------------------------------------------------------------
# 3. AI 适配器与模型调度
# ---------------------------------------------------------------------------
def _submenu_adapters(io: MenuIO, inst: str) -> None:
    def summary() -> list[str]:
        cfg = load_instance_config(inst)
        ad_desc = ", ".join(
            f"{n}({p.get('type', n)}: light={p.get('light', '-')}, heavy={p.get('heavy', '-')})"
            for n, p in sorted(cfg.adapters.items())
        ) or "（尚未配置任何适配器）"
        dig = ",".join(f"{a}:{t}" for a, t in cfg.digest) or "（未配，将仅跑机械兜底）"
        wk = ",".join(f"{a}:{t}" for a, t in cfg.weekly) or "（未配）"
        return [
            f"已配适配器: {ad_desc}",
            f"每日整理链(digest): {dig} | 每周链(weekly): {wk}",
            f"一键派活端(dispatch): {cfg.dispatch_adapter or '（未指定）'} | 每日预算上限: {cfg.max_daily_calls} 次",
        ]

    def upsert_adapter() -> None:
        cfg = load_instance_config(inst)
        name = _prompt_non_empty(io, "输入适配器名称（小写，如 claude、agy、fake）", "claude").lower()
        cur = cfg.adapters.get(name, {})
        atype = _prompt_non_empty(
            io,
            "适配类型 (claude / agy / fake)",
            cur.get("type", name if name in ("claude", "agy", "fake") else "claude"),
        ).lower()
        updates: dict[str, str | None] = {f"{name}.type": atype}
        if atype == "fake":
            fixtures = _prompt_non_empty(io, "fixtures 样例目录路径", cur.get("fixtures", ""))
            updates[f"{name}.fixtures"] = fixtures
        else:
            cmd = _prompt(io, "可执行命令路径（留空则默认按类型名查找）", cur.get("command", atype))
            light = _prompt(io, "轻档模型 (light)", cur.get("light", "haiku" if atype == "claude" else "gemini-3.8-flash-low"))
            heavy = _prompt(io, "重档模型 (heavy)", cur.get("heavy", "sonnet" if atype == "claude" else "gemini-4-argon-high"))
            timeout = _prompt_non_empty(io, "单次调用超时秒数", cur.get("timeout", "180"))
            updates[f"{name}.command"] = cmd or None
            updates[f"{name}.light"] = light or None
            updates[f"{name}.heavy"] = heavy or None
            updates[f"{name}.timeout"] = timeout
        _safe_apply(io, inst, {"adapters": updates}, f"适配器 `{name}` 已更新。")

    def delete_adapter() -> None:
        cfg = load_instance_config(inst)
        if not cfg.adapters:
            io.write("当前没有可删除的适配器。")
            return
        name = _prompt_non_empty(io, f"输入要删除的适配器名（可选: {', '.join(cfg.adapters)}）").lower()
        if name not in cfg.adapters:
            io.write(f"适配器 `{name}` 不存在。")
            return
        del_map: dict[str, str | None] = {f"{name}.{k}": None for k in cfg.adapters[name]}
        del_map[f"{name}.type"] = None
        _safe_apply(io, inst, {"adapters": del_map}, f"已移除适配器 `{name}`。")

    def set_digest_chain() -> None:
        cfg = load_instance_config(inst)
        cur = ",".join(f"{a}:{t}" for a, t in cfg.digest)
        val = _prompt(io, "每日整理回退链（格式 <adapter>:<light|heavy>，输入 clear 清空）", cur)
        if val.lower() in ("clear", "none", "-", "清空"):
            val = ""
        _safe_apply(io, inst, {"jobs": {"digest": val}}, f"digest 回退链已更新为: {val or '（空）'}")

    def set_weekly_chain() -> None:
        cfg = load_instance_config(inst)
        cur = ",".join(f"{a}:{t}" for a, t in cfg.weekly)
        val = _prompt(io, "每周回顾回退链（格式 <adapter>:<light|heavy>，输入 clear 清空）", cur)
        if val.lower() in ("clear", "none", "-", "清空"):
            val = ""
        _safe_apply(io, inst, {"jobs": {"weekly": val}}, f"weekly 回退链已更新为: {val or '（空）'}")

    def set_dispatch() -> None:
        cfg = load_instance_config(inst)
        val = _prompt(io, "默认派活适配器名称（仅填名字，如 agy 或 claude；输入 clear 清空）", cfg.dispatch_adapter)
        if val.lower() in ("clear", "none", "-", "清空"):
            val = ""
        _safe_apply(io, inst, {"jobs": {"dispatch": val}}, f"默认派活端已更新为: {val or '（空）'}")

    def set_budget() -> None:
        cfg = load_instance_config(inst)
        val = _prompt_non_empty(io, "每日模型调用预算上限 (0 表示不启用模型调用)", str(cfg.max_daily_calls))
        _safe_apply(io, inst, {"budget": {"max_daily_calls": val}}, f"每日预算上限已设为 {val} 次")

    def test_adapter() -> None:
        cfg = load_instance_config(inst)
        if not cfg.adapters:
            io.write("尚未配置任何适配器，请先添加。")
            return
        name = _prompt_non_empty(io, f"要自检的适配器名（可选: {', '.join(cfg.adapters)}）", next(iter(cfg.adapters)))
        tier = _prompt_non_empty(io, "测试档位 (light / heavy)", "light")
        res = run_adapter_test(inst, name, tier=tier)
        status = "成功" if res["ok"] else "失败"
        io.write(f"自检结果 ({res['adapter']}:{res['tier']}): {status} | 模型={res['model'] or '-'} | 耗时={res['duration']}s")
        if res.get("error"):
            io.write(f"错误信息: {res['error']}")

    _menu_loop(
        io,
        "3. AI 适配器与模型调度",
        summary,
        [
            ("添加或修改 AI 适配器 (claude / agy / fake)", upsert_adapter),
            ("删除 AI 适配器", delete_adapter),
            ("设置每日整理回退链 ([jobs] digest)", set_digest_chain),
            ("设置每周回顾回退链 ([jobs] weekly)", set_weekly_chain),
            ("设置一键派活默认适配器 ([jobs] dispatch)", set_dispatch),
            ("设置每日模型调用预算 ([budget] max_daily_calls)", set_budget),
            ("立即自检指定适配器", test_adapter),
        ],
    )


# ---------------------------------------------------------------------------
# 4. 常驻管家管理与跨端路由
# ---------------------------------------------------------------------------
def _submenu_steward(io: MenuIO, inst: str) -> None:
    def summary() -> list[str]:
        cfg = load_instance_config(inst)
        ext = load_ext(cfg)
        st = steward.status(cfg)
        chains = ", ".join(
            f"{d}->" + "/".join(f"{a}:{t}" for a, t in ch)
            for d, ch in sorted(ext.routing_chains.items())
        ) or "（无）"
        return [
            f"当前管家后端: {st['backend']} | tmux({st['tmux_session']}): {'alive' if st['tmux_alive'] else 'dead'} | systemd: {st['service_state']}",
            f"跨端路由([routing]): {'开启' if ext.routing_enabled else '关闭'} | 受信任端: {','.join(ext.trusted_doors) or '（无）'} | 日限: {ext.routing_max_daily}",
            f"目标路由链: {chains}",
        ]

    def show_status() -> None:
        st = steward.status(load_instance_config(inst))
        io.write(f"后端={st['backend']}, service={st['service']}({st['service_state']}), tmux={st['tmux_session']}({'alive' if st['tmux_alive'] else 'dead'})")
        io.write(f"启动脚本: {st['start_script']}")

    def switch_backend(target: str) -> None:
        cfg = load_instance_config(inst)
        no_restart = io.read_line("是否立即重启 systemd 管家服务使切换生效？[Y/n]: ").strip().lower() in ("n", "no", "0")
        res = steward.use_backend(cfg, target, restart=not no_restart)
        io.write(f"切换管家后端: {res['previous_backend']} -> {res['backend']}｜服务: {res['restart_detail']}")

    def do_restart() -> None:
        res = steward.restart_steward(load_instance_config(inst))
        io.write(f"重启管家 ({res['service']}): {res['detail']}")

    def config_routing_basic() -> None:
        cfg = load_instance_config(inst)
        ext = load_ext(cfg)
        en = _prompt_bool(io, "是否启用跨端 @留言 路由", ext.routing_enabled)
        trusted = _prompt(io, "受信任的来源端（逗号分隔）", ",".join(ext.trusted_doors))
        max_d = _prompt_non_empty(io, "每日最大路由次数", str(ext.routing_max_daily))
        _safe_apply(
            io,
            inst,
            {
                "routing": {
                    "enabled": "true" if en else "false",
                    "trusted_doors": trusted,
                    "max_daily": max_d,
                }
            },
            "跨端路由基础设置已更新。",
        )

    def set_routing_target() -> None:
        ext = load_ext(load_instance_config(inst))
        door = _prompt_non_empty(io, "目标端名称（如 steward）", "steward")
        cur = ",".join(f"{a}:{t}" for a, t in ext.routing_chains.get(door, []))
        chain = _prompt(io, f"目标 `{door}` 的模型链（如 agy:light,claude:light；留空删除）", cur)
        _safe_apply(
            io,
            inst,
            {"routing": {door: chain if chain else None}},
            f"目标端 `{door}` 路由链已更新。",
        )

    _menu_loop(
        io,
        "4. 常驻管家与跨端路由",
        summary,
        [
            ("查看管家运行详情", show_status),
            ("切换管家后端为 claude", lambda: switch_backend("claude")),
            ("切换管家后端为 agy", lambda: switch_backend("agy")),
            ("立即重启管家服务", do_restart),
            ("设置跨端路由开关与受信任来源端", config_routing_basic),
            ("配置/删除某个目标端的路由模型链", set_routing_target),
        ],
    )


# ---------------------------------------------------------------------------
# 5. 多端接入与同步
# ---------------------------------------------------------------------------
def _submenu_sync_and_doors(io: MenuIO, inst: str) -> None:
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


# ---------------------------------------------------------------------------
# 6. 第二大脑（Second-Brain）设置与操作
# ---------------------------------------------------------------------------
def _submenu_brain(io: MenuIO, inst: str) -> None:
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
        cfg = load_instance_config(inst)
        b = load_brain_config(cfg)
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
        cfg = load_instance_config(inst)
        q = _prompt_non_empty(io, "输入要向第二大脑提问的问题")
        res = answer_from_notes(cfg, q)
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


# ---------------------------------------------------------------------------
# 7. 日常查看与待办操作
# ---------------------------------------------------------------------------
def _submenu_daily_ops(io: MenuIO, inst: str) -> None:
    def summary() -> list[str]:
        cfg = load_instance_config(inst)
        pending_cands = candidates.pending(candidates.load(cfg))
        return [f"当前待批候选记忆: {len(pending_cands)} 条"]

    def show_today() -> None:
        _, lines = get_today_status(inst)
        io.write("\n" + "\n".join(lines))

    def show_brief() -> None:
        data = brief.build_brief(load_instance_config(inst))
        io.write("\n" + brief.render_md(data))

    def quick_inbox() -> None:
        cfg = load_instance_config(inst)
        door = _prompt_non_empty(io, f"写入端编号（已知: {','.join(cfg.known_doors)}）", cfg.steward_door)
        domain = _prompt_non_empty(io, f"所属领域（可选: {','.join(cfg.domains)}）", cfg.domains[0])
        did = _prompt_non_empty(io, "做了什么（简述事实或记录）")
        artifact = _prompt(io, "产物路径（无则直接回车）", "") or None
        todo_txt = _prompt(io, "下一步待办（无则直接回车）", "") or None
        p = add_inbox_entry(cfg.name, door=door, domain=domain, did=did, artifact=artifact, todo=todo_txt)
        io.write(f"✓ 已追加收件至: {p}")

    def todo_action() -> None:
        _, lines = get_today_status(inst)
        io.write("\n" + "\n".join(lines))
        action = _prompt_non_empty(io, "选择动作 (done / defer / drop / accept)", "done")
        target = _prompt_non_empty(io, "输入待办序号或 YYMMDD-N（如 1）")
        to_date = _prompt(io, "如为 defer，输入目标日期 YYYY-MM-DD（回车默认+7天）", "") or None
        p = run_todo_action(inst, action=action, target=target, to_date=to_date)
        io.write(f"✓ 已记录动作并触发对账: {p}")

    def review_candidates() -> None:
        cfg = load_instance_config(inst)
        rows = candidates.pending(candidates.load(cfg))
        if not rows:
            io.write("当前没有待审批的候选记忆。")
            return
        for c in rows:
            io.write(f"  {c.id}｜{c.kind}｜{c.text}")
        cid = _prompt_non_empty(io, "输入要处理的候选编号（如 C-261114-1）", rows[0].id)
        act = _prompt_non_empty(io, "处理方式 (approve-profile / approve-rules / discard)", "approve-profile")
        if act == "discard":
            p = candidates.command(cfg, "discard", cid, "", cfg.steward_door)
        elif act == "approve-rules":
            p = candidates.command(cfg, "approve", cid, "rules", cfg.steward_door)
        else:
            p = candidates.command(cfg, "approve", cid, "profile", cfg.steward_door)
        io.write(f"✓ 已记录候选审批: {p}")

    def web_hint() -> None:
        io.write(f"在服务器终端运行：`hushai serve --instance {inst} --port 8765`")
        io.write("若从电脑/手机安全访问，通过 SSH 转发端口：`ssh -L 8765:127.0.0.1:8765 <你的服务器>` 后打开打印的带令牌链接即可。")

    _menu_loop(
        io,
        "7. 日常查看与待办操作",
        summary,
        [
            ("查看今日待办 (today)", show_today),
            ("查看晨间简报 (brief)", show_brief),
            ("记一件事到收件箱 (inbox add)", quick_inbox),
            ("更新待办状态（完成 / 推迟 / 放弃 / 按推荐定）", todo_action),
            ("审批候选记忆 (candidate approve / discard)", review_candidates),
            ("查看本机待办网页启动与远程访问说明", web_hint),
        ],
    )


# ---------------------------------------------------------------------------
# 8. 手动触发整理与系统维护
# ---------------------------------------------------------------------------
def _submenu_maintenance(io: MenuIO, inst: str) -> None:
    def summary() -> list[str]:
        return ["说明：所有写操作均采用原子写，修改前自动持锁保护。"]

    def do_reconcile() -> None:
        res = digest.run_mechanical_only(load_instance_config(inst))
        io.write(f"✓ 机械对账完成：处理 {res.get('processed_lines', 0)} 行，应用 {res.get('mechanical_applied', 0)} 个动作")

    def do_digest() -> None:
        dry = io.read_line("是否演练模式 (--dry-run)？[y/N]: ").strip().lower() in ("y", "yes", "1")
        res = digest.run_digest(load_instance_config(inst), dry_run=dry)
        io.write(f"✓ 每日汇总完成 ({res['date']})：新增收件 {res['new_lines']} 行，明日待办 {res['today_count']} 条，降级={res.get('degraded') or '否'}")

    def do_weekly() -> None:
        dry = io.read_line("是否演练模式 (--dry-run)？[y/N]: ").strip().lower() in ("y", "yes", "1")
        res = weekly.run_weekly(load_instance_config(inst), dry_run=dry)
        io.write(f"✓ 每周回顾完成 ({res['date']})：逾期 {res['overdue_count']} 条，陈旧 {res['stale_count']} 条，洞察 {res['insights_count']} 条")

    def do_heal() -> None:
        res = heal.run_heal(load_instance_config(inst))
        for c in res["checks"]:
            io.write(f"  [{c['status'].upper()}] {c['id']} ({c['name']}): {c.get('result') or c.get('problem') or ''}")

    def do_doctor() -> None:
        rep = doctor.run_doctor(inst)
        for c in rep["checks"]:
            io.write(f"  [{c['level']}] {c['id']}: {c['message']}")

    def do_export() -> None:
        cfg = load_instance_config(inst)
        out = _prompt_non_empty(io, "输入备份输出路径（以 .tar.gz 结尾自动打包，或填目录）", str(Path.home() / f"{inst}-backup.tar.gz"))
        res = lifecycle.run_export(cfg, out)
        io.write(f"✓ 导出完成: {res['path']}（含 {res['vault_files']} 个记忆库文件）")

    def do_upgrade() -> None:
        res = lifecycle.run_upgrade(load_instance_config(inst))
        io.write("✓ 已更新模板: " + ", ".join(res["updated"]) if res["updated"] else "✓ 框架模板已是最新。")

    _menu_loop(
        io,
        "8. 手动触发整理与系统维护",
        summary,
        [
            ("立即执行机械对账 (reconcile)", do_reconcile),
            ("立即执行每日汇总整理 (digest)", do_digest),
            ("立即执行每周认知回顾 (weekly)", do_weekly),
            ("立即执行静默自愈自检 (heal)", do_heal),
            ("全面体检实例 (doctor)", do_doctor),
            ("全量导出备份 (export)", do_export),
            ("升级框架托管模板 (upgrade)", do_upgrade),
        ],
    )


def run_config_menu(instance: str | None = None, io: MenuIO | None = None) -> int:
    """运行交互式 `hushai config` 1-N 设置与控制菜单。"""
    cio = io or MenuIO()
    try:
        current = _select_initial_instance(cio, instance)
        if not current:
            return 0

        while True:
            cfg = load_instance_config(current)
            brain = load_brain_config(cfg)
            ad_names = ",".join(sorted(cfg.adapters)) or "未配置"
            cio.write("\n==================================================")
            cio.write(f"  hushAi-agent 设置与控制中心  [实例: {cfg.name}]")
            cio.write(f"  记忆库: {cfg.vault}")
            cio.write(
                f"  时区: {cfg.timezone} | 语言: {cfg.lang} | 适配器: {ad_names} | "
                f"第二大脑总开关: {'已开启' if brain.enabled else '已禁用'}"
            )
            cio.write("==================================================")
            cio.write("  1. 基础与记忆库设置（时区、语言、路径、仅本机目录、篇幅与窗口限制）")
            cio.write("  2. 领域与接入端管理（领域列表、管家端、已知端、审批权、交换区）")
            cio.write("  3. AI 适配器与模型调度（claude/agy/fake、回退链、派活、每日预算、自检）")
            cio.write("  4. 常驻管家管理与跨端路由（查看/切换 claude|agy/重启、@路由）")
            cio.write("  5. 多端接入与同步（rclone 同步、doorkit、systemd 单元、电脑/手机接入速查）")
            cio.write("  6. 第二大脑 (Second-Brain) 设置与操作（开关、引导、Ask、加工、Routine）")
            cio.write("  7. 日常查看与待办操作（今日待办、晨间简报、记收件、销待办、批记忆）")
            cio.write("  8. 手动触发整理与系统维护（reconcile/digest/weekly/heal/doctor/export/upgrade）")
            cio.write("  9. 切换或新建实例")
            cio.write("  0. 退出")

            choice = cio.read_line("请选择菜单编号 [0-9]: ").strip()
            if choice in ("0", "q", "exit"):
                cio.write("已退出 hushAi 设置中心。")
                return 0
            if choice == "1":
                _submenu_basic(cio, current)
            elif choice == "2":
                _submenu_doors_domains(cio, current)
            elif choice == "3":
                _submenu_adapters(cio, current)
            elif choice == "4":
                _submenu_steward(cio, current)
            elif choice == "5":
                _submenu_sync_and_doors(cio, current)
            elif choice == "6":
                _submenu_brain(cio, current)
            elif choice == "7":
                _submenu_daily_ops(cio, current)
            elif choice == "8":
                _submenu_maintenance(cio, current)
            elif choice == "9":
                picked = _select_initial_instance(cio, None)
                if picked:
                    current = picked
            else:
                cio.write("请输入 0 到 9 之间的数字。")
    except (EOFError, KeyboardInterrupt):
        cio.write("\n已退出 hushAi 设置中心。")
        return 0
