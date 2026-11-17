"""M3e 单元测试：hushai heal（静默自愈、探针超时、上报去重、白名单、adapter-skip）、capture 与决定/教训。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import alerts, capture, digest, heal, heal_checks, ledger, runner  # noqa: E402
from hushai_agent.adapters import proc  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.validate import validate_vault  # noqa: E402
from hushai_agent.vault import add_inbox_entry, init_instance  # noqa: E402
from m3a_stubs import run_hushai_cli  # noqa: E402


class TestM3e(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:00:00"
        self.vault = self.base / "vault"
        init_instance("demo", self.vault, no_git=True)
        self.cfg_file = self.base / "config" / "demo" / "config.ini"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def cfg(self):
        return load_instance_config("demo")

    def test_01_all_normal_silent(self) -> None:
        """1. 全正常：只写账本一条 job=heal, result=ok，不写收件、不改 today.md。"""
        today_file = self.vault / "hub" / "today.md"
        orig_today = today_file.read_text(encoding="utf-8")
        res = heal.run_heal(self.cfg(), hooks={"installed_units": set()})
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"], "ok")
        self.assertEqual(res["healed_inbox_files"], [])
        self.assertEqual(res["alerts_written"], [])
        self.assertEqual(today_file.read_text(encoding="utf-8"), orig_today)
        inbox_files = list((self.vault / "hub" / "inbox").glob("*-heal.md"))
        self.assertEqual(inbox_files, [])
        rows = ledger.read(self.cfg())
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["job"], rows[0]["result"]), ("job", "heal", "ok"))

    def test_02_vault_probe_subprocess_timeout(self) -> None:
        """2. vault 探针子进程超时（用 sleep 模拟卡住）：在超时内判定失败，不把主进程挂死。"""
        orig_run = proc.run

        def _slow_probe_run(argv, stdin_text, cwd, timeout):
            # 将子进程改为 sleep 5，并将超时缩为 1 秒以快速验证子进程超时杀灭机制
            if len(argv) >= 3 and argv[1] == "-c" and ".heal-probe" in str(argv[-1]):
                return orig_run([sys.executable, "-c", "import time; time.sleep(5)"], None, cwd, 1)
            return orig_run(argv, stdin_text, cwd, timeout)

        t0 = time.monotonic()
        with mock.patch("hushai_agent.heal_checks.proc.run", side_effect=_slow_probe_run):
            ok, detail = heal_checks._probe_vault_subprocess(self.vault, {})
        elapsed = time.monotonic() - t0
        self.assertFalse(ok)
        self.assertLess(elapsed, 4.0)
        self.assertTrue("超时" in detail or "失败" in detail or "timeout" in detail.lower())

    def test_03_self_healed_writes_inbox_not_today(self) -> None:
        """3. 已自愈：写账本 + <state>/alerts.jsonl 一条，不写 -heal.md 也不改 today.md。"""
        today_file = self.vault / "hub" / "today.md"
        orig_today = today_file.read_text(encoding="utf-8")
        watch_state = {"active": False}

        def _fake_cmd(argv: list[str], timeout: int = 15) -> tuple[int, str]:
            if argv[:2] == ["systemctl", "is-active"]:
                return (0, "active") if watch_state["active"] else (3, "inactive")
            if argv[:2] == ["systemctl", "start"]:
                watch_state["active"] = True
                return (0, "")
            return (0, "")

        res = heal.run_heal(
            self.cfg(),
            hooks={"installed_units": {"hushai-demo-watch.path"}, "cmd_runner": _fake_cmd},
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"], "ok")
        self.assertEqual(today_file.read_text(encoding="utf-8"), orig_today)
        self.assertFalse((self.vault / "hub" / "inbox" / "2026-03-02-heal.md").exists())
        recent = alerts.load_recent_alerts(self.cfg(), limit=20)
        self.assertTrue(any("自愈：" in a.get("message", "") and "hushai-demo-watch.path" in a.get("message", "") for a in recent))
        self.assertTrue(validate_vault(self.vault)["ok"])

    def test_04_failed_twice_reports_to_today_with_24h_dedup(self) -> None:
        """4. 2 次仍失败上报：写 alerts.jsonl（不污染 today.md）；24 小时内再跑不重复写；账本记 needs_human。"""
        calls: list[list[str]] = []

        def _always_inactive(argv: list[str], timeout: int = 15) -> tuple[int, str]:
            calls.append(argv)
            if argv[:2] == ["systemctl", "is-active"]:
                return (3, "inactive")
            return (0, "")

        hooks = {"installed_units": {"hushai-demo-watch.path"}, "cmd_runner": _always_inactive}
        res1 = heal.run_heal(self.cfg(), hooks=hooks)
        self.assertFalse(res1["ok"])
        self.assertEqual(res1["result"], "needs_human")
        self.assertEqual(len(res1["alerts_written"]), 1)

        today_text1 = (self.vault / "hub" / "today.md").read_text(encoding="utf-8")
        self.assertEqual(today_text1.count("- 系统："), 0)
        recent1 = alerts.load_recent_alerts(self.cfg(), limit=20)
        self.assertEqual(sum(1 for a in recent1 if "坏了，建议" in a.get("message", "")), 1)

        # 2 小时后再跑：同一问题在 24 小时内不重复写入
        os.environ["HUSHAI_NOW"] = "2026-03-02T12:00:00"
        res2 = heal.run_heal(self.cfg(), hooks=hooks)
        self.assertEqual(res2["result"], "needs_human")
        self.assertEqual(res2["alerts_written"], [])
        recent2 = alerts.load_recent_alerts(self.cfg(), limit=20)
        self.assertEqual(sum(1 for a in recent2 if "坏了，建议" in a.get("message", "")), 1)

        heal_jobs = [r for r in ledger.read(self.cfg()) if r.get("job") == "heal"]
        self.assertEqual([r["result"] for r in heal_jobs], ["needs_human", "needs_human"])

    def test_05_whitelist_blocks_unauthorized_units(self) -> None:
        """5. 白名单拦截：传入非 hushai-<实例>-*、非 rclone-* 的单元名（如 nginx.service、legacy-digest.service）被拒绝操作。"""
        cfg = self.cfg()
        self.assertTrue(heal.is_whitelisted_unit("demo", "hushai-demo-watch.path"))
        self.assertTrue(heal.is_whitelisted_unit("demo", "hushai-demo-digest.service"))
        self.assertTrue(heal.is_whitelisted_unit("demo", "rclone-hub.service"))
        self.assertTrue(heal.is_whitelisted_unit("demo", "mnt-onedrive-00.mount"))

        for bad_unit in (
            "nginx.service",
            "legacy-digest.service",
            "hushai-other-watch.path",
            "ssh.service",
            "../evil.service",
        ):
            self.assertFalse(heal.is_whitelisted_unit("demo", bad_unit), msg=bad_unit)
            ok, msg = heal.operate_systemd_unit(cfg, "restart", bad_unit)
            self.assertFalse(ok)
            self.assertIn("拒绝操作", msg)

    def test_06_adapter_skip_and_auto_recovery(self) -> None:
        """6. adapter-skip.json：首家连续 3 次失败后，heal 写入跳过文件；runner 在有效期内直接从第二家开始；过期后恢复从首家开始。"""
        fixtures = self.base / "fixtures"
        fixtures.mkdir()
        (fixtures / "digest-2026-03-02.json").write_text(
            json.dumps({"items": [], "status_rows": [], "memory_candidates": []}), encoding="utf-8"
        )
        cfg_text = self.cfg_file.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace(
            "[adapters]",
            f"[adapters]\nfirst.type = fake\nfirst.fixtures = {fixtures}\nsecond.type = fake\nsecond.fixtures = {fixtures}",
        )
        cfg_text = cfg_text.replace("digest =", "digest = first:light, second:light")
        self.cfg_file.write_text(cfg_text, encoding="utf-8")
        cfg = self.cfg()

        # 记录 first 连续 3 次失败
        for i in range(3):
            os.environ["HUSHAI_NOW"] = f"2026-03-02T09:0{i}:00"
            ledger.record(cfg, "call", job="digest", adapter="first", tier="light", model="m1", ok=False, error="quota")

        os.environ["HUSHAI_NOW"] = "2026-03-02T10:00:00"
        res = heal.run_heal(cfg, before="digest", hooks={"installed_units": set()})
        self.assertTrue(res["ok"])
        model_chk = {c["id"]: c for c in res["checks"]}["model_available"]
        self.assertEqual(model_chk["status"], "healed")

        skip_file = self.base / "state" / "demo" / "adapter-skip.json"
        self.assertTrue(skip_file.is_file())

        # 在 6 小时有效期内（12:00），runner 直接跳过 first 从 second 开始
        os.environ["HUSHAI_NOW"] = "2026-03-02T12:00:00"
        data, info = runner.run_model(
            cfg, "digest", "prompt", None, {"job": "digest", "date": "2026-03-02"}, lambda d: (d, None)
        )
        self.assertIsNotNone(data)
        self.assertEqual(info["adapter"], "second")
        self.assertEqual(info["calls"], 1)

        # 超过 6 小时后（16:05），自动恢复从 first 开始
        os.environ["HUSHAI_NOW"] = "2026-03-02T16:05:00"
        data2, info2 = runner.run_model(
            cfg, "digest", "prompt", None, {"job": "digest", "date": "2026-03-02"}, lambda d: (d, None)
        )
        self.assertIsNotNone(data2)
        self.assertEqual(info2["adapter"], "first")
        self.assertEqual(info2["calls"], 1)

    def test_07_capture_extracts_last_human_and_throttles_10min(self) -> None:
        """7. capture：给一段假 Claude 会话 JSONL，写出 -auto.md 一行；10 分钟内再跑同 session_id 不重复写。"""
        transcript = self.base / "session.jsonl"
        long_human_msg = "请帮我核对第三章译稿的专有名词表，并确认下周五之前要提交给出版社的审校清单与封面文案" + "A" * 40
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "第一条早期人类消息"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "好的"}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": long_human_msg}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "工具输出不计入"}]}}),
        ]
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

        hook_stdin = json.dumps({"session_id": "sess-123", "transcript_path": str(transcript)})
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:00:00"
        r1 = capture.run_capture(self.cfg(), door="steward", stdin_text=hook_stdin)
        self.assertTrue(r1["written"])
        self.assertEqual(len(r1["snippet"]), min(82, capture.MAX_CHARS))  # 未配模型时退回机械采集，上限 MAX_CHARS

        auto_file = self.vault / "hub" / "inbox" / "2026-03-02-steward-auto.md"
        self.assertTrue(auto_file.is_file())
        record_lines = [ln for ln in auto_file.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
        self.assertEqual(len(record_lines), 1)

        # 5 分钟后再跑同 session_id -> 被节流拦截，不重复写
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:05:00"
        r2 = capture.run_capture(self.cfg(), door="steward", stdin_text=hook_stdin)
        self.assertFalse(r2["written"])
        self.assertEqual(r2["reason"], "throttled")
        record_lines_after = [ln for ln in auto_file.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
        self.assertEqual(len(record_lines_after), 1)

        # 11 分钟后再跑同 session_id -> 允许写入第 2 行
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:11:00"
        r3 = capture.run_capture(self.cfg(), door="steward", stdin_text=hook_stdin)
        self.assertTrue(r3["written"])
        record_lines_final = [ln for ln in auto_file.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
        self.assertEqual(len(record_lines_final), 2)

    def test_08_decisions_and_lessons_reconcile_and_validate(self) -> None:
        """8. decisions.md 与 lessons.md：机械对账识别并幂等追加；validate 通过合法文件、拒绝缺栏行。"""
        os.environ["HUSHAI_NOW"] = "2026-03-02T11:00:00"
        add_inbox_entry(
            "demo",
            "steward",
            "工作",
            "决定：周重写排在汇总后 45 分钟；理由：避免同时跑两个重任务；批准人：steward",
        )
        add_inbox_entry(
            "demo",
            "steward",
            "工作",
            "教训：挂载卡死导致早间任务失败；根因：无超时探针；防线：heal 增加 10 秒探针",
        )

        dec_file = self.vault / "hub" / "decisions.md"
        les_file = self.vault / "hub" / "lessons.md"
        dec_lines = [ln for ln in dec_file.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
        les_lines = [ln for ln in les_file.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
        self.assertEqual(len(dec_lines), 1)
        self.assertEqual(len(les_lines), 1)
        self.assertIn("2026-03-02｜周重写排在汇总后 45 分钟｜避免同时跑两个重任务｜steward｜L-20260302-steward-", dec_lines[0])
        self.assertEqual(les_lines[0], "- 2026-03-02｜挂载卡死导致早间任务失败｜无超时探针｜heal 增加 10 秒探针")

        # 再次运行机械对账或汇总，按收件行编号幂等去重，不重复追加
        digest.run_mechanical_only(self.cfg())
        self.assertEqual(len([ln for ln in dec_file.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]), 1)
        self.assertEqual(len([ln for ln in les_file.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]), 1)
        self.assertTrue(validate_vault(self.vault)["ok"])

        # 写入缺栏坏行后 validate 拒绝
        dec_file.write_text(dec_file.read_text(encoding="utf-8") + "- 2026-03-02｜只有两栏\n", encoding="utf-8")
        les_file.write_text(les_file.read_text(encoding="utf-8") + "- 2026-03-02｜只有两栏\n", encoding="utf-8")
        rep = validate_vault(self.vault)
        self.assertFalse(rep["ok"])
        levels = {c["id"]: c["level"] for c in rep["checks"]}
        self.assertEqual(levels["decisions"], "FAIL")
        self.assertEqual(levels["lessons"], "FAIL")


if __name__ == "__main__":
    unittest.main()
