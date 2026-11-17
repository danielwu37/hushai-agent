"""2026-11-16 复审补漏：补跑日期、周报定时去重、权限规则写法、隔离领域待办、维护对话不进收件、brain 单元清理。"""

from __future__ import annotations

import datetime
import fnmatch
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import brain_cfg, capture, digest, digest_plan, doorkit, install, items, judge, ledger  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.config_edit import apply_config_updates  # noqa: E402
from hushai_agent.paths import get_instance_config_dir  # noqa: E402
from hushai_agent.vault import add_inbox_entry, init_instance  # noqa: E402
from m3a_stubs import run_hushai_cli  # noqa: E402


class TestAuditRecheck(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_MODEL_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_NOW"] = "2026-11-15T21:30:00"
        self.vault = self.base / "vault"
        init_instance("demo", self.vault, timezone="Asia/Shanghai", no_git=True)
        apply_config_updates("demo", {"domains": {"names": "工作,生活,健康"}})
        self.cfg = load_instance_config("demo")
        units = get_instance_config_dir("demo") / "units"
        units.mkdir(parents=True, exist_ok=True)
        install.render_units(self.cfg, units, digest_time="21:30", with_brain=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _digest_at(self, now: str) -> str:
        os.environ["HUSHAI_NOW"] = now
        as_of, for_date = digest_plan.resolve_default_digest_dates(self.cfg)
        digest.run_digest(self.cfg, as_of=as_of, for_date=for_date)
        return (self.vault / "hub" / "today.md").read_text(encoding="utf-8").splitlines()[0]

    def test_today_date_follows_digest_time(self) -> None:
        """到 digest_time 才翻到明天；凌晨、白天手动跑都写当天。"""
        add_inbox_entry("demo", "steward", "工作", "周日记录", todo="周一交报告")
        self.assertIn("2026-11-16", self._digest_at("2026-11-15T21:30:00"))
        self.assertIn("2026-11-16", self._digest_at("2026-11-16T00:05:00"))
        self.assertIn("2026-11-16", self._digest_at("2026-11-16T01:00:00"))
        self.assertIn("2026-11-16", self._digest_at("2026-11-16T10:30:00"))
        self.assertIn("2026-11-17", self._digest_at("2026-11-16T21:30:00"))

    def test_midnight_catchup_points_back_when_yesterday_missing(self) -> None:
        os.environ["HUSHAI_NOW"] = "2026-11-16T00:05:00"
        self.assertEqual(
            digest_plan.resolve_default_digest_dates(self.cfg),
            (datetime.date(2026, 11, 15), datetime.date(2026, 11, 16)),
        )

    def _weekly_reports(self) -> list[str]:
        return sorted(p.name for p in (self.vault / "hub" / "reports").glob("weekly-*.md"))

    def test_scheduled_weekly_catchup_does_not_duplicate(self) -> None:
        """周日已出，周一凌晨定时器补跑跳过；周日没出，补跑记在周日名下；手动跑不受限。"""
        os.environ["HUSHAI_NOW"] = "2026-11-15T22:15:00"
        self.assertEqual(run_hushai_cli(SRC_DIR, "weekly", "--instance", "demo", "--scheduled").returncode, 0)
        os.environ["HUSHAI_NOW"] = "2026-11-16T00:05:00"
        proc = run_hushai_cli(SRC_DIR, "weekly", "--instance", "demo", "--scheduled")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(self._weekly_reports(), ["weekly-2026-11-15.md"])
        self.assertTrue(any(r.get("job") == "weekly" and str(r.get("note")).startswith("skipped") for r in ledger.read(self.cfg)))

        self.assertEqual(run_hushai_cli(SRC_DIR, "weekly", "--instance", "demo").returncode, 0)
        self.assertEqual(self._weekly_reports(), ["weekly-2026-11-15.md", "weekly-2026-11-16.md"])

    def test_scheduled_weekly_late_run_uses_scheduled_sunday(self) -> None:
        os.environ["HUSHAI_NOW"] = "2026-11-16T00:05:00"
        self.assertEqual(run_hushai_cli(SRC_DIR, "weekly", "--instance", "demo", "--scheduled").returncode, 0)
        self.assertEqual(self._weekly_reports(), ["weekly-2026-11-15.md"])
        unit = (get_instance_config_dir("demo") / "units" / "hushai-demo-weekly.service").read_text(encoding="utf-8")
        self.assertIn("weekly --instance demo --scheduled", unit)

    def test_every_dangerous_command_hits_a_rule(self) -> None:
        """逐条危险命令必须命中 deny 或 ask；路径规则用 // 绝对写法并递归。"""

        def hit(rules: list[str], prefix: str, cmd: str) -> bool:
            pats = [r[len(prefix) + 1 : -1] for r in rules if r.startswith(prefix + "(")]
            return any(fnmatch.fnmatchcase(cmd, p) for p in pats)

        deny_cmds = [
            "rm -rf /", "rm -rf ~/x", "rm -rf hub", "rm -r hub/notes", "rm -fr hub", "find . -name x -delete",
            "git push --force origin master", "git reset --hard HEAD~1", "mkfs.ext4 /dev/sda", "dd if=/dev/zero of=/dev/sda",
            "systemctl stop hushai-wu-steward.service", "systemctl disable x", "mail -s hi a@b.c", "sendmail a@b.c", "shutdown now", "reboot",
        ]
        ask_cmds = [
            "rm hub/today.md", "git rm hub/a.md", "git push origin master", "curl https://x", "wget https://x",
            "scp a b:", "rsync a b:", "rclone copy a b:", "ssh host", "systemctl restart x",
        ]
        for cmd in deny_cmds:
            self.assertTrue(hit(doorkit.DENY_RULES, "Bash", cmd), cmd)
            self.assertTrue(hit(doorkit.AGY_DENY_RULES, "command", cmd), cmd)
        for cmd in ask_cmds:
            self.assertTrue(hit(doorkit.DENY_RULES + doorkit.ASK_RULES, "Bash", cmd), cmd)
            self.assertTrue(hit(doorkit.AGY_DENY_RULES + doorkit.AGY_ASK_RULES, "command", cmd), cmd)
        self.assertIn("WebFetch", doorkit.ASK_RULES)

        path_rules = [r for r in doorkit.DENY_RULES if r.startswith(("Write(", "Edit("))]
        self.assertEqual(len(path_rules), 8)
        for rule in path_rules:
            inner = rule[rule.index("(") + 1 : -1]
            self.assertTrue(inner.startswith("//") and inner.endswith("/**"), rule)
        for tool in ("Write", "Edit"):
            for root in ("//etc", "//root/.ssh", "//root/.config", "//root/.claude"):
                self.assertIn(f"{tool}({root}/**)", doorkit.DENY_RULES)

    def test_local_only_todo_becomes_item_without_model(self) -> None:
        """隔离领域带待办的行：不进提示词，但落成开放项、进今日待办，重跑不重复。"""
        add_inbox_entry("demo", "steward", "健康", "血压偏高", todo="复测血压")
        add_inbox_entry("demo", "steward", "健康", "今天走了八千步")
        res = digest.run_digest(self.cfg, as_of="2026-11-15")
        health = [it for it in items.load(self.cfg) if it.domain == "健康"]
        self.assertEqual([it.title for it in health], ["复测血压"])
        self.assertEqual(len(health[0].src), 1)
        self.assertIn("复测血压", (self.vault / "hub" / "today.md").read_text(encoding="utf-8"))
        self.assertIn(health[0].id, [c["id"] for c in res["created"]])
        self.assertNotIn("血压", judge.build_prompt(self.cfg, [], items.load(self.cfg), "2026-11-15"))

        add_inbox_entry("demo", "steward", "工作", "另一条")
        digest.run_digest(self.cfg, as_of="2026-11-15")
        self.assertEqual(len([it for it in items.load(self.cfg) if it.domain == "健康"]), 1)

    def test_capture_skips_maintenance_dialog(self) -> None:
        tr = self.base / "s.jsonl"
        text = "请对 King 上的 hushAi-agent 框架代码做一次全面、独立的代码审核，只读，出报告"
        tr.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n", encoding="utf-8")
        cap = capture.run_capture("demo", "steward", json.dumps({"session_id": "s1", "transcript_path": str(tr)}))
        self.assertEqual((cap["written"], cap["reason"]), (False, "system-maintenance"))
        self.assertEqual(list((self.vault / "hub" / "inbox").glob("*-auto.md")), [])
        self.assertTrue(any(r.get("job") == "capture" and str(r.get("note")).startswith("skipped") for r in ledger.read(self.cfg)))

        tr.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "下周三下午三点和代理开会"}}, ensure_ascii=False) + "\n", encoding="utf-8")
        cap2 = capture.run_capture("demo", "steward", json.dumps({"session_id": "s2", "transcript_path": str(tr)}))
        self.assertTrue(cap2["written"])

    def test_brain_disable_removes_generated_units_including_legacy(self) -> None:
        units = get_instance_config_dir("demo") / "units"
        self.assertTrue((units / "hushai-demo-brain.timer").is_file())
        (units / "hushai-demo-brain-daily.timer").write_text("legacy", encoding="utf-8")
        brain_cfg.set_enabled(self.cfg, True)
        proc = run_hushai_cli(SRC_DIR, "brain", "disable", "--instance", "demo", "--json")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(len(json.loads(proc.stdout)["units_removed"]), 3)
        left = sorted(p.name for p in units.iterdir())
        self.assertFalse([n for n in left if "brain" in n])
        self.assertIn("hushai-demo-digest.timer", left)
        self.assertFalse(brain_cfg.load_brain_config(load_instance_config("demo")).enabled)

if __name__ == "__main__":
    unittest.main()
