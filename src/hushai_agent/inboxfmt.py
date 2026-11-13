"""收件（inbox）文件名与收件行的解析、格式化与校验。"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

DOOR_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
INBOX_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<door>[a-z][a-z0-9]{1,15})(?P<auto>-auto)?\.md$"
)
TIME_PREFIX_RE = re.compile(r"^- (?P<hour>\d{2}):(?P<minute>\d{2})$")

RESERVED_DOMAINS_ZH = ("候选记忆", "更正")
RESERVED_DOMAINS_EN = ("memory", "correction")
ALL_RESERVED_DOMAINS = frozenset((*RESERVED_DOMAINS_ZH, *RESERVED_DOMAINS_EN))


class InboxFormatError(ValueError):
    """收件文件名或收件行格式错误。"""


@dataclass(frozen=True)
class InboxFileMeta:
    """收件文件名解析结果。"""

    date: datetime.date
    date_str: str
    door: str
    is_auto: bool


@dataclass(frozen=True)
class InboxLine:
    """收件行解析结果。"""

    time_str: str
    domain: str
    did: str
    artifact: str
    todo: str


def is_valid_door(door: str) -> bool:
    """检查端编号是否匹配 [a-z][a-z0-9]{1,15}。"""
    return bool(door and DOOR_RE.match(door))


def parse_inbox_filename(filename: str) -> InboxFileMeta:
    """解析并校验收件文件名（YYYY-MM-DD-<door>.md 或 YYYY-MM-DD-<door>-auto.md）。"""
    match = INBOX_FILENAME_RE.match(filename)
    if not match:
        raise InboxFormatError(f"非法收件文件名: {filename}")

    date_str = match.group("date")
    door = match.group("door")
    is_auto = bool(match.group("auto"))

    try:
        date_obj = datetime.date.fromisoformat(date_str)
    except ValueError as exc:
        raise InboxFormatError(f"收件文件名包含非法日期 {date_str}: {filename}") from exc

    return InboxFileMeta(date=date_obj, date_str=date_str, door=door, is_auto=is_auto)


def format_inbox_filename(date_str: str, door: str, is_auto: bool = False) -> str:
    """生成并校验收件文件名。"""
    if not is_valid_door(door):
        raise InboxFormatError(f"非法端编号: {door}")
    try:
        datetime.date.fromisoformat(date_str)
    except ValueError as exc:
        raise InboxFormatError(f"非法日期: {date_str}") from exc
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise InboxFormatError(f"日期格式必须为 YYYY-MM-DD: {date_str}")

    suffix = "-auto.md" if is_auto else ".md"
    return f"{date_str}-{door}{suffix}"


def is_ignorable_inbox_line(line: str) -> bool:
    """判断是否为收件文件中允许的非记录行（空行、# 开头、> 开头）。"""
    stripped = line.rstrip("\r\n")
    return stripped.strip() == "" or stripped.startswith(("#", ">"))


def parse_inbox_line(
    line: str,
    allowed_domains: Iterable[str] | None = None,
) -> InboxLine:
    """解析并校验单条收件记录行（allowed_domains 为 None 时仅校验领域非空）。"""
    raw = line.rstrip("\r\n")
    if "\n" in raw or "\r" in raw:
        raise InboxFormatError("收件行内不能包含换行符")

    parts = raw.split("｜")
    if len(parts) != 5:
        raise InboxFormatError(f"收件行必须以全角竖线‘｜’分为恰好 5 段，当前为 {len(parts)} 段: {raw}")

    seg_time, seg_domain, seg_did, seg_artifact, seg_todo = parts

    time_match = TIME_PREFIX_RE.match(seg_time)
    if not time_match:
        raise InboxFormatError(f"第 1 段格式必须为 ‘- HH:MM’: {seg_time}")
    hour, minute = int(time_match.group("hour")), int(time_match.group("minute"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise InboxFormatError(f"时间超出 24 小时制范围: {seg_time}")
    time_str = f"{hour:02d}:{minute:02d}"

    domain = seg_domain.strip()
    if not domain or domain != seg_domain:
        raise InboxFormatError(f"领域不能为空或含首尾空白: {seg_domain!r}")
    if allowed_domains is not None and domain not in (set(allowed_domains) | ALL_RESERVED_DOMAINS):
        raise InboxFormatError(f"未知或未配置的领域: {domain}")

    did = seg_did.strip()
    if not did:
        raise InboxFormatError("第 3 段‘做了什么’不能为空")

    if seg_artifact.startswith("产物："):
        artifact = seg_artifact[len("产物：") :].strip()
    elif seg_artifact.startswith("artifact:"):
        artifact = seg_artifact[len("artifact:") :].strip()
    else:
        raise InboxFormatError(f"第 4 段必须以‘产物：’或‘artifact:’开头: {seg_artifact}")
    if not artifact:
        raise InboxFormatError("第 4 段产物内容不能为空")

    if seg_todo.startswith("待办："):
        todo = seg_todo[len("待办：") :].strip()
    elif seg_todo.startswith("todo:"):
        todo = seg_todo[len("todo:") :].strip()
    else:
        raise InboxFormatError(f"第 5 段必须以‘待办：’或‘todo:’开头: {seg_todo}")
    if not todo:
        raise InboxFormatError("第 5 段待办内容不能为空")

    return InboxLine(time_str=time_str, domain=domain, did=did, artifact=artifact, todo=todo)


def format_inbox_line(
    time_str: str,
    domain: str,
    did: str,
    artifact: str | None = None,
    todo: str | None = None,
    lang: str = "zh",
    allowed_domains: Iterable[str] | None = None,
) -> str:
    """将各字段格式化为标准收件行，自动将内容中的全角竖线替换为半角竖线。"""
    if "\n" in did or "\r" in did:
        raise InboxFormatError("‘做了什么’不能包含换行符")
    clean_did = did.replace("｜", "|").strip()
    if not clean_did:
        raise InboxFormatError("‘做了什么’不能为空")

    clean_domain = domain.strip()
    clean_artifact = artifact.replace("｜", "|").strip() if artifact else ""
    clean_todo = todo.replace("｜", "|").strip() if todo else ""

    if lang == "en":
        art_val, todo_val = clean_artifact or "none", clean_todo or "none"
        line = f"- {time_str}｜{clean_domain}｜{clean_did}｜artifact: {art_val}｜todo: {todo_val}"
    elif lang == "zh":
        art_val, todo_val = clean_artifact or "无", clean_todo or "无"
        line = f"- {time_str}｜{clean_domain}｜{clean_did}｜产物：{art_val}｜待办：{todo_val}"
    else:
        raise InboxFormatError(f"不支持的语言配置: {lang}")

    parse_inbox_line(line, allowed_domains=allowed_domains)
    return line


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
        fmt = f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}" if re.match(r"^\d{8}$", cleaned) else cleaned
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
                line_id=lid, date=_DateStr(meta.date_str), door=meta.door, auto=meta.is_auto,
                time_str=parsed.time_str, domain=parsed.domain, did=parsed.did,
                artifact=parsed.artifact, todo=parsed.todo, raw=raw_line.rstrip("\r\n"),
                path=entry, lineno=lineno, occurrence=occ,
            )
