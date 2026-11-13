"""M2b 汇总主流程、七天回放、晚到行、重复跑、降级、回退与预算单元测试（第 1-6 项）。"""

from __future__ import annotations

import datetime
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

from hushai_agent import cursor, digest, items, judge, ledger  # noqa: E402
from hushai_agent.config import InstanceConfig, load_instance_config  # noqa: E402
from hushai_agent.doctor import run_doctor  # noqa: E402
from hushai_agent.inboxscan import iter_inbox_lines  # noqa: E402
from hushai_agent.todaysel import parse_today_entries  # noqa: E402
from hushai_agent.validate import validate_vault  # noqa: E402
from hushai_agent.vault import init_instance  # noqa: E402

DEMO_VAULT_SRC = REPO_ROOT / "examples" / "vault-demo"
DEMO_FIXTURES_SRC = REPO_ROOT / "examples" / "fixtures-demo"
REPLAY_DATES = [
    datetime.date(2026, 3, 2),
    datetime.date(2026, 3, 3),
    datetime.date(2026, 3, 4),
    datetime.date(2026, 3, 5),
    datetime.date(2026, 3, 6),
    datetime.date(2026, 3, 7),
    datetime.date(2026, 3, 8),
]


def setup_demo_instance(
    base_dir: Path,
    instance: str = "demo",
    no_git: bool = True,
    fixtures_dir: Path | None = None,
    extra_config_lines: list[str] | None = None,
) -> tuple[Path, InstanceConfig]:
    """复制 examples/vault-demo 到临时目录并初始化配置 fake 适配项。"""
    vault_dir = base_dir / f"vault-{instance}"
    shutil.copytree(DEMO_VAULT_SRC, vault_dir)
    os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
    init_instance(instance, vault_dir, no_git=no_git)

    actual_fixtures = (fixtures_dir or DEMO_FIXTURES_SRC).resolve()
    cfg_file = Path(os.environ["HUSHAI_CONFIG_ROOT"]) / instance / "config.ini"
    text = cfg_file.read_text(encoding="utf-8")
    text = text.replace(
        "[adapters]",
        "\n".join(
            [
                "[adapters]",
                "demo.type = fake",
                f"demo.fixtures = {actual_fixtures}",
                "demo.light = fake-light-v1",
                "demo.heavy = fake-heavy-v1",
            ]
        ),
    )
    text = text.replace("digest =", "digest = demo:light")
    text = text.replace("weekly =", "weekly = demo:heavy")
    if extra_config_lines:
        text += "\n" + "\n".join(extra_config_lines) + "\n"
    cfg_file.write_text(text, encoding="utf-8")
    return vault_dir, load_instance_config(instance)


class TestM2bCore(unittest.TestCase):
    """覆盖 M2b 第 1-6 项测试。"""

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

    def test_01_seven_day_replay(self) -> None:
        """1. 七天回放（含 Git）：逐天 digest、validate/doctor 全通过，验证全部业务不变量。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=False)
        orig_profile = (vault / "hub" / "profile.md").read_text(encoding="utf-8")

        for d in REPLAY_DATES:
            os.environ["HUSHAI_NOW"] = f"{d.isoformat()}T22:00:00"
            summary = digest.run_digest(cfg, d)
            self.assertIsNone(summary["degraded"])

            val_rep = validate_vault(vault)
            self.assertTrue(val_rep["ok"], msg=f"{d} validate 失败: {val_rep}")
            doc_rep = run_doctor("demo")
            self.assertTrue(doc_rep["ok"], msg=f"{d} doctor 失败: {doc_rep}")

            current_items = items.load(cfg)
            for it in current_items:
                self.assertNotIn("删除", it.title)

            today_text = (vault / "hub" / "today.md").read_text(encoding="utf-8")
            _, t_entries = parse_today_entries(today_text)
            self.assertLessEqual(len(t_entries), 7)
            today_ids = {e.id for e in t_entries}
            # 改期到下周（2026-03-15）的 260304-2 从未进 today.md
            self.assertNotIn("260304-2", today_ids)
            # owner=ai 的事项从未进 today.md
            ai_ids = {it.id for it in current_items if it.owner == "ai"}
            self.assertTrue(today_ids.isdisjoint(ai_ids))

        final_items = {it.id: it for it in items.load(cfg)}
        # 推迟后办完的事最终已办
        self.assertEqual(final_items["260302-1"].status, "done")
        self.assertEqual(final_items["260302-1"].closed, "2026-03-06")
        # 放弃的事（模型 dropped 与机械 放弃）不再开放
        self.assertEqual(final_items["260304-1"].status, "dropped")
        self.assertEqual(final_items["260303-2"].status, "dropped")
        # 机械完成的预置水费与物业费均已办
        self.assertEqual(final_items["260301-1"].status, "done")
        self.assertEqual(final_items["260303-1"].status, "done")

        # 候选记忆进了 reports/candidates.md 而 profile.md 一字未改
        cand_text = (vault / "hub" / "reports" / "candidates.md").read_text(encoding="utf-8")
        self.assertIn("朗读一遍对话段落", cand_text)
        self.assertEqual((vault / "hub" / "profile.md").read_text(encoding="utf-8"), orig_profile)

        # 更正进了 reports/corrections.md
        corr_text = (vault / "hub" / "reports" / "corrections.md").read_text(encoding="utf-8")
        self.assertIn("周岚而非周兰", corr_text)

        # 游标里的 line_id 无重复且覆盖全部合法行
        cursor_lines = (
            (self.state_root / "demo" / "digested.jsonl").read_text(encoding="utf-8").splitlines()
        )
        cursor_id_list = [json.loads(ln)["line_id"] for ln in cursor_lines if ln.strip()]
        self.assertEqual(len(cursor_id_list), len(set(cursor_id_list)))
        all_inbox_ids = {r.line_id for r in iter_inbox_lines(vault / "hub" / "inbox")}
        self.assertEqual(set(cursor_id_list), all_inbox_ids)

    def test_02_late_arriving_line(self) -> None:
        """2. 晚到的行：03-04 跑完后向 03-02-laptop.md 末尾追加新行，03-05 补处理且不重复处理旧行。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        for d in REPLAY_DATES[:3]:  # 03-02, 03-03, 03-04
            os.environ["HUSHAI_NOW"] = f"{d.isoformat()}T22:00:00"
            digest.run_digest(cfg, d)

        before_ids = cursor.load_ids(cfg)
        f_0302 = vault / "hub" / "inbox" / "2026-03-02-laptop.md"
        late_raw = "- 22:30｜工作｜补录断网期间完成的第三章脚注复核｜产物：无｜待办：无"
        with f_0302.open("a", encoding="utf-8") as f:
            f.write(late_raw + "\n")

        # 捕获 03-05 传给 build_prompt 的收件记录
        captured_rest_ids: list[str] = []
        orig_build_prompt = judge.build_prompt

        def spy_build_prompt(c, records, itms, as_of):
            captured_rest_ids.extend(r.line_id for r in records)
            return orig_build_prompt(c, records, itms, as_of)

        judge.build_prompt = spy_build_prompt
        try:
            os.environ["HUSHAI_NOW"] = "2026-03-05T22:00:00"
            s_0305 = digest.run_digest(cfg, datetime.date(2026, 3, 5))
        finally:
            judge.build_prompt = orig_build_prompt

        # 03-05 原本 3 行 + 03-02 晚到 1 行 = 4 行
        self.assertEqual(s_0305["new_lines"], 4)
        late_rec = [
            r for r in iter_inbox_lines(vault / "hub" / "inbox") if "补录断网期间" in r.did
        ][0]
        self.assertIn(late_rec.line_id, captured_rest_ids)
        # 此前已进游标的行没有被再次交给模型
        self.assertTrue(set(captured_rest_ids).isdisjoint(before_ids))
        self.assertIn(late_rec.line_id, cursor.load_ids(cfg))

    def test_03_idempotent_rerun_same_day(self) -> None:
        """3. 重复跑：同一天跑两次，第二次无 call 账本行增加，open-items.md 与 today.md 内容不变。"""
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        digest.run_digest(cfg, "2026-03-02")

        calls_after_first = ledger.calls_today(cfg)
        open_after_first = (vault / "hub" / "open-items.md").read_text(encoding="utf-8")
        today_after_first = (vault / "hub" / "today.md").read_text(encoding="utf-8")
        daily_after_first = (vault / "hub" / "reports" / "daily-2026-03-02.md").read_text(
            encoding="utf-8"
        )

        s2 = digest.run_digest(cfg, "2026-03-02")
        self.assertEqual(s2["new_lines"], 0)
        self.assertEqual(ledger.calls_today(cfg), calls_after_first)
        self.assertEqual(
            (vault / "hub" / "open-items.md").read_text(encoding="utf-8"), open_after_first
        )
        self.assertEqual(
            (vault / "hub" / "today.md").read_text(encoding="utf-8"), today_after_first
        )
        self.assertEqual(
            (vault / "hub" / "reports" / "daily-2026-03-02.md").read_text(encoding="utf-8"),
            daily_after_first,
        )

    def test_04_degraded_and_next_day_recovery(self) -> None:
        """4. 降级：某天样例换成 __fail__ -> 返回 0、today.md 有降级行、机械动作生效、rest 不进游标且次日补处理。"""
        fix_dir = self.base / "fixtures-copy"
        shutil.copytree(DEMO_FIXTURES_SRC, fix_dir)
        vault, cfg = setup_demo_instance(self.base, "demo", no_git=True, fixtures_dir=fix_dir)

        for d in REPLAY_DATES[:5]:  # 03-02 至 03-06
            os.environ["HUSHAI_NOW"] = f"{d.isoformat()}T22:00:00"
            digest.run_digest(cfg, d)

        # 将 03-07 换成 __fail__
        orig_0307 = (fix_dir / "digest-2026-03-07.json").read_text(encoding="utf-8")
        (fix_dir / "digest-2026-03-07.json").write_text(
            '{"__fail__": "provider down"}', encoding="utf-8"
        )

        os.environ["HUSHAI_NOW"] = "2026-03-07T22:00:00"
        res_cli = self.run_cli("digest", "--instance", "demo", "--date", "2026-03-07", "--json")
        self.assertEqual(res_cli.returncode, 0, msg=res_cli.stderr)
        s_0307 = json.loads(res_cli.stdout)
        self.assertEqual(s_0307["degraded"], "all-failed")

        today_0308 = (vault / "hub" / "today.md").read_text(encoding="utf-8")
        self.assertIn("> 今日模型不可用，仅机械对账。", today_0308)

        # 03-07-web 的两条机械动作已生效并进入游标，而 03-07-laptop 的 rest 未进游标
        after_0307_items = {it.id: it for it in items.load(cfg)}
        self.assertEqual(after_0307_items["260303-1"].status, "done")
        self.assertEqual(after_0307_items["260303-2"].status, "dropped")

        recs_0307 = [
            r for r in iter_inbox_lines(vault / "hub" / "inbox") if r.date == "2026-03-07"
        ]
        web_ids = {r.line_id for r in recs_0307 if r.door == "web"}
        laptop_ids = {r.line_id for r in recs_0307 if r.door == "laptop"}
        cur_0307 = cursor.load_ids(cfg)
        self.assertTrue(web_ids.issubset(cur_0307))
        self.assertTrue(laptop_ids.isdisjoint(cur_0307))

        # 次日（03-08）把 03-07 的期望合并到 03-08 样例，验证 03-07 的 rest 行在 03-08 被补处理
        d7_obj = json.loads(orig_0307)
        d8_obj = json.loads((fix_dir / "digest-2026-03-08.json").read_text(encoding="utf-8"))
        d8_obj["items"] = d7_obj["items"] + d8_obj["items"]
        (fix_dir / "digest-2026-03-08.json").write_text(
            json.dumps(d8_obj, ensure_ascii=False), encoding="utf-8"
        )

        os.environ["HUSHAI_NOW"] = "2026-03-08T22:00:00"
        s_0308 = digest.run_digest(cfg, "2026-03-08")
        self.assertIsNone(s_0308["degraded"])
        self.assertTrue(laptop_ids.issubset(cursor.load_ids(cfg)))
        self.assertEqual({it.id: it for it in items.load(cfg)}["260304-1"].status, "dropped")

    def test_05_fallback_two_adapters(self) -> None:
        """5. 回退：配两个 fake 适配项，第一个 __fail__、第二个正常 -> 结果正常且账本里两条 call。"""
        bad_fix = self.base / "fix-bad"
        bad_fix.mkdir()
        (bad_fix / "digest-2026-03-02.json").write_text('{"__fail__": "err1"}', encoding="utf-8")

        _, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        cfg_file = self.cfg_root / "demo" / "config.ini"
        text = cfg_file.read_text(encoding="utf-8")
        text = text.replace(
            "demo.type = fake",
            f"bad.type = fake\nbad.fixtures = {bad_fix}\ndemo.type = fake",
        )
        text = text.replace("digest = demo:light", "digest = bad:light, demo:light")
        cfg_file.write_text(text, encoding="utf-8")
        cfg = load_instance_config("demo")

        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        s = digest.run_digest(cfg, "2026-03-02")
        self.assertIsNone(s["degraded"])
        self.assertEqual(len(s["created"]), 2)

        calls = [r for r in ledger.read(cfg) if r["kind"] == "call"]
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0]["ok"])
        self.assertTrue(calls[1]["ok"])

    def test_06_budget_zero(self) -> None:
        """6. 预算：max_daily_calls = 0 -> 降级原因为 budget，没有 call 行。"""
        _, cfg = setup_demo_instance(self.base, "demo", no_git=True)
        cfg_file = self.cfg_root / "demo" / "config.ini"
        cfg_file.write_text(
            cfg_file.read_text(encoding="utf-8").replace("max_daily_calls = 20", "max_daily_calls = 0"),
            encoding="utf-8",
        )
        cfg = load_instance_config("demo")

        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        s = digest.run_digest(cfg, "2026-03-02")
        self.assertEqual(s["degraded"], "budget")
        calls = [r for r in ledger.read(cfg) if r["kind"] == "call"]
        self.assertEqual(len(calls), 0)


if __name__ == "__main__":
    unittest.main()
