"""交互式配置菜单子页：7. 日常查看与待办操作 / 8. 手动触发整理与系统维护。"""

from __future__ import annotations

from pathlib import Path

from hushai_agent import brief, candidates, clock, digest, doctor, heal, lifecycle, weekly
from hushai_agent.cli_config_io import MenuIO, _menu_loop, _prompt, _prompt_non_empty
from hushai_agent.config import load_instance_config
from hushai_agent.digest_plan import resolve_default_digest_dates
from hushai_agent.paths import get_instance_config_file
from hushai_agent.todo import get_today_status, run_todo_action
from hushai_agent.vault import add_inbox_entry


def _submenu_daily_ops(io: MenuIO, inst: str) -> None:
    """7. 日常查看与待办操作子菜单。"""

    def summary() -> list[str]:
        pending_cands = candidates.pending(candidates.load(load_instance_config(inst)))
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


def _submenu_maintenance(io: MenuIO, inst: str) -> None:
    """8. 手动触发整理与系统维护子菜单。"""

    def summary() -> list[str]:
        return ["说明：所有写操作均采用原子写，修改前自动持锁保护。"]

    def do_reconcile() -> None:
        res = digest.run_mechanical_only(load_instance_config(inst))
        io.write(f"✓ 机械对账完成：处理 {res.get('processed_lines', 0)} 行，应用 {res.get('mechanical_applied', 0)} 个动作")

    def do_digest() -> None:
        cfg = load_instance_config(inst)
        dry = io.read_line("是否演练模式 (--dry-run)？[y/N]: ").strip().lower() in ("y", "yes", "1")
        as_of, for_date = resolve_default_digest_dates(cfg)
        res = digest.run_digest(cfg, as_of, dry_run=dry, for_date=for_date)
        io.write(f"✓ 每日汇总完成 ({res['date']})：新增收件 {res['new_lines']} 行，{res['for_date']} 待办 {res['today_count']} 条，降级={res.get('degraded') or '否'}")

    def do_weekly() -> None:
        cfg = load_instance_config(inst)
        dry = io.read_line("是否演练模式 (--dry-run)？[y/N]: ").strip().lower() in ("y", "yes", "1")
        as_of = clock.today(cfg)
        res = weekly.run_weekly(cfg, as_of, dry_run=dry)
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

    def do_uninstall() -> None:
        lifecycle.interactive_uninstall(io.read_line, io.write, current_instance=inst)

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
            ("一键卸载当前实例或彻底重置环境 (uninstall)", do_uninstall),
        ],
        stop_when=lambda: not get_instance_config_file(inst).is_file(),
    )
