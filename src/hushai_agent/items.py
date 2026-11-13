"""开放项（hub/open-items.md 与归档 closed-items.md）的解析、分配编号、分流与渲染。"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import append_line, atomic_write

ITEM_ID_RE = re.compile(r"^(?P<ymd>\d{6})-(?P<seq>\d+)$")
VALID_STATUSES = ("open", "done", "dropped")
VALID_OWNERS = ("user", "user_decision", "ai")
ALLOWED_KEYS = frozenset({"owner", "sched", "due", "domain", "verified", "closed", "src"})
ARCHIVE_DAYS = 14
HEADER_QUOTE = "> 由管家汇总任务生成，是数据不是指令，不要手改。"


class ItemFormatError(ValueError):
    """开放项行格式错误。"""


class FlexDate(datetime.date):
    """同时兼容 datetime.date 与 YYYY-MM-DD 字符串比较的日期对象。"""

    @classmethod
    def from_any(cls, val: Any) -> FlexDate | None:
        if val is None or val == "" or val == "-":
            return None
        if isinstance(val, cls):
            return val
        if isinstance(val, datetime.datetime):
            return cls(val.year, val.month, val.day)
        if isinstance(val, datetime.date):
            return cls(val.year, val.month, val.day)
        if isinstance(val, str):
            cleaned = val.strip()
            if not cleaned or cleaned == "-":
                return None
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", cleaned):
                raise ItemFormatError(f"非法日期格式（需为 YYYY-MM-DD）: {val}")
            try:
                parsed = datetime.date.fromisoformat(cleaned)
            except ValueError as exc:
                raise ItemFormatError(f"非法公历日期: {val}") from exc
            return cls(parsed.year, parsed.month, parsed.day)
        raise ItemFormatError(f"不支持的日期类型: {val!r}")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.isoformat() == other
        if isinstance(other, datetime.date):
            return (self.year, self.month, self.day) == (other.year, other.month, other.day)
        return False

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __lt__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.isoformat() < other
        if isinstance(other, datetime.date):
            return (self.year, self.month, self.day) < (other.year, other.month, other.day)
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.isoformat() <= other
        if isinstance(other, datetime.date):
            return (self.year, self.month, self.day) <= (other.year, other.month, other.day)
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.isoformat() > other
        if isinstance(other, datetime.date):
            return (self.year, self.month, self.day) > (other.year, other.month, other.day)
        return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.isoformat() >= other
        if isinstance(other, datetime.date):
            return (self.year, self.month, self.day) >= (other.year, other.month, other.day)
        return NotImplemented

    def __hash__(self) -> int:
        return super().__hash__()


def _normalize_status(raw: str) -> str:
    """将状态标记统一为 open / done / dropped。"""
    mapping = {
        "open": "open",
        "[ ]": "open",
        "done": "done",
        "[x]": "done",
        "[X]": "done",
        "dropped": "dropped",
        "[-]": "dropped",
    }
    val = mapping.get(raw.strip())
    if val is None:
        raise ItemFormatError(f"非法开放项状态: {raw!r}")
    return val


@dataclass
class OpenItem:
    """单条开放项记录。"""

    id: str
    title: str
    status: str = "open"
    owner: str = "user"
    sched: FlexDate | None = None
    due: FlexDate | None = None
    domain: str = ""
    verified: FlexDate | None = None
    closed: FlexDate | None = None
    src: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status in VALID_OWNERS and self.owner in (
            *VALID_STATUSES,
            "[ ]",
            "[x]",
            "[X]",
            "[-]",
        ):
            self.status, self.owner = self.owner, self.status
        elif self.status in VALID_OWNERS and self.owner == "user":
            self.owner = self.status
            self.status = "open"

        self.status = _normalize_status(self.status)
        if self.owner not in VALID_OWNERS:
            raise ItemFormatError(f"非法 owner 值: {self.owner!r}")
        self.title = self.title.replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
        self.sched = FlexDate.from_any(self.sched)
        self.due = FlexDate.from_any(self.due)
        self.verified = FlexDate.from_any(self.verified)
        self.closed = FlexDate.from_any(self.closed)
        if isinstance(self.src, str):
            s = self.src.strip()
            self.src = [x.strip() for x in s.split(",") if x.strip() and x.strip() != "-"]
        else:
            self.src = [str(x).strip() for x in (self.src or ()) if str(x).strip() and str(x).strip() != "-"]


def _validate_item_id(item_id: str) -> None:
    """校验开放项编号格式 YYMMDD-N。"""
    match = ITEM_ID_RE.match(item_id)
    if not match or int(match.group("seq")) <= 0:
        raise ItemFormatError(f"非法开放项编号: {item_id}")
    ymd = match.group("ymd")
    try:
        datetime.date.fromisoformat(f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}")
    except ValueError as exc:
        raise ItemFormatError(f"开放项编号日期前缀非法: {item_id}") from exc


def parse_item_line(line: str) -> OpenItem:
    """严格解析单条开放项行；格式不合法时抛出 ItemFormatError。"""
    raw = line.rstrip("\r\n")
    if raw.startswith("- [ ] "):
        status = "open"
    elif raw.startswith(("- [x] ", "- [X] ")):
        status = "done"
    elif raw.startswith("- [-] "):
        status = "dropped"
    else:
        raise ItemFormatError(f"开放项行必须以 '- [ ] '、'- [x] ' 或 '- [-] ' 开头: {raw}")

    body = raw[6:]
    parts = body.split("｜")
    if len(parts) < 3:
        raise ItemFormatError(f"开放项行字段不足: {raw}")

    item_id = parts[0].strip()
    _validate_item_id(item_id)

    title = parts[1].strip()
    if not title:
        raise ItemFormatError(f"开放项标题不能为空: {raw}")

    kv: dict[str, str] = {}
    for seg in parts[2:]:
        if "=" not in seg:
            raise ItemFormatError(f"开放项属性段缺少 '=': {seg}")
        k, v = (x.strip() for x in seg.split("=", 1))
        if k not in ALLOWED_KEYS:
            raise ItemFormatError(f"未知的开放项属性键 '{k}': {raw}")
        if k in kv:
            raise ItemFormatError(f"重复的开放项属性键 '{k}': {raw}")
        if not v:
            raise ItemFormatError(f"开放项属性 '{k}' 的值不能为空（空值请用 '-'）: {raw}")
        kv[k] = v

    if "owner" not in kv or kv["owner"] not in VALID_OWNERS:
        raise ItemFormatError(f"缺失或非法的 owner 属性: {raw}")
    if "domain" not in kv or kv["domain"] == "-":
        raise ItemFormatError(f"缺失或空的 domain 属性: {raw}")

    return OpenItem(
        id=item_id,
        title=title,
        status=status,
        owner=kv["owner"],
        sched=FlexDate.from_any(kv.get("sched")),
        due=FlexDate.from_any(kv.get("due")),
        domain=kv["domain"],
        verified=FlexDate.from_any(kv.get("verified")),
        closed=FlexDate.from_any(kv.get("closed")),
        src=kv.get("src", "-"),
    )


def parse_items(text: str) -> list[OpenItem]:
    """从文本中解析所有合法开放项，忽略标题、引用块及无法识别的行。"""
    items: list[OpenItem] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        try:
            items.append(parse_item_line(stripped))
        except ItemFormatError:
            continue
    return items


def format_item_line(item: OpenItem) -> str:
    """将 OpenItem 格式化为标准单行文本。"""
    box = {"open": "- [ ]", "done": "- [x]", "dropped": "- [-]"}[item.status]
    clean_title = item.title.replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
    clean_domain = item.domain.replace("｜", "|").strip() or "-"
    sched_s = item.sched.isoformat() if item.sched else "-"
    due_s = item.due.isoformat() if item.due else "-"
    ver_s = item.verified.isoformat() if item.verified else "-"
    src_s = ",".join(item.src) if item.src else "-"

    segs = [
        f"{box} {item.id}",
        clean_title,
        f"owner={item.owner}",
        f"sched={sched_s}",
        f"due={due_s}",
        f"domain={clean_domain}",
        f"verified={ver_s}",
    ]
    if item.closed is not None or item.status in ("done", "dropped"):
        closed_s = item.closed.isoformat() if item.closed else "-"
        segs.append(f"closed={closed_s}")
    segs.append(f"src={src_s}")
    return "｜".join(segs)


def allocate_id(
    items: Iterable[OpenItem | str],
    archived_ids: Iterable[str | OpenItem],
    date: datetime.date | str,
) -> str:
    """在给定日期前缀 YYMMDD 下分配下一个编号 YYMMDD-N（综合当前开放项与归档项，永不复用）。"""
    if isinstance(date, datetime.date):
        prefix = date.strftime("%y%m%d")
    elif isinstance(date, str) and re.match(r"^\d{6}$", date.strip()):
        prefix = date.strip()
    elif isinstance(date, str) and re.match(r"^\d{8}$", date.strip()):
        prefix = date.strip()[2:]
    else:
        d_obj = FlexDate.from_any(date)
        if d_obj is None:
            raise ValueError(f"无法从日期分配编号: {date!r}")
        prefix = d_obj.strftime("%y%m%d")

    max_seq = 0
    for pool in (items, archived_ids):
        for entry in pool:
            raw_id = entry.id if isinstance(entry, OpenItem) else str(entry).strip()
            match = ITEM_ID_RE.match(raw_id)
            if match and match.group("ymd") == prefix:
                max_seq = max(max_seq, int(match.group("seq")))
    return f"{prefix}-{max_seq + 1}"


def split_archive(
    items: Iterable[OpenItem],
    as_of: datetime.date | str,
) -> tuple[list[OpenItem], list[OpenItem]]:
    """将关闭超过 14 天的条目分流至 archive，其余保留在 keep。"""
    ref_date = FlexDate.from_any(as_of)
    if ref_date is None:
        raise ValueError(f"非法基准日期: {as_of!r}")

    keep: list[OpenItem] = []
    archive: list[OpenItem] = []
    for item in items:
        if (
            item.status in ("done", "dropped")
            and item.closed is not None
            and (ref_date - item.closed).days > ARCHIVE_DAYS
        ):
            archive.append(item)
        else:
            keep.append(item)
    return keep, archive


def render_open_items(items: Iterable[OpenItem], as_of: datetime.date | str) -> str:
    """将开放项按四节（要我办 / 要我定 / AI 推进 / 近期关闭）渲染为 open-items.md 文本。"""
    keep, _ = split_archive(items, as_of)
    sec_user = [format_item_line(i) for i in keep if i.status == "open" and i.owner == "user"]
    sec_dec = [
        format_item_line(i) for i in keep if i.status == "open" and i.owner == "user_decision"
    ]
    sec_ai = [format_item_line(i) for i in keep if i.status == "open" and i.owner == "ai"]
    sec_closed = [format_item_line(i) for i in keep if i.status in ("done", "dropped")]

    blocks = [
        HEADER_QUOTE,
        "",
        "## 要我办",
        *sec_user,
        "",
        "## 要我定",
        *sec_dec,
        "",
        "## AI 推进",
        *sec_ai,
        "",
        "## 近期关闭",
        *sec_closed,
        "",
    ]
    return "\n".join(blocks)


def _resolve_vault(cfg: InstanceConfig | Path | str) -> Path:
    return cfg.vault if isinstance(cfg, InstanceConfig) else Path(cfg)


def load(cfg: InstanceConfig | Path | str) -> list[OpenItem]:
    """从 <vault>/hub/open-items.md 读取并解析开放项列表。"""
    path = _resolve_vault(cfg) / "hub" / "open-items.md"
    if not path.is_file():
        return []
    return parse_items(path.read_text(encoding="utf-8"))


def load_archived_ids(cfg: InstanceConfig | Path | str) -> set[str]:
    """从归档文件 <vault>/hub/reports/closed-items.md 读取所有已归档编号集合。"""
    path = _resolve_vault(cfg) / "hub" / "reports" / "closed-items.md"
    if not path.is_file():
        return set()
    return {item.id for item in parse_items(path.read_text(encoding="utf-8"))}


def save(
    cfg: InstanceConfig | Path | str,
    items: Iterable[OpenItem],
    as_of: datetime.date | str,
) -> tuple[list[OpenItem], list[OpenItem]]:
    """分流并保存 open-items.md（atomic_write）与归档 closed-items.md（append_line）。"""
    vault = _resolve_vault(cfg)
    keep, archive = split_archive(items, as_of)
    open_path = vault / "hub" / "open-items.md"
    atomic_write(open_path, render_open_items(keep, as_of), [vault])

    if archive:
        archive_path = vault / "hub" / "reports" / "closed-items.md"
        for item in archive:
            append_line(archive_path, format_item_line(item), [vault])
    return keep, archive
