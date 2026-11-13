"""CLI 子命令（init / doctor / validate / inbox add）与 privacy_scan 集成测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


class TestCLIAndWorkflows(unittest.TestCase):
    """在隔离临时目录中测试 CLI 全流程。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self.config_root = self.base_dir / "config"
        self.state_root = self.base_dir / "state"
        self.vault_dir = self.base_dir / "vault"

        self.env = os.environ.copy()
        self.env["HUSHAI_CONFIG_ROOT"] = str(self.config_root)
        self.env["HUSHAI_STATE_ROOT"] = str(self.state_root)
        existing_py = self.env.get("PYTHONPATH", "")
        self.env["PYTHONPATH"] = (
            f"{SRC_DIR}{os.pathsep}{existing_py}" if existing_py else str(SRC_DIR)
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """通过子进程调用 python -m hushai_agent。"""
        return subprocess.run(
            [sys.executable, "-m", "hushai_agent", *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_init_and_doctor_with_and_without_git(self) -> None:
        """1. init 生成完整 vault，随后 doctor 全绿（--no-git 和有 git 两种）。"""
        # (a) --no-git 模式
        vault_nogit = self.base_dir / "vault-nogit"
        r1 = self.run_cli("init", "--instance", "inst-nogit", "--vault", str(vault_nogit), "--no-git")
        self.assertEqual(r1.returncode, 0, msg=r1.stderr)

        d1 = self.run_cli("doctor", "--instance", "inst-nogit", "--json")
        self.assertEqual(d1.returncode, 0, msg=d1.stderr)
        data1 = json.loads(d1.stdout)
        self.assertTrue(data1["ok"])
        levels1 = {c["id"]: c["level"] for c in data1["checks"]}
        self.assertEqual(levels1["vault-git"], "WARN")
        for cid, lvl in levels1.items():
            if cid != "vault-git":
                self.assertEqual(lvl, "OK", msg=f"{cid} 应为 OK: {data1}")

        # (b) 默认有 git 模式
        vault_git = self.base_dir / "vault-git"
        r2 = self.run_cli("init", "--instance", "inst-git", "--vault", str(vault_git))
        self.assertEqual(r2.returncode, 0, msg=r2.stderr)

        d2 = self.run_cli("doctor", "--instance", "inst-git", "--json")
        self.assertEqual(d2.returncode, 0, msg=d2.stderr)
        data2 = json.loads(d2.stdout)
        self.assertTrue(data2["ok"])
        for item in data2["checks"]:
            self.assertEqual(item["level"], "OK", msg=f"有 git 时 {item} 应为 OK")

    def test_init_idempotent(self) -> None:
        """2. init 幂等：改动已有文件不被覆盖；删掉文件后再跑被补回；不同路径冲突报错。"""
        res = self.run_cli("init", "--instance", "demo", "--vault", str(self.vault_dir), "--no-git")
        self.assertEqual(res.returncode, 0, msg=res.stderr)

        profile_path = self.vault_dir / "hub" / "profile.md"
        custom_text = "# 自定义画像\n\n我是测试使用者。\n"
        profile_path.write_text(custom_text, encoding="utf-8")

        today_path = self.vault_dir / "hub" / "today.md"
        today_path.unlink()
        self.assertFalse(today_path.exists())

        res2 = self.run_cli("init", "--instance", "demo", "--vault", str(self.vault_dir), "--no-git")
        self.assertEqual(res2.returncode, 0, msg=res2.stderr)
        # 已有文件未被覆盖
        self.assertEqual(profile_path.read_text(encoding="utf-8"), custom_text)
        # 缺失文件被补回且输出中包含 hub/today.md
        self.assertTrue(today_path.is_file())
        self.assertIn("hub/today.md", res2.stdout)

        # 同一实例指向不同 vault 路径应报错退出
        other_vault = self.base_dir / "other-vault"
        res_conflict = self.run_cli("init", "--instance", "demo", "--vault", str(other_vault), "--no-git")
        self.assertNotEqual(res_conflict.returncode, 0)

    def test_validate_examples_vault_demo(self) -> None:
        """3. validate 通过 examples/vault-demo。"""
        demo_vault = REPO_ROOT / "examples" / "vault-demo"
        res = self.run_cli("validate", str(demo_vault), "--json")
        self.assertEqual(res.returncode, 0, msg=f"stdout={res.stdout}\nstderr={res.stderr}")
        report = json.loads(res.stdout)
        self.assertTrue(report["ok"])
        for item in report["checks"]:
            self.assertEqual(item["level"], "OK", msg=f"vault-demo 检查未全绿: {item}")

    def test_inbox_add_and_validation(self) -> None:
        """5. inbox add：追加两次得到两行且通过校验；未知 door、非法 domain 被拒；｜ 被替换。"""
        self.run_cli("init", "--instance", "demo", "--vault", str(self.vault_dir), "--no-git")

        r1 = self.run_cli(
            "inbox", "add",
            "--instance", "demo",
            "--door", "laptop",
            "--domain", "工作",
            "--did", "整理第一部分｜初稿",
            "--time", "09:15",
        )
        self.assertEqual(r1.returncode, 0, msg=r1.stderr)

        r2 = self.run_cli(
            "inbox", "add",
            "--instance", "demo",
            "--door", "laptop",
            "--domain", "候选记忆",
            "--did", "喜欢清晨写作",
            "--artifact", "hub/profile.md",
            "--todo", "待确认",
            "--time", "10:30",
        )
        self.assertEqual(r2.returncode, 0, msg=r2.stderr)

        inbox_files = [
            p for p in (self.vault_dir / "hub" / "inbox").iterdir() if p.suffix == ".md"
        ]
        self.assertEqual(len(inbox_files), 1)
        lines = inbox_files[0].read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[0].startswith("# "))
        record_lines = [ln for ln in lines if ln.startswith("- ")]
        self.assertEqual(len(record_lines), 2)
        self.assertIn("整理第一部分|初稿", record_lines[0])

        # 通过 doctor 与 validate 校验
        self.assertEqual(self.run_cli("doctor", "--instance", "demo").returncode, 0)
        self.assertEqual(self.run_cli("validate", str(self.vault_dir)).returncode, 0)

        # 未知 door 被拒
        bad_door = self.run_cli(
            "inbox", "add",
            "--instance", "demo",
            "--door", "unknownpc",
            "--domain", "工作",
            "--did", "测试",
        )
        self.assertNotEqual(bad_door.returncode, 0)

        # 非法 domain 被拒
        bad_domain = self.run_cli(
            "inbox", "add",
            "--instance", "demo",
            "--door", "laptop",
            "--domain", "不存在的领域",
            "--did", "测试",
        )
        self.assertNotEqual(bad_domain.returncode, 0)

    def test_doctor_limits_and_conflicts(self) -> None:
        """7. doctor：AGENTS.md 超行数 -> FAIL；today.md 超 7 条 -> FAIL；today (1).md -> WARN。"""
        self.run_cli("init", "--instance", "demo", "--vault", str(self.vault_dir), "--no-git")

        # (a) 存在 today (1).md -> WARN，但整体 ok 为 True（返回码 0）
        conflict_file = self.vault_dir / "hub" / "today (1).md"
        conflict_file.write_text("冲突副本\n", encoding="utf-8")
        d_warn = self.run_cli("doctor", "--instance", "demo", "--json")
        self.assertEqual(d_warn.returncode, 0)
        rep_warn = json.loads(d_warn.stdout)
        self.assertTrue(rep_warn["ok"])
        levels_warn = {c["id"]: c["level"] for c in rep_warn["checks"]}
        self.assertEqual(levels_warn["sync-conflicts"], "WARN")
        conflict_file.unlink()

        # (b) AGENTS.md 超过 200 行 -> FAIL（返回码 1）
        agents_file = self.vault_dir / "hub" / "AGENTS.md"
        orig_agents = agents_file.read_text(encoding="utf-8")
        agents_file.write_text("\n".join(f"行 {i}" for i in range(205)) + "\n", encoding="utf-8")
        d_fail1 = self.run_cli("doctor", "--instance", "demo", "--json")
        self.assertEqual(d_fail1.returncode, 1)
        rep_fail1 = json.loads(d_fail1.stdout)
        self.assertFalse(rep_fail1["ok"])
        levels_fail1 = {c["id"]: c["level"] for c in rep_fail1["checks"]}
        self.assertEqual(levels_fail1["limits"], "FAIL")
        agents_file.write_text(orig_agents, encoding="utf-8")

        # (c) today.md 超过 7 条待办 -> FAIL（返回码 1）
        today_file = self.vault_dir / "hub" / "today.md"
        today_file.write_text(
            "> 由管家汇总任务生成\n\n" + "\n".join(f"- [ ] 待办 {i}" for i in range(8)) + "\n",
            encoding="utf-8",
        )
        d_fail2 = self.run_cli("doctor", "--instance", "demo", "--json")
        self.assertEqual(d_fail2.returncode, 1)
        rep_fail2 = json.loads(d_fail2.stdout)
        self.assertFalse(rep_fail2["ok"])
        levels_fail2 = {c["id"]: c["level"] for c in rep_fail2["checks"]}
        self.assertEqual(levels_fail2["limits"], "FAIL")

    def test_privacy_scan_hit_and_miss(self) -> None:
        """8. privacy_scan：命中返回 1 并打印 文件:行号:匹配内容，不命中返回 0。"""
        scan_script = REPO_ROOT / "tools" / "privacy_scan.py"
        patterns_file = self.base_dir / "my_patterns.txt"
        patterns_file.write_text("# 注释行\nSECRET_TOKEN_[0-9]+\n", encoding="utf-8")

        target_dir = self.base_dir / "scan_target"
        target_dir.mkdir()
        clean_file = target_dir / "note.md"
        clean_file.write_text("这是一段完全公开的普通文本。\n", encoding="utf-8")
        # 二进制文件应被自动跳过
        (target_dir / "binary.dat").write_bytes(b"\x00\x01SECRET_TOKEN_999\x00")

        # 不命中 -> 返回 0
        res_clean = subprocess.run(
            [sys.executable, str(scan_script), "--patterns", str(patterns_file), str(target_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_clean.returncode, 0, msg=res_clean.stderr)
        self.assertEqual(res_clean.stdout.strip(), "")

        # 写入敏感词后命中 -> 返回 1
        dirty_file = target_dir / "leaked.md"
        dirty_file.write_text("第一行正常\n第二行包含 SECRET_TOKEN_12345 泄漏\n", encoding="utf-8")
        res_hit = subprocess.run(
            [sys.executable, str(scan_script), "--patterns", str(patterns_file), str(target_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_hit.returncode, 1)
        self.assertIn("leaked.md:2:SECRET_TOKEN_12345", res_hit.stdout)


if __name__ == "__main__":
    unittest.main()
