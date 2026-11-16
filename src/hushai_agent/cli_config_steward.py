"""交互式配置菜单子页：3. AI 适配器与调度 / 4. 常驻管家与跨端路由。"""

from __future__ import annotations

from hushai_agent import steward
from hushai_agent.adapter_cmd import run_adapter_test
from hushai_agent.cli_config_io import MenuIO, _menu_loop, _prompt, _prompt_bool, _prompt_non_empty, _safe_apply
from hushai_agent.config import load_instance_config
from hushai_agent.config_ext import load_ext


def _submenu_adapters(io: MenuIO, inst: str) -> None:
    """3. AI 适配器与模型调度子菜单。"""

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
            updates[f"{name}.fixtures"] = _prompt_non_empty(io, "fixtures 样例目录路径", cur.get("fixtures", ""))
        else:
            cmd = _prompt(io, "可执行命令路径（留空则默认按类型名查找）", cur.get("command", atype))
            light = _prompt(io, "轻档模型 (light)", cur.get("light", "sonnet" if atype == "claude" else "gemini-3.8-flash-low"))
            heavy = _prompt(io, "重档模型 (heavy)", cur.get("heavy", "opus" if atype == "claude" else "gemini-4-argon-high"))
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


def _submenu_steward(io: MenuIO, inst: str) -> None:
    """4. 常驻管家与跨端路由子菜单。"""

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
