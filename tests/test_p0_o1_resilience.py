"""P0 / O1 故障注入与并发韧性测试：lock-busy 补跑、检查点崩溃恢复、目标文件核验与 brain 锁保护。"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import (  # noqa: E402
    brain_cfg,
    brain_model,
    brain_process,
    brain_routines,
    brief,
    brief_html,
    candidates,
    checkpoint,
    checkpoint_verify,
    cursor,
    digest,
    heal,
    items,
    lock,
    reconcile,
    routing,
    validate,
)
from hushai_agent.brain_cfg import load_brain_state, save_brain_state  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.config_ext import load_ext  # noqa: E402
from hushai_agent.daily_state import load_daily_accumulated  # noqa: E402
from hushai_agent.fsio import append_line_with_header  # noqa: E402
from hushai_agent.inboxscan import iter_inbox_lines  # noqa: E402
from hushai_agent.mechanical import apply_actions  # noqa: E402
from hushai_agent.vault import add_inbox_entry, format_inbox_line, init_instance  # noqa: E402


class TestP0O1Resilience(unittest.TestCase):
    """覆盖 O1 的并发 dirty-flag 补跑、多阶段崩溃零重复恢复、检查点篡改核验与 brain 锁防并发。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._orig_env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        self.vault = self.base / "vault"
        self.fixtures = self.base / "fixtures"
        self.fixtures.mkdir()
        init_instance("demo", self.vault, no_git=True)
        self.cfg_file = self.base / "config" / "demo" / "config.ini"
        self.state_dir = self.base / "state" / "demo"
        self._configure_fake_adapter()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        self._tmp.cleanup()

    def cfg(self):
        return load_instance_config("demo")

    def _configure_fake_adapter(self) -> None:
        txt = self.cfg_file.read_text(encoding="utf-8")
        txt = txt.replace(
            "[adapters]",
            "\n".join(
                [
                    "[adapters]",
                    "fk.type = fake",
                    f"fk.fixtures = {self.fixtures}",
                    "fk.light = fake-light",
                    "fk.heavy = fake-heavy",
                ]
            ),
        )
        txt = txt.replace("digest =", "digest = fk:light")
        txt = txt.replace("weekly =", "weekly = fk:heavy")
        txt = txt.replace(
            "[routing]\nenabled = false\ntrusted_doors =\nmax_daily = 10",
            "[routing]\nenabled = true\ntrusted_doors = laptop\nmax_daily = 10\nsteward = fk:heavy\nagy =",
        )
        self.cfg_file.write_text(txt, encoding="utf-8")

    def _write_raw_inbox_line(self, date_str: str, door: str, time_str: str, domain: str, did: str, todo: str | None = None) -> str:
        """直接落盘收件行但不触发即时 reconcile，便于让全部行一同进入单次 digest 检查点验证。"""
        cfg = self.cfg()
        line = format_inbox_line(
            time_str=time_str,
            domain=domain,
            did=did,
            todo=todo,
            lang=cfg.lang,
            allowed_domains=cfg.allowed_domains,
        )
        inbox_file = self.vault / "hub" / "inbox" / f"{date_str}-{door}.md"
        append_line_with_header(inbox_file, f"# {date_str} {door}", line, [self.vault])
        recs = [r for r in iter_inbox_lines(self.vault / "hub" / "inbox") if r.did == did]
        return recs[-1].line_id

    def _seed_digest_scenario(self, date_str: str = "2026-03-02") -> list[str]:
        """构造涵盖 open-items 归档、status-archive、corrections、candidates 与 brain 的完整场景。"""
        (self.vault / "hub" / "open-items.md").write_text(
            "# 开放项\n"
            "- [ ] 260301-1｜交水费｜owner=user｜sched=2026-03-02｜due=-｜domain=生活｜verified=2026-03-01｜src=-\n"
            "- [x] 260201-9｜旧完成项｜owner=user｜sched=-｜due=-｜domain=生活｜closed=2026-02-01｜verified=2026-02-01｜src=-\n",
            encoding="utf-8",
        )
        (self.vault / "hub" / "status.md").write_text(
            "# 全局状态（status.md）\n\n## 工作\n\n"
            "- 工作｜旧归档事项｜旧总结｜verified=2025-01-01｜src=2025-01-01-laptop#1\n",
            encoding="utf-8",
        )
        cfg_txt = re.sub(
            r"status_index_lines\s*=\s*\d+",
            "status_index_lines = 3",
            self.cfg_file.read_text(encoding="utf-8"),
        )
        self.cfg_file.write_text(cfg_txt, encoding="utf-8")
        id1 = self._write_raw_inbox_line(date_str, "laptop", "09:00", "工作", "推进《潮汐之歌》第四章翻译", todo="核对海洋词汇")
        id2 = self._write_raw_inbox_line(date_str, "laptop", "09:10", "更正", "水表读数是 102 而非 120")
        os.environ["HUSHAI_NOW"] = f"{date_str}T22:00:00"
        (self.fixtures / f"digest-{date_str}.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "ref": "260301-1",
                            "title": "交水费",
                            "owner": "user",
                            "status": "done",
                            "scheduled_date": "2026-03-02",
                            "due_date": None,
                            "domain": "生活",
                            "source_line_ids": [id1],
                        },
                        {
                            "ref": None,
                            "title": "核对海洋词汇",
                            "owner": "user",
                            "status": "open",
                            "scheduled_date": date_str,
                            "due_date": date_str,
                            "domain": "工作",
                            "source_line_ids": [id1],
                        },
                    ],
                    "status_rows": [
                        {
                            "domain": "工作",
                            "topic": "《潮汐之歌》译稿",
                            "summary": "已完成第四章核对",
                            "source_line_ids": [id1],
                        }
                    ],
                    "memory_candidates": [
                        {
                            "kind": "insight",
                            "text": "交稿前单独留出术语表终审窗口",
                            "source_line_ids": [id1],
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return [id1, id2]

    def test_01_lock_busy_dirty_flag_drain_for_reconcile_and_routing(self) -> None:
        """1. 锁冲突期间到达的机械动作与路由事件通过 dirty 标记在释锁后自动排空，无一漏失。"""
        cfg = self.cfg()
        (self.vault / "hub" / "open-items.md").write_text(
            "# 开放项\n"
            "- [ ] 260301-1｜交水费｜owner=user｜sched=2026-03-02｜due=-｜domain=生活｜verified=2026-03-01｜src=-\n",
            encoding="utf-8",
        )
        add_inbox_entry("demo", "laptop", "工作", "日常讨论")
        (self.fixtures / "digest-2026-03-02.json").write_text(
            json.dumps({"items": [], "status_rows": [], "memory_candidates": []}), encoding="utf-8"
        )

        # (A) Thread-1 在 digest 写入期间持锁；Thread-2 并发落盘完成 260301-1 并调 run_mechanical_only 命中 lock-busy -> 写 dirty
        t1_locked = threading.Event()
        t1_release = threading.Event()
        errors: list[BaseException] = []

        def hook_digest(stage: str) -> None:
            if stage == "after_vault_writes":
                t1_locked.set()
                if not t1_release.wait(timeout=5.0):
                    raise TimeoutError("t1_release wait timeout")

        def run_t1() -> None:
            try:
                with mock.patch.object(checkpoint, "_stage_hook", side_effect=hook_digest):
                    digest.run_digest(self.cfg(), as_of="2026-03-02")
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        th = threading.Thread(target=run_t1, daemon=True)
        th.start()
        try:
            self.assertTrue(t1_locked.wait(timeout=5.0))
            self._write_raw_inbox_line("2026-03-02", "laptop", "09:30", "生活", "完成 260301-1：已缴水费")
            busy_res = reconcile.run_mechanical_only(self.cfg())
            self.assertEqual(busy_res.get("skipped"), "lock-busy")
            self.assertTrue(busy_res.get("dirty_marked"))
            self.assertTrue(lock.has_dirty(cfg, "mechanical"))
        finally:
            t1_release.set()
            th.join(timeout=5.0)
        self.assertEqual(errors, [])

        # 释锁排空后，无需人工二次调用，260301-1 已完成且 dirty 标记已清
        self.assertEqual(items.load(self.cfg())[0].status, "done")
        self.assertIn("- [x] 260301-1", (self.vault / "hub" / "open-items.md").read_text(encoding="utf-8"))
        self.assertFalse(lock.has_dirty(cfg, "mechanical"))

        # (B) routing 并发锁冲突 dirty-flag 自动补发
        (self.fixtures / "route-steward-2026-03-02.json").write_text(
            json.dumps({"__raw__": "收到并审阅"}, ensure_ascii=False), encoding="utf-8"
        )
        add_inbox_entry("demo", "laptop", "工作", "第一份请求", todo="@steward 请审第一份")
        r1_locked = threading.Event()
        r1_release = threading.Event()
        first_only = True

        def hook_route(stage: str) -> None:
            nonlocal first_only
            if stage == "after_route_ckpt" and first_only:
                first_only = False
                r1_locked.set()
                if not r1_release.wait(timeout=5.0):
                    raise TimeoutError("r1_release wait timeout")

        def run_r1() -> None:
            try:
                with mock.patch.object(checkpoint, "_stage_hook", side_effect=hook_route):
                    routing.run(self.cfg(), load_ext(self.cfg()))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        th_r = threading.Thread(target=run_r1, daemon=True)
        th_r.start()
        try:
            self.assertTrue(r1_locked.wait(timeout=5.0))
            add_inbox_entry("demo", "laptop", "工作", "第二份请求", todo="@steward 请审第二份")
            rbusy = routing.run(self.cfg(), load_ext(self.cfg()))
            self.assertEqual(rbusy.get("skipped"), "lock-busy")
            self.assertTrue(rbusy.get("dirty_marked"))
        finally:
            r1_release.set()
            th_r.join(timeout=5.0)
        self.assertEqual(errors, [])

        routed_rows = [json.loads(x) for x in (self.state_dir / "routed.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual([r["result"] for r in routed_rows], ["ok", "ok"])
        self.assertFalse(lock.has_dirty(cfg, "routing"))

        # (C) acquire_or_mark_dirty 双重检查边界
        with lock.runner_lock(cfg, "runner.lock"):
            self.assertIsNone(lock.acquire_or_mark_dirty(cfg, "mechanical", "runner.lock"))
            self.assertTrue(lock.has_dirty(cfg, "mechanical"))
        calls: list[int] = []
        drained = lock.drain_after_unlock(cfg, "mechanical", "runner.lock", lambda: calls.append(1))
        self.assertEqual((drained, len(calls), lock.has_dirty(cfg, "mechanical")), (1, 1, False))

    def test_02_crash_before_cursor_zero_duplicates_and_no_repeat_llm(self) -> None:
        """2. before_cursor 阶段崩溃后重入：不重复调模型，不重复追加更正/归档/候选/画像，日报不双倍计数。"""
        line_ids = self._seed_digest_scenario("2026-03-02")
        cfg = self.cfg()
        brain_cfg.set_brain_routine_enabled(cfg, "daily", True)

        def crash_before_cursor(stage: str) -> None:
            if stage == "before_cursor":
                raise RuntimeError("injected crash before_cursor")

        with mock.patch.object(checkpoint, "_stage_hook", side_effect=crash_before_cursor):
            with self.assertRaises(RuntimeError):
                digest.run_digest(self.cfg(), as_of="2026-03-02")

        # 崩溃后：游标尚未推进，但检查点已落盘且核心 Vault 与 daily json 已写
        self.assertEqual(cursor.load_ids(self.cfg()), set())
        self.assertTrue(checkpoint.has_pending(self.cfg()))
        self.assertIn("水表读数是 102", (self.vault / "hub" / "reports" / "corrections.md").read_text(encoding="utf-8"))

        # 重跑 digest：先恢复检查点再继续；此时游标已含旧行，模型绝不能被重复调用
        ckpt_before = list((self.state_dir / "checkpoints").glob("*.json"))[0]
        crashed_run_id = json.loads(ckpt_before.read_text(encoding="utf-8"))["run_id"]
        with mock.patch("hushai_agent.runner.run_model", side_effect=AssertionError("LLM 不应重复调用")):
            digest.run_digest(self.cfg(), as_of="2026-03-02")

        self.assertFalse(checkpoint.has_pending(self.cfg()))
        self.assertEqual(set(line_ids), cursor.load_ids(self.cfg()))
        corr_txt = (self.vault / "hub" / "reports" / "corrections.md").read_text(encoding="utf-8")
        self.assertEqual(corr_txt.count("水表读数是 102"), 1)
        s_arch_txt = (self.vault / "hub" / "reports" / "status-archive.md").read_text(encoding="utf-8")
        self.assertEqual(s_arch_txt.count("旧总结"), 1)
        c_arch_txt = (self.vault / "hub" / "reports" / "closed-items.md").read_text(encoding="utf-8")
        self.assertEqual(c_arch_txt.count("260201-9"), 1)
        cand_list = candidates.load(self.cfg())
        self.assertEqual(len(cand_list), 1)

        acc = load_daily_accumulated(self.state_dir, "2026-03-02")
        applied_runs = acc.get("applied_runs") or []
        self.assertEqual(applied_runs.count(crashed_run_id), 1)
        self.assertEqual(len(applied_runs), len(set(applied_runs)))
        self.assertEqual(acc["new_lines"], 2)
        self.assertEqual(acc["corrections_added"], 1)
        self.assertEqual(acc["candidates_added"], 1)
        self.assertEqual(acc["by_door"].get("laptop"), 2)
        self.assertEqual(len(acc["created"]), 1)
        self.assertEqual(len(acc["closed"]), 1)

        # candidates.apply_mech / decide 写入 profile.md 时按 ，{cand.id}） 去重防重
        self._write_raw_inbox_line("2026-03-02", "steward", "23:00", "工作", f"批准 {cand_list[0].id} 画像")
        recs = [r for r in iter_inbox_lines(self.vault / "hub" / "inbox") if "批准" in r.did]
        mech = apply_actions(items.load(self.cfg()), recs)
        candidates.apply_mech(self.cfg(), mech, ("steward",))
        candidates.apply_mech(self.cfg(), mech, ("steward",))
        prof_txt = (self.vault / "hub" / "profile.md").read_text(encoding="utf-8")
        self.assertEqual(prof_txt.count(f"，{cand_list[0].id}）"), 1)

    def test_03_tampered_checkpoint_still_verifies_and_repairs_actual_disk_targets(self) -> None:
        """3. 篡改 JSON checkpoint 为 stage=closing_done 与 brain_done=True 时，恢复仍核验并修复真实磁盘文件。"""
        line_ids = self._seed_digest_scenario("2026-03-02")
        brain_cfg.set_brain_routine_enabled(self.cfg(), "daily", True)

        def crash_before_cursor(stage: str) -> None:
            if stage == "before_cursor":
                raise RuntimeError("stop mid-run")

        with mock.patch.object(checkpoint, "_stage_hook", side_effect=crash_before_cursor):
            with self.assertRaises(RuntimeError):
                digest.run_digest(self.cfg(), as_of="2026-03-02")

        ckpt_files = list((self.state_dir / "checkpoints").glob("*.json"))
        self.assertEqual(len(ckpt_files), 1)
        raw_ckpt = json.loads(ckpt_files[0].read_text(encoding="utf-8"))
        # 故意把 JSON checkpoint 伪装成“已全部完成”
        raw_ckpt["stage"] = "closing_done"
        raw_ckpt.setdefault("closing", {})["brain_done"] = True
        ckpt_files[0].write_text(json.dumps(raw_ckpt, ensure_ascii=False, indent=2), encoding="utf-8")

        # 同时破坏多处磁盘目标文件
        (self.vault / "hub" / "open-items.md").write_text("# 损坏覆盖\n", encoding="utf-8")
        (self.vault / "hub" / "reports" / "corrections.md").write_text("", encoding="utf-8")
        (self.vault / "hub" / "reports" / "morning-brief.md").unlink(missing_ok=True)
        (self.vault / "hub" / "reports" / "daily-2026-03-02.md").unlink(missing_ok=True)
        daily_json = self.state_dir / "daily" / "2026-03-02.json"
        if daily_json.is_file():
            daily_json.unlink()
        (self.state_dir / "digested.jsonl").unlink(missing_ok=True)
        b_state = load_brain_state(self.cfg())
        b_state["processed_inbox_ids"] = []
        save_brain_state(self.cfg(), b_state)

        reconcile.run_mechanical_only(self.cfg())

        self.assertFalse(checkpoint.has_pending(self.cfg()))
        self.assertIn("核对海洋词汇", (self.vault / "hub" / "open-items.md").read_text(encoding="utf-8"))
        self.assertIn("水表读数是 102", (self.vault / "hub" / "reports" / "corrections.md").read_text(encoding="utf-8"))
        self.assertTrue((self.vault / "hub" / "reports" / "morning-brief.md").is_file())
        self.assertTrue((self.vault / "hub" / "reports" / "morning-brief.html").is_file())
        self.assertTrue((self.vault / "hub" / "reports" / "daily-2026-03-02.md").is_file())
        self.assertIn(line_ids[0], cursor.load_ids(self.cfg()))
        self.assertEqual(load_daily_accumulated(self.state_dir, "2026-03-02")["corrections_added"], 1)
        self.assertIn(line_ids[0], load_brain_state(self.cfg()).get("processed_inbox_ids", []))

    def test_04_crash_after_cursor_completes_closing_steps_on_reconcile_and_heal(self) -> None:
        """4. 游标推进后、收尾前崩溃（after_cursor）：reconcile 与 heal 均能补全 brief / brain / git commit。"""
        git_vault = self.base / "git_vault"
        init_instance("gitdemo", git_vault, no_git=False)
        subprocess.run(["git", "-C", str(git_vault), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(git_vault), "config", "user.name", "Test"], check=True)

        g_cfg_file = self.base / "config" / "gitdemo" / "config.ini"
        txt = g_cfg_file.read_text(encoding="utf-8").replace(
            "[adapters]",
            f"[adapters]\nfk.type = fake\nfk.fixtures = {self.fixtures}\nfk.light = fake-light",
        ).replace("digest =", "digest = fk:light")
        g_cfg_file.write_text(txt, encoding="utf-8")
        gcfg = load_instance_config("gitdemo")
        brain_cfg.set_brain_routine_enabled(gcfg, "daily", True)

        add_inbox_entry("gitdemo", "laptop", "工作", "人物：王敏｜架构师，今天讨论了测试策略，承诺本周发出计划")
        g_line_id = next(r.line_id for r in iter_inbox_lines(git_vault / "hub" / "inbox"))
        (self.fixtures / "digest-2026-03-02.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "ref": None,
                            "title": "发出测试计划",
                            "owner": "user",
                            "status": "open",
                            "scheduled_date": "2026-03-02",
                            "due_date": "2026-03-02",
                            "domain": "工作",
                            "source_line_ids": [g_line_id],
                        }
                    ],
                    "status_rows": [],
                    "memory_candidates": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # (A) after_cursor 崩溃 -> run_mechanical_only 补全收尾并 git commit
        def crash_after_cursor(stage: str) -> None:
            if stage == "after_cursor":
                raise RuntimeError("crash after_cursor before closing")

        with mock.patch.object(checkpoint, "_stage_hook", side_effect=crash_after_cursor):
            with self.assertRaises(RuntimeError):
                digest.run_digest(load_instance_config("gitdemo"), as_of="2026-03-02")

        self.assertIn(g_line_id, cursor.load_ids(gcfg))
        self.assertTrue(checkpoint.has_pending(gcfg))
        dirty_before = subprocess.run(
            ["git", "-C", str(git_vault), "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertNotEqual(dirty_before, "")

        reconcile.run_mechanical_only(load_instance_config("gitdemo"))
        self.assertFalse(checkpoint.has_pending(gcfg))
        self.assertIn("发出测试计划", (git_vault / "hub" / "reports" / "morning-brief.md").read_text(encoding="utf-8"))
        self.assertIn(g_line_id, load_brain_state(gcfg).get("processed_inbox_ids", []))
        dirty_after = subprocess.run(
            ["git", "-C", str(git_vault), "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(dirty_after, "")

        # (B) 再次在 2026-03-03 after_cursor 崩溃 -> heal.run_heal 自动自愈补全
        os.environ["HUSHAI_NOW"] = "2026-03-03T22:00:00"
        add_inbox_entry("gitdemo", "laptop", "工作", "记录复盘")
        g_line_2 = max(
            (r for r in iter_inbox_lines(git_vault / "hub" / "inbox") if r.did == "记录复盘"),
            key=lambda r: r.line_id,
        ).line_id
        (self.fixtures / "digest-2026-03-03.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "ref": None,
                            "title": "归档复盘报告",
                            "owner": "user",
                            "status": "open",
                            "scheduled_date": "2026-03-03",
                            "due_date": "2026-03-03",
                            "domain": "工作",
                            "source_line_ids": [g_line_2],
                        }
                    ],
                    "status_rows": [],
                    "memory_candidates": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with mock.patch.object(checkpoint, "_stage_hook", side_effect=crash_after_cursor):
            with self.assertRaises(RuntimeError):
                digest.run_digest(load_instance_config("gitdemo"), as_of="2026-03-03")

        hres = heal.run_heal(load_instance_config("gitdemo"))
        self.assertFalse(checkpoint.has_pending(gcfg))
        self.assertGreaterEqual(hres.get("checkpoints_recovered", 0), 1)
        self.assertIn("归档复盘报告", (git_vault / "hub" / "reports" / "morning-brief.md").read_text(encoding="utf-8"))
        dirty_heal = subprocess.run(
            ["git", "-C", str(git_vault), "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(dirty_heal, "")

    def test_05_routing_crash_after_receipt_before_routed_jsonl_no_duplicate_receipt(self) -> None:
        """5. routing 在写出 R-*.md 与管家回执后、routed.jsonl 前崩溃，恢复不重复写回执。"""
        (self.fixtures / "route-steward-2026-03-02.json").write_text(
            json.dumps({"__raw__": "方案可行"}, ensure_ascii=False), encoding="utf-8"
        )
        add_inbox_entry("demo", "laptop", "工作", "提交方案", todo="@steward 请核对方案")

        orig_append = checkpoint_verify.append_line

        def crash_on_routed(path: Path, line: str, roots: list[Path]) -> None:
            if path.name == "routed.jsonl":
                raise RuntimeError("injected crash before routed.jsonl")
            return orig_append(path, line, roots)

        with mock.patch.object(checkpoint_verify, "append_line", side_effect=crash_on_routed):
            with self.assertRaises(RuntimeError):
                routing.run(self.cfg(), load_ext(self.cfg()))

        steward_inbox = (self.vault / "hub" / "inbox" / "2026-03-02-steward.md").read_text(encoding="utf-8")
        self.assertEqual(steward_inbox.count("回复 @laptop："), 1)
        self.assertFalse((self.state_dir / "routed.jsonl").is_file())
        self.assertTrue(checkpoint.has_pending(self.cfg()))

        with mock.patch("hushai_agent.runner.run_model", side_effect=AssertionError("路由恢复不应重调模型")):
            routing.run(self.cfg(), load_ext(self.cfg()))

        steward_after = (self.vault / "hub" / "inbox" / "2026-03-02-steward.md").read_text(encoding="utf-8")
        self.assertEqual(steward_after.count("回复 @laptop："), 1)
        routed = [json.loads(x) for x in (self.state_dir / "routed.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0]["result"], "ok")
        self.assertFalse(checkpoint.has_pending(self.cfg()))

    def test_06_brain_routines_lock_conflict_and_process_source_ref_idempotency(self) -> None:
        """6. 独立 brain_routines 受 runner.lock 互斥保护；brain_process 崩溃重入按 source_ref 去重。"""
        cfg = self.cfg()
        brain_cfg.set_brain_routine_enabled(cfg, "all", True)

        with lock.runner_lock(cfg):
            for fn in (
                lambda: brain_routines.run_daily_brain(cfg, as_of="2026-03-02"),
                lambda: brain_routines.run_weekly_brain(cfg, as_of="2026-03-08"),
                lambda: brain_routines.run_resurface(cfg, as_of="2026-03-02"),
                lambda: brain_routines.run_monthly_maintenance(cfg, as_of="2026-03-31"),
                lambda: brain_process.process_inbox_to_brain(cfg, as_of_date="2026-03-02"),
            ):
                with self.assertRaises(lock.LockBusy):
                    fn()

        # 模拟 brain_process 已写入 sources/ 与 notes/ 但尚未更新 brain-state.json 游标即崩溃
        add_inbox_entry(
            "demo",
            "laptop",
            "学习",
            "来源：https://example.com/always-on 关于持久化检查点的架构实践要点",
        )
        added_line_id = next(r.line_id for r in iter_inbox_lines(self.vault / "hub" / "inbox"))
        with mock.patch.object(
            brain_process, "save_brain_state", side_effect=RuntimeError("crash before brain-state save")
        ):
            with self.assertRaises(RuntimeError):
                brain_process.process_inbox_to_brain(self.cfg(), as_of_date="2026-03-02")

        notes_before = sorted(p.name for p in (self.vault / "hub" / "notes").glob("*.md"))
        sources_before = sorted(p.name for p in (self.vault / "hub" / "sources").glob("*.md"))
        self.assertGreaterEqual(len(notes_before), 1)
        self.assertGreaterEqual(len(sources_before), 1)
        self.assertEqual(load_brain_state(self.cfg()).get("processed_inbox_ids", []), [])

        res2 = brain_process.process_inbox_to_brain(self.cfg(), as_of_date="2026-03-02")
        notes_after = sorted(p.name for p in (self.vault / "hub" / "notes").glob("*.md"))
        sources_after = sorted(p.name for p in (self.vault / "hub" / "sources").glob("*.md"))
        self.assertEqual(notes_after, notes_before)
        self.assertEqual(sources_after, sources_before)
        self.assertEqual(res2.get("filed"), [])
        self.assertIn(added_line_id, load_brain_state(self.cfg()).get("processed_inbox_ids", []))

    def test_07_reconcile_crash_before_cursor_and_monthly_unlock_drain(self) -> None:
        """7. reconcile 在 before_cursor 崩溃后恢复零重复；monthly 持锁释放后自动排空并发机械事件。"""
        (self.vault / "hub" / "open-items.md").write_text(
            "# 开放项\n"
            "- [ ] 260301-1｜交水费｜owner=user｜sched=2026-03-02｜due=-｜domain=生活｜verified=2026-03-01｜src=-\n"
            "- [ ] 260301-2｜写月报｜owner=user｜sched=2026-03-02｜due=-｜domain=工作｜verified=2026-03-01｜src=-\n",
            encoding="utf-8",
        )
        self._write_raw_inbox_line("2026-03-02", "laptop", "10:00", "更正", "电费表号是 8801")
        self._write_raw_inbox_line("2026-03-02", "laptop", "10:05", "生活", "完成 260301-1：已缴")

        with mock.patch.object(
            checkpoint,
            "_stage_hook",
            side_effect=lambda s: (_ for _ in ()).throw(RuntimeError("reconcile crash")) if s == "before_cursor" else None,
        ):
            with self.assertRaises(RuntimeError):
                reconcile.run_mechanical_only(self.cfg())

        self.assertTrue(checkpoint.has_pending(self.cfg()))
        res_rec = reconcile.run_mechanical_only(self.cfg())
        self.assertFalse(checkpoint.has_pending(self.cfg()))
        self.assertEqual(res_rec["processed_lines"], 0)
        corr_txt = (self.vault / "hub" / "reports" / "corrections.md").read_text(encoding="utf-8")
        self.assertEqual(corr_txt.count("电费表号是 8801"), 1)
        self.assertEqual(load_daily_accumulated(self.state_dir, "2026-03-02")["corrections_added"], 1)

        # monthly 持锁期间抵达的机械动作在释锁后自动排空
        brain_cfg.set_brain_routine_enabled(self.cfg(), "monthly", True)
        m_locked = threading.Event()
        m_release = threading.Event()
        orig_find_dup = brain_routines.find_duplicate_notes

        def slow_find_dup(*args, **kwargs):
            m_locked.set()
            if not m_release.wait(timeout=5.0):
                raise TimeoutError("m_release timeout")
            return orig_find_dup(*args, **kwargs)

        errs: list[BaseException] = []

        def run_monthly() -> None:
            try:
                with mock.patch.object(brain_routines, "find_duplicate_notes", side_effect=slow_find_dup):
                    brain_routines.run_monthly_maintenance(self.cfg(), as_of="2026-03-31", confirm_merge=False)
            except BaseException as exc:  # pragma: no cover
                errs.append(exc)

        th = threading.Thread(target=run_monthly, daemon=True)
        th.start()
        try:
            self.assertTrue(m_locked.wait(timeout=5.0))
            self._write_raw_inbox_line("2026-03-02", "laptop", "11:00", "工作", "完成 260301-2：月报已发")
            busy = reconcile.run_mechanical_only(self.cfg())
            self.assertEqual(busy.get("skipped"), "lock-busy")
            self.assertTrue(busy.get("dirty_marked"))
        finally:
            m_release.set()
            th.join(timeout=5.0)
        self.assertEqual(errs, [])
        done_map = {it.id: it.status for it in items.load(self.cfg())}
        self.assertEqual(done_map["260301-2"], "done")
        self.assertFalse(lock.has_dirty(self.cfg(), "mechanical"))

    def test_08_b3_tampered_brain_done_html_corruption_and_append_manifest(self) -> None:
        """8. brain_done=True 篡改 + people/projects/daily/index/html/profile/decisions/lessons 缺失全修复。"""
        cfg = self.cfg()
        brain_cfg.set_brain_routine_enabled(cfg, "daily", True)
        (self.fixtures / "digest-2026-03-02.json").write_text(
            json.dumps({"items": [], "status_rows": [], "today_focus": [], "candidates": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        cand_id = candidates.add(
            cfg,
            ["- 2026-03-02｜偏好｜采用本地检查点引擎｜src=L-20260302-laptop-0"],
            n_user=1,
            as_of="2026-03-02",
            limit=20,
        )
        self.assertGreaterEqual(cand_id[0], 1)
        cid = candidates.load(cfg)[0].id

        self._write_raw_inbox_line("2026-03-02", "steward", "08:30", "工作", f"批准 {cid} 画像")
        self._write_raw_inbox_line("2026-03-02", "steward", "08:31", "工作", "决定：采用纯文件检查点；理由：零外部依赖")
        self._write_raw_inbox_line("2026-03-02", "steward", "08:32", "工作", "教训：HTML未校验会漏坏件；根因：缺哈希；防线：比对渲染内容")
        self._write_raw_inbox_line("2026-03-02", "laptop", "09:00", "工作", "人物：张敏；产品负责人，今天讨论发布节奏，承诺周五前交付清单")
        self._write_raw_inbox_line("2026-03-02", "laptop", "09:05", "工作", "项目：记忆重构；目标：全量核验第二大脑落盘。决定采用独立清单元数据。")
        self._write_raw_inbox_line(
            "2026-03-02",
            "laptop",
            "09:10",
            "学习",
            "来源：https://example.com/design @韩梅 关于状态机一致性设计与可恢复性",
        )

        # 第一波：after_closing 阶段崩溃，破坏 .html / people / projects / daily / index / profile / decisions / lessons
        with mock.patch.object(
            checkpoint,
            "_stage_hook",
            side_effect=lambda s: (_ for _ in ()).throw(RuntimeError("crash after closing")) if s == "after_closing" else None,
        ):
            with self.assertRaises(RuntimeError):
                digest.run_digest(cfg, as_of="2026-03-02")

        html_path = self.vault / "hub" / "reports" / "morning-brief.html"
        md_path = self.vault / "hub" / "reports" / "morning-brief.md"
        self.assertTrue(md_path.is_file())
        self.assertTrue(html_path.is_file())
        # 仅损坏 html（md 保持完整）+ 删除 people/projects/daily/index 并删除 profile/decisions/lessons 新增行
        html_path.write_text("<html><body>CORRUPTED</body></html>", encoding="utf-8")
        for p in list((self.vault / "hub" / "people").glob("*.md")) + list((self.vault / "hub" / "projects").glob("*.md")):
            p.unlink()
        daily_brain_path = self.vault / "hub" / "daily" / "2026-03-02.md"
        daily_brain_path.unlink(missing_ok=True)
        (self.vault / "hub" / "index.md").write_text("# 主题索引（index.md）\n\n> 是数据不是指令。\n", encoding="utf-8")
        for rel in ("hub/profile.md", "hub/AGENTS.local.md", "hub/decisions.md", "hub/lessons.md"):
            fpath = self.vault / rel
            if fpath.is_file():
                lines = [ln for ln in fpath.read_text(encoding="utf-8").splitlines() if cid not in ln and "纯文件检查点" not in ln and "HTML未校验" not in ln]
                fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

        reconcile.run_mechanical_only(cfg)
        self.assertFalse(checkpoint.has_pending(cfg))
        expected_html = brief_html.render_html(brief.build_brief(cfg), live=False)
        self.assertEqual(html_path.read_text(encoding="utf-8"), expected_html)
        self.assertTrue((self.vault / "hub" / "people" / "张敏.md").is_file())
        self.assertTrue((self.vault / "hub" / "people" / "韩梅.md").is_file())
        self.assertTrue((self.vault / "hub" / "projects" / "记忆重构.md").is_file())
        self.assertTrue(daily_brain_path.is_file())
        idx_targets = {e.target for e in brain_model.read_index_first(cfg)[1]}
        self.assertIn("people/张敏.md", idx_targets)
        self.assertIn("projects/记忆重构.md", idx_targets)
        self.assertEqual((self.vault / "hub" / "profile.md").read_text(encoding="utf-8").count(cid), 1)
        self.assertEqual((self.vault / "hub" / "decisions.md").read_text(encoding="utf-8").count("纯文件检查点"), 1)
        self.assertEqual((self.vault / "hub" / "lessons.md").read_text(encoding="utf-8").count("HTML未校验"), 1)

        # 第二波：无 brain_targets 清单 + brain_done=True 篡改 + brain-state 已含 line_id，但磁盘删除 people 与 daily
        self._write_raw_inbox_line("2026-03-02", "laptop", "10:00", "工作", "人物：周强；后端主程，今天对接性能基准，承诺周三提供火焰图")
        rec_id = next(r.line_id for r in iter_inbox_lines(self.vault / "hub" / "inbox") if "周强" in r.did)
        with mock.patch.object(
            checkpoint,
            "_stage_hook",
            side_effect=lambda s: (_ for _ in ()).throw(RuntimeError("crash before brain targets")) if s == "after_vault_writes" else None,
        ):
            with self.assertRaises(RuntimeError):
                digest.run_digest(cfg, as_of="2026-03-02")

        ckpt_file = next((self.state_dir / "checkpoints").glob("*.json"))
        raw = json.loads(ckpt_file.read_text(encoding="utf-8"))
        raw["stage"] = "closing_done"
        raw["closing"]["brain_done"] = True
        raw["closing"]["brain_targets"] = {}
        ckpt_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        # 故意把 line_id 写进 brain-state.json 游标，同时删掉周强文件与 daily
        b_state = load_brain_state(cfg)
        b_state["processed_inbox_ids"] = list(dict.fromkeys([*b_state.get("processed_inbox_ids", []), rec_id]))
        save_brain_state(cfg, b_state)
        (self.vault / "hub" / "people" / "周强.md").unlink(missing_ok=True)
        daily_brain_path.unlink(missing_ok=True)

        reconcile.run_mechanical_only(cfg)
        self.assertTrue((self.vault / "hub" / "people" / "周强.md").is_file())
        self.assertIn(rec_id, (self.vault / "hub" / "people" / "周强.md").read_text(encoding="utf-8"))
        self.assertTrue(daily_brain_path.is_file())
        self.assertIn("people/周强.md", {e.target for e in brain_model.read_index_first(cfg)[1]})

    def test_09_real_subprocess_sigkill_and_people_projects_idempotency(self) -> None:
        """9. 真实子进程 os.kill(SIGKILL) 中断恢复：people/projects/sources/notes 零重复且通过 validate_vault。"""
        cfg = self.cfg()
        brain_cfg.set_brain_routine_enabled(cfg, "all", True)
        (self.fixtures / "digest-2026-03-02.json").write_text(
            json.dumps({"items": [], "status_rows": [], "today_focus": [], "candidates": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        self._write_raw_inbox_line("2026-03-02", "laptop", "09:00", "工作", "人物：赵云；安全工程师，讨论沙箱边界，承诺明天给审计脚本")
        self._write_raw_inbox_line("2026-03-02", "laptop", "09:01", "工作", "项目：零trust沙箱；目标：构建进程级隔离。需要验证信号安全。")
        self._write_raw_inbox_line("2026-03-02", "laptop", "09:02", "学习", "来源：https://example.com/sigkill @陈平 关于强杀恢复设计")

        ready_flag = self.base / "sigkill_ready.flag"
        child_code = (
            "from pathlib import Path\n"
            "import time\n"
            "from unittest import mock\n"
            "from hushai_agent import brain_process\n"
            "from hushai_agent.config import load_instance_config\n"
            f"flag = Path({str(ready_flag)!r})\n"
            "def hang_on_state_save(c, state):\n"
            "    flag.write_text('ready', encoding='utf-8')\n"
            "    time.sleep(30)\n"
            "with mock.patch.object(brain_process, 'save_brain_state', side_effect=hang_on_state_save):\n"
            "    brain_process.process_inbox_to_brain(load_instance_config('demo'), as_of_date='2026-03-02')\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            env={**os.environ, "PYTHONPATH": f"{SRC_DIR}:{TESTS_DIR}"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 8.0
            while not ready_flag.is_file() and time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            if not ready_flag.is_file():
                err_txt = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                self.fail(f"ready_flag not written in time; rc={proc.poll()} stderr={err_txt}")
            os.kill(proc.pid, signal.SIGKILL)
            rc = proc.wait(timeout=5.0)
            self.assertEqual(rc, -signal.SIGKILL)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()

        # 子进程已在写出 people/projects/sources/notes 之后、保存 brain-state.json 之前被 SIGKILL
        people_txt_before = (self.vault / "hub" / "people" / "赵云.md").read_text(encoding="utf-8")
        proj_txt_before = (self.vault / "hub" / "projects" / "零trust沙箱.md").read_text(encoding="utf-8")
        sources_before = sorted(p.name for p in (self.vault / "hub" / "sources").glob("*.md"))
        notes_before = sorted(p.name for p in (self.vault / "hub" / "notes").glob("*.md"))
        self.assertEqual(people_txt_before.count("讨论沙箱边界"), 1)
        self.assertIn("需要验证信号安全", proj_txt_before)
        self.assertEqual(load_brain_state(cfg).get("processed_inbox_ids", []), [])

        # 二次重启恢复：zero duplicates 且游标推进
        res2 = brain_process.process_inbox_to_brain(self.cfg(), as_of_date="2026-03-02")
        self.assertEqual(res2.get("filed"), [])
        self.assertEqual(sorted(p.name for p in (self.vault / "hub" / "sources").glob("*.md")), sources_before)
        self.assertEqual(sorted(p.name for p in (self.vault / "hub" / "notes").glob("*.md")), notes_before)
        self.assertEqual((self.vault / "hub" / "people" / "赵云.md").read_text(encoding="utf-8"), people_txt_before)
        self.assertEqual((self.vault / "hub" / "projects" / "零trust沙箱.md").read_text(encoding="utf-8"), proj_txt_before)
        self.assertEqual(len(load_brain_state(cfg).get("processed_inbox_ids", [])), 3)

        # 第二次真实 SIGKILL：在 digest before_cursor 强杀后由父进程恢复
        self._write_raw_inbox_line("2026-03-02", "laptop", "11:00", "更正", "门禁卡编号是 A77")
        digest_flag = self.base / "digest_sigkill.flag"
        digest_code = (
            "from pathlib import Path\n"
            "import time\n"
            "from unittest import mock\n"
            "from hushai_agent import checkpoint, digest\n"
            "from hushai_agent.config import load_instance_config\n"
            f"flag = Path({str(digest_flag)!r})\n"
            "def hang_hook(stage):\n"
            "    if stage == 'before_cursor':\n"
            "        flag.write_text('ready', encoding='utf-8')\n"
            "        time.sleep(30)\n"
            "with mock.patch.object(checkpoint, '_stage_hook', side_effect=hang_hook):\n"
            "    digest.run_digest(load_instance_config('demo'), as_of='2026-03-02')\n"
        )
        proc2 = subprocess.Popen(
            [sys.executable, "-c", digest_code],
            env={**os.environ, "PYTHONPATH": f"{SRC_DIR}:{TESTS_DIR}"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 8.0
            while not digest_flag.is_file() and time.monotonic() < deadline:
                if proc2.poll() is not None:
                    break
                time.sleep(0.05)
            if not digest_flag.is_file():
                err_txt = proc2.stderr.read().decode(errors="replace") if proc2.stderr else ""
                self.fail(f"digest_flag not written in time; rc={proc2.poll()} stderr={err_txt}")
            os.kill(proc2.pid, signal.SIGKILL)
            self.assertEqual(proc2.wait(timeout=5.0), -signal.SIGKILL)
        finally:
            if proc2.poll() is None:
                proc2.kill()
                proc2.wait(timeout=5.0)
            for pipe in (proc2.stdout, proc2.stderr):
                if pipe is not None:
                    pipe.close()

        self.assertTrue(checkpoint.has_pending(self.cfg()))
        reconcile.run_mechanical_only(self.cfg())
        self.assertFalse(checkpoint.has_pending(self.cfg()))
        self.assertEqual((self.vault / "hub" / "reports" / "corrections.md").read_text(encoding="utf-8").count("门禁卡编号是 A77"), 1)
        self.assertTrue(validate.validate_vault(self.vault)["ok"])

    def test_10_corrupt_checkpoint_quarantine_and_multiprocess_contention(self) -> None:
        """10. 损坏 checkpoint 隔离到 corrupt-* 不阻塞后续运行；多进程并发竞争零丢失零重复。"""
        cfg = self.cfg()
        cdir = checkpoint.checkpoints_dir(cfg)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "001-bad-json.json").write_text("{corrupt-json", encoding="utf-8")
        (cdir / "002-bad-type.json").write_text("[1, 2, 3]", encoding="utf-8")
        (cdir / "003-bad-schema.json").write_text(
            json.dumps({"kind": "digest", "run_id": "x", "as_of": "2026-03-02", "atomic_targets": "not-a-dict"}),
            encoding="utf-8",
        )
        (cdir / "route-L-bad.json").write_text(json.dumps({"line_id": "L-bad"}), encoding="utf-8")

        rec_res = checkpoint.recover_pending(cfg)
        route_res = checkpoint.recover_pending_routes(cfg)
        self.assertEqual(rec_res["skipped_invalid"], 3)
        self.assertEqual(route_res, 0)
        self.assertFalse(checkpoint.has_pending(cfg))
        quarantined = sorted(p.name for p in cdir.glob("corrupt-*.bad"))
        self.assertEqual(len(quarantined), 4)

        # 多进程高竞争压测：reconcile / digest / routing / heal 同刻在 4 个独立 OS 进程启动竞争锁
        (self.vault / "hub" / "open-items.md").write_text(
            "# 开放项\n"
            "- [ ] 260301-1｜并发办结任务｜owner=user｜sched=2026-03-02｜due=-｜domain=工作｜verified=2026-03-01｜src=-\n",
            encoding="utf-8",
        )
        (self.fixtures / "route-steward-2026-03-02.json").write_text(
            json.dumps({"__raw__": "并发路由已收到"}, ensure_ascii=False), encoding="utf-8"
        )
        self._write_raw_inbox_line("2026-03-02", "laptop", "12:00", "工作", "完成 260301-1：已交付")
        self._write_raw_inbox_line("2026-03-02", "laptop", "12:01", "更正", "并发压测记录唯一")
        self._write_raw_inbox_line("2026-03-02", "laptop", "12:02", "工作", "发出并发路由", todo="@steward 请确认并发路由回复")
        self._write_raw_inbox_line("2026-03-02", "laptop", "12:03", "工作", "复盘多进程并发文件锁与脏标记协同设计")

        (self.fixtures / "digest-2026-03-02.json").write_text(
            json.dumps({"items": [], "status_rows": [], "today_focus": [], "candidates": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        go_gate = self.base / "mp_go.gate"
        model_calls_log = self.base / "model_calls.log"
        wait_py = (
            "import time\nfrom pathlib import Path\n"
            f"gate = Path({str(go_gate)!r})\n"
            "deadline = time.monotonic() + 8.0\n"
            "while not gate.is_file() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
        )
        scripts = [
            wait_py + "from hushai_agent import reconcile\nreconcile.run_mechanical_only('demo')\n",
            wait_py + (
                "from pathlib import Path\nfrom unittest import mock\n"
                "from hushai_agent import digest, lock, runner\n"
                f"log = Path({str(model_calls_log)!r})\n"
                "orig_run = runner.run_model\n"
                "def counted_run(*args, **kwargs):\n"
                "    with open(log, 'a', encoding='utf-8') as f: f.write('1\\n')\n"
                "    return orig_run(*args, **kwargs)\n"
                "try:\n"
                "    with mock.patch('hushai_agent.digest.runner.run_model', side_effect=counted_run):\n"
                "        digest.run_digest('demo', as_of='2026-03-02')\n"
                "except lock.LockBusy:\n"
                "    pass\n"
            ),
            wait_py + (
                "from hushai_agent import routing\nfrom hushai_agent.config import load_instance_config\n"
                "from hushai_agent.config_ext import load_ext\n"
                "c = load_instance_config('demo')\nrouting.run(c, load_ext(c))\n"
            ),
            wait_py + "from hushai_agent import heal, lock\ntry:\n    heal.run_heal('demo')\nexcept lock.LockBusy:\n    pass\n",
        ]
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code],
                env={**os.environ, "PYTHONPATH": f"{SRC_DIR}:{TESTS_DIR}", "HUSHAI_LOCK_WAIT": "0.1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for code in scripts
        ]
        try:
            time.sleep(0.15)
            go_gate.write_text("go", encoding="utf-8")
            for p in procs:
                out, err = p.communicate(timeout=15.0)
                self.assertEqual(p.returncode, 0, msg=f"worker failed rc={p.returncode}\nstdout={out.decode()}\nstderr={err.decode()}")
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=5.0)
                for pipe in (p.stdout, p.stderr):
                    if pipe is not None:
                        pipe.close()

        # 排空任何锁冲突期间留下的 dirty 标记后再补一次干净汇总
        reconcile.run_mechanical_only(self.cfg())
        routing.run(self.cfg(), load_ext(self.cfg()))
        self.assertFalse(checkpoint.has_pending(self.cfg()))
        self.assertEqual((self.vault / "hub" / "reports" / "corrections.md").read_text(encoding="utf-8").count("并发压测记录唯一"), 1)
        done_map = {it.id: it.status for it in items.load(self.cfg())}
        self.assertEqual(done_map["260301-1"], "done")
        routed_lines = [ln for ln in (self.state_dir / "routed.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(routed_lines), 1)
        calls = len(model_calls_log.read_text(encoding="utf-8").splitlines()) if model_calls_log.is_file() else 0
        self.assertLessEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
