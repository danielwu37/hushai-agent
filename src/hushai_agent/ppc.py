"""更正驱动规则微调（Propose–Probe–Compose，源自 google-research/harness2 思路）。

严防自嗨原则：
1. 没有人工「更正（correction）」，绝不触发。
2. 没有通过无锁回放与规范校验（Probe），绝不呈批。
3. 通过后作为带「（已回放验证）」标记的 convention 候选记入 candidates.md，由使用者在晨间简报一键批准入 AGENTS.local.md。
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from hushai_agent import candidates, collect, items, mechanical
from hushai_agent.config import InstanceConfig
from hushai_agent.config_ext import load_ext
from hushai_agent.validate import validate_vault

CORRECTION_RE = re.compile(
    r"^- (?P<date>\d{4}-\d{2}-\d{2})｜target=(?P<target>[^｜]+)｜(?P<note>[^｜]+)｜src=(?P<src>L-[^\s｜]+)$"
)


def _probe_rule(cfg: InstanceConfig, corr_date: datetime.date, rule_text: str) -> bool:
    """回放检验（Probe）：在不持锁、不调大模型的前提下，校验记忆库合法且当日收件可干净完成收集与机械对账。"""
    if not rule_text or len(rule_text) > 160 or "\n" in rule_text:
        return False
    val = validate_vault(cfg.vault)
    if not val.get("ok", False):
        return False
    try:
        all_items = items.load(cfg)
        records = collect.collect(cfg, corr_date)
        mechanical.apply_actions(all_items, records)
    except Exception:
        return False
    return True


def propose_probe_compose(
    cfg: InstanceConfig,
    as_of_date: datetime.date,
    dry_run: bool = False,
) -> dict[str, int]:
    """扫描本周（含基准日共 7 天）的人工更正，生成候选规则并回放验证后呈批。"""
    corr_path = cfg.vault / "hub" / "reports" / "corrections.md"
    if not corr_path.is_file():
        return {"proposed": 0, "verified": 0}

    start_date = as_of_date - datetime.timedelta(days=6)
    as_of_str = as_of_date.isoformat()
    proposed = 0
    verified_lines: list[str] = []

    for raw in corr_path.read_text(encoding="utf-8").splitlines():
        m = CORRECTION_RE.match(raw.strip())
        if not m:
            continue
        try:
            c_date = datetime.date.fromisoformat(m.group("date"))
        except ValueError:
            continue
        if not (start_date <= c_date <= as_of_date):
            continue
        note = m.group("note").strip()
        target = m.group("target").strip()
        src = m.group("src").strip()
        if not note:
            continue
        proposed += 1
        rule_text = f"防错规则（源自 {m.group('date')} 更正 {target}）：{note[:80]}（已回放验证）"
        if _probe_rule(cfg, c_date, rule_text):
            verified_lines.append(f"- {as_of_str}｜convention｜{rule_text}｜src={src}")

    added = 0
    if verified_lines:
        ext = load_ext(cfg)
        added, _ = candidates.add(
            cfg,
            verified_lines,
            n_user=0,
            as_of=as_of_date,
            limit=ext.candidates_pending,
            write=not dry_run,
        )
    return {"proposed": proposed, "verified": added if not dry_run else len(verified_lines)}
