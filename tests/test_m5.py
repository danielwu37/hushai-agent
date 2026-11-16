"""M5 单元测试：常驻管家（steward 切换与额度自愈）与 Muse SSH 闸门（兼容命令、路径映射与越权拦截）。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
BIN_GATE_SSH = TESTS_DIR.parent / "bin" / "hushai-gate-ssh"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import alerts, doorkit, gate, heal, items, steward  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.validate import validate_vault  # noqa: E402
from hushai_agent.vault import init_instance  # noqa: E402
from m3a_stubs import run_hushai_cli  # noqa: E402


class TestM5StewardAndMuseGate(unittest.TestCase):
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
        self.state_dir = self.base / "state" / "demo"

        # 将 muse 加入 known doors
        txt = self.cfg_file.read_text(encoding="utf-8")
        txt = txt.replace("known = steward,laptop,web", "known = steward,laptop,web,muse")
        self.cfg_file.write_text(txt, encoding="utf-8")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def cfg(self):
        return load_instance_config("demo")

    def test_01_steward_use_and_status(self) -> None:
        """1. steward use agy -> status -> use claude -> status，验证配置、启动脚本与收件记录。"""
        cmds: list[list[str]] = []

        def _fake_cmd(argv: list[str], timeout: int = 15) -> tuple[int, str]:
            cmds.append(argv)
            if argv[:2] == ["systemctl", "is-active"]:
                return 0, "active"
            if argv[:2] == ["tmux", "has-session"]:
                return 0, ""
            return 0, ""

        hooks = {"cmd_runner": _fake_cmd}
        r1 = steward.use_backend(self.cfg(), "agy", hooks=hooks)
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["backend"], "agy")
        st1 = steward.status(self.cfg(), hooks=hooks)
        self.assertEqual((st1["backend"], st1["service_active"], st1["tmux_alive"]), ("agy", True, True))
        script_path = Path(st1["start_script"])
        self.assertTrue(script_path.is_file())
        self.assertIn('BACKEND="agy"', script_path.read_text(encoding="utf-8"))

        r2 = steward.use_backend(self.cfg(), "claude", hooks=hooks)
        self.assertTrue(r2["ok"])
        self.assertEqual((r2["previous_backend"], r2["backend"]), ("agy", "claude"))
        st2 = steward.status(self.cfg(), hooks=hooks)
        self.assertEqual(st2["backend"], "claude")
        self.assertIn('BACKEND="claude"', script_path.read_text(encoding="utf-8"))
        self.assertTrue((self.vault / ".claude" / "settings.local.json").is_file())
        agents_hooks_path = self.vault / ".agents" / "hooks.json"
        self.assertTrue(agents_hooks_path.is_file())
        agents_hooks = json.loads(agents_hooks_path.read_text(encoding="utf-8"))
        self.assertNotIn("hooks", agents_hooks)
        self.assertIn("hushai-steward", agents_hooks)
        steward_hook = agents_hooks["hushai-steward"]
        self.assertEqual([h["type"] for h in steward_hook["SessionStart"]], ["command"])
        self.assertEqual([h["type"] for h in steward_hook["Stop"]], ["command", "command"])

        agy_settings_path = script_path.parent / "agy-settings.json"
        self.assertTrue(agy_settings_path.is_file())
        agy_settings = json.loads(agy_settings_path.read_text(encoding="utf-8"))
        self.assertEqual(agy_settings["trustedWorkspaces"], [str(self.vault)])
        self.assertTrue(set(doorkit.AGY_DENY_RULES).issubset(set(agy_settings["permissions"]["deny"])))
        self.assertTrue(set(doorkit.AGY_ASK_RULES).issubset(set(agy_settings["permissions"]["ask"])))
        self.assertNotIn("hooks", agy_settings)

        self.assertFalse((self.vault / "hub" / "inbox" / "2026-03-02-steward.md").exists())
        recent_sw = alerts.load_recent_alerts(self.cfg(), limit=20)
        self.assertTrue(any("切前 agy，切后 claude" in a.get("message", "") for a in recent_sw))
        self.assertTrue(validate_vault(self.vault)["ok"])

    def test_02_heal_steward_online_restarts_dead_tmux(self) -> None:
        """2. 管家 tmux 会话死掉时，heal 自动重启 hushai-demo-steward.service 并记收件。"""
        state = {"alive": False}

        def _fake_cmd(argv: list[str], timeout: int = 15) -> tuple[int, str]:
            if argv[:2] == ["systemctl", "is-active"]:
                return (0, "active") if state["alive"] else (3, "inactive")
            if argv[:2] == ["tmux", "has-session"]:
                return (0, "") if state["alive"] else (1, "no session")
            if argv[:2] == ["systemctl", "restart"] and argv[2] == "hushai-demo-steward.service":
                state["alive"] = True
                return 0, ""
            return 0, ""

        res = heal.run_heal(
            self.cfg(),
            hooks={"installed_units": {"hushai-demo-steward.service"}, "cmd_runner": _fake_cmd},
        )
        self.assertTrue(res["ok"])
        chk = {c["id"]: c for c in res["checks"]}["steward_online"]
        self.assertEqual(chk["status"], "healed")
        self.assertTrue(state["alive"])
        self.assertFalse((self.vault / "hub" / "inbox" / "2026-03-02-heal.md").exists())
        recent = alerts.load_recent_alerts(self.cfg(), limit=20)
        self.assertTrue(any("管家离线" in a.get("message", "") for a in recent))

    def test_03_heal_steward_quota_exhausted_switches_to_agy(self) -> None:
        """3. backend=claude 且日志出现真实额度耗尽特征时，heal 自动切到 agy 并写入 alerts.jsonl（不污染 today.md）。"""
        steward.set_backend(self.cfg(), "claude")
        log_file = self.state_dir / "steward.log"
        log_file.write_text(
            "[hushai-steward start backend=claude ts=2026-03-02T09:00:00+08:00]\n"
            "You've hit your 5-hour limit · resets 4:30am (UTC)\n",
            encoding="utf-8",
        )

        def _fake_cmd(argv: list[str], timeout: int = 15) -> tuple[int, str]:
            return 0, "active"

        res = heal.run_heal(
            self.cfg(),
            hooks={"installed_units": {"hushai-demo-steward.service"}, "cmd_runner": _fake_cmd},
        )
        self.assertTrue(res["ok"])
        chk = {c["id"]: c for c in res["checks"]}["steward_online"]
        self.assertEqual(chk["status"], "healed")
        self.assertEqual(steward.get_backend(self.cfg()), "agy")

        today_txt = (self.vault / "hub" / "today.md").read_text(encoding="utf-8")
        self.assertNotIn("- 系统：", today_txt)
        recent = alerts.load_recent_alerts(self.cfg(), limit=20)
        self.assertTrue(any("Claude 额度用尽自动接手" in a.get("message", "") and "切前 claude，切后 agy" in a.get("message", "") and a.get("kind") == "steward_switch" for a in recent))
        self.assertFalse((self.vault / "hub" / "inbox" / "2026-03-02-steward.md").exists())
        self.assertTrue(validate_vault(self.vault)["ok"])

    def test_04_muse_gate_commands_and_security(self) -> None:
        """4. Muse SSH 闸门：覆盖 ping/ls/read/append-inbox/put/verify 及 6 类越权拒绝。"""
        ext_root = self.base / "onedrive"
        workos = ext_root / "workos"
        music = ext_root / "music"
        (workos / "raw").mkdir(parents=True)
        (workos / "projects" / "confidential").mkdir(parents=True)
        (workos / "docs").mkdir(parents=True)
        (music / "Playlists" / "_输入清单" / "待下载").mkdir(parents=True)

        (workos / "docs" / "readme.md").write_text("项目公开文档\n", encoding="utf-8")
        (workos / "raw" / "secret.txt").write_text("原始机密\n", encoding="utf-8")
        (workos / "projects" / "confidential" / "case.md").write_text("敏感案件\n", encoding="utf-8")

        priv_file = self.base / "private-paths.txt"
        priv_file.write_text("workos/raw/\nworkos/projects/confidential\n", encoding="utf-8")
        log_file = self.base / "gate.log"

        gate_cfg = (
            "\n[gate]\n"
            f"read_roots = workos={workos}, music={music}\n"
            f"private_paths_file = {priv_file}\n"
            "write_allow = 00-我/交换/入/, hub/exchange/in/, music/Playlists/_输入清单/待下载/\n"
            "legacy_prefix_map = 00-我=hub,00-我/今日待办.md=hub/today.md,00-我/近况-索引.md=hub/status-index.md,00-我/近况.md=hub/status.md,00-我/交换/入/=hub/exchange/in/\n"
            "legacy_fallback = 00-我/收件=hub/reports/legacy-00-我/收件\n"
            "txt_only_dirs = music/Playlists/_输入清单/待下载/\n"
            "deny_names = spotify_config.json\n"
            f"log_file = {log_file}\n"
        )
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8") + gate_cfg, encoding="utf-8")

        # 放入一条开放项供 append-inbox 机械对账测试
        (self.vault / "hub" / "open-items.md").write_text(
            "# 开放项\n- [ ] 260301-1｜交水费｜owner=user｜sched=-｜due=-｜domain=生活｜verified=2026-03-01｜src=-\n",
            encoding="utf-8",
        )

        cfg = self.cfg()

        def _ssh(cmd: str, stdin_bytes: bytes = b"") -> tuple[dict, int]:
            with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": cmd}):
                return gate.run(cfg, "muse", "ssh", {}, io.BytesIO(stdin_bytes))

        # (a) ping
        out, rc = _ssh("ping")
        self.assertEqual(rc, 0)
        self.assertIn("pong", out["stdout"])

        # (b) ls 00-我 与 ls workos（自动过滤私密子目录 raw）
        out, rc = _ssh("ls 00-我")
        self.assertEqual(rc, 0)
        self.assertIn("\ttoday.md", out["stdout"])
        self.assertIn("d\t", out["stdout"])

        out_w, rc_w = _ssh("ls workos")
        self.assertEqual(rc_w, 0)
        self.assertIn("\tdocs", out_w["stdout"])
        self.assertNotIn("raw", out_w["stdout"])

        # (c) read 00-我/今日待办.md、00-我/近况-索引.md、workos/docs/readme.md
        out_r1, rc_r1 = _ssh("read 00-我/今日待办.md")
        self.assertEqual(rc_r1, 0)
        self.assertIn("今日待办".encode("utf-8"), out_r1["stdout"])

        out_r2, rc_r2 = _ssh("read 00-我/近况-索引.md")
        self.assertEqual(rc_r2, 0)
        self.assertIsInstance(out_r2["stdout"], bytes)

        out_r3, rc_r3 = _ssh("read workos/docs/readme.md")
        self.assertEqual((rc_r3, out_r3["stdout"]), (0, "项目公开文档\n".encode("utf-8")))

        # (d) append-inbox YYYY-MM-DD [auto]
        inbox_line = "- 10:05｜生活｜完成 260301-1：交水费｜产物：无｜待办：无\n".encode("utf-8")
        out_ap, rc_ap = _ssh("append-inbox 2026-03-02", inbox_line)
        self.assertEqual(rc_ap, 0)
        self.assertIn("追加 1 行，跳过重复 0 行", out_ap["stdout"])
        self.assertEqual(items.load(cfg)[0].status, "done")

        out_apa, rc_apa = _ssh(
            "append-inbox 2026-03-02 auto",
            "- 10:06｜工作｜Muse 自动记录｜产物：无｜待办：无\n".encode("utf-8"),
        )
        self.assertEqual(rc_apa, 0)
        self.assertTrue((self.vault / "hub" / "inbox" / "2026-03-02-muse-auto.md").is_file())

        # (e) put 与 verify
        payload = "# 交换文稿\n测试正文\n".encode("utf-8")
        out_put, rc_put = _ssh("put 00-我/交换/入/2026-03-02-muse-note.md", payload)
        self.assertEqual(rc_put, 0)
        self.assertIn("OK ", out_put["stdout"])
        self.assertEqual((self.vault / "hub" / "exchange" / "in" / "2026-03-02-muse-note.md").read_bytes(), payload)

        out_put2, rc_put2 = _ssh("put music/Playlists/_输入清单/待下载/song.txt", b" track1 \n")
        self.assertEqual(rc_put2, 0)
        self.assertTrue((music / "Playlists" / "_输入清单" / "待下载" / "song.txt").is_file())

        out_ver, rc_ver = _ssh("verify 00-我/交换/入/2026-03-02-muse-note.md")
        self.assertEqual(rc_ver, 0)
        self.assertIn("OK ", out_ver["stdout"])

        # (f) 越权测试：..、绝对路径、符号链接、私密路径、写只读根、删除
        outside_secret = self.base / "outside.txt"
        outside_secret.write_text("绝密", encoding="utf-8")
        os.symlink(outside_secret, self.vault / "hub" / "evil-link.md")
        (self.vault / "domains" / "health").mkdir(parents=True, exist_ok=True)
        (self.vault / "domains" / "health" / "bp.md").write_text("health-local", encoding="utf-8")

        refused_cases = [
            ("read ../outside.txt", b""),
            ("ls 00-我/../../", b""),
            ("read /etc/passwd", b""),
            ("read 00-我/evil-link.md", b""),
            ("read workos/raw/secret.txt", b""),
            ("read workos/projects/confidential/case.md", b""),
            ("read domains/health/bp.md", b""),
            ("put workos/docs/hack.md", b"hack"),
            ("put 00-我/今日待办.md", b"hack"),
            ("rm 00-我/今日待办.md", b""),
            ("delete 00-我/今日待办.md", b""),
        ]
        for bad_cmd, stdin_b in refused_cases:
            out_b, rc_b = _ssh(bad_cmd, stdin_b)
            self.assertEqual(rc_b, 2, msg=f"应拒绝 (rc=2): {bad_cmd} -> {out_b}")
            self.assertIn("拒绝", out_b.get("error", ""), msg=bad_cmd)

        # (g) 验证独立入口脚本在 `/usr/bin/python3 -I` 隔离模式下正常工作
        env = {
            "HOME": str(self.base),
            "HUSHAI_CONFIG_ROOT": str(self.base / "config"),
            "HUSHAI_STATE_ROOT": str(self.base / "state"),
            "HUSHAI_NOW": "2026-03-02T10:00:00",
            "SSH_ORIGINAL_COMMAND": "ping",
            "PYTHONPATH": "/nonexistent/ignored/by/isolated/mode",
        }
        proc = subprocess.run(
            [sys.executable, "-I", str(BIN_GATE_SSH), "--instance", "demo", "--door", "muse"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("pong", proc.stdout)
        self.assertTrue(log_file.is_file())


if __name__ == "__main__":
    unittest.main()
