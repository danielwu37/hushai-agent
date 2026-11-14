"""交互式设置菜单 (`hushai config`) 与安全配置修改测试。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hushai_agent.brain_cfg import load_brain_config
from hushai_agent.cli_config import MenuIO, run_config_menu
from hushai_agent.config import ConfigError, load_instance_config
from hushai_agent.config_edit import apply_config_updates
from hushai_agent.paths import get_instance_config_file
from hushai_agent.vault import init_instance


class TestConfigMenu(unittest.TestCase):
    """覆盖 hushai config 交互菜单与 config_edit 自动回滚逻辑。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._old_env = {k: os.environ.get(k) for k in ("HUSHAI_CONFIG_ROOT", "HUSHAI_STATE_ROOT", "HUSHAI_NOW")}
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "cfg")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_NOW"] = "2026-11-14T10:00:00"

    def tearDown(self) -> None:
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _run_script(self, inputs: list[str], instance: str | None = None) -> tuple[int, str]:
        it = iter(inputs)
        outputs: list[str] = []

        def fake_read(prompt: str) -> str:
            outputs.append(prompt)
            try:
                return next(it)
            except StopIteration as exc:
                raise EOFError from exc

        rc = run_config_menu(instance=instance, io=MenuIO(read_line=fake_read, write=outputs.append))
        return rc, "\n".join(outputs)

    def test_01_apply_config_updates_preserves_comments_and_rolls_back_on_error(self) -> None:
        init_instance("demo", self.base / "vault", no_git=True)
        cfg_file = get_instance_config_file("demo")
        before = cfg_file.read_text(encoding="utf-8")
        self.assertIn("# <名字>.type", before)

        apply_config_updates("demo", {"instance": {"timezone": "UTC"}})
        self.assertEqual(load_instance_config("demo").timezone, "UTC")
        self.assertIn("# <名字>.type", cfg_file.read_text(encoding="utf-8"))

        snap = cfg_file.read_text(encoding="utf-8")
        with self.assertRaises(ConfigError):
            apply_config_updates("demo", {"instance": {"timezone": "Invalid/NoSuchZone"}})
        self.assertEqual(cfg_file.read_text(encoding="utf-8"), snap)

    def test_02_menu_configures_basic_domains_adapters_brain_and_daily_ops(self) -> None:
        init_instance("my", self.base / "vault", no_git=True)

        script = [
            # 1. 基础设置 -> 1. 修改时区 -> UTC -> 回车返回
            "1", "1", "UTC", "0",
            # 2. 领域与端 -> 1. 修改领域 -> 工作,研发,生活 -> 3. 修改 known doors -> steward,office,laptop,phone -> 返回
            "2", "1", "工作,研发,生活", "3", "steward,office,laptop,phone", "0",
            # 3. AI 适配器 -> 1. 添加/修改 claude -> claude -> 命令路径 -> 轻 -> 重 -> 120 -> 3. digest 链 -> 返回
            "3", "1", "claude", "claude", "/usr/local/bin/claude", "haiku", "sonnet", "120", "3", "claude:light", "0",
            # 6. 第二大脑 -> 1. 开关 routine -> daily -> y -> 返回
            "6", "1", "daily", "y", "0",
            # 7. 日常操作 -> 3. 记一件事 -> office -> 研发 -> 完成菜单重构 -> 无 -> 接手机端 -> 1. 看 today -> 返回
            "7", "3", "office", "研发", "完成菜单重构", "", "接手机端", "1", "0",
            # 0. 退出
            "0",
        ]
        rc, text = self._run_script(script)
        self.assertEqual(rc, 0)
        self.assertIn("hushAi-agent 设置与控制中心  [实例: my]", text)
        self.assertIn("已退出 hushAi 设置中心", text)

        cfg = load_instance_config("my")
        self.assertEqual(cfg.timezone, "UTC")
        self.assertEqual(cfg.domains, ("工作", "研发", "生活"))
        self.assertIn("office", cfg.known_doors)
        self.assertIn("phone", cfg.known_doors)
        self.assertEqual(cfg.adapters["claude"]["command"], "/usr/local/bin/claude")
        self.assertEqual(cfg.digest, [("claude", "light")])

        brain = load_brain_config(cfg)
        self.assertTrue(brain.enabled)
        self.assertTrue(brain.daily_enabled)
        self.assertFalse(brain.weekly_enabled)

        inbox_files = list((cfg.vault / "hub" / "inbox").glob("*-office.md"))
        self.assertEqual(len(inbox_files), 1)
        self.assertIn("完成菜单重构", inbox_files[0].read_text(encoding="utf-8"))

    def test_03_multi_instance_switch_and_cli_help(self) -> None:
        init_instance("alpha", self.base / "v1", no_git=True)
        init_instance("beta", self.base / "v2", no_git=True)
        rc, out = self._run_script(["2", "9", "1", "0"])
        self.assertEqual(rc, 0)
        self.assertIn("[实例: beta]", out)
        self.assertIn("[实例: alpha]", out)

        proc = subprocess.run(
            [sys.executable, "-m", "hushai_agent", "config", "--help"],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(SRC)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--instance", proc.stdout)


if __name__ == "__main__":
    unittest.main()
