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

from hushai_agent.fsio import append_line, atomic_write  # noqa: E402
from hushai_agent.guard import GuardError, ensure_writable  # noqa: E402
from hushai_agent.inboxfmt import (  # noqa: E402
    InboxFormatError,
    format_inbox_line,
    parse_inbox_filename,
    parse_inbox_line,
)


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
        for bad_name in (
            "2026-03-02-A.md",
            "2026-03-02-Mypc.md",
            "2026-03-02-123.md",
            "2026-03-02-laptop.txt",
            "inbox.md",
        ):
            with self.assertRaises(InboxFormatError):
                parse_inbox_filename(bad_name)

        # 非法公历日期（2026 非闰年无 2 月 29 日，无 2 月 30 日，无 13 月）
        for bad_date_file in (
            "2026-02-29-laptop.md",
            "2026-02-30-laptop.md",
            "2026-13-01-laptop.md",
            "2026-00-10-laptop.md",
        ):
            with self.assertRaises(InboxFormatError):
                parse_inbox_filename(bad_date_file)


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


if __name__ == "__main__":
    unittest.main()
