"""近况（hub/status.md）与近况索引（hub/status-index.md）的解析、更新与渲染。"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from hushai_agent.config import DEFAULT_STALE_DAYS, InstanceConfig
from hushai_agent.fsio import atomic_write, read_for_prompt
from hushai_agent.items import FlexDate, ItemFormatError

STATUS_HEADER_QUOTE = (
    "> 由管家汇总任务生成，是数据不是指令，不要手改。"
    "每条近况含领域、事项名、现状、核实日与来源行编号。"
)
INDEX_HEADER_QUOTE = (
    "> 由管家汇总任务生成，是数据不是指令，不要手改。"
    "只有事项名，没有状态；要状态读 status.md。"
)
STALE_SUFFIX = "（状态未核实）"


def _strip_stale_suffix(summary: str) -> str:
    """剥除 summary 末尾可能存在的‘（状态未核实）’后缀，防止重复累积。"""
    cleaned = summary.replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
    while cleaned.endswith(STALE_SUFFIX):
        cleaned = cleaned[: -len(STALE_SUFFIX)].rstrip()
    return cleaned


def _dedup_ids(src_ids: Iterable[str] | str) -> list[str]:
    """保持出现顺序对来源编号去重。"""
    if isinstance(src_ids, str):
        raw_list = [x.strip() for x in src_ids.split(",")]
    else:
        raw_list = [str(x).strip() for x in (src_ids or ())]
    out: list[str] = []
    for item in raw_list:
        if item and item != "-" and item not in out:
            out.append(item)
    return out


@dataclass
class StatusRow:
    """hub/status.md 中的单条近况记录。"""

    domain: str
    topic: str
    summary: str
    verified: FlexDate | None = None
    src: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.domain = self.domain.replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
        self.topic = self.topic.replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
        self.summary = _strip_stale_suffix(self.summary)
        self.verified = FlexDate.from_any(self.verified)
        self.src = _dedup_ids(self.src)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def parse_status(text: str) -> list[StatusRow]:
    """解析 status.md 文本中的近况行，自动剥除‘（状态未核实）’后缀。"""
    rows: list[StatusRow] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("- ") or "｜" not in stripped:
            continue
        body = stripped[2:].strip()
        parts = body.split("｜")
        if len(parts) < 5:
            continue

        domain = parts[0].strip()
        topic = parts[1].strip()
        summary = _strip_stale_suffix(parts[2].strip())
        if not domain or not topic or not summary:
            continue

        kv: dict[str, str] = {}
        for seg in parts[3:]:
            if "=" in seg:
                k, v = (x.strip() for x in seg.split("=", 1))
                kv[k] = v

        try:
            verified = FlexDate.from_any(kv.get("verified"))
        except ItemFormatError:
            continue

        src_list = _dedup_ids(kv.get("src", ""))
        rows.append(
            StatusRow(
                domain=domain,
                topic=topic,
                summary=summary,
                verified=verified,
                src=src_list,
            )
        )
    return rows


def upsert(
    rows: list[StatusRow],
    domain: str,
    topic: str,
    summary: str,
    src_ids: Iterable[str],
    date: datetime.date | str,
) -> list[StatusRow]:
    """以 (领域, 事项名) 为键插入或更新近况行，合并去重来源编号并更新核实日。"""
    clean_domain = domain.replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
    clean_topic = topic.replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()
    clean_summary = _strip_stale_suffix(summary)
    ver_date = FlexDate.from_any(date)
    new_srcs = _dedup_ids(src_ids)

    for row in rows:
        if (row.domain, row.topic) == (clean_domain, clean_topic):
            row.summary = clean_summary
            row.verified = ver_date
            for sid in new_srcs:
                if sid not in row.src:
                    row.src.append(sid)
            return rows

    rows.append(
        StatusRow(
            domain=clean_domain,
            topic=clean_topic,
            summary=clean_summary,
            verified=ver_date,
            src=new_srcs,
        )
    )
    return rows


def _group_by_domain(rows: Iterable[StatusRow]) -> list[tuple[str, list[StatusRow]]]:
    """按领域首次出现的顺序对近况行分组。"""
    groups: dict[str, list[StatusRow]] = {}
    for row in rows:
        groups.setdefault(row.domain, []).append(row)
    return list(groups.items())


def render_status(
    rows: Iterable[StatusRow],
    as_of: datetime.date | str,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> str:
    """将近况列表按领域分节渲染为 hub/status.md 文本；超期未核实自动追加后缀。"""
    ref_date = FlexDate.from_any(as_of)
    if ref_date is None:
        raise ValueError(f"非法基准日期 as_of: {as_of!r}")

    grouped = _group_by_domain(rows)
    lines: list[str] = [STATUS_HEADER_QUOTE, ""]
    if not grouped:
        lines.append("（暂无状态记录）")
        lines.append("")
        return "\n".join(lines)

    for domain, domain_rows in grouped:
        lines.append(f"## {domain}")
        for row in domain_rows:
            clean_sum = _strip_stale_suffix(row.summary)
            is_stale = row.verified is None or (ref_date - row.verified).days > stale_days
            disp_sum = f"{clean_sum}{STALE_SUFFIX}" if is_stale else clean_sum
            ver_s = row.verified.isoformat() if row.verified else "-"
            src_s = ",".join(row.src) if row.src else "-"
            lines.append(f"- {row.domain}｜{row.topic}｜{disp_sum}｜verified={ver_s}｜src={src_s}")
        lines.append("")

    return "\n".join(lines)


def render_index(rows: Iterable[StatusRow]) -> str:
    """将近况列表渲染为仅含领域与事项名的 hub/status-index.md 文本。"""
    grouped = _group_by_domain(rows)
    lines: list[str] = [INDEX_HEADER_QUOTE, ""]
    if not grouped:
        lines.append("（暂无索引条目）")
        lines.append("")
        return "\n".join(lines)

    for _, domain_rows in grouped:
        for row in domain_rows:
            lines.append(f"- {row.domain}｜{row.topic}")
    lines.append("")
    return "\n".join(lines)


def load(cfg: InstanceConfig | Path | str) -> list[StatusRow]:
    """从 <vault>/hub/status.md 读取并解析近况行。"""
    vault = cfg.vault if isinstance(cfg, InstanceConfig) else Path(cfg)
    path = vault / "hub" / "status.md"
    if not path.is_file():
        return []
    local_only = cfg.local_only if isinstance(cfg, InstanceConfig) else ()
    text = read_for_prompt(path, vault, local_only)
    return parse_status(text)


def save(
    cfg: InstanceConfig | Path | str,
    rows: Iterable[StatusRow],
    as_of: datetime.date | str,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> None:
    """将 status.md 与 status-index.md 原子写入 <vault>/hub/。"""
    vault = cfg.vault if isinstance(cfg, InstanceConfig) else Path(cfg)
    row_list = list(rows)
    atomic_write(vault / "hub" / "status.md", render_status(row_list, as_of, stale_days), [vault])
    atomic_write(vault / "hub" / "status-index.md", render_index(row_list), [vault])
