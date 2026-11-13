"""收件行确定性编号（line_id）计算与收件箱目录有序遍历。"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class _DateStr(str):
    """兼容 str 与 datetime.date 相等比较的日期字符串。"""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, datetime.date):
            return str(self) == other.isoformat()
        return super().__eq__(other)

    def __hash__(self) -> int:
        return super().__hash__()

    def isoformat(self) -> str:
        return str(self)


@dataclass(frozen=True)
class InboxRecord:
    """iter_inbox_lines 产出的单条收件行完整记录。"""

    line_id: str
    date: str
    door: str
    auto: bool
    time_str: str
    domain: str
    did: str
    artifact: str
    todo: str
    raw: str
    path: Path
    lineno: int
    occurrence: int = 0

    @property
    def time(self) -> str:
        return self.time_str

    @property
    def is_auto(self) -> bool:
        return self.auto

    @property
    def date_str(self) -> str:
        return str(self.date)


def _normalize_date_to_ymd(date: str | datetime.date) -> str:
    """将 YYYY-MM-DD、YYYYMMDD 或 datetime.date 统一转换为 8 位 YYYYMMDD。"""
    if isinstance(date, datetime.date):
        return date.strftime("%Y%m%d")
    if isinstance(date, str):
        cleaned = date.strip()
        fmt = (
            f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
            if re.match(r"^\d{8}$", cleaned)
            else cleaned
        )
        if re.match(r"^\d{4}-\d{2}-\d{2}$", fmt):
            try:
                return datetime.date.fromisoformat(fmt).strftime("%Y%m%d")
            except ValueError as exc:
                raise InboxFormatError(f"非法日期: {date}") from exc
    raise InboxFormatError(f"非法日期格式: {date!r}")


def line_id(
    date: str | datetime.date,
    door: str,
    auto: bool,
    raw_line: str,
    occurrence: int = 0,
) -> str:
    """计算收件行的确定性编号 L-<YYYYMMDD>-<door>[-auto]-<HHMM>-<h>[-<occurrence>]。"""
    if occurrence < 0:
        raise ValueError(f"occurrence 不能为负数: {occurrence}")
    if not is_valid_door(door):
        raise InboxFormatError(f"非法端编号: {door}")

    ymd = _normalize_date_to_ymd(date)
    stripped = raw_line.strip()
    parsed = parse_inbox_line(stripped, allowed_domains=None)
    hhmm = parsed.time_str.replace(":", "")
    digest = hashlib.sha1(stripped.encode("utf-8")).hexdigest()[:8]
    auto_part = "-auto" if auto else ""
    base = f"L-{ymd}-{door}{auto_part}-{hhmm}-{digest}"
    return f"{base}-{occurrence}" if occurrence > 0 else base


def iter_inbox_lines(inbox_dir: str | Path) -> Iterator[InboxRecord]:
    """按日期、端、手写先于自动、行号的顺序遍历所有合法收件行，非法文件或非法行自动跳过。"""
    inbox_path = Path(inbox_dir)
    if not inbox_path.is_dir():
        return

    files = []
    for entry in inbox_path.iterdir():
        if not entry.is_file():
            continue
        try:
            meta = parse_inbox_filename(entry.name)
        except InboxFormatError:
            continue
        files.append((meta.date_str, meta.door, meta.is_auto, entry, meta))

    # 按日期、端、手写在前自动在后排序（单纯按文件名排会把 -auto 排到手写文件前面）
    for _, _, _, entry, meta in sorted(files, key=lambda t: t[:3]):
        try:
            lines = entry.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        seen_counts: dict[str, int] = {}
        for lineno, raw_line in enumerate(lines, start=1):
            if is_ignorable_inbox_line(raw_line):
                continue
            try:
                parsed = parse_inbox_line(raw_line, allowed_domains=None)
            except InboxFormatError:
                continue

            norm_key = raw_line.strip()
            occ = seen_counts.get(norm_key, 0)
            seen_counts[norm_key] = occ + 1
            lid = line_id(meta.date_str, meta.door, meta.is_auto, raw_line, occurrence=occ)
            yield InboxRecord(
                line_id=lid,
                date=_DateStr(meta.date_str),
                door=meta.door,
                auto=meta.is_auto,
                time_str=parsed.time_str,
                domain=parsed.domain,
                did=parsed.did,
                artifact=parsed.artifact,
                todo=parsed.todo,
                raw=raw_line.rstrip("\r\n"),
                path=entry,
                lineno=lineno,
                occurrence=occ,
            )


from hushai_agent.inboxfmt import (  # noqa: E402
    InboxFormatError,
    is_ignorable_inbox_line,
    is_valid_door,
    parse_inbox_filename,
    parse_inbox_line,
)
