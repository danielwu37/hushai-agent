"""第二大脑（LLM Wiki 式）：原料收集、编译增改、隐私、锁定页、重放幂等、单独预算、体检、检索、简报、CLI。"""

from __future__ import annotations

import datetime
import json
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

from hushai_agent import (  # noqa: E402
    brain_ask,
    brain_cfg,
    brain_ingest,
    brain_lint,
    brain_raw,
    brain_write,
    brief,
    ledger,
    validate,
)
from hushai_agent.brain_page import parse_page, render_page  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.config_edit import apply_config_updates  # noqa: E402
from hushai_agent.vault import add_inbox_entry, init_instance  # noqa: E402
from m3a_stubs import run_hushai_cli  # noqa: E402

DAY = "2026-11-17"


class TestBrainWiki(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_MODEL_LOCK_WAIT"] = "0"
        os.environ["HUSHAI_NOW"] = f"{DAY}T22:30:00"
        self.vault = self.base / "vault"
        self.fixtures = self.base / "fixtures"
        self.fixtures.mkdir()
        init_instance("demo", self.vault, timezone="Asia/Shanghai", no_git=True)
        cfg_file = self.base / "config" / "demo" / "config.ini"
        txt = cfg_file.read_text(encoding="utf-8").replace(
            "[adapters]", f"[adapters]\nfk.type = fake\nfk.fixtures = {self.fixtures}\nfk.light = fake-light"
        ).replace("[jobs]", "[jobs]\nbrain = fk:light")
        cfg_file.write_text(txt, encoding="utf-8")
        apply_config_updates("demo", {"domains": {"names": "工作,生活,健康"}})
        brain_cfg.set_enabled(self.cfg(), True, since=DAY)
        self.hub = self.vault / "hub"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def cfg(self):
        return load_instance_config("demo")

    def fixture(self, data: dict) -> None:
        (self.fixtures / f"brain-{DAY}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_page_roundtrip_keeps_user_section_and_refs(self) -> None:
        text = ("---\ntype: topic\ntitle: 中转住宿\naliases: [免费住宿, 中转]\nlocked: true\n---\n\n正文 [[张三]]\n\n"
                "## 来源\n- 收件 a\n- 收件 b\n\n## 我的笔记\n\n我自己写的\n- 列表也保留\n")
        page = parse_page("notes/中转住宿.md", text)
        self.assertEqual(page.meta["aliases"], ["免费住宿", "中转"])
        self.assertIs(page.meta["locked"], True)
        self.assertEqual(page.refs, ["收件 a", "收件 b"])
        self.assertIn("- 列表也保留", page.user)
        again = parse_page(page.rel, render_page(page))
        self.assertEqual((again.body, again.refs, again.user, again.meta), (page.body, page.refs, page.user, page.meta))

    def test_raw_collects_only_explicit_prefixes_and_respects_privacy_and_drop_rules(self) -> None:
        add_inbox_entry("demo", "steward", "工作", "笔记：中转住宿改为一季度")
        add_inbox_entry("demo", "steward", "工作", "普通流水不入脑")
        add_inbox_entry("demo", "steward", "健康", "笔记：体检复查")
        add_inbox_entry("demo", "laptop", "生活", "人物：王老师，双胞胎数学老师")
        drop = self.hub / "exchange" / "in" / "入脑"
        drop.mkdir(parents=True)
        (drop / "文章.md").write_text("# 文章\n正文", encoding="utf-8")
        (drop / "大文件.md").write_text("x" * (brain_raw.FILE_MAX_BYTES + 1), encoding="utf-8")
        (self.hub / "exchange" / "in" / "muse").mkdir(parents=True)
        (self.hub / "exchange" / "in" / "muse" / "入脑-笔记.md").write_text("muse 投放", encoding="utf-8")
        (self.hub / "exchange" / "in" / "muse" / "别的.md").write_text("不收", encoding="utf-8")
        batches, skipped = brain_raw.build_batches(self.cfg(), brain_cfg.default_state(), DAY)
        self.assertEqual([b.kind for b in batches], ["lines", "file", "file"])
        self.assertEqual(len(batches[0].refs), 2)
        self.assertNotIn("体检", batches[0].text)
        self.assertEqual({b.file_rel for b in batches[1:]}, {"exchange/in/入脑/文章.md", "exchange/in/muse/入脑-笔记.md"})
        self.assertTrue(any("大文件" in s for s in skipped))
        self.assertEqual(brain_raw.build_batches(self.cfg(), brain_cfg.default_state(), "2026-11-18")[0][0].kind, "file")

    def test_ingest_creates_updates_merges_and_guards(self) -> None:
        add_inbox_entry("demo", "steward", "工作", "项目：中转住宿政策申请改为 2027 年一季度")
        self.fixture({"pages": [
            {"title": "中转住宿政策申请", "type": "project", "domain": "工作", "aliases": ["中转住宿"],
             "summary": "改为一季度", "body": "申请改为 2027 年一季度。\n\n## 来源\n- 模型乱写", "refs": [1]},
            {"title": "体检", "type": "topic", "domain": "健康", "summary": "x", "body": "不该出现", "refs": [1]},
            {"title": "无出处", "type": "topic", "domain": "工作", "summary": "x", "body": "没有 refs", "refs": []},
        ], "log": "新建中转住宿"})
        res = brain_ingest.run_ingest(self.cfg())
        self.assertEqual((res["done"], res["created"]), (1, ["中转住宿政策申请"]))
        page_path = self.hub / "projects" / "中转住宿政策申请.md"
        page = parse_page("projects/中转住宿政策申请.md", page_path.read_text(encoding="utf-8"))
        self.assertEqual(page.meta["status"], "有出处")
        self.assertNotIn("模型乱写", page.body)
        self.assertTrue(page.refs[0].startswith(f"收件 {DAY} steward"))
        self.assertFalse((self.hub / "notes" / "体检.md").exists())
        self.assertFalse((self.hub / "notes" / "无出处.md").exists())
        self.assertIn("[[中转住宿政策申请]]", (self.hub / "index.md").read_text(encoding="utf-8"))
        self.assertIn("新建 [[中转住宿政策申请]]", (self.hub / "daily" / f"{DAY}.md").read_text(encoding="utf-8"))

        # 使用者手写区与锁定页
        page_path.write_text(page_path.read_text(encoding="utf-8") + "\n我的批注\n", encoding="utf-8")
        (self.hub / "people" / "王老师.md").write_text("---\ntype: person\ntitle: 王老师\nlocked: true\n---\n\n原文\n", encoding="utf-8")
        add_inbox_entry("demo", "steward", "工作", "笔记：中转住宿测算人数以 11110 为准")
        self.fixture({"pages": [
            {"title": "中转住宿", "type": "project", "domain": "工作", "summary": "一季度，人数 11110",
             "body": "申请改为 2027 年一季度，测算人数 11110。", "refs": [1]},
            {"title": "王老师", "type": "person", "domain": "工作", "summary": "x", "body": "被改了", "refs": [1]},
        ], "log": "更新"})
        res2 = brain_ingest.run_ingest(self.cfg())
        self.assertEqual((res2["created"], res2["updated"], res2["locked"]), ([], ["中转住宿政策申请"], ["王老师"]))
        text = page_path.read_text(encoding="utf-8")
        self.assertIn("11110", text)
        self.assertIn("我的批注", text)
        self.assertEqual(text.count(f"收件 {DAY} steward"), 2)
        self.assertIn("原文", (self.hub / "people" / "王老师.md").read_text(encoding="utf-8"))
        self.assertEqual(brain_ingest.run_ingest(self.cfg())["pending"], 0)

    def test_file_source_page_links_and_crash_replay_is_idempotent(self) -> None:
        drop = self.hub / "exchange" / "in" / "入脑"
        drop.mkdir(parents=True)
        (drop / "llm-wiki.md").write_text("卡帕西：让模型维护一个 Markdown 维基。", encoding="utf-8")
        self.fixture({"pages": [{"title": "LLM Wiki", "type": "topic", "domain": "工作", "summary": "模型维护的维基",
                                 "body": "由模型维护的互链笔记。"}],
                      "source": {"title": "卡帕西 LLM Wiki", "summary": "一页摘要", "body": "### 要点\n- 编译而非检索"},
                      "log": "读文章"})
        with mock.patch.object(brain_ingest, "save_state", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                brain_ingest.run_ingest(self.cfg())
        res = brain_ingest.run_ingest(self.cfg())
        self.assertEqual(res["updated"], ["LLM Wiki", "来源-卡帕西 LLM Wiki"])
        topic = (self.hub / "notes" / "LLM Wiki.md").read_text(encoding="utf-8")
        self.assertEqual(topic.count("[[来源-卡帕西 LLM Wiki]]"), 1)
        src = (self.hub / "sources" / "来源-卡帕西 LLM Wiki.md").read_text(encoding="utf-8")
        self.assertIn("涉及：[[LLM Wiki]]", src)
        self.assertIn("原件 exchange/in/入脑/llm-wiki.md", src)
        self.assertEqual(brain_lint.lint(self.cfg())["broken_links"], [])

    def test_budget_is_separate_and_failures_keep_material(self) -> None:
        add_inbox_entry("demo", "steward", "工作", "笔记：一条")
        self.fixture({"__fail__": "boom"})
        res = brain_ingest.run_ingest(self.cfg())
        self.assertEqual((res["done"], res["degraded"]), (0, "all-failed"))
        self.assertEqual(ledger.calls_today(self.cfg(), only_job="brain"), 1)
        self.assertEqual(ledger.calls_today(self.cfg(), exclude_job=ledger.SEPARATE_BUDGET_JOBS), 0)
        self.assertEqual(len(brain_raw.pending_lines(self.cfg(), brain_cfg.load_state(self.cfg()), DAY)), 1)
        brain_cfg.set_enabled(self.cfg(), False)
        self.assertEqual(brain_ingest.run_ingest(self.cfg()), {"skipped": "disabled"})
        dry = brain_ingest.run_ingest(self.cfg(), force=True, dry_run=True)
        self.assertEqual(len(dry["prompts"]), 1)
        self.assertFalse((self.hub / "daily" / f"{DAY}.md").exists())

    def test_lint_ask_brief_and_validate(self) -> None:
        (self.hub / "projects" / "资讯中枢.md").write_text(
            "---\ntype: project\ntitle: 资讯中枢\naliases: [news]\nsummary: 自建订阅与 AI 打分\nupdated: 2026-08-01\n---\n\n"
            "订阅源每天打分，见 [[打分口味]] 与 [[不存在]]。\n\n## 来源\n- x\n", encoding="utf-8")
        (self.hub / "notes" / "打分口味.md").write_text(
            f"---\ntype: topic\ntitle: 打分口味\nsummary: 偏深度\nupdated: {DAY}\n---\n\n偏深度分析。\n\n## 来源\n- y\n", encoding="utf-8")
        (self.hub / "notes" / "news.md").write_text("---\ntype: topic\ntitle: news\n---\n\n重名\n", encoding="utf-8")
        (self.hub / "notes" / "私事.md").write_text("---\ntype: topic\ntitle: 私事\ndomain: 健康\n---\n\nx\n", encoding="utf-8")
        rep = brain_lint.lint(self.cfg())
        self.assertEqual(rep["broken_links"], ["资讯中枢 -> [[不存在]]"])
        self.assertEqual(len(rep["duplicates"]), 1)
        self.assertEqual(rep["private"], ["notes/私事.md"])
        self.assertIn("资讯中枢", rep["stale"])
        self.assertEqual(rep["level"], "FAIL")
        self.assertEqual(brain_lint.check_item(self.cfg()).level, "FAIL")
        self.assertEqual(validate.check_brain(self.vault).level, "WARN")

        ans = brain_ask.ask(self.cfg(), "资讯打分怎么做？")
        self.assertEqual(ans["hits"][0]["title"], "资讯中枢")
        self.assertEqual(ans["hits"][1]["title"], "打分口味")
        sec = brain_ask.brief_section(self.cfg(), datetime.date.fromisoformat(DAY))
        self.assertIn("打分口味", sec["fresh"])
        self.assertEqual(sec["revisit"]["title"], "资讯中枢")
        self.assertIn("## 第二大脑", brief.render_md(brief.build_brief(self.cfg())))

    def test_cli_add_verify_status_and_units(self) -> None:
        src = self.base / "a.md"
        src.write_text("文章", encoding="utf-8")
        out = run_hushai_cli(SRC_DIR, "brain", "add", str(src), "--name", "好文", "--instance", "demo", "--json")
        self.assertEqual(json.loads(out.stdout)["added"], "hub/exchange/in/入脑/好文.md")
        (self.hub / "notes" / "页.md").write_text("---\ntype: topic\ntitle: 页\naliases: [别名]\n---\n\nx\n", encoding="utf-8")
        out = run_hushai_cli(SRC_DIR, "brain", "verify", "别名", "--instance", "demo")
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        self.assertIn("status: 已核实", (self.hub / "notes" / "页.md").read_text(encoding="utf-8"))
        st = json.loads(run_hushai_cli(SRC_DIR, "brain", "status", "--instance", "demo", "--json").stdout)
        self.assertEqual((st["enabled"], st["since"], st["pending_files"]), (True, DAY, 1))
        units = self.base / "units"
        units.mkdir()
        out = run_hushai_cli(SRC_DIR, "brain", "units", "--out", str(units), "--instance", "demo")
        self.assertEqual(out.returncode, 0, msg=out.stderr)
        self.assertIn("22:30:00 Asia/Shanghai", (units / "hushai-demo-brain.timer").read_text(encoding="utf-8"))

    def test_index_respects_line_limit(self) -> None:
        pages = [parse_page(f"notes/t{i}.md", f"---\ntype: topic\ntitle: t{i}\nupdated: 2026-11-{i % 28 + 1:02d}\n---\n")
                 for i in range(60)]
        text = brain_write.render_index(pages, 30, "now")
        self.assertLessEqual(len(text.splitlines()), 30)
        self.assertIn("…其余", text)


if __name__ == "__main__":
    unittest.main()
