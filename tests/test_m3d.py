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
        self.assertIn("SessionStart", claude_cfg["hooks"])
        self.assertIn("Stop", claude_cfg["hooks"])
        start_sh = (kit_dir / "start-claude-steward.sh").read_text(encoding="utf-8")
        self.assertIn("--setting-sources project,local --settings", start_sh)
        self.assertTrue(bool(stat.S_IMODE((kit_dir / "start-claude-steward.sh").stat().st_mode) & stat.S_IXUSR))
        self.assertTrue((kit_dir / "agy-hooks.json").is_file())

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
        self.assertEqual(len(data["units"]), 7)
        self.assertEqual(len(data["doorkit"]), 4)
        wiz_cfg = load_instance_config("wiz")
        self.assertEqual(wiz_cfg.timezone, "Asia/Yangon")
        self.assertEqual(wiz_cfg.dispatch_adapter, "agy")
        self.assertEqual(wiz_cfg.digest, [("agy", "light"), ("claude", "light")])

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

        # uninstall：默认 dry-run 不删；--yes --keep-vault 保留 vault；--yes 全删
        p4 = run_hushai_cli(SRC_DIR, "uninstall", "--instance", "demo", "--json")
        self.assertEqual(p4.returncode, 0)
        self.assertTrue(json.loads(p4.stdout)["dry_run"])
        self.assertTrue(self.cfg_file.is_file())

        p5 = run_hushai_cli(SRC_DIR, "uninstall", "--instance", "demo", "--yes", "--keep-vault", "--json")
        self.assertEqual(p5.returncode, 0)
        self.assertFalse(self.cfg_file.exists())
        self.assertTrue((self.vault / "hub" / "profile.md").is_file())

        init_instance("demo2", self.base / "vault2", no_git=True)
        lifecycle.run_uninstall("demo2", yes=True, keep_vault=False)
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


if __name__ == "__main__":
    unittest.main()
