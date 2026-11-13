"""M2a 每日闭环底座单元测试（第 1-6 项）。"""

from __future__ import annotations

import json
import os
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

from hushai_agent import adapters, clock, ledger, lock, runner  # noqa: E402
from hushai_agent.config import ConfigError, load_instance_config, parse_fallback_chain  # noqa: E402
from hushai_agent.inboxscan import iter_inbox_lines  # noqa: E402
from hushai_agent.vault import add_inbox_entry, init_instance  # noqa: E402


class TestM2aBase(unittest.TestCase):
    """覆盖 clock、config、ledger、lock、fake 与 runner。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.cfg_root = self.base / "config"
        self.state_root = self.base / "state"
        self.vault = self.base / "vault"
        self._orig_env = os.environ.copy()

        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.cfg_root)
        os.environ["HUSHAI_STATE_ROOT"] = str(self.state_root)
        os.environ["HUSHAI_NOW"] = "2026-03-03T08:05:00"
        init_instance("demo", self.vault, no_git=True)
        self.cfg = load_instance_config("demo")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp.cleanup()

    def test_01_clock_and_inbox_add(self) -> None:
        """1. clock：HUSHAI_NOW 生效；inbox add 的时间和文件日期跟着它走。"""
        dt = clock.now(self.cfg)
        self.assertEqual(dt.strftime("%Y-%m-%d %H:%M"), "2026-03-03 08:05")
        self.assertEqual(clock.today(self.cfg).isoformat(), "2026-03-03")

        p1 = add_inbox_entry("demo", "laptop", "工作", "晨间校对")
        self.assertEqual(p1.name, "2026-03-03-laptop.md")
        self.assertIn("- 08:05｜工作｜晨间校对", p1.read_text(encoding="utf-8"))

        p2 = add_inbox_entry("demo", "laptop", "工作", "指定时间记录", time_str="09:40")
        self.assertIn("- 09:40｜工作｜指定时间记录", p2.read_text(encoding="utf-8"))

    def test_02_old_config_and_fallback_chain(self) -> None:
        """2. 旧配置（不含新节）能读且默认值正确；回退链解析（含空、非法 tier 报错）。"""
        cfg_file = self.cfg_root / "demo" / "config.ini"
        old_lines = []
        for ln in cfg_file.read_text(encoding="utf-8").splitlines():
            if ln.strip() in ("[adapters]", "[jobs]", "[budget]"):
                break
            old_lines.append(ln)
        cfg_file.write_text("\n".join(old_lines) + "\n", encoding="utf-8")

        loaded = load_instance_config("demo")
        self.assertEqual(loaded.digest, [])
        self.assertEqual(loaded.weekly, [])
        self.assertEqual(loaded.window_days, 14)
        self.assertEqual(loaded.stale_days, 14)
        self.assertEqual(loaded.max_daily_calls, 20)

        self.assertEqual(parse_fallback_chain(""), [])
        self.assertEqual(
            parse_fallback_chain("agy:light, claude:heavy"),
            [("agy", "light"), ("claude", "heavy")],
        )
        with self.assertRaises(ConfigError):
            parse_fallback_chain("agy:medium")
        with self.assertRaises(ConfigError):
            parse_fallback_chain("invalid_entry")

    def test_03_ledger_read_write_and_calls_today(self) -> None:
        """3. ledger：写、读、calls_today 只数当天的 call 行；坏行不崩。"""
        ledger.record(self.cfg, "call", job="digest", adapter="fake", tier="light", model="m1", ok=True, duration=0.1)
        ledger.record(self.cfg, "job", job="digest", date="2026-03-03", dry_run=False, result="ok", note="")
        os.environ["HUSHAI_NOW"] = "2026-03-02T23:50:00"
        ledger.record(self.cfg, "call", job="digest", adapter="fake", tier="light", model="m1", ok=True, duration=0.2)
        os.environ["HUSHAI_NOW"] = "2026-03-03T10:00:00"

        runs_file = self.state_root / "demo" / "runs.jsonl"
        with runs_file.open("a", encoding="utf-8") as f:
            f.write("{corrupted json line\n")

        rows = ledger.read(self.cfg)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(ledger.read(self.cfg, limit=2)), 2)
        self.assertEqual(ledger.calls_today(self.cfg), 1)

    def test_04_lock_conflict_and_release(self) -> None:
        """4. lock：同一进程两个文件描述符模拟第二个持有者，第二次拿锁抛 LockBusy；释放后可再拿。"""
        with lock.runner_lock(self.cfg) as lock_path:
            self.assertTrue(lock_path.is_file())
            with self.assertRaises(lock.LockBusy):
                with lock.runner_lock(self.cfg):
                    pass
        with lock.runner_lock(self.cfg):
            pass

    def test_05_fake_adapter_and_locator_resolution(self) -> None:
        """5. fake：文件不存在、__fail__、__raw__、正常数据、来源定位符解析（含无法解析原样保留）。"""
        fix_dir = self.base / "fixtures"
        fix_dir.mkdir()
        add_inbox_entry("demo", "laptop", "工作", "第一条记录", time_str="09:00")
        add_inbox_entry("demo", "laptop", "工作", "第二条记录", time_str="09:30")
        add_inbox_entry("demo", "laptop", "工作", "第三条记录", time_str="10:00")
        add_inbox_entry("demo", "laptop", "工作", "自动记录一", time_str="11:00", is_auto=True)

        inbox_dir = self.vault / "hub" / "inbox"
        recs = list(iter_inbox_lines(inbox_dir))
        lid_manual_3 = [r for r in recs if not r.auto][2].line_id
        lid_auto_1 = [r for r in recs if r.auto][0].line_id

        cfg_file = self.cfg_root / "demo" / "config.ini"
        lines = cfg_file.read_text(encoding="utf-8").splitlines()
        idx_ad = lines.index("[adapters]")
        lines[idx_ad + 1 : idx_ad + 1] = [f"fake.fixtures = {fix_dir}", "fake.light = fk-l"]
        cfg_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cfg = load_instance_config("demo")
        ad = adapters.build(cfg, "fake")

        r_missing = ad.complete("p", "light", None, 10, {"job": "digest", "date": "2026-03-01", "empty": {"items": []}})
        self.assertTrue(r_missing.ok)
        self.assertEqual(r_missing.data, {"items": []})

        (fix_dir / "digest-2026-03-02.json").write_text('{"__fail__": "timeout"}', encoding="utf-8")
        r_fail = ad.complete("p", "light", None, 10, {"job": "digest", "date": "2026-03-02"})
        self.assertFalse(r_fail.ok)
        self.assertEqual(r_fail.error, "timeout")

        (fix_dir / "digest-2026-03-04.json").write_text('{"__raw__": "not json"}', encoding="utf-8")
        r_raw = ad.complete("p", "light", None, 10, {"job": "digest", "date": "2026-03-04"})
        self.assertTrue(r_raw.ok)
        self.assertIsNone(r_raw.data)
        self.assertEqual(r_raw.text, "not json")

        (fix_dir / "digest-2026-03-03.json").write_text(
            json.dumps({
                "items": [{
                    "title": "测试",
                    "source_line_ids": [
                        "2026-03-03-laptop#3",
                        "2026-03-03-laptop-auto#1",
                        "2026-03-03-laptop#99",
                        "L-already-id",
                    ],
                }]
            }),
            encoding="utf-8",
        )
        r_ok = ad.complete("p", "light", None, 10, {"job": "digest", "date": "2026-03-03", "inbox_dir": inbox_dir})
        self.assertTrue(r_ok.ok)
        self.assertEqual(
            r_ok.data["items"][0]["source_line_ids"],
            [lid_manual_3, lid_auto_1, "2026-03-03-laptop#99", "L-already-id"],
        )

    def test_06_runner_fallback_retry_and_budget(self) -> None:
        """6. runner：空链、首败次成、校验重试后切换、全败、预算耗尽拦截及 call 计数一致。"""
        fix1, fix2 = self.base / "fix1", self.base / "fix2"
        fix1.mkdir()
        fix2.mkdir()

        data0, info0 = runner.run_model(self.cfg, "digest", "p", None, {"date": "2026-03-03"}, lambda d: (d, None))
        self.assertIsNone(data0)
        self.assertEqual(info0["degraded"], "no-adapter")

        cfg_file = self.cfg_root / "demo" / "config.ini"
        cfg_lines = cfg_file.read_text(encoding="utf-8").replace(
            "digest =", "digest = f1:light, f2:light"
        ).splitlines()
        idx_ad = cfg_lines.index("[adapters]")
        cfg_lines[idx_ad + 1 : idx_ad + 1] = [
            "f1.type = fake", f"f1.fixtures = {fix1}", "f2.type = fake", f"f2.fixtures = {fix2}"
        ]
        cfg_file.write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")
        cfg = load_instance_config("demo")

        (fix1 / "digest-2026-03-03.json").write_text('{"__fail__": "err1"}', encoding="utf-8")
        (fix2 / "digest-2026-03-03.json").write_text('{"ok_val": 1}', encoding="utf-8")
        d1, i1 = runner.run_model(cfg, "digest", "p", None, {"date": "2026-03-03"}, lambda d: (d, None))
        self.assertEqual(d1, {"ok_val": 1})
        self.assertEqual(i1["adapter"], "f2")
        self.assertEqual(i1["calls"], 2)
        self.assertEqual(ledger.calls_today(cfg), 2)

        (fix1 / "digest-2026-03-03.json").write_text('{"bad": 1}', encoding="utf-8")
        d2, i2 = runner.run_model(
            cfg, "digest", "p", None, {"date": "2026-03-03"},
            lambda d: (d, None) if d and "ok_val" in d else (None, "schema_err"),
        )
        self.assertEqual(d2, {"ok_val": 1})
        self.assertEqual(i2["calls"], 3)
        self.assertEqual(ledger.calls_today(cfg), 5)

        (fix2 / "digest-2026-03-03.json").write_text('{"__fail__": "err2"}', encoding="utf-8")
        d3, i3 = runner.run_model(
            cfg, "digest", "p", None, {"date": "2026-03-03"},
            lambda d: (None, "always_bad"),
        )
        self.assertIsNone(d3)
        self.assertEqual(i3["degraded"], "all-failed")
        self.assertEqual(ledger.calls_today(cfg), 8)

        for _ in range(12):
            ledger.record(cfg, "call", job="digest", adapter="f1", tier="light", model="m", ok=True)
        self.assertEqual(ledger.calls_today(cfg), 20)
        d4, i4 = runner.run_model(cfg, "digest", "p", None, {"date": "2026-03-03"}, lambda d: (d, None))
        self.assertIsNone(d4)
        self.assertEqual(i4["degraded"], "budget")
        self.assertEqual(ledger.calls_today(cfg), 20)


if __name__ == "__main__":
    unittest.main()
