"""2026-11-16 代码审核（B-1~L-10 & 2.1）全量回归测试。"""

from __future__ import annotations

import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import (  # noqa: E402
    alerts,
    brain_cfg,
    brain_raw,
    brief,
    brief_html,
    capture,
    clock,
    collect,
    digest,
    digest_plan,
    doctor,
    doorkit,
    gate,
    heal_checks,
    heal_checks_ext,
    install,
    items,
    ledger,
    lock,
    on_event,
    reconcile,
    runner,
    steward,
    sync,
    validate,
)
from hushai_agent.cli_config import MenuIO, run_config_menu  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.config_edit import apply_config_updates  # noqa: E402
from hushai_agent.paths import get_instance_config_file  # noqa: E402
from hushai_agent.vault import add_inbox_entry, init_instance  # noqa: E402
from m3a_stubs import run_hushai_cli  # noqa: E402


class TestAuditFixes(unittest.TestCase):
    """覆盖 B-1、B-2、H-1~H-4、M-1~M-8、L-1~L-10 与 2.1 整改项。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_MODEL_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_NOW"] = "2026-11-16T10:00:00"
        self.vault = self.base / "vault"
        init_instance("demo", self.vault, timezone="Asia/Shanghai", no_git=True)
        self.cfg = load_instance_config("demo")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _enable_real_stub_adapter(self, call_log: Path) -> None:
        stub = self.base / "claude_stub.py"
        stub.write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(call_log)!r}).write_text('called\\n', encoding='utf-8')\n"
            "print('{\"items\":[],\"status_rows\":[],\"candidates\":[],\"summary\":{}}')\n",
            encoding="utf-8",
        )
        apply_config_updates(
            "demo",
            {
                "adapters": {
                    "claude.type": "claude",
                    "claude.command": f"{sys.executable} {stub}",
                    "claude.light": "sonnet",
                    "claude.heavy": "opus",
                    "claude.timeout": "15",
                },
                "jobs": {"digest": "claude:light", "weekly": "claude:heavy", "dispatch": "claude"},
                "budget": {"max_daily_calls": "10"},
            },
        )

    def test_b1_digest_dates_for_date_and_weekly_cycle_dedup(self) -> None:
        """B-1：跨午夜默认回指昨日、catchup 按 digest_time 对齐、周报同 ISO 周不跨周重跑、--for-date CLI。"""
        # 1) 凌晨 01:30 且昨日日报缺失 -> 默认补昨日、目标为今日
        os.environ["HUSHAI_NOW"] = "2026-11-16T01:30:00"
        as_of_1, for_1 = digest_plan.resolve_default_digest_dates(self.cfg)
        self.assertEqual((as_of_1, for_1), (datetime.date(2026, 11, 15), datetime.date(2026, 11, 16)))

        # 昨日日报已有 -> 基准日回落为今日；还没到 digest_time，今日待办仍写当天
        (self.vault / "hub" / "reports" / "daily-2026-11-15.md").write_text("# 日报\n", encoding="utf-8")
        as_of_2, for_2 = digest_plan.resolve_default_digest_dates(self.cfg)
        self.assertEqual((as_of_2, for_2), (datetime.date(2026, 11, 16), datetime.date(2026, 11, 16)))

        # 2) catchup 日期判断
        units_dir = self.base / "units"
        units_dir.mkdir()
        install.render_units(self.cfg, units_dir, digest_time="22:00")
        early_dt = datetime.datetime(2026, 11, 16, 10, 0, tzinfo=clock.now(self.cfg).tzinfo)
        late_dt = datetime.datetime(2026, 11, 16, 22, 30, tzinfo=clock.now(self.cfg).tzinfo)
        self.assertEqual(heal_checks_ext.digest_catchup_dates(self.cfg, early_dt), (datetime.date(2026, 11, 15), datetime.date(2026, 11, 16)))
        self.assertEqual(heal_checks_ext.digest_catchup_dates(self.cfg, late_dt), (datetime.date(2026, 11, 16), datetime.date(2026, 11, 17)))

        # 3) 周中手动跑了周报（周三 2026-11-11）-> 本 ISO 周后续检查不再重复补跑
        (self.vault / "hub" / "reports" / "weekly-2026-11-11.md").write_text("# 周报\n", encoding="utf-8")
        sched_sun = datetime.date(2026, 11, 15)
        self.assertTrue(heal_checks_ext.weekly_ran_in_cycle(self.cfg, sched_sun, [], datetime.date(2026, 11, 16)))

        # 4) --for-date CLI
        os.environ["HUSHAI_NOW"] = "2026-11-16T08:00:00"
        proc = run_hushai_cli(SRC_DIR, "digest", "--instance", "demo", "--date", "2026-11-15", "--for-date", "2026-11-16", "--json")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["date"], "2026-11-15")
        self.assertEqual(data["for_date"], "2026-11-16")

    def test_m4_dry_run_skips_real_model_unless_with_model(self) -> None:
        """M-4：非 fake 适配器在 --dry-run 时绝不调真实模型；传 --with-model 时才放行。"""
        call_log = self.base / "stub_called.txt"
        self._enable_real_stub_adapter(call_log)
        cfg = load_instance_config("demo")
        self.assertFalse(digest_plan.should_call_model_in_run(cfg, "digest", dry_run=True, with_model=False))
        self.assertTrue(digest_plan.should_call_model_in_run(cfg, "digest", dry_run=True, with_model=True))

        add_inbox_entry("demo", "steward", "工作", "演练不调真实模型测试")
        res_dry = digest.run_digest(cfg, "2026-11-16", dry_run=True)
        self.assertFalse(call_log.exists())
        self.assertTrue(res_dry["dry_run"])

    def test_m1_model_lock_serializes_concurrent_model_calls(self) -> None:
        """M-1：runner 与 dispatch 共享 <state>/model.lock 避免并发挤爆模型。"""
        self.assertEqual(lock.model_wait_seconds(), 0.0)
        cfg = self.cfg
        with lock.model_lock(cfg, wait_seconds=0.0):
            with self.assertRaises(lock.LockBusy):
                with lock.model_lock(cfg, wait_seconds=0.0):
                    pass

        call_log = self.base / "lock_stub.txt"
        self._enable_real_stub_adapter(call_log)
        cfg2 = load_instance_config("demo")
        with lock.model_lock(cfg2, wait_seconds=0.0):
            data, meta = runner.run_model(cfg2, "digest", "hi", None, None, lambda d: (d, None))
        self.assertIsNone(data)
        self.assertEqual(meta["degraded"], "lock-busy")
        self.assertFalse(call_log.exists())

    def test_h1_local_only_domains_isolated_everywhere(self) -> None:
        """H-1：[privacy] local_only_domains=健康,health 的内容在 collect / brain 均被隔离。"""
        self.assertEqual(self.cfg.local_only_domains, ("health", "健康"))
        add_inbox_entry("demo", "steward", "健康", "血压 135/88 需要复查", todo="复测血压")
        add_inbox_entry("demo", "steward", "工作", "推进架构评审会")

        ai_recs = collect.collect(self.cfg, "2026-11-16", include_local_domains=False)
        self.assertEqual([r.domain for r in ai_recs], ["工作"])
        all_recs = collect.collect(self.cfg, "2026-11-16", include_local_domains=True)
        self.assertEqual({r.domain for r in all_recs}, {"健康", "工作"})

        add_inbox_entry("demo", "steward", "健康", "笔记：体检复查结果待看")
        add_inbox_entry("demo", "steward", "工作", "笔记：架构评审结论")
        brain_cfg.set_enabled(self.cfg, True, since="2026-11-16")
        lines = brain_raw.pending_lines(load_instance_config("demo"), brain_cfg.default_state(), "2026-11-16")
        self.assertEqual([r.domain for r in lines], ["工作"])

    def test_h2_doorkit_permissions_for_claude_and_agy(self) -> None:
        """H-2：claude-settings.json 与 agy-settings.json 均生成 permissions deny + ask。"""
        kit = self.base / "kit"
        kit.mkdir()
        doorkit.render_doorkit(self.cfg, kit, adapter="all", door="steward")
        claude_s = json.loads((kit / "claude-settings.json").read_text(encoding="utf-8"))
        agy_s = json.loads((kit / "agy-settings.json").read_text(encoding="utf-8"))
        self.assertIn("Bash(rm -rf /*)", claude_s["permissions"]["deny"])
        self.assertIn("Bash(find * -delete*)", claude_s["permissions"]["deny"])
        self.assertIn("Bash(systemctl stop*)", claude_s["permissions"]["deny"])
        self.assertIn("Bash(curl *)", claude_s["permissions"]["ask"])
        self.assertIn("Bash(rclone *)", claude_s["permissions"]["ask"])
        self.assertIn("WebFetch", claude_s["permissions"]["ask"])
        self.assertIn("command(rm -rf /*)", agy_s["permissions"]["deny"])
        self.assertIn("command(find * -delete*)", agy_s["permissions"]["deny"])
        self.assertIn("write_file(/etc/)", agy_s["permissions"]["deny"])
        self.assertIn("edit_file(/etc/)", agy_s["permissions"]["deny"])
        self.assertIn("command(curl *)", agy_s["permissions"]["ask"])
        self.assertIn("command(rclone *)", agy_s["permissions"]["ask"])

    def test_h3_menu8_maintenance_smoke(self) -> None:
        """H-3：hushai config 菜单 8 手动触发 reconcile / digest / weekly / heal / doctor 不报 TypeError。"""
        add_inbox_entry("demo", "steward", "工作", "菜单 8 冒烟验证记录")
        out_lines: list[str] = []
        seq = iter(["8", "1", "2", "y", "3", "y", "4", "5", "0", "0"])
        rc = run_config_menu(
            instance="demo",
            io=MenuIO(read_line=lambda p: (out_lines.append(p) or next(seq)), write=out_lines.append),
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(out_lines)
        self.assertNotIn("✗ 操作失败", joined)
        self.assertIn("✓ 机械对账完成", joined)
        self.assertIn("✓ 每日汇总完成", joined)
        self.assertIn("✓ 每周回顾完成", joined)

    def test_h4_and_21_alerts_brief_capture_and_doctor(self) -> None:
        """H-4 & 2.1：alerts.jsonl 写入、brief 渲染系统通知、capture ASCII 边界、doctor 检出过期收件与未处理告警。"""
        alerts.record_alert(self.cfg, kind="heal_needs_human", level="needs_human", message="watchdog 离线", suggestion="检查单元", source="heal:watch")
        data = brief.build_brief(self.cfg)
        self.assertEqual(len(data["alerts"]), 1)
        self.assertIn("## 系统通知", brief.render_md(data))
        self.assertIn("系统通知", brief_html.render_html(data, live=False))

        # doctor WARN recent-alerts + inbox-expired
        old_inbox = self.vault / "hub" / "inbox" / "2026-10-01-steward.md"
        old_inbox.write_text("# 2026-10-01 steward\n- 10:00｜工作｜超窗口未消化事项｜产物：无｜待办：无\n", encoding="utf-8")
        rep = doctor.run_doctor("demo")
        by_id = {c["id"]: c for c in rep["checks"]}
        self.assertEqual(by_id["system-alerts"]["level"], "WARN")
        self.assertEqual(by_id["inbox-expired"]["level"], "WARN")

        # capture: AI 不误匹配 MAIL；- 系统：静默忽略
        apply_config_updates("demo", {"domains": {"names": "工作,家庭,生活,学习,健康,AI"}})
        tr = self.base / "sess.jsonl"
        tr.write_text('{"type":"user","message":{"role":"user","content":"Check MAIL waitlist status"}}\n', encoding="utf-8")
        cap = capture.run_capture("demo", "steward", json.dumps({"session_id": "s-ascii", "transcript_path": str(tr)}))
        self.assertEqual(cap["domain"], "生活")

        tr_sys = self.base / "sys.jsonl"
        tr_sys.write_text('{"type":"user","message":{"role":"user","content":"- 系统：测试告警"}}\n', encoding="utf-8")
        cap_sys = capture.run_capture("demo", "steward", json.dumps({"session_id": "s-sys", "transcript_path": str(tr_sys)}))
        self.assertFalse(cap_sys["written"])
        self.assertEqual(cap_sys["reason"], "system-noise")

    def test_m2_m6_l5_systemd_templates_and_tmux_socket(self) -> None:
        """M-2 / M-6 / L-5：heal.timer=*:50、sync.timer=*:00,15,45、TZ 注入、tmux -L hushai-<inst> 隔离。"""
        apply_config_updates("demo", {"sync": {"remote": "onedrive:demo"}})
        cfg = load_instance_config("demo")
        u = self.base / "u"
        u.mkdir()
        install.render_units(cfg, u, digest_time="23:10", with_brain=True)
        self.assertIn("OnCalendar=*-*-* *:50:00 Asia/Shanghai", (u / "hushai-demo-heal.timer").read_text(encoding="utf-8"))
        sync_tmr = (u / "hushai-demo-sync.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* *:00,15,45:00 Asia/Shanghai", sync_tmr)
        self.assertIn("RandomizedDelaySec=30", sync_tmr)
        self.assertIn("TimeoutStartSec=15min", (u / "hushai-demo-sync.service").read_text(encoding="utf-8"))
        self.assertIn("Environment=TZ=Asia/Shanghai", (u / "hushai-demo-digest.service").read_text(encoding="utf-8"))
        self.assertIn("-L hushai-demo", (u / "hushai-demo-steward.service").read_text(encoding="utf-8"))

        calls: list[list[str]] = []
        steward.status(cfg, hooks={"cmd_runner": lambda argv, timeout=15: (calls.append(argv) or (0, "active"))})
        tmux_calls = [c for c in calls if c and c[0] == "tmux"]
        self.assertTrue(tmux_calls and tmux_calls[0][:3] == ["tmux", "-L", "hushai-demo"])

    def test_m3_m8_sync_pull_exclusions_and_on_event_budget(self) -> None:
        """M-3 / M-8：sync pull 排除服务端端文件并带 --update；on_event 超时留脏路由标志。"""
        pull_argv = sync._build_pull_argv(self.cfg, ["rclone"], "remote:hub", dry_run=False)
        self.assertIn("--update", pull_argv)
        joined = " ".join(pull_argv)
        for door in ("steward", "web", "muse", "heal"):
            self.assertIn(f"/inbox/*-{door}.md", joined)
        self.assertIn("/domains/**", joined)

        fx = self.base / "fx"
        fx.mkdir()
        apply_config_updates(
            "demo",
            {
                "adapters": {"fake.type": "fake", "fake.fixtures": str(fx)},
                "doors": {"known": "steward,laptop,web"},
                "routing": {"enabled": "true", "trusted_doors": "laptop", "steward": "fake:light"},
            },
        )
        add_inbox_entry("demo", "laptop", "工作", "请求管家", todo="@steward 请查看")
        ev = on_event.handle(load_instance_config("demo"), budget_seconds=0.0)
        self.assertEqual(ev["routing"], {"skipped": "budget-exhausted", "dirty_marked": True})
        self.assertEqual(ev["outbox"], {"skipped": "budget-exhausted"})

    def test_m5_l2_gate_decoupled_put_non_overwrite_and_append_inbox(self) -> None:
        """M-5 & L-2：核心零个人路径耦合；put 禁止回落 legacy 且不覆盖同名文件；append-inbox 触发机械对账。"""
        apply_config_updates("demo", {"doors": {"known": "steward,laptop,web,muse"}})
        cfg = load_instance_config("demo")
        (self.vault / "hub" / "open-items.md").write_text(
            "# 开放项\n- [ ] 261116-1｜核对清单｜owner=user｜sched=-｜due=-｜domain=生活｜verified=2026-11-16｜src=-\n",
            encoding="utf-8",
        )
        # 未配置 [gate] 映射时：00-我 不应映射到 hub，spotify_config.json 也不应内置拦截
        with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "read 00-我/今日待办.md"}):
            _, rc_legacy = gate.run(cfg, "muse", "ssh", {}, io.BytesIO(b""))
        self.assertEqual(rc_legacy, 2)

        with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "put hub/exchange/in/note.md"}):
            out1, rc1 = gate.run(cfg, "muse", "ssh", {}, io.BytesIO(b"first\n"))
        with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "put hub/exchange/in/note.md"}):
            out2, rc2 = gate.run(cfg, "muse", "ssh", {}, io.BytesIO(b"second\n"))
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertIn("note-1.md", out2["stdout"])
        self.assertEqual((self.vault / "hub" / "exchange" / "in" / "note.md").read_bytes(), b"first\n")
        self.assertEqual((self.vault / "hub" / "exchange" / "in" / "note-1.md").read_bytes(), b"second\n")

        with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "append-inbox 2026-11-16"}):
            _, rc_ap = gate.run(cfg, "muse", "ssh", {}, io.BytesIO("- 10:00｜生活｜完成 261116-1：已核对｜产物：无｜待办：无\n".encode("utf-8")))
        self.assertEqual(rc_ap, 0)
        self.assertEqual(items.load(cfg)[0].status, "done")

    def test_l1_and_l6_module_line_limits_and_conflict_filename(self) -> None:
        """L-1 & L-6：全部 src 模块不超过 300 行；正常含 '冲突' 标题文件不被误判为同步冲突副本。"""
        pkg_dir = SRC_DIR / "hushai_agent"
        oversized = [(len(p.read_text(encoding="utf-8").splitlines()), p.name) for p in pkg_dir.glob("*.py")]
        self.assertEqual([(n, f) for n, f in oversized if n > 300], [])

        self.assertFalse(validate.is_sync_conflict_filename("记忆冲突裁决规则.md"))
        self.assertFalse(validate.is_sync_conflict_filename("notes/人际冲突复盘.md"))
        self.assertTrue(validate.is_sync_conflict_filename("汇报_冲突版.docx"))
        self.assertTrue(validate.is_sync_conflict_filename("notes (冲突).md"))
        self.assertTrue(validate.is_sync_conflict_filename("today.sync-conflict-20261116-120000-PC.md"))


if __name__ == "__main__":
    unittest.main()
