"""M2a 开放项、机械对账、游标、今日待办筛选与 CLI 子进程测试（第 7-10 项）。"""

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

from hushai_agent import cursor, items, ledger, mechanical, todaysel  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.inboxscan import iter_inbox_lines  # noqa: E402
from hushai_agent.items import OpenItem  # noqa: E402
from hushai_agent.validate import validate_vault  # noqa: E402
from hushai_agent.vault import add_inbox_entry, init_instance  # noqa: E402


class TestM2aLoop(unittest.TestCase):
    """覆盖 items、mechanical、cursor、todaysel 及 todo/today/ledger CLI。"""

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

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        py_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{py_path}" if py_path else str(SRC_DIR)
        return subprocess.run(
            [sys.executable, "-m", "hushai_agent", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_07_items_roundtrip_allocate_archive_and_validate(self) -> None:
        """7. items：往返一致、编号分配看归档不复用、四节归类、超 14 天归档消失、坏行让 validate FAIL。"""
        as_of = "2026-03-20"
        sample = [
            OpenItem("260302-1", "交水费", "open", "user", "2026-03-05", None, "生活", "2026-03-02", None, ["L-1"]),
            OpenItem("260302-2", "定下家教", "open", "user_decision", None, "2026-03-10", "家庭", "2026-03-02", None, ["L-2"]),
            OpenItem("260303-1", "整理译注", "open", "ai", None, None, "工作", "2026-03-03", None, ["L-3"]),
            OpenItem("260310-1", "寄合同", "done", "user", None, None, "工作", "2026-03-10", "2026-03-10", ["L-4"]),
            OpenItem("260301-1", "旧完成项", "done", "user", None, None, "生活", "2026-03-01", "2026-03-01", ["L-0"]),
        ]
        keep, arch = items.save(self.cfg, sample, as_of)
        self.assertEqual(len(keep), 4)
        self.assertEqual(len(arch), 1)
        self.assertEqual(arch[0].id, "260301-1")

        loaded = items.load(self.cfg)
        self.assertEqual(loaded, keep)
        rendered = items.render_open_items(keep, as_of)
        for sec in ("## 要我办", "## 要我定", "## AI 推进", "## 近期关闭"):
            self.assertIn(sec, rendered)

        arch_ids = items.load_archived_ids(self.cfg)
        self.assertIn("260301-1", arch_ids)
        self.assertEqual(items.allocate_id(loaded, arch_ids, "2026-03-01"), "260301-2")
        self.assertEqual(items.allocate_id(loaded, arch_ids, "2026-03-02"), "260302-3")

        open_file = self.vault / "hub" / "open-items.md"
        open_file.write_text(rendered + "- [ ] 坏行没有竖线和键值\n", encoding="utf-8")
        rep = validate_vault(self.vault)
        self.assertFalse(rep["ok"])
        levels = {c["id"]: c["level"] for c in rep["checks"]}
        self.assertEqual(levels["open-items"], "FAIL")

    def test_08_mechanical_actions_and_apply(self) -> None:
        """8. mechanical：中英文四动词、带说明、非法推迟 None、unmatched、保留领域与 rest 分流。"""
        self.assertEqual(mechanical.parse_action("完成 260302-1：交水费").kind, "done")
        self.assertEqual(mechanical.parse_action("done 260302-1: paid").target, "260302-1")
        self.assertEqual(mechanical.parse_action("推迟 260302-1 到 2026-03-08：等通知").date, "2026-03-08")
        self.assertEqual(mechanical.parse_action("defer 260302-1 to 2026-03-09").date, "2026-03-09")
        self.assertEqual(mechanical.parse_action("放弃 260303-1").kind, "drop")
        self.assertEqual(mechanical.parse_action("drop 260303-1 no need").kind, "drop")
        self.assertEqual(mechanical.parse_action("更正 260302-1：金额实为 50 元").kind, "correct")
        self.assertEqual(
            mechanical.parse_action("correct L-20260302-web-0800-12345678: 误记").target,
            "L-20260302-web-0800-12345678",
        )
        self.assertIsNone(mechanical.parse_action("推迟 260302-1 到 2026-02-30"))
        self.assertIsNone(mechanical.parse_action("推迟 260302-1"))

        add_inbox_entry("demo", "web", "生活", "完成 260302-1：已交", time_str="08:10")
        add_inbox_entry("demo", "web", "生活", "推迟 260302-1 到 2026-03-09", time_str="08:15")
        add_inbox_entry("demo", "web", "工作", "放弃 999999-1", time_str="08:20")
        add_inbox_entry("demo", "laptop", "更正", "人名周岚非周兰", time_str="08:25")
        add_inbox_entry("demo", "laptop", "候选记忆", "偏好早间汇总", time_str="08:30")
        add_inbox_entry("demo", "laptop", "工作", "正常翻译第四章", time_str="09:00")

        recs = list(iter_inbox_lines(self.vault / "hub" / "inbox"))
        open_list = [OpenItem("260302-1", "交水费", "open", "user", None, None, "生活", "2026-03-02", None, [])]
        out = mechanical.apply_actions(open_list, recs)

        self.assertEqual(open_list[0].status, "done")
        self.assertEqual(open_list[0].closed, "2026-03-03")
        self.assertEqual(len(out.applied), 1)
        self.assertEqual(len(out.unmatched), 2)
        self.assertEqual(len(out.corrections), 1)
        self.assertEqual(len(out.candidates), 1)
        self.assertEqual(len(out.rest), 1)
        self.assertEqual(out.rest[0].did, "正常翻译第四章")

    def test_09_todaysel_filter_sort_render_and_doctor_limits(self) -> None:
        """9. todaysel：排期/owner 过滤、四档排序、上限截取、往返解析、降级行、空文案及过 doctor limits。"""
        pool = [
            OpenItem("260301-2", "未来排期不入选", "open", "user", "2026-03-05", None, "生活", "2026-03-01"),
            OpenItem("260301-3", "AI推进不入选", "open", "ai", None, "2026-03-01", "工作", "2026-03-01"),
            OpenItem("260302-2", "无截止后编号", "open", "user", None, None, "生活", "2026-03-02"),
            OpenItem("260302-1", "无截止先编号", "open", "user", None, None, "生活", "2026-03-02"),
            OpenItem("260301-5", "未来截止", "open", "user", None, "2026-03-06", "工作", "2026-03-01"),
            OpenItem("260301-4", "今日到期", "open", "user", None, "2026-03-03", "工作", "2026-03-01"),
            OpenItem("260301-1", "逾期要我定", "open", "user_decision", None, "2026-03-01", "家庭", "2026-03-01"),
        ]
        sel = todaysel.select_today(pool, "2026-03-03", limit=5)
        self.assertEqual([i.id for i in sel], ["260301-1", "260301-4", "260301-5", "260302-1", "260302-2"])
        self.assertEqual(len(todaysel.select_today(pool, "2026-03-03", limit=2)), 2)

        txt = todaysel.render_today(sel, "2026-03-03", degraded="no-adapter")
        self.assertIn("> 今日模型不可用，仅机械对账。", txt)
        self.assertIn("（260301-1｜要我定｜逾期）", txt)
        self.assertIn("（260301-5｜到期 03-06）", txt)
        self.assertEqual(todaysel.parse_today(txt), [(idx, it.id) for idx, it in enumerate(sel, 1)])

        empty_txt = todaysel.render_today([], "2026-03-03", degraded=None)
        self.assertIn("（今天没有要你办的事）", empty_txt)

        (self.vault / "hub" / "today.md").write_text(txt, encoding="utf-8")
        doc = self.run_cli("doctor", "--instance", "demo", "--json")
        self.assertEqual(doc.returncode, 0, msg=doc.stderr)

    def test_10_todo_today_cursor_and_ledger_cli(self) -> None:
        """10. todo 与 today 命令（子进程级）：序号与编号回写、缺 --to 报错、已关闭报错不写、today --json 叠加。"""
        open_items = [
            OpenItem("260302-1", "交水费", "open", "user", None, "2026-03-05", "生活", "2026-03-02"),
            OpenItem("260302-2", "寄合同", "open", "user", None, None, "工作", "2026-03-02"),
            OpenItem("260301-1", "已完结事项", "done", "user", None, None, "工作", "2026-03-01", "2026-03-01"),
        ]
        items.save(self.cfg, open_items, "2026-03-03")
        sel = todaysel.select_today(open_items, "2026-03-03", limit=7)
        (self.vault / "hub" / "today.md").write_text(todaysel.render_today(sel, "2026-03-03"), encoding="utf-8")

        # defer 非法 --to 报错且不生成收件文件
        self.assertNotEqual(
            self.run_cli("todo", "defer", "1", "--to", "invalid-date", "--instance", "demo").returncode, 0
        )
        # 已关闭项报错且不写文件
        self.assertNotEqual(self.run_cli("todo", "done", "260301-1", "--instance", "demo").returncode, 0)
        self.assertEqual(list((self.vault / "hub" / "inbox").glob("*.md")), [])

        # 用序号 1 完成，用编号 260302-2 推迟
        self.assertEqual(self.run_cli("todo", "done", "1", "--instance", "demo").returncode, 0)
        self.assertEqual(
            self.run_cli("todo", "defer", "260302-2", "--to", "2026-03-09", "--instance", "demo").returncode, 0
        )
        self.assertEqual(self.run_cli("validate", str(self.vault)).returncode, 0)

        r_today = self.run_cli("today", "--instance", "demo", "--json")
        self.assertEqual(r_today.returncode, 0, msg=r_today.stderr)
        t_data = json.loads(r_today.stdout)
        self.assertEqual(t_data["for_date"], "2026-03-03")
        states = {it["id"]: it["state"] for it in t_data["items"]}
        self.assertEqual(states["260302-1"], "done")
        self.assertEqual(states["260302-2"], "deferred")

        # 校验 cursor 与 ledger CLI
        cursor.append(self.cfg, ["L-test-1"], "2026-03-03")
        self.assertIn("L-test-1", cursor.load_ids(self.cfg))
        ledger.record(self.cfg, "job", job="digest", date="2026-03-03", dry_run=False, result="ok")
        r_led = self.run_cli("ledger", "--instance", "demo", "--limit", "1", "--json")
        self.assertEqual(r_led.returncode, 0)
        self.assertEqual(len(json.loads(r_led.stdout)), 1)


if __name__ == "__main__":
    unittest.main()
