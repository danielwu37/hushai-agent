"""交互式配置菜单子页：1. 基础与记忆库设置 / 2. 领域与接入端管理。"""

from __future__ import annotations

from pathlib import Path

from hushai_agent.cli_config_io import MenuIO, _menu_loop, _prompt, _prompt_non_empty, _safe_apply
from hushai_agent.config import DEFAULT_DOMAINS_EN, DEFAULT_DOMAINS_ZH, load_instance_config
from hushai_agent.config_ext import load_ext
from hushai_agent.vault import init_instance


def _submenu_basic(io: MenuIO, inst: str) -> None:
    """1. 基础与记忆库设置子菜单。"""

    def summary() -> list[str]:
        cfg = load_instance_config(inst)
        ext = load_ext(cfg)
        return [
            f"记忆库(vault): {cfg.vault} | 时区: {cfg.timezone} | 语言: {cfg.lang}",
            f"仅本机目录(local_only): {','.join(cfg.local_only) or '（无）'} | "
            f"仅本机领域(local_only_domains): {','.join(cfg.local_only_domains) or '（无）'}",
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

    def set_local_only_domains() -> None:
        cfg = load_instance_config(inst)
        val = _prompt(io, "输入仅本机领域（逗号分隔，不进外部 AI 上下文）", ",".join(cfg.local_only_domains))
        _safe_apply(
            io,
            inst,
            {"privacy": {"local_only_domains": val}},
            f"local_only_domains 已更新为: {val or '（空）'}",
        )

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
            ("修改「仅本机」隐私领域 (local_only_domains)", set_local_only_domains),
        ],
    )


def _submenu_doors_domains(io: MenuIO, inst: str) -> None:
    """2. 领域与接入端管理子菜单。"""

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
