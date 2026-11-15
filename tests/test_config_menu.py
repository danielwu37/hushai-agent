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
from hushai_agent.brain_ask import answer_from_notes
from hushai_agent.brain_cfg import load_brain_config, set_brain_routine_enabled
from hushai_agent.brain_process import process_inbox_to_brain
from hushai_agent.brain_routines import run_daily_brain
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
            # 6. 第二大脑 -> 1. 开关 routine -> daily -> y -> n(暂不重生成units) -> 返回
            "6", "1", "daily", "y", "n", "0",
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

    def test_04_brain_time_params_propagate_to_generated_units_and_reject_invalid(self) -> None:
        init_instance("t4", self.base / "v4", no_git=True)
        # 非法时间应当被拦截并回滚
        rc, out = self._run_script(["6", "2", "99:99", "Funday", "18:00", "09:00", "09:30", "2", "6", "200", "0", "0"], instance="t4")
        self.assertEqual(rc, 0)
        self.assertIn("✗ 修改未保存", out)
        self.assertEqual(load_brain_config(load_instance_config("t4")).daily_time, "21:00")

        # 启用 routine 并修改时间为 07:15 / Mon / 06:45 / 08:05 / 05:20，选择立即重生成 units
        rc, out = self._run_script(
            [
                "6", "1", "all", "y", "n",
                "2", "07:15", "Mon", "06:45", "08:05", "05:20", "3", "12", "150", "y",
                "0", "0",
            ],
            instance="t4",
        )
        self.assertEqual(rc, 0)
        units_dir = get_instance_config_dir("t4") / "units"
        daily_timer = (units_dir / "hushai-t4-brain-daily.timer").read_text(encoding="utf-8")
        weekly_timer = (units_dir / "hushai-t4-brain-weekly.timer").read_text(encoding="utf-8")
        resurface_timer = (units_dir / "hushai-t4-resurface.timer").read_text(encoding="utf-8")
        monthly_timer = (units_dir / "hushai-t4-monthly.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 07:15:00", daily_timer)
        self.assertIn("OnCalendar=Mon *-*-* 06:45:00", weekly_timer)
        self.assertIn("OnCalendar=*-*-* 08:05:00", resurface_timer)
        self.assertIn("OnCalendar=*-*-01 05:20:00", monthly_timer)

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

    def test_06_second_brain_links_ask_and_sync_includes(self) -> None:
        init_instance("t6", self.base / "v6", no_git=True)
        cfg = load_instance_config("t6")
        add_inbox_entry("t6", door="steward", domain="工作", did="和张三讨论了 Q4 预算，决定把云服务支出压缩 20%", todo="周五发预算表")
        add_inbox_entry("t6", door="steward", domain="学习", did="读了 https://example.com/llm-agents 关于 LLM agent 记忆设计的文章")
        add_inbox_entry("t6", door="steward", domain="工作", did="项目 hushai 上线 v0.2.0 配置菜单，云服务预算继续压缩")

        res = process_inbox_to_brain(cfg)
        self.assertGreaterEqual(res["filed_count"], 3)
        for note_p in (cfg.vault / "hub" / "notes").glob("*.md"):
            txt = note_p.read_text(encoding="utf-8")
            self.assertNotIn("2026-11-14、unverified", txt)
            self.assertNotIn("  ", note_p.splitlines()[0] if hasattr(note_p, "splitlines") else txt.splitlines()[0])

        ans = answer_from_notes(cfg, "云服务预算压缩多少？")
        self.assertTrue(ans["covered"])
        self.assertIn("20%", ans["hits"][0]["quote"])
        self.assertNotIn("共同关键词", ans["hits"][0]["quote"])

        push_argv = sync._build_push_argv(cfg, ["rclone"], "onedrive:vault/hub", True)
        self.assertIn("/index.md", push_argv)
        self.assertIn("/notes/**", push_argv)

    def test_07_serve_hot_reload_digest_time_preserve_and_daily_cumulative(self) -> None:
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

        # 3. 同一天两次运行 brain daily 不应覆盖前一次归档的明细
        set_brain_routine_enabled(cfg, "daily", True)
        add_inbox_entry("t7", door="steward", domain="工作", did="上午完成了接口联调")
        d1 = run_daily_brain(cfg, as_of="2026-11-14")
        self.assertFalse(d1["silent"])
        add_inbox_entry("t7", door="steward", domain="工作", did="晚上补充了回归用例")
        d2 = run_daily_brain(cfg, as_of="2026-11-14")
        self.assertFalse(d2["silent"])
        daily_md = (cfg.vault / "hub" / "daily" / "2026-11-14.md").read_text(encoding="utf-8")
        self.assertIn("上午完成了接口联调", daily_md)
        self.assertIn("晚上补充了回归用例", daily_md)

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
            # 先测试确认名错误会取消，再正式卸载 my（不保留 vault，清理 systemd）
            "8", "8", "1", "y", "y", "wrong-name",
            "8", "1", "n", "y", "my",
        ]
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}), \
             mock.patch.object(lifecycle, "DEFAULT_SYSTEMD_DIR", fake_sd), \
             mock.patch.object(lifecycle, "DEFAULT_DEPLOY_APP_DIR", fake_app):
            rc, out = self._run_script(script_1)

        self.assertEqual(rc, 0)
        self.assertIn("实例 my 初始化完成", out)
        self.assertIn("确认名称不匹配，已取消卸载", out)
        self.assertIn("当前实例已卸载，已无剩余实例，已退出设置中心", out)
        self.assertFalse(get_instance_config_dir("my").exists())
        self.assertFalse(vault_my.exists())
        self.assertFalse(sd_unit.exists())
        self.assertFalse(sd_link.exists())

        # 2) 多实例：先在菜单 8->8->1 卸载 a 并切到 b，再用 8->8->2 RESET 全清 + 删除软链
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
            "8", "8", "1", "n", "y", "a",  # 卸载 a -> 回到多实例只剩 b 自动切换为 b
            "8", "8", "2", "n", "y", "y", "RESET",  # 一键重置全清（含 b 的 vault、systemd、bin_link，保留 fake_app）
        ]
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}), \
             mock.patch.object(lifecycle, "DEFAULT_SYSTEMD_DIR", fake_sd), \
             mock.patch.object(lifecycle, "DEFAULT_DEPLOY_APP_DIR", fake_app):
            rc2, out2 = self._run_script(script_2)

        self.assertEqual(rc2, 0)
        self.assertFalse(get_instance_config_dir("a").exists())
        self.assertFalse((self.base / "va").exists())
        self.assertFalse(get_instance_config_dir("b").exists())
        self.assertFalse((self.base / "vb").exists())
        self.assertFalse((fake_sd / "hushai-b-heal.service").exists())
        self.assertFalse(bin_link.exists())


if __name__ == "__main__":
    unittest.main()
