"""M3c 单元测试：候选记忆闭环、带推荐的待定事项、晨间简报、待办页面。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
for p in (str(SRC_DIR), str(TESTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from hushai_agent import brief, candidates, digest, items, mechanical  # noqa: E402
from hushai_agent.brief_html import render_html  # noqa: E402
from hushai_agent.config import load_instance_config  # noqa: E402
from hushai_agent.todo import run_todo_action  # noqa: E402
from hushai_agent.validate import validate_vault  # noqa: E402
from hushai_agent.vault import add_inbox_entry, init_instance  # noqa: E402
from m3a_stubs import run_hushai_cli  # noqa: E402

XSS = "<script>alert(1)</script>"


def item_line(item_id: str, title: str, owner: str = "user", extra: str = "") -> str:
    return f"- [ ] {item_id}｜{title}｜owner={owner}｜sched=-｜due=-｜domain=工作｜verified=2026-03-01{extra}｜src=-\n"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = os.environ.copy()
        os.environ["HUSHAI_CONFIG_ROOT"] = str(self.base / "config")
        os.environ["HUSHAI_STATE_ROOT"] = str(self.base / "state")
        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        self.vault = self.base / "vault"
        init_instance("demo", self.vault, no_git=True)
        self.cfg_file = self.base / "config" / "demo" / "config.ini"
        self.fixtures = self.base / "fixtures"
        self.fixtures.mkdir()
        self.edit_config("[adapters]", f"[adapters]\nfk.type = fake\nfk.fixtures = {self.fixtures}")
        self.edit_config("digest =", "digest = fk:light")
        self.hub = self.vault / "hub"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def cfg(self):
        return load_instance_config("demo")

    def edit_config(self, old: str, new: str) -> None:
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    def write_items(self, *lines: str) -> None:
        (self.hub / "open-items.md").write_text("# 开放项\n" + "".join(lines), encoding="utf-8")

    def write_today(self, *ids_titles: tuple[str, str]) -> None:
        body = "".join(f"- [ ] {n}. {t}（{i}）\n" for n, (i, t) in enumerate(ids_titles, start=1))
        (self.hub / "today.md").write_text("# 今日待办 2026-03-03\n" + body, encoding="utf-8")

    def cand_text(self) -> str:
        return (self.hub / "reports" / "candidates.md").read_text(encoding="utf-8")

    def say(self, did: str, door: str = "steward", domain: str = "工作") -> None:
        add_inbox_entry("demo", door, domain, did)

    def fixture(self, day: str, payload: dict) -> None:
        (self.fixtures / f"digest-{day}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestCandidates(Base):
    def test_migration_ids_and_validate(self) -> None:
        path = self.hub / "reports" / "candidates.md"
        path.write_text(
            "- 2026-03-01｜inbox｜旧格式一｜src=L-1\n- 2026-03-01｜insight｜旧格式二｜src=L-2\n"
            "- [-] C-260301-7｜inbox｜已丢弃的｜added=2026-03-01｜closed=2026-03-02｜src=-\n", encoding="utf-8")
        cands = candidates.load(self.cfg())
        self.assertEqual([c.id for c in cands], ["C-260301-1", "C-260301-2", "C-260301-7"])
        self.assertEqual([c.status for c in cands], ["pending", "pending", "discarded"])
        # 编号不复用：同一天再加，接在最大号后面
        self.say("交稿前先朗读一遍", domain="候选记忆")
        os.environ["HUSHAI_NOW"] = "2026-03-01T10:00:00"
        candidates.add(self.cfg(), ["- 2026-03-01｜inbox｜新来的｜src=L-9"], 1, "2026-03-01", 20)
        ids = [c.id for c in candidates.load(self.cfg())]
        self.assertIn("C-260301-8", ids)
        self.assertIn("C-260302-1", ids)
        self.assertIn("- [ ] C-260301-1｜inbox｜旧格式一｜added=2026-03-01｜src=L-1", self.cand_text())
        self.assertTrue(validate_vault(self.vault)["ok"])
        with path.open("a", encoding="utf-8") as fh:
            fh.write("- [ ] 坏行没有编号\n")
        report = validate_vault(self.vault)
        self.assertEqual([c["level"] for c in report["checks"] if c["id"] == "candidates"], ["FAIL"])
        candidates.save(self.cfg(), candidates.load(self.cfg()))
        self.assertIn("坏行没有编号", self.cand_text())  # 解析不了的行不丢

    def test_pending_limit(self) -> None:
        self.cfg_file.write_text(self.cfg_file.read_text(encoding="utf-8").replace("[limits]", "[limits]\ncandidates_pending = 1"), encoding="utf-8")
        os.environ["HUSHAI_NOW"] = "2026-03-02T09:00:00"
        add_inbox_entry("demo", "laptop", "工作", "聊了写作习惯")
        add_inbox_entry("demo", "laptop", "候选记忆", "使用者自己写的第一条")
        add_inbox_entry("demo", "laptop", "候选记忆", "使用者自己写的第二条")
        self.fixture("2026-03-02", {"items": [], "status_rows": [], "memory_candidates": [
            {"kind": "insight", "text": "模型给的候选", "source_line_ids": ["2026-03-02-laptop#1"]}]})
        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        summary = digest.run_digest(self.cfg(), "2026-03-02")
        self.assertEqual(summary["candidates_skipped"], 1)
        texts = [c.text for c in candidates.load(self.cfg())]
        self.assertEqual(texts, ["使用者自己写的第一条", "使用者自己写的第二条"])
        daily = (self.hub / "reports" / "daily-2026-03-02.md").read_text(encoding="utf-8")
        self.assertIn("候选记忆待批已满 1 条，新的没有加入", daily)

    def test_approve_discard_and_rights(self) -> None:
        for text in ("记入画像的", "记入规则的", "要丢弃的", "无权端想批的"):
            self.say(text, domain="候选记忆")
        ids = [c.id for c in candidates.load(self.cfg())]
        self.assertEqual(ids, ["C-260302-1", "C-260302-2", "C-260302-3", "C-260302-4"])
        profile_before = (self.hub / "profile.md").read_text(encoding="utf-8")
        self.say("批准 C-260302-4 画像", door="laptop")
        self.assertEqual((self.hub / "profile.md").read_text(encoding="utf-8"), profile_before)
        self.say("批准 C-260302-1 画像")
        self.say("批准 C-260302-2 规则", door="web")
        self.say("丢弃 C-260302-3")
        self.say("批准 C-260302-1 画像")  # 重复
        self.say("批准 C-260399-1 画像")  # 不存在
        profile = (self.hub / "profile.md").read_text(encoding="utf-8")
        self.assertTrue(profile.startswith(profile_before.rstrip("\n")))
        self.assertTrue(profile.rstrip().endswith("## 已批准的记忆\n\n- 记入画像的（2026-03-02 批准，C-260302-1）"))
        rules = (self.hub / "AGENTS.local.md").read_text(encoding="utf-8")
        self.assertIn("## 已批准的补充规则\n\n- 记入规则的（2026-03-02 批准，C-260302-2）", rules)
        text = self.cand_text()
        self.assertIn("- [x] C-260302-1｜inbox｜记入画像的｜added=2026-03-02｜to=profile｜closed=2026-03-02", text)
        self.assertIn("- [x] C-260302-2", text)
        self.assertIn("- [-] C-260302-3", text)
        self.assertIn("- [ ] C-260302-4", text)
        daily = (self.hub / "reports" / "daily-2026-03-02.md").read_text(encoding="utf-8")
        self.assertIn("该端无权批准", daily)
        self.assertIn("该候选已处理过", daily)
        self.assertIn("编号不存在", daily)
        self.assertEqual(mechanical.parse_action("approve c-260302-1 rules").note, "rules")
        self.assertIsNone(mechanical.parse_action("批准 C-260302-1 别处"))

    def test_candidate_cli(self) -> None:
        self.say("第一条", domain="候选记忆")
        self.say("第二条", domain="候选记忆")
        rows = json.loads(run_hushai_cli(SRC_DIR, "candidate", "list", "--instance", "demo", "--json").stdout)
        self.assertEqual([r["id"] for r in rows], ["C-260302-1", "C-260302-2"])
        ok = run_hushai_cli(SRC_DIR, "candidate", "approve", "C-260302-1", "--to", "profile", "--instance", "demo")
        self.assertEqual(ok.returncode, 0, msg=ok.stderr)
        self.assertIn("第一条", (self.hub / "profile.md").read_text(encoding="utf-8"))
        self.assertEqual(run_hushai_cli(SRC_DIR, "candidate", "discard", "C-260302-2", "--instance", "demo").returncode, 0)
        for bad in (("approve", "C-260302-1", "--to", "rules"), ("discard", "C-260302-9"), ("approve", "C-260302-2"),
                    ("discard", "C-260302-2", "--door", "laptop")):
            self.assertEqual(run_hushai_cli(SRC_DIR, "candidate", *bad, "--instance", "demo").returncode, 1, msg=bad)
        self.assertEqual(candidates.pending(candidates.load(self.cfg())), [])


class TestRecommendation(Base):
    def test_rec_roundtrip_and_accept(self) -> None:
        os.environ["HUSHAI_NOW"] = "2026-03-02T09:00:00"
        add_inbox_entry("demo", "laptop", "工作", "两家印厂报价差不多，甲家交期更稳")
        add_inbox_entry("demo", "laptop", "工作", "要不要续订数据库还没想好")
        self.fixture("2026-03-02", {"status_rows": [], "memory_candidates": [], "items": [
            {"ref": None, "title": "选印厂", "owner": "user_decision", "status": "open", "scheduled_date": None,
             "due_date": None, "domain": "工作", "recommendation": "选甲家｜交期稳", "source_line_ids": ["2026-03-02-laptop#1"]},
            {"ref": None, "title": "是否续订数据库", "owner": "user_decision", "status": "open", "scheduled_date": None,
             "due_date": None, "domain": "工作", "recommendation": None, "source_line_ids": ["2026-03-02-laptop#2"]}]})
        os.environ["HUSHAI_NOW"] = "2026-03-02T22:00:00"
        digest.run_digest(self.cfg(), "2026-03-02")
        text = (self.hub / "open-items.md").read_text(encoding="utf-8")
        self.assertIn("rec=选甲家|交期稳｜src=", text)
        by_title = {it.title: it for it in items.load(self.cfg())}
        self.assertEqual((by_title["选印厂"].rec, by_title["是否续订数据库"].rec), ("选甲家|交期稳", ""))
        with self.assertRaises(ValueError):
            run_todo_action("demo", "accept", by_title["是否续订数据库"].id)
        proc = run_hushai_cli(SRC_DIR, "todo", "accept", by_title["选印厂"].id, "--instance", "demo")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        inbox = (self.hub / "inbox" / "2026-03-02-steward.md").read_text(encoding="utf-8")
        self.assertIn(f"完成 {by_title['选印厂'].id}：按推荐，选甲家|交期稳", inbox)
        self.assertEqual({it.title: it.status for it in items.load(self.cfg())}["选印厂"], "done")


class TestBrief(Base):
    def seed(self) -> None:
        self.write_items(
            item_line("260301-1", "交水费"), item_line("260301-2", XSS),
            item_line("260301-3", "选印厂", "user_decision", "｜rec=选甲家"),
            *[item_line(f"260301-{n}", f"待定{n}", "user_decision") for n in range(4, 10)],
            item_line("260301-10", "拖了很久的事", extra="｜defers=3"),
            item_line("260301-11", "周报说办了的事"), item_line("260301-12", "给模型的活", "ai", "｜defers=5"),
        )
        self.write_today(("260301-1", "交水费"), ("260301-2", "x"), ("260301-3", "选印厂"))
        for n in range(1, 5):
            self.say(f"候选{n}", domain="候选记忆")
        (self.hub / "reports" / "weekly-2026-03-01.md").write_text(
            "# 周报\n\n## 已办未销\n- 260301-11｜收件里说已寄出｜src=L-1\n- 260399-1｜不存在的｜src=L-2\n\n## 互相矛盾\n- 260301-1｜不该算｜src=L-3\n", encoding="utf-8")

    def test_build_limits_and_empty(self) -> None:
        empty = brief.build_brief(self.cfg())
        self.assertEqual([empty[k] for k in ("todos", "decisions", "candidates", "suspects")], [[], [], [], []])
        md, html = brief.render_md(empty), render_html(empty, live=False)
        for word in ("要我办", "要我定", "候选记忆", "疑似已办", "怎么回写"):
            self.assertNotIn(word, md)
            self.assertNotIn(f"<h2>{word}", html)
        self.seed()
        data = brief.build_brief(self.cfg())
        self.assertEqual([t["id"] for t in data["todos"]], ["260301-1", "260301-2"])
        self.assertEqual(len(data["decisions"]), 5)
        self.assertEqual(data["decisions"][0], {"id": "260301-3", "title": "选印厂", "rec": "选甲家", "state": "open"})
        self.assertEqual([c["id"] for c in data["candidates"]], ["C-260302-1", "C-260302-2", "C-260302-3"])
        self.assertEqual([(s["id"], s["reason"]) for s in data["suspects"]],
                         [("260301-11", "收件里说已寄出"), ("260301-10", "已推迟 3 次")])
        os.environ["HUSHAI_NOW"] = "2026-03-04T08:00:00"  # 周报超过 2 天不取
        self.assertEqual([s["id"] for s in brief.build_brief(self.cfg())["suspects"]], ["260301-10"])

    def test_render_and_refresh(self) -> None:
        self.seed()
        data = brief.build_brief(self.cfg())
        for live in (False, True):
            html = render_html(data, live=live)
            for banned in ("http://", "https://", "<link", "src="):
                self.assertNotIn(banned, html)
            self.assertNotIn(XSS, html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
            self.assertIn("全部按推荐确认", html)
        snap = render_html(data, live=False)
        self.assertEqual(snap.count("<button"), snap.count(" disabled>"))
        self.assertIn("只读快照", snap)
        self.assertNotIn("<script>", snap)
        self.assertNotIn(" disabled>", render_html(data, live=True))
        md = brief.render_md(data)
        self.assertIn("## 怎么回写", md)
        for way in ("完成 260301-1", "推迟 260301-1 到 2026-03-10", "放弃 260301-1", "批准 C-260302-1 画像", "丢弃 C-260302-1"):
            self.assertIn(way, md)
        # 打勾之后两个文件随之变化
        self.say("完成 260301-1")
        md_file = (self.hub / "reports" / "morning-brief.md").read_text(encoding="utf-8")
        self.assertIn("交水费（260301-1）（已办）", md_file)
        self.assertIn('class="card off"', (self.hub / "reports" / "morning-brief.html").read_text(encoding="utf-8"))
        digest.run_digest(self.cfg(), "2026-03-02")
        self.assertTrue((self.hub / "reports" / "morning-brief.html").is_file())
        proc = run_hushai_cli(SRC_DIR, "brief", "--instance", "demo", "--json")
        self.assertEqual(json.loads(proc.stdout)["for_date"], "2026-03-03")


if __name__ == "__main__":
    unittest.main()
