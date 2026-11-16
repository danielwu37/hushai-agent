"""M2b 单元测试（第 7-12 项及补充项：校验丢弃、演练模式、锁互斥、周重写、日报脱敏、statusfile 与叠加终态）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import cursor, digest, items, ledger, lock, statusfile, todaysel, weekly  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.items import OpenItem  # noqa: E402

DEMO_VAULT_SRC = REPO_ROOT / "examples" / "vault-demo"
DEMO_FIXTURES_SRC = REPO_ROOT / "examples" / "fixtures-demo"


class TestM2bExtra(unittest.TestCase):
    """覆盖 M2b 第 7-12 项与补充项测试。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.cfg_root = self.base / "config"
        self.state_root = self.base / "state"
        self._orig_env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.cfg_root)
        os.environ["HUSHAI_STATE_ROOT"] = str(self.state_root)

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

    def _setup(self, no_git: bool = True, fixtures_dir: Path | None = None):
        from test_m2b import setup_demo_instance

        return setup_demo_instance(self.base, "demo", no_git=no_git, fixtures_dir=fixtures_dir)

    def test_07_validation_drop_and_raw_retry(self) -> None:
        """7. 校验：混入未知来源、非法枚举、未知 ref 的条目被逐条丢弃其余照常；__raw__ 重试一次后降级。"""
        fix_dir = self.base / "fix-val"
        shutil.copytree(DEMO_FIXTURES_SRC, fix_dir)
        d2 = json.loads((fix_dir / "digest-2026-03-02.json").read_text(encoding="utf-8"))
        d2["items"].extend(
            [
                {
                    "ref": None,
                    "title": "未知来源条目",
                    "owner": "user",
                    "status": "open",
                    "scheduled_date": None,
                    "due_date": None,
                    "domain": "工作",
                    "source_line_ids": ["L-99999999-unknown"],
                },
                {
                    "ref": None,
                    "title": "非法枚举条目",
                    "owner": "invalid_owner",
                    "status": "open",
                    "scheduled_date": None,
                    "due_date": None,
                    "domain": "工作",
                    "source_line_ids": ["2026-03-02-laptop#1"],
                },
                {
                    "ref": "999999-9",
                    "title": "未知ref条目",
                    "owner": "user",
                    "status": "done",
                    "scheduled_date": None,
                    "due_date": None,
                    "domain": "工作",
                    "source_line_ids": ["2026-03-02-laptop#1"],
                },
            ]
        )
        (fix_dir / "digest-2026-03-02.json").write_text(
            json.dumps(d2, ensure_ascii=False), encoding="utf-8"
        )
        (fix_dir / "digest-2026-03-03.json").write_text(
            '{"__raw__": "invalid json output"}', encoding="utf-8"
        )

        _, cfg = self._setup(no_git=True, fixtures_dir=fix_dir)
        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        s2 = digest.run_digest(cfg, "2026-03-02")
        self.assertIsNone(s2["degraded"])
        self.assertEqual(s2["dropped"], 3)
        self.assertEqual(len(s2["created"]), 2)

        os.environ["HUSHAI_NOW"] = "2026-03-03T22:00:00"
        s3 = digest.run_digest(cfg, "2026-03-03")
        self.assertEqual(s3["degraded"], "all-failed")
        self.assertEqual(ledger.calls_today(cfg), 2)

    def test_08_dry_run_preserves_vault_cursor_and_git(self) -> None:
        """8. 演练模式：--dry-run 后 vault 文件、游标、git 提交数都不变，dry-run/<日期>/hub/today.md 存在。"""
        vault, cfg = self._setup(no_git=False)

        def _git_count() -> int:
            r = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=str(vault),
                capture_output=True,
                text=True,
                check=True,
            )
            return int(r.stdout.strip())

        commits_before = _git_count()
        open_before = (vault / "hub" / "open-items.md").read_text(encoding="utf-8")
        today_before = (vault / "hub" / "today.md").read_text(encoding="utf-8")

        res = self.run_cli("digest", "--instance", "demo", "--date", "2026-03-02", "--dry-run", "--json")
        self.assertEqual(res.returncode, 0, msg=res.stderr)

        self.assertEqual(_git_count(), commits_before)
        self.assertEqual((vault / "hub" / "open-items.md").read_text(encoding="utf-8"), open_before)
        self.assertEqual((vault / "hub" / "today.md").read_text(encoding="utf-8"), today_before)
        self.assertEqual(cursor.load_ids(cfg), set())
        self.assertFalse((vault / "hub" / "reports" / "closed-items.md").exists())

        dry_today = self.state_root / "demo" / "dry-run" / "2026-03-02" / "hub" / "today.md"
        self.assertTrue(dry_today.is_file())
        job_rows = [r for r in ledger.read(cfg) if r["kind"] == "job"]
        self.assertEqual(len(job_rows), 1)
        self.assertTrue(job_rows[0]["dry_run"])

    def test_09_lock_busy_returns_1_and_records_failed(self) -> None:
        """9. 锁：先占住锁再跑 digest -> 返回 1，账本有 failed 行（note=lock-busy）。"""
        _, cfg = self._setup(no_git=True)
        os.environ["HUSHAI_LOCK_WAIT"] = "0"
        with lock.runner_lock(cfg):
            res = self.run_cli("digest", "--instance", "demo", "--date", "2026-03-02")
            self.assertEqual(res.returncode, 1)

        jobs = [r for r in ledger.read(cfg) if r["kind"] == "job"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["result"], "failed")
        self.assertIn("lock-busy", jobs[0]["note"])

    def test_10_weekly_rewrite_archive_and_degraded(self) -> None:
        """10. 周重写：03-08 跑 weekly -> insights.md 被重写且旧版进归档（最多 4 份）；周报含四张清单；降级时不动。"""
        fix_dir = self.base / "fix-weekly"
        shutil.copytree(DEMO_FIXTURES_SRC, fix_dir)
        vault, cfg = self._setup(no_git=True, fixtures_dir=fix_dir)

        from test_m2b import REPLAY_DATES

        for d in REPLAY_DATES:
            os.environ["HUSHAI_NOW"] = f"{d.isoformat()}T22:00:00"
            digest.run_digest(cfg, d)

        # L-8：预置 4 份历史归档，周重写时永不删库，所有历史归档全部保留
        arch_dir = vault / "hub" / "reports" / "insights-archive"
        arch_dir.mkdir(parents=True, exist_ok=True)
        for old_d in ("2026-02-08", "2026-02-15", "2026-02-22", "2026-03-01"):
            (arch_dir / f"insights-{old_d}.md").write_text(f"old {old_d}\n", encoding="utf-8")

        orig_insights = (vault / "hub" / "insights.md").read_text(encoding="utf-8")
        res = self.run_cli("weekly", "--instance", "demo", "--date", "2026-03-08", "--json")
        self.assertEqual(res.returncode, 0, msg=res.stderr)

        new_insights = (vault / "hub" / "insights.md").read_text(encoding="utf-8")
        self.assertNotEqual(new_insights, orig_insights)
        self.assertIn("〔已证｜", new_insights)
        self.assertIn("〔推测｜", new_insights)

        arch_files = sorted(p.name for p in arch_dir.glob("insights-*.md"))
        self.assertEqual(len(arch_files), 5)
        self.assertIn("insights-2026-03-08.md", arch_files)
        self.assertIn("insights-2026-02-08.md", arch_files)
        self.assertEqual((arch_dir / "insights-2026-03-08.md").read_text(encoding="utf-8"), orig_insights)

        weekly_rep = (vault / "hub" / "reports" / "weekly-2026-03-08.md").read_text(encoding="utf-8")
        for sec in ("## 逾期开放项", "## 已办未销", "## 互相矛盾", "## 状态未核实"):
            self.assertIn(sec, weekly_rep)

        # 降级时 insights.md 不变
        (fix_dir / "weekly-2026-03-08.json").write_text('{"__fail__": "down"}', encoding="utf-8")
        w_deg = weekly.run_weekly(cfg, "2026-03-08")
        self.assertEqual(w_deg["degraded"], "all-failed")
        self.assertEqual((vault / "hub" / "insights.md").read_text(encoding="utf-8"), new_insights)

    def test_11_daily_report_contains_no_adapter_or_model_names(self) -> None:
        """11. 日报：不含任何适配项名或模型名。"""
        vault, cfg = self._setup(no_git=True)
        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        digest.run_digest(cfg, "2026-03-02")
        daily_text = (vault / "hub" / "reports" / "daily-2026-03-02.md").read_text(encoding="utf-8")

        for forbidden in ("fake-light-v1", "fake-heavy-v1", "fake-light", "claude", "agy"):
            self.assertNotIn(forbidden, daily_text)
        self.assertIn("260302-1", daily_text)
        self.assertIn("260301-1", daily_text)

    def test_12_statusfile_roundtrip_stale_and_index(self) -> None:
        """12. statusfile：往返一致；过期行加后缀且不累积；索引只有事项名。"""
        rows: list[statusfile.StatusRow] = []
        statusfile.upsert(rows, "工作", "第三章译稿", "初译完成", ["L-1", "L-2"], "2026-03-01")
        statusfile.upsert(rows, "工作", "第三章译稿", "初译完成，等编辑反馈", ["L-2", "L-3"], "2026-03-04")
        statusfile.upsert(rows, "生活", "阳台绿植", "浇水正常", ["L-4"], "2026-02-10")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].src, ["L-1", "L-2", "L-3"])

        rendered_1 = statusfile.render_status(rows, as_of="2026-03-05", stale_days=14)
        self.assertNotIn("等编辑反馈（状态未核实）", rendered_1)
        self.assertIn("浇水正常（状态未核实）", rendered_1)

        parsed_1 = statusfile.parse_status(rendered_1)
        self.assertEqual(len(parsed_1), 2)
        self.assertEqual(parsed_1[1].summary, "浇水正常")

        # 再次渲染再解析，后缀绝不累积
        rendered_2 = statusfile.render_status(parsed_1, as_of="2026-03-05", stale_days=14)
        self.assertEqual(rendered_1, rendered_2)
        self.assertNotIn("（状态未核实）（状态未核实）", rendered_2)

        idx_text = statusfile.render_index(parsed_1)
        self.assertIn("只有事项名，没有状态；要状态读 status.md", idx_text)
        self.assertIn("- 工作｜第三章译稿", idx_text)
        self.assertIn("- 生活｜阳台绿植", idx_text)
        self.assertNotIn("初译完成", idx_text)
        self.assertNotIn("浇水正常", idx_text)

    def test_13_today_overlay_terminal_state_and_todo_guard(self) -> None:
        """13. 补充项：today 叠加视图终态不可逆（完成/放弃后忽略推迟）；todo 对叠加终态项报错返回 1 不写文件。"""
        vault, cfg = self._setup(no_git=True)
        open_items = [
            OpenItem("260302-1", "交水费", "open", "user", None, None, "生活", "2026-03-02"),
            OpenItem("260302-2", "寄合同", "open", "user", None, None, "工作", "2026-03-02"),
        ]
        items.save(cfg, open_items, "2026-03-03")
        sel = todaysel.select_today(open_items, "2026-03-03", limit=7)
        (vault / "hub" / "today.md").write_text(
            todaysel.render_today(sel, "2026-03-03"), encoding="utf-8"
        )
        os.environ["HUSHAI_NOW"] = "2026-03-03T09:00:00"

        # 1. 先通过 todo done 将序号 1（260302-1）标记完成
        r_done = self.run_cli("todo", "done", "1", "--instance", "demo")
        self.assertEqual(r_done.returncode, 0, msg=r_done.stderr)

        inbox_0303 = vault / "hub" / "inbox" / "2026-03-03-steward.md"
        lines_before = inbox_0303.read_text(encoding="utf-8").splitlines()

        # 2. 对在叠加视图里已是终态（done）的 1 或 260302-1 再次执行 defer / drop / done -> 报错返回 1，不写文件
        for bad_cmd in (
            ("todo", "defer", "1", "--to", "2026-03-09", "--instance", "demo"),
            ("todo", "drop", "260302-1", "--instance", "demo"),
        ):
            r_bad = self.run_cli(*bad_cmd)
            self.assertEqual(r_bad.returncode, 1)
        self.assertEqual(inbox_0303.read_text(encoding="utf-8").splitlines(), lines_before)

        # 3. 即使外部端手工向收件箱追加了对 260302-1 的推迟行，today 叠加视图仍保持 done 终态
        with inbox_0303.open("a", encoding="utf-8") as f:
            f.write("- 10:00｜生活｜推迟 260302-1 到 2026-03-10：误操作｜产物：无｜待办：无\n")

        r_today = self.run_cli("today", "--instance", "demo", "--json")
        self.assertEqual(r_today.returncode, 0)
        states = {it["id"]: it["state"] for it in json.loads(r_today.stdout)["items"]}
        self.assertEqual(states["260302-1"], "done")


if __name__ == "__main__":
    unittest.main()
