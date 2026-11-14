"""M3a 单元测试：claude/agy 桩适配项、adapter test、dispatch 派活与 doctor 新检查。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import adapters, digest, ledger  # noqa: E402
from hushai_agent.adapters.fake import _build_stem_index, resolve_source_line_ids  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.doctor import run_doctor  # noqa: E402
from hushai_agent.vault import init_instance  # noqa: E402
from m3a_stubs import init_with_adapters, make_stub_command, read_stub_logs, run_hushai_cli  # noqa: E402
from test_m2b import DEMO_FIXTURES_SRC, DEMO_VAULT_SRC, setup_demo_instance  # noqa: E402


class TestM3a(unittest.TestCase):
    """覆盖 M3a 第 1-6 项全部测试要求。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.cfg_root = self.base / "config"
        self.state_root = self.base / "state"
        self._orig_env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.cfg_root)
        os.environ["HUSHAI_STATE_ROOT"] = str(self.state_root)
        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp.cleanup()

    def test_01_claude_adapter_and_failures(self) -> None:
        """1. claude 桩：固定参数、标准输入、空工作目录、结果解析及三种失败。"""
        cmd_ok, log_ok = make_stub_command(self.base, "claude-ok", {
            "stdout_json": {
                "is_error": False, "result": "完成", "structured_output": {"answer": 42},
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "modelUsage": {"claude-real-model": {"inputTokens": 10}},
            }
        })
        cmd_err, _ = make_stub_command(self.base, "claude-iserr", {"stdout_json": {"is_error": True, "result": "模型拒绝"}})
        cmd_rc, _ = make_stub_command(self.base, "claude-rc", {"rc": 2, "stderr": "boom stderr", "stdout_json": {"is_error": False, "result": ""}})
        cmd_bad, _ = make_stub_command(self.base, "claude-badjson", {"raw_stdout": "not-json"})

        init_with_adapters(self.base, self.cfg_root, "demo", [
            "c_ok.type = claude", f"c_ok.command = {cmd_ok}", "c_ok.light = claude-light",
            "c_err.type = claude", f"c_err.command = {cmd_err}", "c_err.light = claude-light",
            "c_rc.type = claude", f"c_rc.command = {cmd_rc}", "c_rc.light = claude-light",
            "c_bad.type = claude", f"c_bad.command = {cmd_bad}", "c_bad.light = claude-light",
        ])
        cfg = load_instance_config("demo")
        dirty_dir = self.state_root / "demo" / "empty-cwd"
        dirty_dir.mkdir(parents=True, exist_ok=True)
        (dirty_dir / "rule.md").write_text("should be cleared", encoding="utf-8")

        res = adapters.build(cfg, "c_ok").complete("测试提示词", "light", schema={"type": "object"})
        self.assertTrue(res.ok)
        self.assertEqual(res.data, {"answer": 42})
        self.assertEqual(res.text, "完成")
        self.assertEqual(res.model, "claude-real-model")
        self.assertEqual(res.usage, {"input_tokens": 10, "output_tokens": 5})

        logs = read_stub_logs(log_ok)
        self.assertEqual(len(logs), 1)
        args = logs[0]["args"]
        self.assertIn("--safe-mode", args)
        self.assertIn("--no-session-persistence", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(logs[0]["stdin"], "测试提示词")
        self.assertEqual(logs[0]["cwd_entries"], [])
        self.assertEqual(Path(logs[0]["cwd"]).resolve(), dirty_dir.resolve())
        for bad_name in ("c_err", "c_rc", "c_bad"):
            self.assertFalse(adapters.build(cfg, bad_name).complete("p", "light").ok)

    def test_02_agy_adapter_schema_oversize_error_timeout(self) -> None:
        """2. agy 桩：-p= 末参、schema 文件存在、超大不调用、ERROR 判失败、超时判失败。"""
        cmd_ok, log_ok = make_stub_command(self.base, "agy-ok", {
            "stdout_json": {"status": "SUCCESS", "response": "ok-text", "structured_output": {"v": 1}, "usage": {"total": 7}, "model": "agy-m1"}
        })
        cmd_err, _ = make_stub_command(self.base, "agy-err", {"stdout_json": {"status": "ERROR", "error": "bad request"}})
        cmd_slow, _ = make_stub_command(self.base, "agy-slow", {"sleep": 4.5, "stdout_json": {"status": "SUCCESS", "response": "late"}})
        init_with_adapters(self.base, self.cfg_root, "demo", [
            "a_ok.type = agy", f"a_ok.command = {cmd_ok}", "a_ok.light = agy-l",
            "a_err.type = agy", f"a_err.command = {cmd_err}", "a_err.light = agy-l",
            "a_slow.type = agy", f"a_slow.command = {cmd_slow}", "a_slow.light = agy-l", "a_slow.timeout = 1",
        ])
        cfg = load_instance_config("demo")
        schema = {"type": "object", "properties": {"v": {"type": "integer"}}}
        res = adapters.build(cfg, "a_ok").complete("我的提示词", "light", schema=schema, meta={"job": "digest"})
        self.assertTrue(res.ok)
        self.assertEqual(res.data, {"v": 1})
        self.assertEqual(res.text, "ok-text")

        logs = read_stub_logs(log_ok)
        self.assertEqual(len(logs), 1)
        args = logs[0]["args"]
        self.assertTrue(args[-1].startswith("-p="))
        self.assertIn("我的提示词", args[-1])
        schema_arg = args[args.index("--json-schema") + 1]
        self.assertTrue(Path(schema_arg).is_file())
        self.assertEqual(json.loads(logs[0]["schema_content"]), schema)

        big = adapters.build(cfg, "a_ok").complete("字" * 40000, "light")
        self.assertFalse(big.ok)
        self.assertEqual(big.error, "prompt-too-large")
        self.assertEqual(len(read_stub_logs(log_ok)), 1)
        self.assertFalse(adapters.build(cfg, "a_err").complete("p", "light").ok)
        self.assertFalse(adapters.build(cfg, "a_slow").complete("p", "light").ok)

    def test_03_real_digest_and_fallback_chain(self) -> None:
        """3. 用桩跑一次真实 digest 与 fake 对齐；回退链 agy 失败 -> claude 成功，记两条 call。"""
        v_fake, cfg_fake = setup_demo_instance(self.base, "demo-fake", no_git=True)
        s_fake = digest.run_digest(cfg_fake, "2026-03-02")

        v_stub = self.base / "vault-stub"
        shutil.copytree(DEMO_VAULT_SRC, v_stub)
        init_instance("demo-stub", v_stub, no_git=True)
        raw_fix = json.loads((DEMO_FIXTURES_SRC / "digest-2026-03-02.json").read_text(encoding="utf-8"))
        resolved = resolve_source_line_ids(raw_fix, _build_stem_index(v_stub / "hub" / "inbox"))

        cmd_fail, _ = make_stub_command(self.base, "d-fail", {"rc": 1, "stdout_json": {"status": "ERROR", "error": "upstream down"}})
        cmd_ok, _ = make_stub_command(self.base, "d-ok", {
            "stdout_json": {"is_error": False, "result": "ok", "structured_output": resolved, "usage": {"input": 10}, "modelUsage": {"claude-sonnet": {}}}
        })
        cfg_file = self.cfg_root / "demo-stub" / "config.ini"
        txt = cfg_file.read_text(encoding="utf-8").replace(
            "[adapters]",
            "\n".join(["[adapters]", f"agy.command = {cmd_fail}", "agy.light = agy-l", f"claude.command = {cmd_ok}", "claude.light = claude-l"]),
        ).replace("digest =", "digest = agy:light, claude:light")
        cfg_file.write_text(txt, encoding="utf-8")

        s_stub = digest.run_digest(load_instance_config("demo-stub"), "2026-03-02")
        self.assertIsNone(s_stub["degraded"])
        self.assertEqual(s_stub["created"], s_fake["created"])
        self.assertEqual(s_stub["today_items"], s_fake["today_items"])
        self.assertEqual((v_stub / "hub" / "open-items.md").read_text(encoding="utf-8"), (v_fake / "hub" / "open-items.md").read_text(encoding="utf-8"))
        calls = [r for r in ledger.read(load_instance_config("demo-stub")) if r["kind"] == "call"]
        self.assertEqual([c["ok"] for c in calls], [False, True])

    def test_04_adapter_test_cli(self) -> None:
        """4. adapter test：成功返回 0 并记一条 call；模型名未配返回 1。"""
        cmd_ok, _ = make_stub_command(self.base, "atest", {
            "stdout_json": {"is_error": False, "result": "ok", "structured_output": {"ok": True}, "modelUsage": {"claude-l": {}}}
        })
        init_with_adapters(self.base, self.cfg_root, "demo", [
            "mycl.type = claude", f"mycl.command = {cmd_ok}", "mycl.light = claude-l",
            "nomodel.type = claude", f"nomodel.command = {cmd_ok}",
        ])
        r0 = run_hushai_cli(SRC_DIR, "adapter", "test", "mycl", "--instance", "demo", "--json")
        self.assertEqual(r0.returncode, 0, msg=r0.stderr)
        self.assertTrue(json.loads(r0.stdout)["ok"])
        self.assertEqual(ledger.calls_today(load_instance_config("demo")), 1)
        self.assertEqual(run_hushai_cli(SRC_DIR, "adapter", "test", "nomodel", "--instance", "demo").returncode, 1)

    def test_05_dispatch_task_goal_resume_budget_and_safety(self) -> None:
        """5. dispatch：--task / --goal、续跑、重名与 vault 拦截、预算 0、无 dangerously。"""
        cmd_res, log_res = make_stub_command(self.base, "disp", {
            "steps": [
                {"stdout_json": {"status": "ERROR", "error": "stream interrupted", "conversation_id": "conv-77", "model": "agy-h"}, "write_files": {"draft.txt": "part1"}},
                {"stdout_json": {"status": "SUCCESS", "response": "任务已完成并自检", "conversation_id": "conv-77", "model": "agy-h"}, "write_files": {"result.md": "# 产出\n完成\n"}},
            ]
        })
        vault, cfg_file = init_with_adapters(self.base, self.cfg_root, "demo", [
            "agy.command = " + cmd_res, "agy.heavy = agy-h",
            "claude.command = " + cmd_res, "claude.heavy = claude-h",
        ])
        (vault / "domains" / "work").mkdir(parents=True, exist_ok=True)
        (vault / "domains" / "work" / "AGENTS.md").write_text("翻译术语统一准则\n", encoding="utf-8")
        (vault / "domains" / "health").mkdir(parents=True, exist_ok=True)
        (vault / "domains" / "health" / "AGENTS.md").write_text("秘密健康记录\n", encoding="utf-8")
        cfg_file.write_text(cfg_file.read_text(encoding="utf-8").replace("dispatch =", "dispatch = agy"), encoding="utf-8")

        task_src = self.base / "brief.md"
        task_src.write_text("# 外来任务书\n请产出 result.md\n", encoding="utf-8")
        r1 = run_hushai_cli(SRC_DIR, "dispatch", "--instance", "demo", "--name", "job-1", "--task", str(task_src), "--json")
        self.assertEqual(r1.returncode, 0, msg=r1.stderr + r1.stdout)
        d1 = json.loads(r1.stdout)
        self.assertTrue(d1["ok"] and d1["resumed"])
        job1_dir = self.state_root / "demo" / "jobs" / "job-1"
        for rel in ("task.md", "out.pre.json", "out.json", "work/result.md"):
            self.assertTrue((job1_dir / rel).is_file())

        logs = read_stub_logs(log_res)
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[1]["args"][logs[1]["args"].index("--conversation") + 1], "conv-77")
        for rec in logs:
            self.assertFalse(any("dangerously" in a for a in rec["args"]))

        rows = ledger.read(load_instance_config("demo"))
        self.assertEqual(sum(1 for r in rows if r["kind"] == "call"), 2)
        self.assertEqual(sum(1 for r in rows if r["kind"] == "job"), 1)

        self.assertEqual(run_hushai_cli(SRC_DIR, "dispatch", "--instance", "demo", "--name", "job-1", "--task", str(task_src)).returncode, 1)
        self.assertEqual(run_hushai_cli(SRC_DIR, "dispatch", "--instance", "demo", "--name", "job-v", "--task", str(task_src), "--workdir", str(vault / "hub")).returncode, 1)
        self.assertEqual(run_hushai_cli(SRC_DIR, "dispatch", "--instance", "demo", "--name", "job-c", "--task", str(task_src), "--adapter", "claude").returncode, 1)

        self.assertEqual(run_hushai_cli(SRC_DIR, "dispatch", "--instance", "demo", "--name", "goal-w", "--goal", "整理本周进度", "--domain", "work").returncode, 0)
        gw_txt = (self.state_root / "demo" / "jobs" / "goal-w" / "task.md").read_text(encoding="utf-8")
        for sec in ("【目标】", "【背景】", "【边界】", "【产出】", "【自检】", "整理本周进度", "翻译术语统一准则"):
            self.assertIn(sec, gw_txt)

        self.assertEqual(run_hushai_cli(SRC_DIR, "dispatch", "--instance", "demo", "--name", "goal-h", "--goal", "整理健康笔记", "--domain", "health").returncode, 0)
        gh_txt = (self.state_root / "demo" / "jobs" / "goal-h" / "task.md").read_text(encoding="utf-8")
        self.assertNotIn("秘密健康记录", gh_txt)
        self.assertIn("## 【背景】\n\n无", gh_txt)

        before_cnt = len(read_stub_logs(log_res))
        cfg_file.write_text(cfg_file.read_text(encoding="utf-8").replace("max_daily_calls = 20", "max_daily_calls = 0"), encoding="utf-8")
        self.assertEqual(run_hushai_cli(SRC_DIR, "dispatch", "--instance", "demo", "--name", "job-b0", "--goal", "测试预算").returncode, 1)
        self.assertEqual(len(read_stub_logs(log_res)), before_cnt)

    def test_06_doctor_ledger_recent_and_inbox_backlog(self) -> None:
        """6. doctor 两项新检查 ledger-recent 与 inbox-backlog 的 OK 与 WARN。"""
        vault, _ = init_with_adapters(self.base, self.cfg_root, "demo", [])
        cfg = load_instance_config("demo")
        self.assertEqual({c["id"]: c["level"] for c in run_doctor("demo")["checks"]}["ledger-recent"], "OK")

        os.environ["HUSHAI_NOW"] = "2026-03-01T10:00:00"
        ledger.record(cfg, "call", job="digest", adapter="a", tier="light", model="m", ok=True)
        os.environ["HUSHAI_NOW"] = "2026-03-03T10:00:00"
        self.assertEqual({c["id"]: c["level"] for c in run_doctor("demo")["checks"]}["ledger-recent"], "OK")

        os.environ["HUSHAI_NOW"] = "2026-03-05T10:00:00"
        self.assertEqual({c["id"]: c["level"] for c in run_doctor("demo")["checks"]}["ledger-recent"], "WARN")

        os.environ["HUSHAI_NOW"] = "2026-03-04T22:00:00"
        ledger.record(cfg, "job", job="digest", date="2026-03-04", dry_run=False, result="ok")
        os.environ["HUSHAI_NOW"] = "2026-03-05T10:00:00"
        self.assertEqual({c["id"]: c["level"] for c in run_doctor("demo")["checks"]}["ledger-recent"], "OK")

        (vault / "hub" / "inbox" / "2026-03-03-laptop.md").write_text("- 09:00｜工作｜两天前记录｜产物：无｜待办：无\n", encoding="utf-8")
        self.assertEqual({c["id"]: c["level"] for c in run_doctor("demo")["checks"]}["inbox-backlog"], "OK")
        (vault / "hub" / "inbox" / "2026-03-02-laptop.md").write_text(
            "- 09:00｜工作｜未处理积压一｜产物：无｜待办：无\n- 10:00｜工作｜未处理积压二｜产物：无｜待办：无\n", encoding="utf-8",
        )
        chk = {c["id"]: c for c in run_doctor("demo")["checks"]}["inbox-backlog"]
        self.assertEqual(chk["level"], "WARN")
        self.assertIn("2", chk["message"])


if __name__ == "__main__":
    unittest.main()
