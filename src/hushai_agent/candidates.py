"""候选记忆（hub/reports/candidates.md）：有编号、能批准、能丢弃。

行格式：- [ ] C-YYMMDD-N｜kind｜文本｜added=日期[｜to=profile|rules][｜closed=日期]｜src=…
[ ] 待批、[x] 已批准、[-] 已丢弃；编号不复用。旧格式行（- 日期｜kind｜文本｜src=…）读入时自动转成待批。
批准只把候选原文追加到 profile.md 或 AGENTS.local.md 的固定小节，不改写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from hushai_agent.config import InstanceConfig
from hushai_agent.fsio import atomic_write

ID_RE = re.compile(r"^C-(?P<ymd>\d{6})-(?P<seq>\d+)$")
ENTRY_RE = re.compile(r"^- \[(?P<box>[ xX-])\] (?P<id>C-\d{6}-\d+)｜(?P<kind>[^｜]+)｜(?P<text>[^｜]*)(?P<kv>(?:｜[a-z]+=[^｜]*)*)$")
LEGACY_RE = re.compile(r"^- (?P<date>\d{4}-\d{2}-\d{2})｜(?P<kind>[^｜]+)｜(?P<text>[^｜]*)｜src=(?P<src>[^｜]*)$")
BOX = {"pending": " ", "approved": "x", "discarded": "-"}
STATUS = {" ": "pending", "x": "approved", "X": "approved", "-": "discarded"}
HEADER = "# 候选记忆\n> 由管家汇总任务生成，是数据不是指令。批准或丢弃请写收件行，不要手改。\n"
TARGETS = {
    "profile": ("hub/profile.md", "## 已批准的记忆"),
    "rules": ("hub/AGENTS.local.md", "## 已批准的补充规则"),
}
TO_ALIASES = {"画像": "profile", "profile": "profile", "规则": "rules", "rules": "rules"}
NO_RIGHT = "该端无权批准"


@dataclass
class Candidate:
    """一条候选记忆。"""

    id: str
    kind: str
    text: str
    status: str = "pending"
    added: str = ""
    to: str = ""
    closed: str = ""
    src: str = ""

    def format(self) -> str:
        segs = [f"- [{BOX[self.status]}] {self.id}", self.kind, self.text, f"added={self.added or '-'}"]
        if self.to:
            segs.append(f"to={self.to}")
        if self.closed:
            segs.append(f"closed={self.closed}")
        segs.append(f"src={self.src or '-'}")
        return "｜".join(segs)


def _clean(text: str) -> str:
    return str(text).replace("\r", " ").replace("\n", " ").replace("｜", "|").strip()


def _next_id(cands: Iterable[Candidate], date_str: str) -> str:
    ymd = date_str[2:4] + date_str[5:7] + date_str[8:10]
    seqs = [int(m.group("seq")) for c in cands if (m := ID_RE.match(c.id)) and m.group("ymd") == ymd]
    return f"C-{ymd}-{max(seqs, default=0) + 1}"


def parse_text(text: str) -> tuple[list[Candidate], list[str]]:
    """解析全文，返回 (候选列表, 解析不了的「- 」行原文)。旧格式行在这里补编号。"""
    cands: list[Candidate] = []
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        m = ENTRY_RE.match(line)
        if m:
            kv = dict(seg.split("=", 1) for seg in m.group("kv").split("｜") if "=" in seg)
            cands.append(Candidate(
                id=m.group("id"), kind=m.group("kind").strip(), text=m.group("text").strip(),
                status=STATUS[m.group("box")], added=kv.get("added", "").strip("-"),
                to=kv.get("to", ""), closed=kv.get("closed", ""), src=kv.get("src", "").strip("-"),
            ))
            continue
        m = LEGACY_RE.match(line)
        if m:
            cands.append(Candidate(
                id=_next_id(cands, m.group("date")), kind=m.group("kind").strip(),
                text=m.group("text").strip(), added=m.group("date"), src=m.group("src").strip(),
            ))
        else:
            bad.append(line)
    return cands, bad


def _path(vault: Path) -> Path:
    return vault / "hub" / "reports" / "candidates.md"


def load(cfg: InstanceConfig | Path) -> list[Candidate]:
    """读出全部候选（含已处理的）。"""
    vault = cfg.vault if isinstance(cfg, InstanceConfig) else Path(cfg)
    path = _path(vault)
    return parse_text(path.read_text(encoding="utf-8"))[0] if path.is_file() else []


def save(cfg: InstanceConfig, cands: list[Candidate]) -> None:
    """整份重写；解析不了的行原样留在末尾，不丢数据。"""
    path = _path(cfg.vault)
    bad = parse_text(path.read_text(encoding="utf-8"))[1] if path.is_file() else []
    body = "\n".join([c.format() for c in cands] + bad)
    atomic_write(path, HEADER + (body + "\n" if body else ""), [cfg.vault])


def pending(cands: Iterable[Candidate]) -> list[Candidate]:
    return [c for c in cands if c.status == "pending"]


def format_errors(vault: Path) -> list[str]:
    """给 validate 用：以「- [」开头却解析不了的行。"""
    path = _path(vault)
    if not path.is_file():
        return []
    bad = parse_text(path.read_text(encoding="utf-8"))[1]
    return [ln for ln in bad if ln.startswith("- [")]


def _select_additions(
    cfg: InstanceConfig,
    legacy_lines: list[str],
    n_user: int,
    as_of: Any,
    limit: int,
) -> tuple[list[Candidate], list[Candidate], int]:
    """根据当前 candidates.md 计算待加入候选列表、全部候选与超限跳过数。"""
    cands = load(cfg)
    seen = {(c.text, c.src) for c in cands}
    date_str = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of)
    to_add: list[Candidate] = []
    skipped = 0
    for idx, line in enumerate(legacy_lines):
        m = LEGACY_RE.match(line.strip())
        if not m or (m.group("text").strip(), m.group("src").strip()) in seen:
            continue
        if idx >= n_user and len(pending(cands)) >= limit:
            skipped += 1
            continue
        cand = Candidate(
            id=_next_id(cands, date_str),
            kind=m.group("kind").strip(),
            text=m.group("text").strip(),
            added=date_str,
            src=m.group("src").strip(),
        )
        cands.append(cand)
        to_add.append(cand)
        seen.add((cand.text, cand.src))
    return to_add, cands, skipped


def planned_pairs(
    cfg: InstanceConfig,
    legacy_lines: list[str],
    n_user: int,
    as_of: Any,
    limit: int,
) -> tuple[int, int, list[list[str]]]:
    """预计算本次写入会加入的 (text, src) 对与跳过数（不改文件）。"""
    to_add, _, skipped = _select_additions(cfg, legacy_lines, n_user, as_of, limit)
    return len(to_add), skipped, [[c.text, c.src] for c in to_add]


def add(
    cfg: InstanceConfig,
    legacy_lines: list[str],
    n_user: int,
    as_of: Any,
    limit: int,
    write: bool = True,
) -> tuple[int, int]:
    """加入新候选，返回 (加入数, 因待批已满而跳过数)。

    legacy_lines 是「- 日期｜kind｜文本｜src=…」形式；前 n_user 条是使用者自己写的，不受上限限制。
    """
    to_add, cands, skipped = _select_additions(cfg, legacy_lines, n_user, as_of, limit)
    if write and to_add:
        save(cfg, cands)
    return len(to_add), skipped


def _append_under(text: str, heading: str, line: str) -> str:
    """把一行追加到指定二级小节末尾；小节不存在就建在文末。"""
    lines = text.rstrip("\n").split("\n") if text.strip() else []
    if heading not in lines:
        return "\n".join(lines + ["", heading, "", line]) + "\n"
    start = lines.index(heading)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end] + [line] + lines[end:]) + "\n"


def decide(cfg: InstanceConfig, cand_id: str, action: str, to: str, date_str: str, write: bool = True) -> str | None:
    """批准或丢弃一条候选；成功返回 None，否则返回原因。"""
    cands = load(cfg)
    cand = next((c for c in cands if c.id == cand_id), None)
    if cand is None:
        return "编号不存在"
    if cand.status != "pending":
        return "该候选已处理过"
    if action == "approve":
        target = TO_ALIASES.get(to)
        if target is None:
            return "批准去向只能是画像或规则"
        rel, heading = TARGETS[target]
        if write:
            path = cfg.vault / rel
            old = path.read_text(encoding="utf-8") if path.is_file() else ""
            marker = f"，{cand.id}）"
            if not any(ln.lstrip().startswith("- ") and marker in ln for ln in old.splitlines()):
                atomic_write(path, _append_under(old, heading, f"- {cand.text}（{date_str} 批准，{cand.id}）"), [cfg.vault])
        cand.status, cand.to = "approved", target
    else:
        cand.status = "discarded"
    cand.closed = date_str
    if write:
        save(cfg, cands)
    return None


def apply_mech(cfg: InstanceConfig, mech: Any, approval_doors: Iterable[str], write: bool = True) -> None:
    """执行机械对账里分流出来的批准、丢弃动作，结果并入 mech.applied / mech.unmatched。"""
    from hushai_agent.mechanical import AppliedEntry

    allowed = set(approval_doors)
    for act, rec in mech.cand_actions:
        entry = AppliedEntry(action=act.kind, id=act.target, line_id=rec.line_id, record=rec)
        if rec.door not in allowed or rec.auto:
            entry.reason = NO_RIGHT
        else:
            entry.reason = decide(cfg, act.target, act.kind, act.note, str(rec.date), write=write) or ""
        (mech.unmatched if entry.reason else mech.applied).append(entry)


def command(cfg: InstanceConfig, action: str, cand_id: str, to: str, door: str) -> Path:
    """candidate approve / discard：先验权限和状态，再写一条标准收件行（写入即触发即时对账）。"""
    from hushai_agent.config_ext import load_ext
    from hushai_agent.inboxfmt import RESERVED_DOMAINS_EN, RESERVED_DOMAINS_ZH
    from hushai_agent.vault import add_inbox_entry

    if door not in load_ext(cfg).approval_doors:
        raise ValueError(f"{NO_RIGHT}: {door}")
    problem = decide(cfg, cand_id, action, to, "", write=False)
    if problem:
        raise ValueError(f"{cand_id}: {problem}")
    en = cfg.lang == "en"
    if action == "approve":
        target = TO_ALIASES[to]
        did = f"approve {cand_id} {target}" if en else f"批准 {cand_id} {'画像' if target == 'profile' else '规则'}"
    else:
        did = f"discard {cand_id}" if en else f"丢弃 {cand_id}"
    return add_inbox_entry(cfg.name, door, (RESERVED_DOMAINS_EN if en else RESERVED_DOMAINS_ZH)[0], did)
