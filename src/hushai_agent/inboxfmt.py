"""收件（inbox）文件名与收件行的解析、格式化与校验。"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Iterable

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
    if stripped.strip() == "":
        return True
    if stripped.startswith("#") or stripped.startswith(">"):
        return True
    return False


def parse_inbox_line(
    line: str,
    allowed_domains: Iterable[str] | None = None,
) -> InboxLine:
    """解析并校验单条收件记录行。

    若提供 allowed_domains，则校验领域是否在允许列表或保留领域中；
    若 allowed_domains 为 None（如 validate 模式），仅校验领域非空。
    """
    raw = line.rstrip("\r\n")
    if "\n" in raw or "\r" in raw:
        raise InboxFormatError("收件行内不能包含换行符")

    parts = raw.split("｜")
    if len(parts) != 5:
        raise InboxFormatError(
            f"收件行必须以全角竖线‘｜’分为恰好 5 段，当前为 {len(parts)} 段: {raw}"
        )

    seg_time, seg_domain, seg_did, seg_artifact, seg_todo = parts

    # 1. 时间段校验
    time_match = TIME_PREFIX_RE.match(seg_time)
    if not time_match:
        raise InboxFormatError(f"第 1 段格式必须为 ‘- HH:MM’: {seg_time}")
    hour = int(time_match.group("hour"))
    minute = int(time_match.group("minute"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise InboxFormatError(f"时间超出 24 小时制范围: {seg_time}")
    time_str = f"{hour:02d}:{minute:02d}"

    # 2. 领域段校验
    domain = seg_domain.strip()
    if not domain or domain != seg_domain:
        raise InboxFormatError(f"领域不能为空或含首尾空白: {seg_domain!r}")
    if allowed_domains is not None:
        valid_domains = set(allowed_domains) | ALL_RESERVED_DOMAINS
        if domain not in valid_domains:
            raise InboxFormatError(f"未知或未配置的领域: {domain}")

    # 3. 做了什么校验
    did = seg_did.strip()
    if not did:
        raise InboxFormatError("第 3 段‘做了什么’不能为空")

    # 4. 产物段校验
    if seg_artifact.startswith("产物："):
        artifact = seg_artifact[len("产物：") :].strip()
    elif seg_artifact.startswith("artifact:"):
        artifact = seg_artifact[len("artifact:") :].strip()
    else:
        raise InboxFormatError(f"第 4 段必须以‘产物：’或‘artifact:’开头: {seg_artifact}")
    if not artifact:
        raise InboxFormatError("第 4 段产物内容不能为空")

    # 5. 待办段校验
    if seg_todo.startswith("待办："):
        todo = seg_todo[len("待办：") :].strip()
    elif seg_todo.startswith("todo:"):
        todo = seg_todo[len("todo:") :].strip()
    else:
        raise InboxFormatError(f"第 5 段必须以‘待办：’或‘todo:’开头: {seg_todo}")
    if not todo:
        raise InboxFormatError("第 5 段待办内容不能为空")

    return InboxLine(
        time_str=time_str,
        domain=domain,
        did=did,
        artifact=artifact,
        todo=todo,
    )


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
        art_val = clean_artifact or "none"
        todo_val = clean_todo or "none"
        line = f"- {time_str}｜{clean_domain}｜{clean_did}｜artifact: {art_val}｜todo: {todo_val}"
    elif lang == "zh":
        art_val = clean_artifact or "无"
        todo_val = clean_todo or "无"
        line = f"- {time_str}｜{clean_domain}｜{clean_did}｜产物：{art_val}｜待办：{todo_val}"
    else:
        raise InboxFormatError(f"不支持的语言配置: {lang}")

    # 校验格式化后的行
    parse_inbox_line(line, allowed_domains=allowed_domains)
    return line
