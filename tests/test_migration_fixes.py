"""迁移检查后的修补回归：周重写保留往期认知、闸门旧路径回落与私密规则作用域、收件格式错误原因、今日待办排序、自动记录领域。"""

from __future__ import annotations

import datetime
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import gate, gate_muse, todaysel  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.items import FlexDate, OpenItem  # noqa: E402
from hushai_agent.vault import init_instance  # noqa: E402
from hushai_agent.weekly_parts import CARRY_HEADING, render_insights  # noqa: E402


class TestInsightsCarryOver(unittest.TestCase):
    def test_previous_bullets_survive_rewrite(self) -> None:
        old = "> 头\n\n# 认知\n\n## 近期重心\n\n- 旧判断甲〔已证｜2026-11-10-king 19:28〕\n- 旧判断乙〔推测｜x〕\n"
        new_entry = {"text": "新判断", "basis": "evidenced", "source_line_ids": ["L-20260301-a-0900-1"]}
        text = render_insights([new_entry], old)
        self.assertIn("- 新判断〔已证｜L-20260301-a-0900-1〕", text)
        self.assertIn(CARRY_HEADING, text)
        self.assertIn("- 旧判断甲〔已证｜2026-11-10-king 19:28〕", text)
        self.assertLess(text.index("新判断"), text.index(CARRY_HEADING))

    def test_rerun_does_not_duplicate(self) -> None:
        first = render_insights([], "- 甲\n- 乙\n")
        second = render_insights([], first)
        self.assertEqual(second.count("- 甲"), 1)
        self.assertNotIn("暂无洞察记录", second)

    def test_empty_when_nothing(self) -> None:
        self.assertIn("暂无洞察记录", render_insights([], ""))


class TestTodayOrdering(unittest.TestCase):
    def _it(self, iid: str, due: str | None, sched: str | None = None) -> OpenItem:
        return OpenItem(
            id=iid, title=iid, owner="user",
            due=FlexDate.from_any(due) if due else None,
            sched=FlexDate.from_any(sched) if sched else None,
        )

    def test_soon_due_before_overdue(self) -> None:
        items_ = [
            self._it("260301-1", "2026-03-01"),               # 逾期
            self._it("260301-2", "2026-03-04"),               # 后天到期
            self._it("260301-3", "2026-03-20"),               # 更晚
            self._it("260301-5", None),
        ]
        got = [it.id for it in todaysel.select_today(items_, "2026-03-02")]
        self.assertEqual(got, ["260301-2", "260301-1", "260301-3", "260301-5"])


class TestMuseGateMigration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_NOW"] = "2026-03-02T10:00:00"
        self.vault = self.base / "vault"
        init_instance("demo", self.vault, no_git=True)
        cfg_file = self.base / "config" / "demo" / "config.ini"
        txt = cfg_file.read_text(encoding="utf-8").replace("known = steward,laptop,web", "known = steward,laptop,web,muse")

        od = self.base / "od"
        self.workos = od / "10-工作" / "workos"
        self.eduos = od / "30-家庭" / "eduos"
        (self.workos / "raw").mkdir(parents=True)
        (self.workos / "docs").mkdir(parents=True)
        (self.eduos / "raw").mkdir(parents=True)
        (self.workos / "raw" / "x.md").write_text("机密\n", encoding="utf-8")
        (self.workos / "docs" / "a.md").write_text("公开\n", encoding="utf-8")
        (self.eduos / "raw" / "y.md").write_text("教育原件\n", encoding="utf-8")
        priv = self.base / "private-paths.txt"
        priv.write_text("/raw/**\n", encoding="utf-8")
        txt += (
            "\n[gate]\n"
            f"read_roots = 10-工作/workos={self.workos}, 30-家庭/eduos={self.eduos}\n"
            f"private_paths_file = {priv}\n"
            "private_paths_scope = 10-工作/workos\n"
            f"log_file = {self.base / 'gate.log'}\n"
        )
        cfg_file.write_text(txt, encoding="utf-8")

        legacy = self.vault / "hub" / "reports" / "legacy-00-我" / "收件"
        legacy.mkdir(parents=True)
        (legacy / "2026-02-27-king.md").write_text("# 2026-02-27 king\n- 09:00｜工作｜旧行｜产物：无｜待办：无\n", encoding="utf-8")
        self.cfg = load_instance_config("demo")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _ssh(self, cmd: str, stdin: bytes = b"") -> tuple[dict, int]:
        with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": cmd}):
            return gate.run(self.cfg, "muse", "ssh", {}, io.BytesIO(stdin))

    def test_legacy_inbox_fallback_read_and_ls(self) -> None:
        out, rc = self._ssh("read 00-我/收件/2026-02-27-king.md")
        self.assertEqual(rc, 0, out)
        self.assertIn("旧行", out["stdout"].decode("utf-8"))
        out, rc = self._ssh("ls 00-我/收件")
        self.assertEqual(rc, 0, out)
        self.assertIn("2026-02-27-king.md", out["stdout"])
        # 新路径 hub/inbox 不回落
        out, rc = self._ssh("read hub/inbox/2026-02-27-king.md")
        self.assertEqual(rc, 2)

    def test_private_scope_only_applies_to_workos(self) -> None:
        self.assertEqual(self._ssh("read 10-工作/workos/raw/x.md")[1], 2)
        self.assertEqual(self._ssh("read 10-工作/workos/docs/a.md")[1], 0)
        self.assertEqual(self._ssh("read 30-家庭/eduos/raw/y.md")[1], 0)

    def test_refusal_lists_configured_roots(self) -> None:
        out, rc = self._ssh("ls 20-个人/aios")
        self.assertEqual(rc, 2)
        self.assertIn("10-工作/workos", out["error"])
        self.assertNotIn("23-健康", out["error"])

    def test_append_inbox_explains_error(self) -> None:
        out, rc = self._ssh("append-inbox 2026-03-02 auto", "- 18:49｜工作｜事情｜产物:无｜待办：无\n".encode("utf-8"))
        self.assertEqual(rc, 2)
        self.assertIn("产物：", out["error"])
        out, rc = self._ssh("append-inbox 2026-03-02 auto", "18:49｜工作｜事情｜产物：无｜待办：无\n".encode("utf-8"))
        self.assertIn("- HH:MM｜", out["error"])

    def test_music_download_only_txt(self) -> None:
        music = self.base / "od" / "music"
        (music / "Playlists" / "_输入清单" / "待下载").mkdir(parents=True)
        cfg_file = self.base / "config" / "demo" / "config.ini"
        txt = cfg_file.read_text(encoding="utf-8").replace(
            f"30-家庭/eduos={self.eduos}", f"30-家庭/eduos={self.eduos}, music={music}"
        ) + "write_allow = hub/exchange/in/, music/Playlists/_输入清单/待下载/\n"
        cfg_file.write_text(txt, encoding="utf-8")
        self.cfg = load_instance_config("demo")
        self.assertEqual(self._ssh("put music/Playlists/_输入清单/待下载/a.txt", b"song\n")[1], 0)
        self.assertEqual(self._ssh("put music/Playlists/_输入清单/待下载/a.sh", b"x\n")[1], 2)
        self.assertEqual(self._ssh("put music/Playlists/_输入清单/待下载/sub/a.txt", b"x\n")[1], 2)

    def test_latest_daily_maps_to_morning_brief(self) -> None:
        self.assertEqual(gate_muse.map_legacy_path("00-我/日报/最新.md"), "hub/reports/morning-brief.md")
        self.assertEqual(gate_muse.map_legacy_path("00-我/日报/2026-11.md"), "hub/reports/2026-11.md")

    def test_explain_segments(self) -> None:
        self.assertIn("4 段", gate_muse.explain_line("- 09:00｜工作｜事｜待办：无"))


class TestCaptureDomain(unittest.TestCase):
    def test_domain_from_text(self) -> None:
        from hushai_agent import capture

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            env = {"HUSHAI_CONFIG_ROOT": str(base / "c"), "HUSHAI_STATE_ROOT": str(base / "s"), "HUSHAI_NOW": "2026-03-02T10:00:00"}
            with mock.patch.dict(os.environ, env):
                init_instance("demo", base / "v", no_git=True)
                tr = base / "t.jsonl"
                tr.write_text('{"type":"user","message":{"role":"user","content":"帮我看看音乐歌单"}}\n', encoding="utf-8")
                res = capture.run_capture("demo", "steward", f'{{"session_id":"s1","transcript_path":"{tr}"}}')
                self.assertTrue(res.get("written"), res)
                cfg = load_instance_config("demo")
                expect = "音乐" if "音乐" in cfg.domains else cfg.domains[0]
                self.assertEqual(res["domain"], expect)


if __name__ == "__main__":
    unittest.main()
