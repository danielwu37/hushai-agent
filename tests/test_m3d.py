"""M3d 单元测试：同步器、端接入包与安装向导、导出/升级/卸载、更正驱动规则微调（PPC）。"""

from __future__ import annotations

import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import candidates, doorkit, items, ledger, lifecycle, sync, weekly  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.vault import init_instance  # noqa: E402
from m3a_stubs import run_hushai_cli  # noqa: E402


def _write_rclone_stub(stub_dir: Path) -> tuple[Path, Path, Path]:
    """写一个冒充 rclone 的 Python 脚本：记录每次调用的 argv，并可在 pull 时往目标目录写收件。"""
    log_file = stub_dir / "rclone_calls.jsonl"
    ctrl_file = stub_dir / "rclone_ctrl.json"
    ctrl_file.write_text(json.dumps({"fail_first": False, "pull_line": ""}), encoding="utf-8")
    script = stub_dir / "rclone_stub.py"
    script.write_text(
        "import json, pathlib, sys\n"
        f"log_p = pathlib.Path({str(log_file)!r})\n"
        f"ctrl_p = pathlib.Path({str(ctrl_file)!r})\n"
        "ctrl = json.loads(ctrl_p.read_text(encoding='utf-8'))\n"
        "calls = [json.loads(x) for x in log_p.read_text(encoding='utf-8').splitlines()] if log_p.is_file() else []\n"
        "calls.append({'argv': sys.argv[1:], 'cwd': str(pathlib.Path.cwd())})\n"
        "log_p.write_text('\\n'.join(json.dumps(c, ensure_ascii=False) for c in calls) + '\\n', encoding='utf-8')\n"
        "if ctrl.get('fail_first') and len(calls) == 1:\n"
        "    sys.stderr.write('simulated network glitch\\n')\n"
        "    sys.exit(2)\n"
        "if ctrl.get('always_fail'):\n"
        "    sys.stderr.write('permanent failure\\n')\n"
        "    sys.exit(3)\n"
        "argv = sys.argv[1:]\n"
        "# 当 pull（目标路径是本地 hub 目录）且配了 pull_line 时，模拟拉回一条远端收件\n"
        "if len(argv) >= 3 and argv[0] == 'copy' and not argv[2].startswith('onedrive:') and '--dry-run' not in argv:\n"
        "    line = ctrl.get('pull_line') or ''\n"
        "    if line:\n"
        "        inbox = pathlib.Path(argv[2]) / 'inbox' / '2026-03-02-laptop.md'\n"
        "        inbox.parent.mkdir(parents=True, exist_ok=True)\n"
        "        inbox.write_text('# 2026-03-02 laptop\\n' + line + '\\n', encoding='utf-8')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    return script, log_file, ctrl_file


class TestM3d(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:00:00"
        self.vault = self.base / "vault"
        init_instance("demo", self.vault, no_git=False)
        self.cfg_file = self.base / "config" / "demo" / "config.ini"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def cfg(self):
        return load_instance_config("demo")

    def test_01_sync_push_pull_retry_and_reconcile(self) -> None:
        """1. sync：未配 remote 失败记账；push/pull 参数过滤、失败重试 1 次、pull 后触发即时机械对账。"""
        summary, rc = sync.run_sync(self.cfg())
        self.assertEqual((summary["ok"], rc), (False, 1))

        stub, log_file, ctrl_file = _write_rclone_stub(self.base)
        (self.vault / "hub" / "open-items.md").write_text(
            "# 开放项\n- [ ] 260301-1｜交水费｜owner=user｜sched=-｜due=-｜domain=生活｜verified=2026-03-01｜src=-\n",
            encoding="utf-8",
        )
        cfg_text = self.cfg_file.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace("local_only = domains/health", "local_only = domains/health,hub/secret")
        cfg_text += f"\n[sync]\ntool = {sys.executable} {stub}\nremote = onedrive:vault\ntimeout = 15\n"
        self.cfg_file.write_text(cfg_text, encoding="utf-8")

        ctrl_file.write_text(
            json.dumps({
                "fail_first": True,
                "pull_line": "- 09:30｜生活｜完成 260301-1：交水费｜产物：无｜待办：无",
            }),
            encoding="utf-8",
        )
        proc = run_hushai_cli(SRC_DIR, "sync", "--instance", "demo", "--json")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(out["push"]["tries"], 2)  # 第一次失败，自动重试一次成功
        self.assertEqual(out["pull"]["tries"], 1)

        calls = [json.loads(x) for x in log_file.read_text(encoding="utf-8").splitlines()]
        push_argv = calls[0]["argv"]
        pull_argv = calls[2]["argv"]
        self.assertIn("/AGENTS.md", push_argv)
        self.assertIn("/inbox/*-steward.md", push_argv)
        self.assertIn("/secret", push_argv)
        self.assertIn(".git/**", push_argv)
        self.assertIn("/inbox/*.md", pull_argv)
        self.assertIn("/exchange/in/**", pull_argv)

        # pull 完成后自动触发 on_event，260301-1 已变为 done
        self.assertEqual(items.load(self.cfg())[0].status, "done")
        sync_jobs = [r for r in ledger.read(self.cfg()) if r.get("job") == "sync"]
        self.assertEqual([r["result"] for r in sync_jobs], ["failed", "ok"])

    def test_02_doorkit_and_install_wizard(self) -> None:
        """2. install doorkit 与 wizard：生成同账号零冲突配置、权限拦截、单元文件，且 doctor 全绿。"""
        kit_dir = self.base / "kit"
        kit_dir.mkdir()
        proc = run_hushai_cli(SRC_DIR, "install", "doorkit", "--instance", "demo", "--out", str(kit_dir))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        claude_cfg = json.loads((kit_dir / "claude-settings.json").read_text(encoding="utf-8"))
        self.assertIn("Bash(rm -rf /*)", claude_cfg["permissions"]["deny"])
        self.assertTrue(claude_cfg["claudeMdExcludes"][0].endswith("CLAUDE.md"))
        self.assertIn("SessionStart", claude_cfg["hooks"])
        self.assertIn("Stop", claude_cfg["hooks"])
        start_sh = (kit_dir / "start-claude-steward.sh").read_text(encoding="utf-8")
        self.assertIn("--setting-sources project,local --settings", start_sh)
        self.assertTrue(bool(stat.S_IMODE((kit_dir / "start-claude-steward.sh").stat().st_mode) & stat.S_IXUSR))
        self.assertTrue((kit_dir / "agy-hooks.json").is_file())
        agy_hooks = json.loads((kit_dir / "agy-hooks.json").read_text(encoding="utf-8"))
        self.assertNotIn("hooks", agy_hooks)
        self.assertIn("hushai", agy_hooks)
        self.assertEqual(agy_hooks["hushai"]["SessionStart"][0]["type"], "command")
        self.assertEqual(agy_hooks["hushai"]["Stop"][0]["type"], "command")
        agy_settings = json.loads((kit_dir / "agy-settings.json").read_text(encoding="utf-8"))
        self.assertEqual(agy_settings["trustedWorkspaces"], [str(self.vault.resolve())])
        self.assertTrue(set(doorkit.AGY_DENY_RULES).issubset(set(agy_settings["permissions"]["deny"])))
        self.assertTrue(set(doorkit.AGY_ASK_RULES).issubset(set(agy_settings["permissions"]["ask"])))
        self.assertNotIn("hooks", agy_settings)

        wiz_vault = self.base / "wiz-vault"
        proc_w = run_hushai_cli(
            SRC_DIR, "install", "wizard",
            "--instance", "wiz",
            "--vault", str(wiz_vault),
            "--timezone", "Asia/Yangon",
            "--remote", "onedrive:wiz",
            "--json",
        )
        self.assertEqual(proc_w.returncode, 0, msg=proc_w.stderr)
        data = json.loads(proc_w.stdout)
        self.assertTrue(data["ok"])
        # 向导带了 --remote，所以多出同步定时器两个单元
        self.assertEqual(len(data["units"]), 12)
        self.assertTrue(any(str(u).endswith("hushai-wiz-sync.timer") for u in data["units"]))
        self.assertEqual(len(data["doorkit"]), 6)
        wiz_cfg = load_instance_config("wiz")
        self.assertEqual(wiz_cfg.timezone, "Asia/Yangon")
        self.assertEqual(wiz_cfg.dispatch_adapter, "agy")
        self.assertEqual(wiz_cfg.digest, [("agy", "light"), ("claude", "light")])
        self.assertEqual(wiz_cfg.adapters["claude"]["light"], "sonnet")
        self.assertEqual(wiz_cfg.adapters["claude"]["heavy"], "opus")

    def test_03_export_upgrade_and_uninstall(self) -> None:
        """3. export（目录与 tar.gz，排除 token 与 .git）、upgrade（只更模板不碰画像/补充规则）、uninstall。"""
        token_file = self.base / "config" / "demo" / "serve.token"
        token_file.write_text("secret-token-value\n", encoding="utf-8")
        ledger.record(self.cfg(), "job", job="digest", date="2026-03-02", result="ok")

        exp_dir = self.base / "exported_dir"
        p1 = run_hushai_cli(SRC_DIR, "export", "--instance", "demo", "--out", str(exp_dir), "--json")
        self.assertEqual(p1.returncode, 0, msg=p1.stderr)
        self.assertTrue((exp_dir / "vault" / "hub" / "AGENTS.md").is_file())
        self.assertFalse((exp_dir / "vault" / ".git").exists())
        self.assertTrue((exp_dir / "config" / "config.ini").is_file())
        self.assertFalse((exp_dir / "config" / "serve.token").exists())
        self.assertTrue((exp_dir / "state" / "runs.jsonl").is_file())

        exp_tar = self.base / "exported.tar.gz"
        p2 = run_hushai_cli(SRC_DIR, "export", "--instance", "demo", "--out", str(exp_tar), "--json")
        self.assertEqual(p2.returncode, 0, msg=p2.stderr)
        with tarfile.open(exp_tar, "r:gz") as tf:
            names = tf.getnames()
        self.assertTrue(any(n.endswith("hub/AGENTS.md") for n in names))
        self.assertFalse(any("serve.token" in n or ".git" in n.split("/") for n in names))

        # upgrade：改动 AGENTS.md、AGENTS.local.md、profile.md，升级后只还原 AGENTS.md
        (self.vault / "hub" / "AGENTS.md").write_text("被改坏的规则\n", encoding="utf-8")
        (self.vault / "hub" / "AGENTS.local.md").write_text("# 我的自定义规则\n- 永远保留\n", encoding="utf-8")
        (self.vault / "hub" / "profile.md").write_text("# 我的画像\n- 名字：测试者\n", encoding="utf-8")
        p3 = run_hushai_cli(SRC_DIR, "upgrade", "--instance", "demo", "--json")
        self.assertEqual(p3.returncode, 0, msg=p3.stderr)
        upg = json.loads(p3.stdout)
        self.assertIn("hub/AGENTS.md", upg["updated"])
        self.assertNotEqual((self.vault / "hub" / "AGENTS.md").read_text(encoding="utf-8"), "被改坏的规则\n")
        self.assertIn("永远保留", (self.vault / "hub" / "AGENTS.local.md").read_text(encoding="utf-8"))
        self.assertIn("名字：测试者", (self.vault / "hub" / "profile.md").read_text(encoding="utf-8"))

        # uninstall：默认 dry-run 不删；加 --yes 默认永久保留 vault；只有显式 --delete-vault 才删 vault
        p4 = run_hushai_cli(SRC_DIR, "uninstall", "--instance", "demo", "--json")
        self.assertEqual(p4.returncode, 0)
        self.assertTrue(json.loads(p4.stdout)["dry_run"])
        self.assertTrue(json.loads(p4.stdout)["keep_vault"])
        self.assertTrue(self.cfg_file.is_file())

        p5 = run_hushai_cli(SRC_DIR, "uninstall", "--instance", "demo", "--yes", "--json")
        self.assertEqual(p5.returncode, 0, msg=p5.stderr)
        p5_res = json.loads(p5.stdout)
        self.assertTrue(p5_res["keep_vault"])
        self.assertFalse(self.cfg_file.exists())
        self.assertTrue((self.vault / "hub" / "profile.md").is_file())

        p_conflict = run_hushai_cli(
            SRC_DIR, "uninstall", "--instance", "demo", "--keep-vault", "--delete-vault"
        )
        self.assertNotEqual(p_conflict.returncode, 0)

        init_instance("demo2", self.base / "vault2", no_git=True)
        p6 = run_hushai_cli(SRC_DIR, "uninstall", "--instance", "demo2", "--yes", "--delete-vault", "--json")
        self.assertEqual(p6.returncode, 0, msg=p6.stderr)
        self.assertFalse(json.loads(p6.stdout)["keep_vault"])
        self.assertFalse((self.base / "vault2").exists())

    def test_04_propose_probe_compose_in_weekly(self) -> None:
        """4. PPC：无更正时不生成规则候选；有更正时经回放检验生成带（已回放验证）的 convention 候选。"""
        fixtures = self.base / "fixtures"
        fixtures.mkdir()
        (fixtures / "weekly-2026-03-08.json").write_text(
            json.dumps({"insights": [], "suspect_done": [], "contradictions": []}), encoding="utf-8"
        )
        cfg_text = self.cfg_file.read_text(encoding="utf-8")
        cfg_text = cfg_text.replace("[adapters]", f"[adapters]\nfk.type = fake\nfk.fixtures = {fixtures}")
        cfg_text = cfg_text.replace("weekly =", "weekly = fk:heavy")
        self.cfg_file.write_text(cfg_text, encoding="utf-8")

        res0 = weekly.run_weekly(self.cfg(), "2026-03-08")
        self.assertEqual((res0["ppc_proposed"], res0["ppc_verified"]), (0, 0))
        self.assertEqual(candidates.load(self.cfg()), [])

        corr_file = self.vault / "hub" / "reports" / "corrections.md"
        corr_file.write_text(
            "# 更正记录\n"
            "- 2026-03-06｜target=260305-1｜周会固定在周五下午而非周四｜src=L-20260306-steward-1000-12345678\n"
            "- 2026-02-01｜target=260201-1｜窗口外的旧更正不触发｜src=L-20260201-steward-1000-00000000\n",
            encoding="utf-8",
        )
        res1 = weekly.run_weekly(self.cfg(), "2026-03-08")
        self.assertEqual((res1["ppc_proposed"], res1["ppc_verified"]), (1, 1))
        cands = candidates.load(self.cfg())
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].kind, "convention")
        self.assertIn("已回放验证", cands[0].text)
        self.assertIn("周会固定在周五下午", cands[0].text)

        # 同日再跑一次不重复添加
        res2 = weekly.run_weekly(self.cfg(), "2026-03-08")
        self.assertEqual(res2["ppc_verified"], 0)
        self.assertEqual(len(candidates.load(self.cfg())), 1)

    def test_05_uninstall_script_purge_and_protected_dev_repo(self) -> None:
        """5. 独立脚本 bin/hushai-uninstall、--with-systemd、install wizard 默认实例名 my 与开发库防误删保护。"""
        import subprocess

        script_path = Path(__file__).resolve().parent.parent / "bin" / "hushai-uninstall"
        self.assertTrue(script_path.is_file())
        self.assertTrue(bool(stat.S_IMODE(script_path.stat().st_mode) & stat.S_IXUSR))

        # 1) install wizard 不传 --instance 时在非交互环境默认使用 my
        wiz_default_vault = self.base / "wiz-default-my"
        p_wiz = run_hushai_cli(
            SRC_DIR,
            "install", "wizard",
            "--vault", str(wiz_default_vault),
            "--no-git",
            "--json",
        )
        self.assertEqual(p_wiz.returncode, 0, msg=p_wiz.stderr)
        self.assertEqual(json.loads(p_wiz.stdout)["instance"], "my")
        self.assertTrue((self.base / "config" / "my" / "config.ini").is_file())

        # 2) bin/hushai-uninstall --instance demo --with-systemd --yes 清理 demo 及其 systemd 文件
        fake_sd = self.base / "sd"
        wants = fake_sd / "timers.target.wants"
        wants.mkdir(parents=True)
        u_demo = fake_sd / "hushai-demo-digest.timer"
        u_demo.write_text("[Timer]\n", encoding="utf-8")
        u_link = wants / "hushai-demo-digest.timer"
        u_link.symlink_to(u_demo)
        u_my = fake_sd / "hushai-my-heal.timer"
        u_my.write_text("[Timer]\n", encoding="utf-8")

        env = os.environ.copy()
        env["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        env["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        proc1 = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--instance", "demo",
                "--yes",
                "--systemd-dir", str(fake_sd),
                "--json",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc1.returncode, 0, msg=proc1.stderr)
        res1 = json.loads(proc1.stdout)
        self.assertTrue(res1["ok"])
        self.assertFalse((self.base / "config" / "demo").exists())
        self.assertFalse(u_demo.exists())
        self.assertFalse(u_link.exists())
        self.assertTrue(u_my.is_file())

        # 3) bin/hushai-uninstall --all --yes 默认永久保留 vault，并拒绝误删 /root/hushai-agent
        fake_bin_link = self.base / "local_bin" / "hushai"
        fake_bin_link.parent.mkdir()
        fake_bin_link.symlink_to(script_path.parent / "hushai")

        proc_guard = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--all",
                "--yes",
                "--remove-bin-link",
                "--remove-app",
                "--systemd-dir", str(fake_sd),
                "--bin-link", str(fake_bin_link),
                "--app-dir", "/root/hushai-agent",
                "--json",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_guard.returncode, 0, msg=proc_guard.stderr)
        res_guard = json.loads(proc_guard.stdout)
        self.assertTrue(res_guard["keep_vault"])
        self.assertFalse(res_guard["app_dir_removed"])
        self.assertIn("开发仓库", res_guard["app_dir_skipped_reason"])
        self.assertTrue(Path("/root/hushai-agent/README.md").is_file())
        self.assertFalse(fake_bin_link.exists())
        self.assertFalse(u_my.exists())
        self.assertTrue((wiz_default_vault / "hub" / "profile.md").is_file())

        # 4) 显式 --delete-vault 才会连 vault 一起删除，且允许删除受识别的隔离部署目录
        wipe_vault = self.base / "wiz-wipe-vault"
        p_wipe = run_hushai_cli(
            SRC_DIR,
            "install", "wizard",
            "--instance", "wipe",
            "--vault", str(wipe_vault),
            "--no-git",
            "--json",
        )
        self.assertEqual(p_wipe.returncode, 0, msg=p_wipe.stderr)
        fake_opt_app = self.base / "opt" / "hushai-agent"
        (fake_opt_app / "bin").mkdir(parents=True)
        (fake_opt_app / "bin" / "hushai").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        proc_app = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--all",
                "--yes",
                "--delete-vault",
                "--remove-app",
                "--systemd-dir", str(fake_sd),
                "--bin-link", str(fake_bin_link),
                "--app-dir", str(fake_opt_app),
                "--json",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_app.returncode, 0, msg=proc_app.stderr)
        app_res = json.loads(proc_app.stdout)
        self.assertTrue(app_res["app_dir_removed"])
        self.assertFalse(app_res["keep_vault"])
        self.assertFalse(wipe_vault.exists())
        self.assertFalse(fake_opt_app.exists())


if __name__ == "__main__":
    unittest.main()
