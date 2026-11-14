"""v0.1.0 代码审阅修补的回归测试：挂载自愈顺序、归档先于重写、坏行隔离、交换区不覆盖、
Muse 写入白名单边界与日期、agy 问答式无工具、管家切换失败如实上报、锁等待、超时整组终止。"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import gate, heal, heal_checks, items, lock, steward  # noqa: E402
from hushai_agent.adapters import agy, proc  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.items import OpenItem  # noqa: E402
from hushai_agent.vault import init_instance  # noqa: E402

OPEN_LINE = "- [ ] 260301-1｜交水费｜owner=user｜sched=-｜due=-｜domain=生活｜verified=2026-03-01｜src=-"


class TestReviewFixes(unittest.TestCase):
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
        txt = self.cfg_file.read_text(encoding="utf-8")
        self.cfg_file.write_text(txt.replace("known = steward,laptop,web", "known = steward,laptop,web,muse"), encoding="utf-8")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def cfg(self):
        return load_instance_config("demo")

    # ---- 自愈：挂载 ----
    def test_mount_whitelist_accepts_top_level(self) -> None:
        self.assertTrue(heal.is_whitelisted_unit("demo", "mnt-onedrive.mount"))
        self.assertTrue(heal.is_whitelisted_unit("demo", "mnt-onedrive-music.mount"))
        self.assertFalse(heal.is_whitelisted_unit("demo", "mnt-data.mount"))

    def test_no_unmount_when_unit_not_whitelisted(self) -> None:
        calls: list[list[str]] = []

        def _cmd(argv, timeout=15):
            calls.append(list(argv))
            return 0, ""

        hooks = {"rclone_mount": ("/mnt/data", "mnt-data.mount"), "cmd_runner": _cmd}
        with mock.patch.object(heal_checks, "_probe_vault_subprocess", return_value=(False, "超时")):
            res = heal_checks.check_vault_rw(self.cfg(), None, False, hooks)
        self.assertEqual(res["status"], "failed")
        self.assertFalse(any(c and c[0] == "fusermount" for c in calls))

    # ---- 开放项：归档顺序与坏行 ----
    def test_archive_written_before_open_rewrite(self) -> None:
        cfg = self.cfg()
        done = OpenItem("260101-1", "旧事", "done", "user", None, None, "生活", "2026-01-01")
        done.closed = items.FlexDate.from_any("2026-01-01")
        with mock.patch("hushai_agent.items_store.atomic_write", side_effect=OSError("断电")):
            with self.assertRaises(OSError):
                items.save(cfg, [done], "2026-03-02")
        closed = (self.vault / "hub" / "reports" / "closed-items.md").read_text(encoding="utf-8")
        self.assertIn("260101-1", closed)
        items.save(cfg, [done], "2026-03-02")  # 重跑不重复归档
        closed = (self.vault / "hub" / "reports" / "closed-items.md").read_text(encoding="utf-8")
        self.assertEqual(closed.count("260101-1"), 1)

    def test_unparsable_line_is_quarantined_not_lost(self) -> None:
        cfg = self.cfg()
        bad = "- [ ] 手写的一条没有编号的事"
        (self.vault / "hub" / "open-items.md").write_text(f"# 开放项\n{OPEN_LINE}\n{bad}\n", encoding="utf-8")
        items.save(cfg, items.load(cfg), "2026-03-02")
        q = (self.vault / "hub" / "reports" / "quarantine.md").read_text(encoding="utf-8")
        self.assertIn(bad, q)

    # ---- 闸门 ----
    def test_put_exchange_never_overwrites(self) -> None:
        cfg = self.cfg()
        results: list[str] = []

        def _put(i: int) -> None:
            results.append(gate.put_exchange(cfg, "muse", "note.md", f"v{i}".encode())["path"])

        threads = [threading.Thread(target=_put, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(results)), 5)

    def _muse(self, cmd: str, data: bytes = b""):
        with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": cmd}):
            return gate.run(self.cfg(), "muse", "ssh", {}, io.BytesIO(data))

    def test_muse_write_allow_is_directory_boundary(self) -> None:
        with self.cfg_file.open("a", encoding="utf-8") as fh:
            fh.write("\n[gate]\nwrite_allow = hub/exchange/in\n")
        _, rc_bad = self._muse("put hub/exchange/injected.md", b"x")
        self.assertNotEqual(rc_bad, 0)
        self.assertFalse((self.vault / "hub" / "exchange" / "injected.md").exists())

    def test_muse_append_inbox_rejects_impossible_date(self) -> None:
        line = "- 10:00｜生活｜测试｜产物：无｜待办：无".encode()
        _, rc = self._muse("append-inbox 2026-02-31", line)
        self.assertNotEqual(rc, 0)
        self.assertFalse((self.vault / "hub" / "inbox" / "2026-02-31-muse.md").exists())

    # ---- agy 问答式无工具 ----
    def test_agy_complete_denies_all_tools(self) -> None:
        cfg = self.cfg()
        settings = proc.load_settings("agy", "agy", {"command": "agy", "light": "m"})
        seen: list[list[str]] = []

        def _run(argv, stdin_text, cwd, timeout):
            seen.append(list(argv))
            return 0, '{"status":"SUCCESS","result":"{}"}', "", 0.1

        with mock.patch.object(proc, "run", side_effect=_run):
            agy.AgyAdapter(cfg, settings).complete("hi", "light")
        argv = seen[0]
        self.assertIn("--settings", argv)
        payload = argv[argv.index("--settings") + 1]
        for rule in ("command(*)", "write_file(*)", "read_file(*)", "mcp(*)"):
            self.assertIn(rule, payload)
        self.assertTrue(argv[-1].startswith("-p="))

    # ---- 管家 ----
    def test_quota_switch_reports_failed_when_restart_fails(self) -> None:
        steward.set_backend(self.cfg(), "claude")

        def _cmd(argv, timeout=15):
            if argv[:2] == ["systemctl", "restart"]:
                return 1, "Access denied"
            return 0, "active"

        hooks = {"installed_units": {"hushai-demo-steward.service"}, "cmd_runner": _cmd,
                 "steward_log_text": "You've hit your 5-hour limit"}
        res = steward.check_steward_online(self.cfg(), None, False, hooks)
        self.assertEqual(res["status"], "failed")

    def test_tmux_targets_are_exact(self) -> None:
        seen: list[list[str]] = []

        def _cmd(argv, timeout=10):
            seen.append(list(argv))
            return 0, "active"

        steward.status(self.cfg(), hooks={"cmd_runner": _cmd})
        tmux = [a for a in seen if a and a[0] == "tmux"][0]
        self.assertEqual(tmux[tmux.index("-t") + 1], "=hushai-demo")

    # ---- 锁与子进程 ----
    def test_runner_lock_waits(self) -> None:
        cfg = self.cfg()
        holder = lock.runner_lock(cfg)
        holder.__enter__()
        threading.Timer(1.0, lambda: holder.__exit__(None, None, None)).start()
        t0 = time.monotonic()
        with lock.runner_lock(cfg, wait_seconds=5):
            pass
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)

    def test_timeout_kills_grandchildren(self) -> None:
        marker = self.base / "grandchild-alive"
        child = self.base / "child.py"
        child.write_text(f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w').write('x')\n", encoding="utf-8")
        script = f"import subprocess,sys,time; subprocess.Popen([sys.executable, {str(child)!r}]); time.sleep(30)"
        rc, _, err, _ = proc.run([sys.executable, "-c", script], None, self.base, 1)
        self.assertEqual(rc, proc.TIMEOUT_RC)
        time.sleep(3.5)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
