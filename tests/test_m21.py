"""M2.1 七处修补、推迟默认值与次数、即时机械对账单元测试（第 1-12 项）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import cursor, digest, items, judge, ledger, lock, statusfile, todaysel, weekly  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.doctor import run_doctor  # noqa: E402
from hushai_agent.items import OpenItem  # noqa: E402
from hushai_agent.statusfile import StatusRow  # noqa: E402
from test_m2b import DEMO_FIXTURES_SRC, REPLAY_DATES, setup_demo_instance  # noqa: E402


class TestM21(unittest.TestCase):
    """覆盖 M2.1 全部 12 项单测要点。"""

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

    def test_01_same_day_multiple_runs_accumulate_daily_report(self) -> None:
        """1. 同一天先跑一次 digest，再打勾，再跑一次 digest：日报里第一次新建的开放项仍在，打勾的关闭也在。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        os.environ["HUSHAI_NOW"] = "2026-03-02T20:00:00"
        # 演练模式不写 daily/<日期>.json 累积文件
        digest.run_digest(cfg, "2026-03-02", dry_run=True)
        daily_json = self.state_root / "demo" / "daily" / "2026-03-02.json"
        self.assertFalse(daily_json.exists())

        s1 = digest.run_digest(cfg, "2026-03-02")
        self.assertEqual(len(s1["created"]), 2)
        self.assertTrue(daily_json.is_file())

        f_web = vault / "hub" / "inbox" / "2026-03-02-web.md"
        with f_web.open("a", encoding="utf-8") as f:
            f.write("- 21:30｜工作｜完成 260302-1：译注已核完｜产物：无｜待办：无\n")
        s2 = digest.run_digest(cfg, "2026-03-02")
        self.assertEqual(s2["mechanical_applied"], 1)

        daily_md = (vault / "hub" / "reports" / "daily-2026-03-02.md").read_text(encoding="utf-8")
        self.assertIn("- 260302-1｜核对《潮汐之歌》第三章海洋生物译注", daily_md)
        self.assertIn("- 260302-2｜读完《海洋生态史导论》第四章", daily_md)
        self.assertIn("- [已办] 260302-1｜核对《潮汐之歌》第三章海洋生物译注", daily_md)
        self.assertIn("- [已办] 260301-1｜交水费", daily_md)

    def test_02_closed_this_round_visible_and_ref_merges_src_only(self) -> None:
        """2. 上午机械关闭某项、下午有提到它的自然语言行：提示词带 closed-this-round；ref 只合并 src 不改状态。"""
        fix_dir = self.base / "fix-a2"
        fix_dir.mkdir()
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True, fixtures_dir=fix_dir)
        for p in (vault / "hub" / "inbox").glob("*.md"):
            p.unlink()
        items.save(cfg, [OpenItem("260302-1", "交水费", "open", "user", None, None, "生活", "2026-03-02")], "2026-03-02")

        (vault / "hub" / "inbox" / "2026-03-03-web.md").write_text(
            "# 2026-03-03 web\n- 09:00｜生活｜完成 260302-1：交水费｜产物：无｜待办：无\n", encoding="utf-8"
        )
        (vault / "hub" / "inbox" / "2026-03-03-laptop.md").write_text(
            "# 2026-03-03 laptop\n- 15:00｜生活｜水费电子发票已归档备查｜产物：无｜待办：无\n", encoding="utf-8"
        )
        (fix_dir / "digest-2026-03-03.json").write_text(
            json.dumps({
                "items": [{
                    "ref": "260302-1", "title": "交水费", "owner": "user", "status": "open",
                    "scheduled_date": None, "due_date": None, "domain": "生活",
                    "source_line_ids": ["2026-03-03-laptop#1"],
                }],
                "status_rows": [], "memory_candidates": [],
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        prompts: list[str] = []
        orig_bp = judge.build_prompt
        judge.build_prompt = lambda c, r, i, a: (prompts.append(orig_bp(c, r, i, a)) or prompts[-1])
        try:
            os.environ["HUSHAI_NOW"] = "2026-03-03T22:00:00"
            s = digest.run_digest(cfg, "2026-03-03")
        finally:
            judge.build_prompt = orig_bp

        self.assertEqual(len(prompts), 1)
        self.assertIn("260302-1\tuser closed-this-round", prompts[0])
        loaded = items.load(cfg)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].status, "done")
        self.assertEqual(len(loaded[0].src), 2)
        self.assertEqual(s["touched_closed"], ["260302-1"])
        daily_md = (vault / "hub" / "reports" / "daily-2026-03-03.md").read_text(encoding="utf-8")
        self.assertIn("## 已关闭的事项又有新记录", daily_md)
        self.assertIn("- 260302-1｜交水费", daily_md)

    def test_03_status_archive_by_age_and_index_hard_limit(self) -> None:
        """3. 近况归档：超 status_archive_days 进归档并从索引消失；硬上限截断后索引不超且 doctor limits 为 OK。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        rows = [
            StatusRow("工作", "旧项目归档", "已结项", "2026-01-10", ["L-old"]),
            StatusRow("工作", "当前译稿", "进行中", "2026-03-05", ["L-new"]),
        ]
        statusfile.save(cfg, rows, "2026-03-08")
        os.environ["HUSHAI_NOW"] = "2026-03-08T22:00:00"
        weekly.run_weekly(cfg, "2026-03-08")

        arch_text = (vault / "hub" / "reports" / "status-archive.md").read_text(encoding="utf-8")
        self.assertIn("旧项目归档", arch_text)
        self.assertNotIn("旧项目归档", (vault / "hub" / "status-index.md").read_text(encoding="utf-8"))
        self.assertIn("当前译稿", (vault / "hub" / "status-index.md").read_text(encoding="utf-8"))

        many_rows = [
            StatusRow("工作", f"事项-{i:03d}", "正常", f"2026-02-{(i % 28) + 1:02d}", [f"L-{i}"])
            for i in range(85)
        ]
        (vault / "hub" / "status.md").write_text(statusfile.render_status(many_rows, "2026-03-02"), encoding="utf-8")
        digest.run_digest(cfg, "2026-03-02")
        idx_lines = (vault / "hub" / "status-index.md").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(idx_lines), cfg.status_index_lines)
        doc = run_doctor("demo")
        self.assertEqual({c["id"]: c["level"] for c in doc["checks"]}["limits"], "OK")

    def test_04_weekly_same_day_rerun_does_not_overwrite_archive(self) -> None:
        """4. weekly 同日跑两次，归档文件内容仍是第一次重写前的版本。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        for d in REPLAY_DATES:
            os.environ["HUSHAI_NOW"] = f"{d.isoformat()}T22:00:00"
            digest.run_digest(cfg, d)

        first_before = "# 第一次重写前的原始洞察\n"
        (vault / "hub" / "insights.md").write_text(first_before, encoding="utf-8")
        weekly.run_weekly(cfg, "2026-03-08")
        arch_file = vault / "hub" / "reports" / "insights-archive" / "insights-2026-03-08.md"
        self.assertEqual(arch_file.read_text(encoding="utf-8"), first_before)
        self.assertNotEqual((vault / "hub" / "insights.md").read_text(encoding="utf-8"), first_before)

        weekly.run_weekly(cfg, "2026-03-08")
        self.assertEqual(arch_file.read_text(encoding="utf-8"), first_before)

    def test_05_doctor_checks_open_items_format(self) -> None:
        """5. open-items.md 写坏一行 -> doctor 报 FAIL。"""
        vault, _ = setup_demo_instance(self.base, "demo", no_git=True)
        (vault / "hub" / "open-items.md").write_text("- [ ] 坏格式行没有竖线\n", encoding="utf-8")
        rep = run_doctor("demo")
        self.assertFalse(rep["ok"])
        self.assertEqual({c["id"]: c["level"] for c in rep["checks"]}["open-items"], "FAIL")
        self.assertEqual(self.run_cli("doctor", "--instance", "demo").returncode, 1)

    def test_06_digest_for_date_argument(self) -> None:
        """6. --for-date 生效；默认行为不变。"""
        vault, _ = setup_demo_instance(self.base, "demo", no_git=True)
        r1 = self.run_cli("digest", "--instance", "demo", "--date", "2026-03-02", "--for-date", "2026-03-02", "--json")
        self.assertEqual(r1.returncode, 0, msg=r1.stderr)
        self.assertEqual(json.loads(r1.stdout)["for_date"], "2026-03-02")
        today_txt = (vault / "hub" / "today.md").read_text(encoding="utf-8")
        self.assertIn("# 今日待办 2026-03-02", today_txt)
        # 260302-1 排期为 2026-03-03，在 for_date=2026-03-02 时不入选
        self.assertNotIn("260302-1", today_txt)

        r2 = self.run_cli("digest", "--instance", "demo", "--date", "2026-03-02", "--json")
        self.assertEqual(json.loads(r2.stdout)["for_date"], "2026-03-03")
        self.assertIn("# 今日待办 2026-03-03", (vault / "hub" / "today.md").read_text(encoding="utf-8"))

    def test_07_todo_defer_defaults_plus_nd_and_defers_suggestion(self) -> None:
        """7. todo defer 不带 --to 默认今天加 7 天；+3d 生效；连续推迟 3 次后 today.md 出现「建议放弃」。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        for p in (vault / "hub" / "inbox").glob("*.md"):
            p.unlink()
        it = OpenItem("260302-1", "整理书柜", "open", "user", None, None, "生活", "2026-03-02")
        items.save(cfg, [it], "2026-03-03")
        (vault / "hub" / "today.md").write_text(todaysel.render_today([it], "2026-03-03"), encoding="utf-8")
        os.environ["HUSHAI_NOW"] = "2026-03-03T09:00:00"

        self.assertEqual(self.run_cli("todo", "defer", "1", "--instance", "demo").returncode, 0)
        inbox_txt = (vault / "hub" / "inbox" / "2026-03-03-steward.md").read_text(encoding="utf-8")
        self.assertIn("推迟 260302-1 到 2026-03-10", inbox_txt)
        self.assertEqual(items.load(cfg)[0].defers, 1)

        os.environ["HUSHAI_NOW"] = "2026-03-03T09:10:00"
        self.assertEqual(self.run_cli("todo", "defer", "260302-1", "--to", "+3d", "--instance", "demo").returncode, 0)
        self.assertEqual(items.load(cfg)[0].sched, "2026-03-06")
        self.assertEqual(items.load(cfg)[0].defers, 2)

        os.environ["HUSHAI_NOW"] = "2026-03-03T09:20:00"
        self.assertEqual(
            self.run_cli("todo", "defer", "260302-1", "--to", "2026-03-03", "--instance", "demo").returncode, 0
        )
        self.assertEqual(items.load(cfg)[0].defers, 3)
        today_txt = (vault / "hub" / "today.md").read_text(encoding="utf-8")
        self.assertIn("已推迟 3 次，建议放弃", today_txt)

    def test_08_immediate_reconcile_on_todo_and_four_today_states(self) -> None:
        """8. todo done 1 后不跑 digest 即更新 open-items 与 today.md；无 call 账本行；覆盖 [x]/[>]/[-]/[ ] 四态。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        for p in (vault / "hub" / "inbox").glob("*.md"):
            p.unlink()
        pool = [
            OpenItem("260302-1", "任务一", "open", "user", None, "2026-03-05", "工作", "2026-03-02"),
            OpenItem("260302-2", "任务二", "open", "user", None, "2026-03-06", "工作", "2026-03-02"),
            OpenItem("260302-3", "任务三", "open", "user", None, None, "生活", "2026-03-02"),
            OpenItem("260302-4", "任务四", "open", "user", None, "2026-03-08", "生活", "2026-03-02"),
        ]
        items.save(cfg, pool, "2026-03-03")
        (vault / "hub" / "today.md").write_text(todaysel.render_today(pool, "2026-03-03"), encoding="utf-8")
        before_lines = (vault / "hub" / "today.md").read_text(encoding="utf-8").splitlines()

        os.environ["HUSHAI_NOW"] = "2026-03-03T10:00:00"
        self.assertEqual(self.run_cli("todo", "done", "1", "--instance", "demo").returncode, 0)
        self.assertEqual({i.id: i.status for i in items.load(cfg)}["260302-1"], "done")

        after_lines = (vault / "hub" / "today.md").read_text(encoding="utf-8").splitlines()
        self.assertIn("- [x] 1. 任务一（260302-1｜已办）", after_lines)
        self.assertEqual(after_lines[3:], before_lines[3:])
        self.assertEqual(ledger.calls_today(cfg), 0)
        self.assertEqual(len(cursor.load_ids(cfg)), 1)

        r_rec = self.run_cli("reconcile", "--instance", "demo", "--json")
        self.assertEqual(json.loads(r_rec.stdout)["processed_lines"], 0)

        os.environ["HUSHAI_NOW"] = "2026-03-03T10:05:00"
        self.run_cli("todo", "defer", "2", "--to", "2026-03-12", "--instance", "demo")
        os.environ["HUSHAI_NOW"] = "2026-03-03T10:10:00"
        self.run_cli("todo", "drop", "3", "--instance", "demo")

        final_today = (vault / "hub" / "today.md").read_text(encoding="utf-8")
        self.assertIn("- [>] 2. 任务二（260302-2｜已推迟到 03-12）", final_today)
        self.assertIn("- [-] 3. 任务三（260302-3｜已放弃）", final_today)
        self.assertIn("- [ ] 4. 任务四（260302-4｜到期 03-08）", final_today)
        self.assertEqual(len(todaysel.parse_today(final_today)), 4)

    def test_09_inbox_add_normal_vs_candidate_trigger(self) -> None:
        """9. inbox add 普通行不触发对账；写入「候选记忆」领域的行触发，candidates.md 立即多一行。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        for p in (vault / "hub" / "inbox").glob("*.md"):
            p.unlink()
        os.environ["HUSHAI_NOW"] = "2026-03-03T11:00:00"
        open_before = (vault / "hub" / "open-items.md").read_text(encoding="utf-8")

        self.run_cli("inbox", "add", "--instance", "demo", "--door", "laptop", "--domain", "工作", "--did", "普通翻译推进")
        self.assertEqual(cursor.load_ids(cfg), set())
        self.assertFalse((vault / "hub" / "reports" / "candidates.md").exists())
        self.assertEqual((vault / "hub" / "open-items.md").read_text(encoding="utf-8"), open_before)

        self.run_cli("inbox", "add", "--instance", "demo", "--door", "laptop", "--domain", "候选记忆", "--did", "喜欢深色模式")
        cand_file = vault / "hub" / "reports" / "candidates.md"
        self.assertTrue(cand_file.is_file())
        self.assertIn("喜欢深色模式", cand_file.read_text(encoding="utf-8"))
        self.assertEqual(len(cursor.load_ids(cfg)), 1)

    def test_10_lock_busy_on_todo_done_and_reconcile_recovers(self) -> None:
        """10. 占住锁时 todo done 仍返回 0、收件已写入、开放项暂未变；释放后 reconcile 补上。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        for p in (vault / "hub" / "inbox").glob("*.md"):
            p.unlink()
        os.environ["HUSHAI_NOW"] = "2026-03-03T12:00:00"
        (vault / "hub" / "today.md").write_text(
            todaysel.render_today(items.load(cfg), "2026-03-03"), encoding="utf-8"
        )

        with lock.runner_lock(cfg):
            res = self.run_cli("todo", "done", "1", "--instance", "demo")
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertEqual(items.load(cfg)[0].status, "open")
            self.assertTrue((vault / "hub" / "inbox" / "2026-03-03-steward.md").is_file())

        r_rec = self.run_cli("reconcile", "--instance", "demo", "--json")
        self.assertEqual(r_rec.returncode, 0)
        self.assertEqual(items.load(cfg)[0].status, "done")
        self.assertIn("- [x] 1.", (vault / "hub" / "today.md").read_text(encoding="utf-8"))

    def test_11_run_mechanical_only_under_half_second(self) -> None:
        """11. run_mechanical_only 在示例 vault 上单次调用耗时小于 0.5 秒（函数级计时）。"""
        _, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        os.environ["HUSHAI_NOW"] = "2026-03-02T12:35:00"
        t0 = time.perf_counter()
        res = digest.run_mechanical_only(cfg)
        elapsed = time.perf_counter() - t0
        self.assertEqual(res["mechanical_applied"], 1)
        self.assertLess(elapsed, 0.5)

    def test_12_digest_after_immediate_reconcile_rebuilds_clean_today(self) -> None:
        """12. 即时对账之后晚上的 digest 正常：已处理的机械行不重复处理，today.md 整份重算后不含 [x] 行。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        os.environ["HUSHAI_NOW"] = "2026-03-02T12:35:00"
        (vault / "hub" / "today.md").write_text(
            todaysel.render_today(items.load(cfg), "2026-03-02"), encoding="utf-8"
        )
        m_res = digest.run_mechanical_only(cfg)
        self.assertEqual(m_res["mechanical_applied"], 1)
        self.assertIn("- [x] 1.", (vault / "hub" / "today.md").read_text(encoding="utf-8"))

        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        d_res = digest.run_digest(cfg, "2026-03-02")
        self.assertEqual(d_res["mechanical_applied"], 0)
        self.assertEqual(len(d_res["created"]), 2)
        evening_today = (vault / "hub" / "today.md").read_text(encoding="utf-8")
        self.assertNotIn("- [x]", evening_today)
        self.assertIn("- [ ] 1.", evening_today)


if __name__ == "__main__":
    unittest.main()
