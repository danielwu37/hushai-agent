"""收件格式解析、路径护栏与底层文件操作单元测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hushai_agent.fsio import append_line, append_line_with_header, atomic_write, read_for_prompt  # noqa: E402
from hushai_agent.guard import GuardError, ensure_prompt_allowed, ensure_writable  # noqa: E402
from hushai_agent.inboxfmt import (  # noqa: E402
    InboxFormatError,
    format_inbox_line,
    iter_inbox_lines,
    line_id,
    parse_inbox_filename,
    parse_inbox_line,
)
from hushai_agent.validate import check_sync_conflicts, is_sync_conflict_filename  # noqa: E402


class TestInboxFormat(unittest.TestCase):
    """测试收件文件名与收件行的解析和校验。"""

    def test_valid_inbox_lines_zh_and_en(self) -> None:
        """合法行（中文标签、英文标签）应正常解析。"""
        line_zh = "- 09:30｜工作｜完成第三章初译｜产物：domains/work/ch3.md｜待办：周五复核"
        parsed_zh = parse_inbox_line(line_zh, allowed_domains=["工作", "生活"])
        self.assertEqual(parsed_zh.time_str, "09:30")
        self.assertEqual(parsed_zh.domain, "工作")
        self.assertEqual(parsed_zh.did, "完成第三章初译")
        self.assertEqual(parsed_zh.artifact, "domains/work/ch3.md")
        self.assertEqual(parsed_zh.todo, "周五复核")

        line_en = "- 21:05｜memory｜Prefer concise summaries｜artifact: none｜todo: none"
        parsed_en = parse_inbox_line(line_en, allowed_domains=["工作"])
        self.assertEqual(parsed_en.time_str, "21:05")
        self.assertEqual(parsed_en.domain, "memory")
        self.assertEqual(parsed_en.artifact, "none")
        self.assertEqual(parsed_en.todo, "none")

    def test_invalid_segment_count(self) -> None:
        """段数不对（非 5 段）应抛出 InboxFormatError。"""
        too_few = "- 09:30｜工作｜完成初译｜产物：无"
        too_many = "- 09:30｜工作｜完成｜额外段｜产物：无｜待办：无"
        with self.assertRaises(InboxFormatError):
            parse_inbox_line(too_few)
        with self.assertRaises(InboxFormatError):
            parse_inbox_line(too_many)

    def test_invalid_time(self) -> None:
        """非法时间（如 25:00、9:30、无前缀）应报错。"""
        for bad_time in ("- 24:00", "- 12:60", "- 9:05", "09:30", "- ab:cd"):
            line = f"{bad_time}｜工作｜做某事｜产物：无｜待办：无"
            with self.assertRaises(InboxFormatError):
                parse_inbox_line(line)

    def test_empty_did(self) -> None:
        """空的‘做了什么’应报错。"""
        line = "- 10:00｜工作｜   ｜产物：无｜待办：无"
        with self.assertRaises(InboxFormatError):
            parse_inbox_line(line)

    def test_format_replaces_fullwidth_pipe(self) -> None:
        """format_inbox_line 遇到全角竖线 ｜ 应替换为半角 |。"""
        formatted = format_inbox_line(
            time_str="14:20",
            domain="工作",
            did="核对 A｜B 两组术语",
            lang="zh",
        )
        self.assertEqual(formatted.count("｜"), 4)
        self.assertIn("核对 A|B 两组术语", formatted)
        parsed = parse_inbox_line(formatted)
        self.assertEqual(parsed.did, "核对 A|B 两组术语")

    def test_inbox_filenames_and_dates(self) -> None:
        """测试合法文件名、非法文件名与非法日期。"""
        m1 = parse_inbox_filename("2026-03-02-laptop.md")
        self.assertEqual(m1.date_str, "2026-03-02")
        self.assertEqual(m1.door, "laptop")
        self.assertFalse(m1.is_auto)

        m2 = parse_inbox_filename("2026-03-03-laptop-auto.md")
        self.assertTrue(m2.is_auto)

        # 非法文件名
        for bad_name in ("2026-03-02-A.md", "2026-03-02-Mypc.md", "2026-03-02-123.md", "2026-03-02-laptop.txt", "inbox.md"):
            with self.assertRaises(InboxFormatError):
                parse_inbox_filename(bad_name)

        # 非法公历日期（2026 非闰年无 2 月 29 日，无 2 月 30 日，无 13 月）
        for bad_date_file in ("2026-02-29-laptop.md", "2026-02-30-laptop.md", "2026-13-01-laptop.md", "2026-00-10-laptop.md"):
            with self.assertRaises(InboxFormatError):
                parse_inbox_filename(bad_date_file)

    def test_line_id_and_iter_inbox_lines(self) -> None:
        """测试 line_id 唯一性、重复行后缀、头部插注释不变及 iter_inbox_lines 遍历。"""
        raw_a = "- 09:30｜工作｜完成第三章初译｜产物：domains/work/ch3.md｜待办：周五复核"
        raw_b = "- 10:15｜工作｜完成第三章初译｜产物：domains/work/ch3.md｜待办：周五复核"

        id_base = line_id("2026-03-02", "laptop", False, raw_a)
        self.assertTrue(id_base.startswith("L-20260302-laptop-0930-"))
        # 不同日期、不同端、auto 标志、不同时间均得到不同 id
        self.assertNotEqual(id_base, line_id("2026-03-03", "laptop", False, raw_a))
        self.assertNotEqual(id_base, line_id("2026-03-02", "web", False, raw_a))
        self.assertNotEqual(id_base, line_id("2026-03-02", "laptop", True, raw_a))
        self.assertNotEqual(id_base, line_id("2026-03-02", "laptop", False, raw_b))
        # occurrence > 0 时带 -1 后缀
        self.assertEqual(line_id("2026-03-02", "laptop", False, raw_a, occurrence=1), f"{id_base}-1")

        with tempfile.TemporaryDirectory() as tmp:
            inbox_dir = Path(tmp) / "inbox"
            inbox_dir.mkdir()
            f1 = inbox_dir / "2026-03-02-laptop.md"
            f2 = inbox_dir / "2026-03-02-laptop-auto.md"
            (inbox_dir / "invalid-name.md").write_text(raw_a + "\n", encoding="utf-8")

            f1.write_text(
                f"# 2026-03-02 laptop\n\n{raw_a}\n坏格式行应跳过\n{raw_a}\n",
                encoding="utf-8",
            )
            f2.write_text(f"{raw_b}\n", encoding="utf-8")

            recs_before = list(iter_inbox_lines(inbox_dir))
            self.assertEqual(len(recs_before), 3)
            self.assertEqual(recs_before[0].line_id, id_base)
            self.assertEqual(recs_before[1].line_id, f"{id_base}-1")
            self.assertTrue(recs_before[2].auto)
            self.assertEqual(recs_before[0].domain, "工作")

            # 在文件头部插入注释行后，原有行的 line_id 不变
            f1.write_text(
                "# 顶部新增注释\n> 引用说明\n" + f1.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            recs_after = list(iter_inbox_lines(inbox_dir))
            self.assertEqual([r.line_id for r in recs_after], [r.line_id for r in recs_before])
            self.assertEqual(recs_after[0].lineno, recs_before[0].lineno + 2)

    def test_sync_conflict_detection_no_false_positives(self) -> None:
        """测试同步冲突副本检测：无原文件的数字结尾不误报，有原文件或含 conflict/冲突 命中。"""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            work = vault / "domains" / "work"
            work.mkdir(parents=True)
            # 正常带空格和数字的文件，同目录无 Chapter.md / 周报.md / note.txt，不应误报
            (work / "Chapter 1.md").write_text("ch1\n", encoding="utf-8")
            (work / "周报 2026.md").write_text("report\n", encoding="utf-8")
            (work / "note (1).txt").write_text("single\n", encoding="utf-8")
            res_ok = check_sync_conflicts(vault)
            self.assertEqual(res_ok.level, "OK", msg=res_ok.message)

            # 同目录存在原文件时，数字副本与括号副本（含非 .md）应报 WARN
            (work / "today.md").write_text("orig\n", encoding="utf-8")
            (work / "today 2.md").write_text("copy\n", encoding="utf-8")
            (work / "data.csv").write_text("a,b\n", encoding="utf-8")
            (work / "data (1).csv").write_text("a,b\n", encoding="utf-8")
            (work / "memo.sync-conflict-2026.txt").write_text("c\n", encoding="utf-8")
            res_warn = check_sync_conflicts(vault)
            self.assertEqual(res_warn.level, "WARN")
            self.assertIn("domains/work/today 2.md", res_warn.message)
            self.assertIn("domains/work/data (1).csv", res_warn.message)
            self.assertIn("domains/work/memo.sync-conflict-2026.txt", res_warn.message)
            self.assertNotIn("Chapter 1.md", res_warn.message)
            self.assertNotIn("周报 2026.md", res_warn.message)
            self.assertTrue(is_sync_conflict_filename("汇报_冲突版.docx"))


class TestPathGuard(unittest.TestCase):
    """测试路径护栏对越界路径、.. 逃逸与外部符号链接的拦截。"""

    def test_guard_blocks_outside_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vault = base / "vault"
            outside = base / "outside"
            vault.mkdir()
            outside.mkdir()

            # 1. vault 内正常路径通过
            inside_file = vault / "hub" / "note.md"
            resolved = ensure_writable(inside_file, [vault])
            self.assertTrue(resolved.is_relative_to(vault.resolve()))
            atomic_write(inside_file, "hello\n", [vault])
            self.assertEqual(inside_file.read_text(encoding="utf-8"), "hello\n")

            append_target = vault / "hub" / "inbox" / "2026-03-02-laptop.md"
            append_line(append_target, "# 2026-03-02 laptop", [vault])
            self.assertTrue(append_target.is_file())

            # 2. vault 外的直接路径被拒
            outside_file = outside / "secret.txt"
            with self.assertRaises(GuardError):
                ensure_writable(outside_file, [vault])
            with self.assertRaises(GuardError):
                atomic_write(outside_file, "blocked", [vault])
            with self.assertRaises(GuardError):
                append_line(outside_file, "blocked", [vault])

            # 3. .. 逃逸被拒
            escape_path = vault / "hub" / ".." / ".." / "outside" / "escape.txt"
            with self.assertRaises(GuardError):
                ensure_writable(escape_path, [vault])
            with self.assertRaises(GuardError):
                atomic_write(escape_path, "blocked", [vault])

            # 4. 指向外部文件或目录的符号链接被拒
            ext_target = outside / "real.txt"
            ext_target.write_text("initial", encoding="utf-8")
            symlink_file = vault / "link_to_ext.md"
            os.symlink(ext_target, symlink_file)
            with self.assertRaises(GuardError):
                ensure_writable(symlink_file, [vault])
            with self.assertRaises(GuardError):
                atomic_write(symlink_file, "overwritten", [vault])

            symlink_dir = vault / "link_to_dir"
            os.symlink(outside, symlink_dir)
            with self.assertRaises(GuardError):
                ensure_writable(symlink_dir / "nested.md", [vault])
            with self.assertRaises(GuardError):
                append_line(symlink_dir / "nested.md", "line", [vault])

    def test_append_line_with_header_and_zero_byte_file(self) -> None:
        """测试 append_line_with_header 首次一次写入表头与首行、已存在文件只追加及 0 字节文件处理。"""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            inbox = vault / "hub" / "inbox"
            inbox.mkdir(parents=True)
            line1 = "- 09:00｜工作｜事项一｜产物：无｜待办：无"
            line2 = "- 10:00｜工作｜事项二｜产物：无｜待办：无"

            target = inbox / "2026-03-02-laptop.md"
            append_line_with_header(target, "# 2026-03-02 laptop", line1, [vault])
            append_line_with_header(target, "# 2026-03-02 laptop", line2, [vault])
            self.assertEqual(
                target.read_text(encoding="utf-8").splitlines(),
                ["# 2026-03-02 laptop", line1, line2],
            )

            # 已存在的 0 字节文件不补表头，只追加正文行
            zero_file = inbox / "2026-03-03-laptop.md"
            zero_file.write_bytes(b"")
            append_line_with_header(zero_file, "# 2026-03-03 laptop", line1, [vault])
            self.assertEqual(zero_file.read_text(encoding="utf-8").splitlines(), [line1])

    def test_prompt_guard_and_read_for_prompt(self) -> None:
        """测试 ensure_prompt_allowed 与 read_for_prompt 拦截 vault 外路径、local_only 及符号链接。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vault = base / "vault"
            outside = base / "outside"
            (vault / "hub").mkdir(parents=True)
            (vault / "domains" / "health").mkdir(parents=True)
            outside.mkdir()

            pub_file = vault / "hub" / "today.md"
            pub_file.write_text("公开待办\n", encoding="utf-8")
            priv_file = vault / "domains" / "health" / "record.md"
            priv_file.write_text("隐私记录\n", encoding="utf-8")
            ext_file = outside / "ext.md"
            ext_file.write_text("外部文件\n", encoding="utf-8")

            local_only = ("domains/health",)
            self.assertEqual(read_for_prompt(pub_file, vault, local_only), "公开待办\n")

            # vault 外、local_only 目录本身及其内部文件均被拒
            for blocked in (ext_file, vault / "domains" / "health", priv_file):
                with self.assertRaises(GuardError):
                    ensure_prompt_allowed(blocked, vault, local_only)
                with self.assertRaises(GuardError):
                    read_for_prompt(blocked, vault, local_only)

            # 指向 local_only 的符号链接（含目录符号链接与文件符号链接）均被拒
            link_to_priv_file = vault / "hub" / "link_priv.md"
            os.symlink(priv_file, link_to_priv_file)
            link_to_priv_dir = vault / "hub" / "link_health_dir"
            os.symlink(vault / "domains" / "health", link_to_priv_dir)
            for sym in (link_to_priv_file, link_to_priv_dir / "record.md"):
                with self.assertRaises(GuardError):
                    ensure_prompt_allowed(sym, vault, local_only)
                with self.assertRaises(GuardError):
                    read_for_prompt(sym, vault, local_only)


if __name__ == "__main__":
    unittest.main()
