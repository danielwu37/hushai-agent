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

from hushai_agent import install, serve, sync
from hushai_agent import brain_ask
from hushai_agent.brain_cfg import load_brain_config
from hushai_agent.cli_config import MenuIO, run_config_menu
from hushai_agent.config import ConfigError, load_instance_config
from hushai_agent.config_edit import apply_config_updates
from hushai_agent.paths import get_instance_config_dir, get_instance_config_file
from hushai_agent.vault import add_inbox_entry, init_instance


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
            # 1. 基础设置 -> 1. 修改时区 -> UTC -> n(暂不重生成units) -> 0 返回
            "1", "1", "UTC", "n", "0",
            # 2. 领域与端 -> 1. 修改领域 -> 工作,研发,生活 -> 3. 修改 known doors -> steward,office,laptop,phone,web -> 返回
            "2", "1", "工作,研发,生活", "3", "steward,office,laptop,phone,web", "0",
            # 3. AI 适配器 -> 1. 添加/修改 claude -> claude -> 命令路径 -> 轻 -> 重 -> 120 -> 3. digest 链 -> 返回
            "3", "1", "claude", "claude", "/usr/local/bin/claude", "haiku", "sonnet", "120", "3", "claude:light", "0",
            # 6. 第二大脑 -> 1. 开关 -> y -> 回车(收件起点今天) -> n(暂不重生成units) -> 返回
            "6", "1", "y", "", "n", "0",
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
        self.assertRegex(brain.since, r"^\d{4}-\d{2}-\d{2}$")

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

    def test_04_brain_time_params_propagate_to_generated_units_and_reject_invalid(self) -> None:
        init_instance("t4", self.base / "v4", no_git=True)
        # 非法时间应当被拦截并回滚
        rc, out = self._run_script(["6", "2", "99:99", "12", "6", "200", "90", "0", "0"], instance="t4")
        self.assertEqual(rc, 0)
        self.assertIn("✗ 修改未保存", out)
        self.assertEqual(load_brain_config(load_instance_config("t4")).time, "22:30")

        # 开启，改编译时间为 07:15，选择立即重生成 units
        rc, out = self._run_script(
            ["6", "1", "y", "", "n", "2", "07:15", "12", "6", "150", "90", "y", "0", "0"],
            instance="t4",
        )
        self.assertEqual(rc, 0)
        units_dir = get_instance_config_dir("t4") / "units"
        timer = (units_dir / "hushai-t4-brain.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 07:15:00", timer)
        self.assertIn("brain ingest --instance t4", (units_dir / "hushai-t4-brain.service").read_text(encoding="utf-8"))
        self.assertEqual(load_brain_config(load_instance_config("t4")).index_max_lines, 150)

    def test_05_cross_validation_rejects_broken_references_and_privacy_paths(self) -> None:
        init_instance("t5", self.base / "v5", no_git=True)
        add_inbox_entry("t5", door="steward", domain="工作", did="写了一行工作记录")

        # 1. 引用不存在的适配器
        with self.assertRaises(ConfigError):
            apply_config_updates("t5", {"jobs": {"digest": "ghost:light"}})

        # 2. 添加适配器并绑定 digest 链，随后尝试删除被引用的适配器 -> 应拒绝并回滚
        rc, out = self._run_script(
            [
                "3", "1", "Claude2", "claude", "claude", "haiku", "sonnet", "120",
                "3", "claude2:light",
                "2", "Claude2",
                "0", "0",
            ],
            instance="t5",
        )
        self.assertIn("✗ 修改未保存", out)
        self.assertIn("claude2", load_instance_config("t5").adapters)

        # 先清空 digest 链再删除大小写混合名 -> 应真正删除
        rc, out = self._run_script(["3", "3", "clear", "2", "Claude2", "0", "0"], instance="t5")
        self.assertNotIn("claude2", load_instance_config("t5").adapters)

        # 3. 非法 local_only 路径
        with self.assertRaises(ConfigError):
            apply_config_updates("t5", {"privacy": {"local_only": "/etc,../secret"}})

        # 4. 不存在的 vault 路径
        with self.assertRaises(ConfigError):
            apply_config_updates("t5", {"instance": {"vault": str(self.base / "no-such-vault")}})

        # 5. 删除已有收件在使用的「工作」领域
        with self.assertRaises(ConfigError):
            apply_config_updates("t5", {"domains": {"names": "家庭,生活"}})

        # 6. approval 端不在 known 中
        with self.assertRaises(ConfigError):
            apply_config_updates("t5", {"approval": {"doors": "ghostdoor"}})

        # 7. sync.timeout = 0
        with self.assertRaises(ConfigError):
            apply_config_updates("t5", {"sync": {"timeout": "0"}})

    def test_06_second_brain_ask_and_sync_includes(self) -> None:
        init_instance("t6", self.base / "v6", no_git=True)
        cfg = load_instance_config("t6")
        (cfg.vault / "hub" / "projects" / "Q4 预算.md").write_text(
            "---\ntype: project\ntitle: Q4 预算\nsummary: 云服务支出压缩 20%\nstatus: 有出处\nupdated: 2026-11-14\n---\n\n"
            "和 [[张三]] 讨论后决定把云服务支出压缩 20%。\n\n## 来源\n- 收件 2026-11-14 steward 10:00：笔记：Q4 预算\n\n## 我的笔记\n",
            encoding="utf-8",
        )
        (cfg.vault / "hub" / "people" / "张三.md").write_text(
            "---\ntype: person\ntitle: 张三\nsummary: 财务同事\n---\n\n财务同事。\n\n## 来源\n- x\n", encoding="utf-8"
        )
        ans = brain_ask.ask(cfg, "云服务预算压缩多少？")
        self.assertEqual(ans["hits"][0]["title"], "Q4 预算")
        self.assertTrue(any("20%" in x for x in ans["hits"][0]["excerpts"]))
        self.assertEqual(ans["related"], ["张三（hub/people/张三.md）"])

        push_argv = sync._build_push_argv(cfg, ["rclone"], "onedrive:vault/hub", True)
        self.assertIn("/index.md", push_argv)
        self.assertIn("/notes/**", push_argv)

    def test_07_serve_hot_reload_and_digest_time_preserve(self) -> None:
        import threading
        import urllib.request

        init_instance("t7", self.base / "v7", no_git=True)
        cfg = load_instance_config("t7")

        # 1. detect_existing_digest_time 保留既有 units 的自定义时间 (22:00)
        units_dir = get_instance_config_dir("t7") / "units"
        units_dir.mkdir(parents=True, exist_ok=True)
        install.render_units(cfg, units_dir, digest_time="22:00")
        self.assertEqual(install.detect_existing_digest_time(cfg), "22:00")
        install.render_units(cfg, units_dir)
        self.assertIn("22:00:00", (units_dir / "hushai-t7-digest.timer").read_text(encoding="utf-8"))

        # 2. serve 在运行期间修改 config.ini（today_items / known_doors）应热加载生效
        srv = serve.make_server(cfg, 0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            opener.open(f"http://127.0.0.1:{port}/?t={srv.token}").read()
            apply_config_updates("t7", {"limits": {"today_items": "3"}})
            opener.open(f"http://127.0.0.1:{port}/api/brief").read()
            self.assertEqual(srv.cfg.today_items, 3)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_08_interactive_wizard_default_and_menu_uninstall(self) -> None:
        from unittest import mock
        from hushai_agent import lifecycle

        fake_home = self.base / "home"
        fake_home.mkdir()
        fake_sd = self.base / "fake_systemd"
        fake_wants = fake_sd / "timers.target.wants"
        fake_wants.mkdir(parents=True)
        fake_app = self.base / "fake_opt_app"
        fake_app.mkdir()

        # 1) 无任何实例时进入 hushai config -> 1 立即初始化 -> 回车接受默认实例名 my -> 8 -> 8 卸载
        sd_unit = fake_sd / "hushai-my-digest.timer"
        sd_unit.write_text("[Timer]\n", encoding="utf-8")
        sd_link = fake_wants / "hushai-my-digest.timer"
        sd_link.symlink_to(sd_unit)

        vault_my = self.base / "v_my"
        script_1 = [
            "1",
            "",  # 默认实例名 my
            str(vault_my),
            "",  # 默认时区
            "",  # 默认语言
            "",  # 默认 steward
            "",  # remote 空
            # 先测试确认名错误会取消；再正式卸载 my 且直接回车保持默认（永久保留 vault，清理 systemd）
            "8", "8", "1", "y", "", "wrong-name",
            "8", "1", "y", "", "my",
        ]
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}), \
             mock.patch.object(lifecycle, "DEFAULT_SYSTEMD_DIR", fake_sd), \
             mock.patch.object(lifecycle, "DEFAULT_DEPLOY_APP_DIR", fake_app):
            rc, out = self._run_script(script_1)

        self.assertEqual(rc, 0)
        self.assertIn("实例 my 初始化完成", out)
        self.assertIn("确认名称不匹配，已取消卸载", out)
        self.assertIn("已永久保留记忆库 vault", out)
        self.assertIn("当前实例已卸载，已无剩余实例，已退出设置中心", out)
        self.assertFalse(get_instance_config_dir("my").exists())
        self.assertTrue((vault_my / "hub" / "profile.md").is_file())
        self.assertFalse(sd_unit.exists())
        self.assertFalse(sd_link.exists())

        # 2) 多实例：先在 8->8->1 卸载 a 并误输二次确认仍保住 va；再 8->8->2 通过 DELETE-VAULT + RESET 删除临时 vb
        init_instance("a", self.base / "va", no_git=True)
        init_instance("b", self.base / "vb", no_git=True)
        bin_link = fake_home / ".local" / "bin" / "hushai"
        bin_link.parent.mkdir(parents=True, exist_ok=True)
        bin_target = self.base / "dummy_bin"
        bin_target.write_text("#!/bin/sh\n", encoding="utf-8")
        bin_link.symlink_to(bin_target)
        (fake_sd / "hushai-b-heal.service").write_text("[Service]\n", encoding="utf-8")

        script_2 = [
            "1",  # 多实例列表先选 a
            "8", "8", "1", "y", "y", "wrong-token", "a",  # 选 y 想删但没输入 DELETE-VAULT -> 必须保住 va
            "8", "8", "2", "y", "y", "y", "DELETE-VAULT", "RESET",  # 二次输入 DELETE-VAULT + RESET 才删 vb + bin_link
        ]
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}), \
             mock.patch.object(lifecycle, "DEFAULT_SYSTEMD_DIR", fake_sd), \
             mock.patch.object(lifecycle, "DEFAULT_DEPLOY_APP_DIR", fake_app):
            rc2, out2 = self._run_script(script_2)

        self.assertEqual(rc2, 0)
        self.assertIn("未输入 DELETE-VAULT，已自动改为永久保留 vault", out2)
        self.assertFalse(get_instance_config_dir("a").exists())
        self.assertTrue(((self.base / "va") / "hub" / "profile.md").is_file())
        self.assertFalse(get_instance_config_dir("b").exists())
        self.assertFalse((self.base / "vb").exists())
        self.assertFalse((fake_sd / "hushai-b-heal.service").exists())
        self.assertFalse(bin_link.exists())


if __name__ == "__main__":
    unittest.main()
